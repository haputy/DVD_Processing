"""The "DVD to Jellyfin" window: put a disc in, click, come back later, review, copy.

This file only lays out widgets and calls app_logic. Long work runs in a worker thread that
never touches a widget: it posts messages to a queue, and the window reads the queue every
100 ms on the Tk event loop. Errors show as a plain message in the log and a dialog; the
full detail goes to ~/.dvd_processor/gui.log.

makemkvcon and HandBrakeCLI run through a procs.ProcessRunner: Cancel stops them at once,
and closing the window kills them (they sit in a kill-on-close Windows job).

    python -m dvd_processor gui
"""
import logging
import os
import queue
import sys
import threading
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional

import customtkinter as ctk

from dvd_processor import app_logic as al
from dvd_processor.config import Config
from dvd_processor.disc import DiscScanner, resolve_makemkv_path
from dvd_processor.discinfo import rip_root
from dvd_processor.drives import drive_has_disc, eject, get_optical_drives
from dvd_processor.handbrake import HandBrakeEncoder, resolve_handbrake_path
from dvd_processor.nas import nas_movie_root, nas_tv_root, resume_renaming, unfinished_rename_plans
from dvd_processor.procs import Cancelled, ProcessRunner

log = logging.getLogger("dvd_processor.gui")

TITLE = "DVD to Jellyfin"
KIND_TV, KIND_MOVIE = "TV", "Movie"
DISC_TAB = "Disc to NAS"
MATCH_TAB = "Match and copy"
NO_SHOW = "(type a name and click Find)"
SEARCHING = "(searching TMDB...)"
NOTHING_FOUND = "(no shows found)"
NO_FOLDER = "(no show folders found)"
PICK_FOLDER = "(pick a show folder)"
ERROR_RED = "#b00020"
TEXT = ("gray10", "gray90")
STOP_EFFECT = ("A rip in progress stops at once: its half-written title is kept as .failed and the disc "
               "folder stays marked incomplete; Go rips that disc again into a new folder. A half-done "
               "encode or copy is removed. Transcribing stops after the title it is on. Press Go again "
               "to carry on.")

COLUMNS = [("include", "Copy", 68), ("disc", "Disc", 150), ("title", "Title", 80), ("minutes", "Min", 50),
           ("episode", "Episode (click to change)", 330), ("score", "Score", 60), ("margin", "Margin", 60),
           ("flag", "Flag", 64), ("note", "Note", 380)]
TICK, NO_TICK = "☑", "☐"


def gui_log_path() -> Path:
    return Path.home() / ".dvd_processor" / "gui.log"


class ReviewTable(ctk.CTkFrame):
    """The match table: a ttk.Treeview with a tick column and an episode list per row.

    Click the tick to include or leave out a row; click the episode to pick another one.
    Clicking any row shows below the table why it was matched (descriptions, the words the
    dialogue shares with them, and those lines of dialogue). on_change() is called after every
    edit so the owner can recheck duplicates."""

    def __init__(self, master, on_change: Callable[[], None], include_heading: str = "Copy"):
        super().__init__(master, fg_color="transparent")
        self.on_change = on_change
        self.rows = []
        self.episodes = []
        self.regulars = []
        self.choices = []
        self.shown: Optional[int] = None   # the row whose explanation is showing
        self.locked = False
        self._editor: Optional[ttk.Combobox] = None
        self.tree = ttk.Treeview(self, columns=[c[0] for c in COLUMNS], show="headings", selectmode="none")
        for key, heading, width in COLUMNS:
            self.tree.heading(key, text=include_heading if key == "include" else heading)
            self.tree.column(key, width=width, stretch=key in ("note", "episode"),
                             anchor="center" if key in ("include", "minutes", "score", "margin", "flag") else "w")
        self.tree.tag_configure("check", background="#fff1b8")
        self.tree.tag_configure("fixed", background="#dcecff")
        self.tree.tag_configure("extra", foreground="#888888")
        self.tree.tag_configure("duplicate", background="#ffd0d0")
        bar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)

        def on_scroll(first, last):
            bar.set(first, last)
            self._close_editor()
        self.tree.configure(yscrollcommand=on_scroll)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        self.detail = ctk.CTkTextbox(self, height=230, wrap="word", font=ctk.CTkFont(size=12))
        self.detail.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._click)
        self._set_detail("")

    def load(self, rows, episodes, regulars=()):
        self._close_editor()
        self.rows, self.episodes, self.regulars = rows, episodes, list(regulars)
        self.choices = al.episode_choices(episodes)
        self.shown = None
        self._set_detail("Click a row to see why it was matched: the episode's description from TMDB and "
                         "Wikipedia, the runner-up's, and the lines of dialogue they share words with."
                         if rows else "")
        self.refresh()

    def _set_detail(self, text: str):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", text)
        self.detail.configure(state="disabled")

    def show_detail(self, n: int):
        self.shown = n
        try:
            text = al.row_detail(self.rows[n - 1], self.episodes, self.regulars)
        except Exception as exc:  # an explanation must never get in the way of the review
            log.exception("explaining row %s", n)
            text = f"Could not explain row {n}: {exc}"
        self._set_detail(f"Row {n}: {text}")

    def clear(self):
        self.load([], [])

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, v in enumerate(al.row_views(self.rows), start=1):
            self.tree.insert("", "end", iid=str(i), tags=(v.highlight,) if v.highlight else (),
                             values=(TICK if v.include else NO_TICK, v.disc, v.title, v.minutes,
                                     v.episode, v.score, v.margin, v.flag, v.note))

    def _click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        item, col = self.tree.identify_row(event.y), self.tree.identify_column(event.x)
        if not item or not col:
            return
        n = int(item)
        self.show_detail(n)
        if self.locked:
            return
        key = COLUMNS[int(col[1:]) - 1][0]
        if key == "include":
            msg = al.set_include(self.rows, n, not self.rows[n - 1].will_act)
            if msg:
                messagebox.showinfo(TITLE, msg, parent=self)
            self.refresh()
            self.on_change()
            return "break"
        if key == "episode":
            self._open_editor(item, col, n)
            return "break"

    def _open_editor(self, item, col, n):
        self._close_editor()
        box_xywh = self.tree.bbox(item, col)
        if not box_xywh:
            return
        x, y, w, h = box_xywh
        box = ttk.Combobox(self.tree, values=self.choices, state="readonly", height=20)
        box.set(al.episode_label(self.rows[n - 1].episode))
        box.place(x=x, y=y, width=max(w, 320), height=h)

        def picked(_e=None):
            label = box.get()
            self._close_editor()
            al.set_episode(self.rows, n, label, self.episodes)
            self.refresh()
            self.show_detail(n)
            self.on_change()
        box.bind("<<ComboboxSelected>>", picked)
        box.bind("<Escape>", lambda e: self._close_editor())
        box.focus_set()
        self._editor = box

        def drop_down():
            try:
                box.tk.call("ttk::combobox::Post", box)
            except Exception:
                pass
        self.after(30, drop_down)

    def _close_editor(self):
        if self._editor is not None:
            self._editor.destroy()
            self._editor = None


Work = Callable[[Callable[[str], None], ProcessRunner], object]

# Which lanes a job waits for. Ripping only needs the drive, so the next disc rips while the
# last one finishes; finishing and the other jobs touch the disc folders and the NAS, so they
# take turns.
BLOCKED_BY = {"rip": {"rip"}, "finish": {"finish", "main"}, "main": {"finish", "main"}}


class Lane:
    def __init__(self, what: str, job: ProcessRunner, cancellable: bool):
        self.what, self.job, self.cancellable = what, job, cancellable
        self.thread: Optional[threading.Thread] = None


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(TITLE)
        self.geometry("1280x880")
        self.minsize(1000, 700)
        self.config_ = Config()
        self._queue: queue.Queue = queue.Queue()
        # What is running, per lane: lane -> Lane. "rip" uses the drive, "finish" encodes,
        # matches and copies one Go disc at a time, "main" is everything else.
        self._lanes: dict[str, Lane] = {}
        self._finish_queue: list = []        # (al.Ripped, load_episodes, handbrake, nas root)
        self._paused: Optional[al.GoResult] = None   # a Go disc stopped for review holds the queue
        self._movie_pick: Optional[list] = None   # the fingerprint of a movie disc waiting for a title pick
        self._client = None
        self._shows: dict[str, al.Show] = {}
        self._unfinished: list = []
        self._nas_picked: Optional[str] = None      # the NAS folder Andy picked, if any
        self.show: Optional[al.Show] = None
        self.match_result: Optional[al.MatchResult] = None
        self.fix_result: Optional[al.MatchResult] = None

        style = ttk.Style(self)
        style.configure("Treeview", rowheight=24, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build_banner()
        self._build_top_bar()
        self._build_tabs()
        self._build_log()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self._startup()

    # ---- layout ------------------------------------------------------------------------------

    def _build_banner(self):
        self.banner = ctk.CTkFrame(self, fg_color="#fde7c2")
        self.banner_label = ctk.CTkLabel(self.banner, text="", text_color="#5a3b00", anchor="w", justify="left")
        self.banner_label.pack(side="left", padx=12, pady=6, fill="x", expand=True)
        self.resume_button = ctk.CTkButton(self.banner, text="Resume", width=100, command=self._resume)
        self.resume_button.pack(side="right", padx=12, pady=6)

    def _build_top_bar(self):
        bar = ctk.CTkFrame(self)
        bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(8, 4))
        self.kind_switch = ctk.CTkSegmentedButton(bar, values=[KIND_TV, KIND_MOVIE], command=self._set_kind)
        self.kind_switch.set(KIND_TV)
        self.kind_switch.grid(row=0, column=0, padx=(10, 8), pady=8)
        self.name_label = ctk.CTkLabel(bar, text="Show")
        self.name_label.grid(row=0, column=1, padx=(0, 4), pady=8)
        self.show_entry = ctk.CTkEntry(bar, width=220, placeholder_text="e.g. NewsRadio")
        self.show_entry.grid(row=0, column=2, pady=8)
        self.show_entry.bind("<Return>", lambda e: self._find())
        self.find_button = ctk.CTkButton(bar, text="Find", width=70, command=self._find)
        self.find_button.grid(row=0, column=3, padx=6, pady=8)
        self.show_menu = ctk.CTkOptionMenu(bar, values=[NO_SHOW], width=340, command=self._pick_show,
                                           dynamic_resizing=False)
        self.show_menu.grid(row=0, column=4, padx=(0, 16), pady=8)
        ctk.CTkLabel(bar, text="Season").grid(row=0, column=5, padx=(0, 4))
        self.season_entry = ctk.CTkEntry(bar, width=50, placeholder_text="any")
        self.season_entry.grid(row=0, column=6, padx=(0, 16))
        ctk.CTkLabel(bar, text="Drive").grid(row=0, column=7, padx=(0, 4))
        self.drives = get_optical_drives()
        self.drive_menu = ctk.CTkOptionMenu(bar, values=self.drives, width=80)
        self.drive_menu.grid(row=0, column=8, padx=(0, 10))
        configured = self.config_.get("default_drive")
        self.drive_menu.set(configured if configured in self.drives else self.drives[0])

    def _build_tabs(self):
        self.tabs = ctk.CTkTabview(self)
        try:  # bigger tab names; a private attribute, so never let it stop the window
            self.tabs._segmented_button.configure(font=ctk.CTkFont(size=14, weight="bold"))
        except Exception:
            pass
        self.tabs.grid(row=2, column=0, sticky="nsew", padx=10, pady=4)
        self._build_rip_tab(self.tabs.add(DISC_TAB))
        self._build_match_tab(self.tabs.add(MATCH_TAB))
        self._build_fix_tab(self.tabs.add("Fix a show on the NAS"))

    def _button_row(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.grid(row=0, column=0, sticky="ew", pady=(4, 6))
        return row

    def _status(self, tab, text):
        label = ctk.CTkLabel(tab, text=text, anchor="w", justify="left", wraplength=1150)
        label.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        return label

    def _build_rip_tab(self, tab):
        row = self._button_row(tab)
        self.go_button = ctk.CTkButton(row, text="Go", width=160, height=36,
                                       font=ctk.CTkFont(size=15, weight="bold"), command=self._go)
        self.go_button.pack(side="left")
        self.rip_status = self._status(
            tab, "Put a disc in, pick the show at the top, then press Go. It rips, encodes, transcribes, "
                 "matches and copies to the NAS, and stops only when a match needs checking.")
        frame = ctk.CTkFrame(tab, fg_color="transparent")
        frame.grid(row=2, column=0, sticky="nsew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        self.scan_tree = ttk.Treeview(frame, columns=("title", "length", "rip", "why"), show="headings",
                                      selectmode="none")
        for key, text, width in [("title", "Title", 80), ("length", "Length", 100), ("rip", "Rip", 80),
                                 ("why", "Why not", 500)]:
            self.scan_tree.heading(key, text=text)
            self.scan_tree.column(key, width=width, anchor="w" if key == "why" else "center", stretch=key == "why")
        self.scan_tree.tag_configure("skip", foreground="#888888")
        self.scan_tree.grid(row=0, column=0, sticky="nsew")

    def _build_match_tab(self, tab):
        row = self._button_row(tab)
        self.match_button = ctk.CTkButton(row, text="Match", width=130, command=self._match)
        self.match_button.pack(side="left", padx=(0, 10))
        self.copy_button = ctk.CTkButton(row, text="Copy to NAS", width=160, command=self._copy)
        self.copy_button.pack(side="left", padx=(0, 16))
        self.later_button = ctk.CTkButton(row, text="Leave for later", width=140, command=self._leave_for_later)
        self.dest_label = ctk.CTkLabel(row, text="", anchor="w")
        self.dest_label.pack(side="left")
        self.match_status = self._status(tab, "When all the discs of the show are ripped, click Match.")
        self.match_table = ReviewTable(tab, self._refresh_buttons, include_heading="Copy")
        self.match_table.grid(row=2, column=0, sticky="nsew")

    def _build_fix_tab(self, tab):
        row = self._button_row(tab)
        ctk.CTkLabel(row, text="Show folder on the NAS").pack(side="left", padx=(0, 6))
        self.nas_menu = ctk.CTkOptionMenu(row, values=[PICK_FOLDER], width=300, dynamic_resizing=False,
                                          command=self._pick_nas_folder)
        self.nas_menu.set(PICK_FOLDER)
        self.nas_menu.pack(side="left", padx=(0, 6))
        self.nas_refresh = ctk.CTkButton(row, text="Refresh", width=80, command=self._list_nas)
        self.nas_refresh.pack(side="left", padx=(0, 16))
        self.fix_button = ctk.CTkButton(row, text="Match", width=130, command=self._fix_match)
        self.fix_button.pack(side="left", padx=(0, 10))
        self.rename_button = ctk.CTkButton(row, text="Rename", width=130, command=self._rename)
        self.rename_button.pack(side="left")
        self.fix_status = self._status(
            tab, "Pick a show folder here and the show at the top, then click Match. A ticked row is renamed.")
        self.fix_table = ReviewTable(tab, self._refresh_buttons, include_heading="Rename")
        self.fix_table.grid(row=2, column=0, sticky="nsew")

    def _build_log(self):
        frame = ctk.CTkFrame(self)
        frame.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 10))
        frame.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(frame, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 2))
        ctk.CTkLabel(head, text="Progress", anchor="w").pack(side="left", padx=4)
        self.cancel_button = ctk.CTkButton(head, text="Cancel", width=90, fg_color="#a33", hover_color="#822",
                                           command=self._cancel)
        self.cancel_button.pack(side="right")
        self.busy_label = ctk.CTkLabel(head, text="", anchor="e")
        self.busy_label.pack(side="right", padx=10)
        self.log_box = ctk.CTkTextbox(frame, height=170, wrap="word")
        self.log_box.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))
        self.log_box.configure(state="disabled")

    # ---- plumbing ----------------------------------------------------------------------------

    def say(self, msg: str):
        """Add a line to the log panel (main thread only)."""
        log.info(msg)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _post(self, msg: str):
        """Progress from a worker thread: queued, shown by _poll on the Tk loop."""
        self._queue.put(("log", msg, None, None))

    @property
    def _busy(self) -> Optional[str]:
        """What is running, in words, or None."""
        return " and ".join(lane.what for lane in self._lanes.values()) or None

    def _waiting_for(self, lane: str) -> Optional[str]:
        """What a job on this lane would wait for, or None when it can start."""
        busy = [self._lanes[k].what for k in BLOCKED_BY[lane] if k in self._lanes]
        return " and ".join(busy) or None

    def _poll(self):
        try:
            while True:
                try:
                    kind, what, payload, lane = self._queue.get_nowait()
                except queue.Empty:
                    break
                if lane:
                    self._lanes.pop(lane, None)
                try:
                    if kind == "log":
                        self.say(what)
                    elif kind == "done":
                        payload[0](payload[1])
                    elif kind == "error":
                        failed, exc = payload
                        if failed:
                            failed()
                        if isinstance(exc, Cancelled):
                            self._show_cancelled(what, exc)
                        else:
                            self._show_error(what, exc)
                except Exception as exc:
                    log.exception("error showing a result")
                    self._show_error("Showing the result", exc)
                finally:
                    if kind != "log":
                        self._refresh_buttons()
                        self._pump_finish()
        finally:
            self.after(100, self._poll)

    def _show_error(self, what: str, exc: BaseException):
        msg = al.friendly_error(exc)
        self.say(f"{what} stopped: {msg}")
        messagebox.showerror(TITLE, f"{what} stopped.\n\n{msg}\n\nDetails are in {gui_log_path()}", parent=self)

    def _show_cancelled(self, what: str, exc: BaseException):
        detail = str(exc)
        self.say(f"{what} cancelled." + (f" {detail}" if detail else ""))
        messagebox.showinfo(TITLE, f"{what} cancelled." + (f"\n\n{detail}" if detail else ""), parent=self)

    def _run(self, what: str, work: Work, done: Callable[[object], None], busy: bool = True,
             failed: Optional[Callable[[], None]] = None, cancellable: bool = True,
             lane: str = "main") -> bool:
        """Run work(progress, job) in a thread, then done(result) on the Tk loop; on an error
        or a cancel, failed() first. busy holds the lane meanwhile (see BLOCKED_BY) and makes
        Cancel stop this job; quick background lookups pass busy=False. A rename passes
        cancellable=False: once started it finishes."""
        job = ProcessRunner()
        if busy:
            waiting = self._waiting_for(lane)
            if waiting:
                messagebox.showinfo(TITLE, f"Wait for {waiting.lower()} to finish, or click Cancel.",
                                    parent=self)
                return False
            self._lanes[lane] = Lane(what, job, cancellable)
            if not cancellable:
                self.say(f"{what}: a rename finishes once started, so Cancel is off until it is done.")
        held = lane if busy else None

        def progress(msg: str):
            job.check()  # every progress line is a place a Cancel takes effect
            self._post(msg)

        def target():
            try:
                result = work(progress, job)
            except Cancelled as exc:
                log.info("%s cancelled: %s", what, exc)
                self._queue.put(("error", what, (failed, exc), held))
            except Exception as exc:
                log.exception("%s failed", what)
                self._queue.put(("error", what, (failed, exc), held))
            else:
                self._queue.put(("done", what, (done, result), held))
        thread = threading.Thread(target=target, name=what, daemon=True)
        if busy:
            self._lanes[lane].thread = thread
            self._refresh_buttons()
        thread.start()
        return True

    def report_callback_exception(self, exc, val, tb):
        log.error("error in the window", exc_info=(exc, val, tb))
        self._show_error("That", val)

    def _refresh_buttons(self):
        free = self._waiting_for("main") is None    # nothing is using the disc folders or the NAS
        picked = self.show is not None

        def state(button, ok):
            button.configure(state="normal" if ok else "disabled")
        for b in (self.find_button, self.nas_refresh, self.resume_button):
            state(b, free)
        for b in (self.match_button, self.fix_button):
            state(b, free and picked and self.show.kind == al.TV)
        state(self.go_button, picked and "rip" not in self._lanes)
        state(self.later_button, free and self._paused is not None)
        if self._paused is None:
            self.later_button.pack_forget()
        elif not self.later_button.winfo_ismapped():
            self.later_button.pack(side="left", padx=(0, 16), before=self.dest_label)
        self.cancel_button.configure(
            state="normal" if any(lane.cancellable for lane in self._lanes.values()) else "disabled")
        waiting = len(self._finish_queue)
        label = f"{self._busy}..." if self._lanes else ""
        if waiting:
            label += f"  ({waiting} disc{'s' if waiting > 1 else ''} waiting to finish)"
        self.busy_label.configure(text=label)
        self.match_table.locked = self.fix_table.locked = not free

        r = self.match_result
        if r is None:
            state(self.copy_button, False)
            self.copy_button.configure(text="Copy to NAS")
        else:
            dupes = al.duplicate_problem(r.rows, "copying")
            n = al.included_count(r.rows)
            state(self.copy_button, free and n > 0 and not dupes)
            self.copy_button.configure(text=f"Copy {n} to NAS" if n else "Copy to NAS")
            self.match_status.configure(text=dupes or al.match_summary(r), text_color=ERROR_RED if dupes else TEXT)
        f = self.fix_result
        if f is None:
            state(self.rename_button, False)
        else:
            dupes = al.duplicate_problem(f.rows, "renaming")
            moves, right = ([], 0) if dupes else al.rename_moves(f)
            state(self.rename_button, free and not dupes and len(moves) > right)
            self.fix_status.configure(
                text=dupes or f"{al.match_summary(f)} {len(moves) - right} to rename, {right} already right.",
                text_color=ERROR_RED if dupes else TEXT)

    def _client_or_ask(self):
        if self._client is None:
            from dvd_processor.tmdb import TmdbClient
            key = self.config_.get("tmdb_api_key")
            if not key:
                key = ctk.CTkInputDialog(title=TITLE, text="TMDB API key (free at themoviedb.org):").get_input()
                if not key:
                    return None
                self.config_.set("tmdb_api_key", key.strip())
            self._client = TmdbClient(api_key=self.config_.get("tmdb_api_key"))
        return self._client

    def _need_show(self) -> Optional[al.Show]:
        if self.show is None:
            messagebox.showinfo(TITLE, "Pick the show or film first: type its name at the top, click Find, "
                                       "and choose it from the list.", parent=self)
        return self.show

    def _season(self):
        """(ok, season or None)."""
        try:
            return True, al.parse_season(self.season_entry.get())
        except al.UserError as exc:
            messagebox.showinfo(TITLE, str(exc), parent=self)
            return False, None

    def _load_episodes(self, client):
        from dvd_processor.episodes import build_episodes, regulars_for_show
        config = self.config_

        def load(show: al.Show):
            return (build_episodes(client, show.tmdb_id, show.name),
                    regulars_for_show(client, show.tmdb_id, show.name, config))
        return load

    def _nas_root(self) -> str:
        return nas_tv_root(self.config_)

    def _cancel(self):
        lanes = [lane for lane in self._lanes.values() if lane.cancellable]
        if not lanes:
            return
        what = " and ".join(lane.what for lane in lanes)
        if not messagebox.askyesno(TITLE, f"Stop {what.lower()}?\n\n{STOP_EFFECT}" + self._waiting_text(),
                                   parent=self):
            return
        self._drop_queue()
        for lane in lanes:
            if lane in self._lanes.values():  # it may have finished while the question was up
                self.say(f"Stopping {lane.what.lower()}...")
                lane.job.cancel()

    def _waiting_text(self) -> str:
        if not self._finish_queue:
            return ""
        names = ", ".join(item[0].folder.name for item in self._finish_queue)
        return (f"\n\nRipped and waiting to finish: {names}. They are left as they are; Go with each "
                f"disc finishes it without ripping again.")

    def _drop_queue(self):
        if self._finish_queue:
            self.say("Left waiting (Go with each disc finishes it without ripping again): "
                     + ", ".join(item[0].folder.name for item in self._finish_queue))
            self._finish_queue.clear()

    def _on_close(self):
        if self._lanes or self._finish_queue:
            if all(lane.cancellable for lane in self._lanes.values()):
                question = (f"Still working ({(self._busy or 'discs waiting to finish').lower()}). Closing "
                            f"stops it now.\n\n{STOP_EFFECT}" + self._waiting_text())
            else:
                question = (f"A rename on the NAS is under way. Closing now may leave it half done; the "
                            f"banner's Resume button finishes it next time.")
            if not messagebox.askyesno(TITLE, question + "\n\nClose anyway?", parent=self):
                return
            lanes = list(self._lanes.values())
            for lane in lanes:
                if lane.cancellable:
                    lane.job.cancel()  # kills makemkvcon or HandBrakeCLI; the job object would too
            for lane in lanes:
                if lane.thread is not None:
                    lane.thread.join(timeout=5)  # let the clean-up (.failed, .part, .encoding.mp4) finish
            if any(lane.thread is not None and lane.thread.is_alive() for lane in lanes):
                log.warning("closing while %s was still stopping", self._busy)
                messagebox.showwarning(
                    TITLE, "It has not finished stopping, so the window closes anyway. A half-written "
                           "file (.encoding.mp4 or .part) may be left behind, a stopped rip's disc "
                           "folder stays marked incomplete, and a rename can be finished with Resume.",
                    parent=self)
        self.destroy()

    # ---- startup -----------------------------------------------------------------------------

    def _startup(self):
        try:
            self.dest_label.configure(text=f"Copies go to {self._nas_root()}")
        except Exception as exc:
            self.dest_label.configure(text=f"NAS root problem: {al.friendly_error(exc)}", text_color=ERROR_RED)
        self._check_unfinished()
        self._refresh_buttons()
        configured = self.config_.get("default_drive")
        drives = list(self.drives)
        initial = self.drive_menu.get()

        def found(drive):
            if self.drive_menu.get() == initial:  # never override a drive Andy picked meanwhile
                self.drive_menu.set(drive)
        self._run("Looking for a disc", lambda p, j: al.pick_drive(drives, drive_has_disc, configured),
                  found, busy=False)
        self._list_nas()
        self.say("Ready. Pick the show, put a disc in and press Go.")

    def _check_unfinished(self):
        self._unfinished = unfinished_rename_plans()
        if not self._unfinished:
            self.banner.grid_forget()
            return
        more = f" ({len(self._unfinished) - 1} more after it)" if len(self._unfinished) > 1 else ""
        self.banner_label.configure(
            text=f"A rename on the NAS did not finish: {al.rename_plan_text(self._unfinished[0])}{more}. "
                 f"Resume finishes it.")
        self.banner.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 0))

    def _resume(self):
        if not self._unfinished:
            return
        plan = self._unfinished[0]
        if not messagebox.askyesno(TITLE, f"Finish this rename?\n\n{al.rename_plan_text(plan)}", parent=self):
            return

        def done(report):
            self.say(f"Resumed {plan.name}: {len(report.renamed)} moved, {len(report.left_as_temp)} left as "
                     f".renaming" + "".join(f"\n  {t} (its destination exists)" for t in report.left_as_temp))
            self._check_unfinished()
        self._run("Resuming the rename", lambda p, j: resume_renaming(plan), done,
                  failed=self._check_unfinished, cancellable=False)

    # ---- top bar -----------------------------------------------------------------------------

    @property
    def kind(self) -> str:
        return al.MOVIE if self.kind_switch.get() == KIND_MOVIE else al.TV

    def _set_kind(self, _value=None):
        """TV or Movie: a new search is needed, and a film has no season or episodes."""
        movie = self.kind == al.MOVIE
        self.show, self._shows = None, {}
        self._movie_pick = None
        self.scan_tree.configure(selectmode="none")
        self._set_show_menu(NO_SHOW)
        self.name_label.configure(text="Film" if movie else "Show")
        self.show_entry.configure(placeholder_text="e.g. Brazil" if movie else "e.g. NewsRadio")
        self.season_entry.configure(state="disabled" if movie else "normal")
        self.say("Movie: Go rips the film, encodes it and copies it to the Movies library." if movie
                 else "TV: Go rips, encodes, transcribes, matches episodes and copies them.")
        self._refresh_buttons()

    def _set_show_menu(self, label: str, values=None):
        self.show_menu.configure(values=values or [label])
        self.show_menu.set(label)

    def _find(self):
        name = self.show_entry.get().strip()
        if not name:
            messagebox.showinfo(TITLE, "Type the name first.", parent=self)
            return
        client = self._client_or_ask()
        if client is None:
            return
        waiting = self._waiting_for("main")
        if waiting:
            messagebox.showinfo(TITLE, f"Wait for {waiting.lower()} to finish first.", parent=self)
            return
        self.show, self._shows = None, {}
        kind = self.kind
        what = "films" if kind == al.MOVIE else "shows"
        search = client.search_movie if kind == al.MOVIE else client.search_show
        self._set_show_menu(SEARCHING)
        self.say(f"Searching TMDB's {what} for {name}...")

        def done(results):
            if not results:
                self._set_show_menu(NOTHING_FOUND)
                self.say(f"TMDB has nothing called {name}.")
                return
            pick = al.auto_pick(results, name)  # on every result, before the list is cut to 15
            shows = [al.Show(r.tmdb_id, r.name, r.year, kind) for r in results[:15]]
            self._shows = {al.show_choice(s): s for s in shows}
            labels = list(self._shows)
            if pick is None:
                prompt = al.pick_prompt(len(shows))
                self._set_show_menu(prompt, [prompt] + labels)
                self.say(f"TMDB found {len(results)} {what} for {name}. Pick the right one from the list "
                         f"(the year and TMDB id tell them apart).")
            else:
                self._set_show_menu(labels[pick], labels)
                self._pick_show(labels[pick])
        self._run("Searching TMDB", lambda p, j: search(name), done,
                  failed=lambda: self._set_show_menu(NO_SHOW))

    def _pick_show(self, label):
        self.show = self._shows.get(label)  # a prompt line picks nothing
        if self.show is not None:
            self.say(f"{'Film' if self.show.kind == al.MOVIE else 'Show'}: {al.show_choice(self.show)}")
        self._refresh_buttons()

    # ---- rip ---------------------------------------------------------------------------------

    def _scanner(self, job: ProcessRunner, drive: str) -> DiscScanner:
        return DiscScanner(drive=drive, makemkv_path=resolve_makemkv_path(self.config_), quiet=True,
                           runner=job.run)

    def _go(self):
        if "rip" in self._lanes:
            messagebox.showinfo(TITLE, "Wait for the disc in the drive to finish ripping. Its log shows how "
                                       "far it has got.", parent=self)
            return
        show = self._need_show()
        if show is None:
            return
        if show.kind == al.MOVIE:
            self._go_movie(show)
            return
        ok, season = self._season()
        client = self._client_or_ask() if ok else None
        if client is None:
            return
        try:
            nas = self._nas_root()
        except Exception as exc:
            self._show_error("Go", exc)
            return
        drive = self.drive_menu.get()
        root = rip_root(self.config_)
        load = self._load_episodes(client)
        handbrake = resolve_handbrake_path(self.config_)
        self.scan_tree.delete(*self.scan_tree.get_children())
        self.rip_status.configure(text="Ripping the disc. The log below shows where it is; the disc comes out "
                                       "when ripping is done, and the next one can go in then.", text_color=TEXT)
        self.say(f"Go: {al.show_choice(show)}" + (f" season {season}" if season else "") + f", drive {drive}.")

        def work(progress, job):
            return al.go_rip(show, season, self._scanner(job, drive), root, progress, eject=lambda: eject(drive))

        def done(ripped):
            for row in ripped.plan.rows:
                self.scan_tree.insert("", "end", values=(row.index, row.length, "yes" if row.rip else "skip",
                                                         row.why), tags=() if row.rip else ("skip",))
            self._finish_queue.append((ripped, load, handbrake, nas))
            self.rip_status.configure(text=f"{ripped.folder.name} is ripped and finishing in turn. Put in the "
                                           f"next disc and press Go.", text_color=TEXT)

        def failed():
            self.rip_status.configure(text="Go stopped; the log says why. Press Go again to carry on from where "
                                           "it stopped.", text_color=ERROR_RED)
        self._run("Ripping", work, done, failed=failed, lane="rip")

    def _go_movie(self, show: al.Show):
        try:
            movies = nas_movie_root(self.config_)
        except Exception as exc:
            self._show_error("Go", exc)
            return
        drive = self.drive_menu.get()
        root = rip_root(self.config_)
        handbrake = resolve_handbrake_path(self.config_)
        pick, fingerprint = None, self._movie_pick
        chosen = self.scan_tree.selection()
        if fingerprint is not None and chosen:
            pick = int(chosen[0])
        self._movie_pick = None
        self.scan_tree.configure(selectmode="none")
        self.scan_tree.delete(*self.scan_tree.get_children())
        self.rip_status.configure(text="Ripping the film. The log below shows where it is; the disc comes out "
                                       "when ripping is done.", text_color=TEXT)
        self.say(f"Go: {al.show_choice(show)}, drive {drive}" + (f", title {pick}." if pick is not None else "."))

        def work(progress, job):
            return al.go_rip_movie(show, self._scanner(job, drive), root, progress, eject=lambda: eject(drive),
                                   pick=pick, pick_fingerprint=fingerprint)

        def done(ripped):
            for row in ripped.plan.rows:
                self.scan_tree.insert("", "end", iid=str(row.index),
                                      values=(row.index, row.length, "yes" if row.rip else "skip", row.why),
                                      tags=() if row.rip else ("skip",))
            if isinstance(ripped, al.MoviePick):
                self._movie_pick = ripped.plan.fingerprint
                self.scan_tree.configure(selectmode="browse")
                lengths = ", ".join(f"title {t.index} ({t.duration_secs // 60} min)" for t in ripped.candidates)
                text = (f"This disc has more than one title as long as the film: {lengths}. Click the one to "
                        f"rip in the table, then press Go. Nothing was ripped.")
                self.say(text)
                self.rip_status.configure(text=text, text_color=ERROR_RED)
                messagebox.showinfo(TITLE, text, parent=self)
                return
            self._finish_queue.append((ripped, None, handbrake, movies))
            self.rip_status.configure(text=f"{ripped.folder.name} is ripped and finishing in turn. Put in the "
                                           f"next disc and press Go.", text_color=TEXT)

        def failed():
            self.rip_status.configure(text="Go stopped; the log says why. Press Go again to carry on from where "
                                           "it stopped.", text_color=ERROR_RED)
        self._run("Ripping", work, done, failed=failed, lane="rip")

    def _pump_finish(self):
        """Start the next ripped disc's encode, match and copy, when its turn has come."""
        if not self._finish_queue or self._paused is not None or self._waiting_for("finish"):
            return
        ripped, load, handbrake, nas = self._finish_queue.pop(0)
        name = ripped.folder.name
        if ripped.show.kind == al.MOVIE:
            self._finish_movie(ripped, handbrake, nas)
            return

        def work(progress, job):
            from dvd_processor.transcribe import transcribe_disc
            from dvd_processor.transcript_matcher import match_transcripts
            return al.go_finish(ripped, HandBrakeEncoder(handbrake, quiet=True, runner=job.run), transcribe_disc,
                                load, match_transcripts, nas, progress, should_stop=lambda: job.cancelled)

        def done(r):
            text = al.go_text(r)
            self.say(text)
            if r.problem:
                self._paused = r
                self.match_result = r.match
                self.match_table.load(r.match.rows, r.match.all_episodes, r.match.regulars)
                self.say(al.unused_text(r.match.rows, r.match.episodes))
                if self._finish_queue:
                    self.say("The discs waiting after it finish once this one is copied or left for later.")
                self.rip_status.configure(text=text, text_color=ERROR_RED)
                self.tabs.set(MATCH_TAB)
                messagebox.showinfo(TITLE, f"{text}\n\nNothing from this disc was copied. Copy it after "
                                           f"checking, or click Leave for later.", parent=self)
                return
            if r.summary is not None and r.summary.failed:
                self.rip_status.configure(text=text, text_color=ERROR_RED)
                messagebox.showwarning(TITLE, text + "\n\nPress Go with the disc again to retry; nothing is "
                                                     "overwritten.", parent=self)
                return
            if "rip" not in self._lanes:
                self.rip_status.configure(text=text + " Put in the next disc and press Go.", text_color=TEXT)
            self.bell()

        self.say(f"Finishing {name}: encode, transcribe, match and copy.")
        self._run(f"Finishing {name}", work, done, lane="finish")

    def _finish_movie(self, ripped, handbrake: str, movies: str):
        name = ripped.folder.name

        def work(progress, job):
            return al.go_finish_movie(ripped, HandBrakeEncoder(handbrake, quiet=True, runner=job.run), movies,
                                      progress, should_stop=lambda: job.cancelled)

        def done(r):
            text = al.movie_text(r)
            self.say(text)
            if "rip" not in self._lanes:
                self.rip_status.configure(text=text + " Put in the next disc and press Go.", text_color=TEXT)
            self.bell()

        self.say(f"Finishing {name}: encode and copy to the Movies library.")
        self._run(f"Finishing {name}", work, done, lane="finish")

    def _leave_for_later(self):
        """Leave the disc that stopped for review as it is and let the waiting discs go on."""
        r = self._paused
        if r is None:
            return
        self._paused = None
        self.say(f"{r.folder.name} left for later; Go with the disc again, or Match, picks it up.")
        self._refresh_buttons()
        self._pump_finish()

    # ---- match and copy ----------------------------------------------------------------------

    def _match(self):
        show = self._need_show()
        if show is None:
            return
        ok, season = self._season()
        client = self._client_or_ask() if ok else None
        if client is None:
            return
        root = rip_root(self.config_)
        load = self._load_episodes(client)
        handbrake = resolve_handbrake_path(self.config_)
        self.match_result = None
        self.match_table.clear()
        self.say(f"Matching {show.name}" + (f" season {season}" if season else "") + "...")

        def work(progress, job):
            from dvd_processor.transcribe import transcribe_disc
            from dvd_processor.transcript_matcher import match_transcripts
            encoder = HandBrakeEncoder(handbrake, quiet=True, runner=job.run)
            return al.run_match(show, season, root, load, transcribe_disc, match_transcripts, progress,
                                encoder=encoder)

        def done(result):
            self.match_result = result
            self.match_table.load(result.rows, result.all_episodes, result.regulars)
            self.say(al.match_summary(result))
            self.say(al.unused_text(result.rows, result.episodes))
        self._run("Matching", work, done)

    def _copy(self):
        r = self.match_result
        if r is None:
            return
        dupes = al.duplicate_problem(r.rows, "copying")
        if dupes:  # the button is disabled then too; this is the backstop
            messagebox.showerror(TITLE, dupes, parent=self)
            return
        try:
            root = self._nas_root()
            items = al.copy_items(r.rows, r.show, root)
        except Exception as exc:
            self._show_error("Copying", exc)
            return
        if not items:
            return
        if not messagebox.askyesno(TITLE, al.copy_confirm_text(items, root, r.rows), parent=self):
            return
        self.say(al.unused_text(r.rows, r.episodes))

        def done(summary):
            text = al.copy_summary_text(summary)
            self.say(text)
            if self._paused is not None and self._paused.match is r:
                self._paused = None  # the disc that stopped for review is done: the queue goes on
            (messagebox.showwarning if summary.failed else messagebox.showinfo)(TITLE, text, parent=self)
        self._run("Copying", lambda p, job: al.run_copy(items, p, should_stop=lambda: job.cancelled), done)

    # ---- fix ---------------------------------------------------------------------------------

    def _list_nas(self):
        try:
            root = self._nas_root()
        except Exception as exc:
            self.say(f"NAS root problem: {al.friendly_error(exc)}")
            return

        def work(progress, job):
            try:
                return al.list_nas_shows(root)
            except al.UserError as exc:
                progress(f"{exc} The Fix tab needs the NAS.")
                return []

        def done(names):
            if not names:
                self._nas_picked = None
                self.nas_menu.configure(values=[NO_FOLDER])
                self.nas_menu.set(NO_FOLDER)
                return
            self.nas_menu.configure(values=[PICK_FOLDER] + names)
            if self._nas_picked not in names:  # keep only a folder Andy picked himself
                self._nas_picked = None
            self.nas_menu.set(self._nas_picked or PICK_FOLDER)
        self._run("Listing the NAS", work, done, busy=False)

    def _pick_nas_folder(self, folder):
        if folder in (PICK_FOLDER, NO_FOLDER):
            self._nas_picked = None
            return
        self._nas_picked = folder
        if not self.show_entry.get().strip():
            self.show_entry.insert(0, al.name_from_folder(folder))

    def _fix_match(self):
        folder = self._nas_picked
        if folder is None:
            messagebox.showinfo(TITLE, "Pick the show's folder on the NAS first (Refresh if the list is "
                                       "empty).", parent=self)
            return
        show = self._need_show()
        if show is None:
            return
        if not al.folder_matches_show(folder, show.name) and not messagebox.askyesno(
                TITLE, f"The folder {folder} does not look like {al.show_choice(show)}.\n\n"
                       f"Match it as that show anyway?", parent=self):
            return
        ok, season = self._season()
        client = self._client_or_ask() if ok else None
        if client is None:
            return
        root = self._nas_root()
        load = self._load_episodes(client)
        self.fix_result = None
        self.fix_table.clear()
        self.say(f"Matching {folder} on the NAS as {al.show_choice(show)}. Transcripts go to the home "
                 f"folder, never onto the NAS.")

        def work(progress, job):
            from dvd_processor.transcribe import transcribe_files
            from dvd_processor.transcript_matcher import match_transcripts
            return al.run_fix_match(show, folder, season, root, load, transcribe_files, match_transcripts, progress)

        def done(result):
            self.fix_result = result
            self.fix_table.load(result.rows, result.all_episodes, result.regulars)
            self.say(al.match_summary(result))
            self.say(al.unused_text(result.rows, result.episodes))
            if result.strays:
                self.say("Left alone (not episodes): " + ", ".join(p.name for p in result.strays))
        self._run("Matching on the NAS", work, done)

    def _rename(self):
        r = self.fix_result
        if r is None:
            return
        dupes = al.duplicate_problem(r.rows, "renaming")
        if dupes:  # the button is disabled then too; this is the backstop
            messagebox.showerror(TITLE, dupes, parent=self)
            return
        moves, right = al.rename_moves(r)
        if len(moves) == right:
            return
        if not messagebox.askyesno(TITLE, al.rename_confirm_text(moves, right, r.show_dir, r.rows), parent=self):
            return

        def done(out):
            text = (f"{out.renamed} renamed, {out.already_right} already right "
                    f"({out.transcripts_moved} cached transcripts moved with them).")
            self.say(text)
            self.fix_result = None
            self.fix_table.clear()
            self.fix_status.configure(text=text, text_color=TEXT)
            self._check_unfinished()
            messagebox.showinfo(TITLE, text, parent=self)
        # a stopped rename leaves a plan to resume, so the banner is checked either way
        self._run("Renaming", lambda p, j: al.run_rename(r, p), done, failed=self._check_unfinished,
                  cancellable=False)


def _setup_logging():
    path = gui_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def build_app() -> App:
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    return App()


def run():
    # pythonw has no console: give stray prints (whisper, downloads) somewhere to go.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    _setup_logging()
    build_app().mainloop()


if __name__ == "__main__":
    run()
