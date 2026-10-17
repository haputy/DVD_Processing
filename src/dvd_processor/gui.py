"""The "DVD to Jellyfin" window: put a disc in, click, come back later, review, copy.

This file only lays out widgets and calls app_logic. Long work runs in a worker thread that
never touches a widget: it posts messages to a queue, and the window reads the queue every
100 ms on the Tk event loop. Errors show as a plain message in the log and a dialog; the
full detail goes to ~/.dvd_processor/gui.log.

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
from dvd_processor.nas import nas_tv_root, resume_renaming, unfinished_rename_plans

log = logging.getLogger("dvd_processor.gui")

TITLE = "DVD to Jellyfin"
NO_SHOW = "(type a name and click Find)"
NO_FOLDER = "(no show folders found)"
ERROR_RED = "#b00020"
TEXT = ("gray10", "gray90")

COLUMNS = [("include", "Copy", 68), ("disc", "Disc", 150), ("title", "Title", 80), ("minutes", "Min", 50),
           ("episode", "Episode (click to change)", 330), ("score", "Score", 60), ("margin", "Margin", 60),
           ("flag", "Flag", 64), ("note", "Note", 380)]
TICK, NO_TICK = "\u2611", "\u2610"


def gui_log_path() -> Path:
    return Path.home() / ".dvd_processor" / "gui.log"


class ReviewTable(ctk.CTkFrame):
    """The match table: a ttk.Treeview with a tick column and an episode list per row.

    Click the tick to include or leave out a row; click the episode to pick another one.
    on_change() is called after every edit so the owner can recheck duplicates."""

    def __init__(self, master, on_change: Callable[[], None], include_heading: str = "Copy"):
        super().__init__(master, fg_color="transparent")
        self.on_change = on_change
        self.rows = []
        self.episodes = []
        self.choices = []
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
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._click)

    def load(self, rows, episodes):
        self._close_editor()
        self.rows, self.episodes = rows, episodes
        self.choices = al.episode_choices(episodes)
        self.refresh()

    def clear(self):
        self.load([], [])

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, v in enumerate(al.row_views(self.rows), start=1):
            self.tree.insert("", "end", iid=str(i), tags=(v.highlight,) if v.highlight else (),
                             values=(TICK if v.include else NO_TICK, v.disc, v.title, v.minutes,
                                     v.episode, v.score, v.margin, v.flag, v.note))

    def _click(self, event):
        if self.locked or self.tree.identify_region(event.x, event.y) != "cell":
            return
        item, col = self.tree.identify_row(event.y), self.tree.identify_column(event.x)
        if not item or not col:
            return
        n = int(item)
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


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(TITLE)
        self.geometry("1280x880")
        self.minsize(1000, 700)
        self.config_ = Config()
        self._queue: queue.Queue = queue.Queue()
        self._busy: Optional[str] = None
        self._client = None
        self._shows: dict[str, al.Show] = {}
        self._unfinished: list = []
        self.show: Optional[al.Show] = None
        self.rip_plan: Optional[al.RipPlan] = None
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
        ctk.CTkLabel(bar, text="Show").grid(row=0, column=0, padx=(10, 4), pady=8)
        self.show_entry = ctk.CTkEntry(bar, width=220, placeholder_text="e.g. NewsRadio")
        self.show_entry.grid(row=0, column=1, pady=8)
        self.show_entry.bind("<Return>", lambda e: self._find())
        self.find_button = ctk.CTkButton(bar, text="Find", width=70, command=self._find)
        self.find_button.grid(row=0, column=2, padx=6, pady=8)
        self.show_menu = ctk.CTkOptionMenu(bar, values=[NO_SHOW], width=340, command=self._pick_show,
                                           dynamic_resizing=False)
        self.show_menu.grid(row=0, column=3, padx=(0, 16), pady=8)
        ctk.CTkLabel(bar, text="Season").grid(row=0, column=4, padx=(0, 4))
        self.season_entry = ctk.CTkEntry(bar, width=50, placeholder_text="any")
        self.season_entry.grid(row=0, column=5, padx=(0, 16))
        ctk.CTkLabel(bar, text="Drive").grid(row=0, column=6, padx=(0, 4))
        self.drives = get_optical_drives()
        self.drive_menu = ctk.CTkOptionMenu(bar, values=self.drives, width=80)
        self.drive_menu.grid(row=0, column=7, padx=(0, 10))
        configured = self.config_.get("default_drive")
        self.drive_menu.set(configured if configured in self.drives else self.drives[0])

    def _build_tabs(self):
        self.tabs = ctk.CTkTabview(self)
        try:  # bigger tab names; a private attribute, so never let it stop the window
            self.tabs._segmented_button.configure(font=ctk.CTkFont(size=14, weight="bold"))
        except Exception:
            pass
        self.tabs.grid(row=2, column=0, sticky="nsew", padx=10, pady=4)
        self._build_rip_tab(self.tabs.add("Rip disc"))
        self._build_match_tab(self.tabs.add("Match and copy"))
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
        self.scan_button = ctk.CTkButton(row, text="Scan disc", width=130, command=self._scan)
        self.scan_button.pack(side="left", padx=(0, 10))
        self.again_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row, text="Rip again", variable=self.again_var,
                        command=self._refresh_buttons).pack(side="left", padx=(0, 10))
        self.rip_button = ctk.CTkButton(row, text="Rip, encode and transcribe", width=220, command=self._rip)
        self.rip_button.pack(side="left")
        self.rip_status = self._status(tab, "Put a disc in, pick the show at the top, then click Scan disc.")
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
        self.dest_label = ctk.CTkLabel(row, text="", anchor="w")
        self.dest_label.pack(side="left")
        self.match_status = self._status(tab, "When all the discs of the show are ripped, click Match.")
        self.match_table = ReviewTable(tab, self._refresh_buttons, include_heading="Copy")
        self.match_table.grid(row=2, column=0, sticky="nsew")

    def _build_fix_tab(self, tab):
        row = self._button_row(tab)
        ctk.CTkLabel(row, text="Show folder on the NAS").pack(side="left", padx=(0, 6))
        self.nas_menu = ctk.CTkOptionMenu(row, values=[NO_FOLDER], width=300, dynamic_resizing=False,
                                          command=self._pick_nas_folder)
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
        ctk.CTkLabel(frame, text="Progress", anchor="w").grid(row=0, column=0, sticky="w", padx=10)
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
        self._queue.put(("log", msg, None, False))

    def _poll(self):
        try:
            while True:
                try:
                    kind, what, payload, busy = self._queue.get_nowait()
                except queue.Empty:
                    break
                if busy:
                    self._busy = None
                try:
                    if kind == "log":
                        self.say(what)
                    elif kind == "done":
                        payload[0](payload[1])
                    elif kind == "error":
                        self._show_error(what, payload)
                except Exception as exc:
                    log.exception("error showing a result")
                    self._show_error("Showing the result", exc)
                finally:
                    if kind != "log":
                        self._refresh_buttons()
        finally:
            self.after(100, self._poll)

    def _show_error(self, what: str, exc: BaseException):
        msg = al.friendly_error(exc)
        self.say(f"{what} stopped: {msg}")
        messagebox.showerror(TITLE, f"{what} stopped.\n\n{msg}\n\nDetails are in {gui_log_path()}", parent=self)

    def _run(self, what: str, work: Callable[[Callable[[str], None]], object],
             done: Callable[[object], None], busy: bool = True) -> bool:
        """Run work(progress) in a thread, then done(result) on the Tk loop. busy disables the
        buttons meanwhile; quick background lookups pass busy=False."""
        if busy:
            if self._busy:
                return False
            self._busy = what
            self._refresh_buttons()

        def target():
            try:
                result = work(self._post)
            except Exception as exc:
                log.exception("%s failed", what)
                self._queue.put(("error", what, exc, busy))
            else:
                self._queue.put(("done", what, (done, result), busy))
        threading.Thread(target=target, name=what, daemon=True).start()
        return True

    def report_callback_exception(self, exc, val, tb):
        log.error("error in the window", exc_info=(exc, val, tb))
        self._show_error("That", val)

    def _refresh_buttons(self):
        idle = self._busy is None

        def state(button, ok):
            button.configure(state="normal" if idle and ok else "disabled")
        for b in (self.find_button, self.scan_button, self.match_button, self.nas_refresh, self.fix_button,
                  self.resume_button):
            state(b, True)
        state(self.rip_button, self.rip_plan is not None and self.rip_plan.can_rip(self.again_var.get()))
        self.match_table.locked = self.fix_table.locked = not idle

        r = self.match_result
        if r is None:
            state(self.copy_button, False)
            self.copy_button.configure(text="Copy to NAS")
        else:
            dupes = al.duplicate_problem(r.rows, "copying")
            n = al.included_count(r.rows)
            state(self.copy_button, n > 0 and not dupes)
            self.copy_button.configure(text=f"Copy {n} to NAS" if n else "Copy to NAS")
            self.match_status.configure(text=dupes or al.match_summary(r), text_color=ERROR_RED if dupes else TEXT)
        f = self.fix_result
        if f is None:
            state(self.rename_button, False)
        else:
            dupes = al.duplicate_problem(f.rows, "renaming")
            moves, right = ([], 0) if dupes else al.rename_moves(f)
            state(self.rename_button, not dupes and len(moves) > right)
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
            messagebox.showinfo(TITLE, "Pick the show first: type its name at the top and click Find.", parent=self)
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

    def _on_close(self):
        if self._busy and not messagebox.askyesno(
                TITLE, f"Still working ({self._busy.lower()}). If you close now, it stops part way, and a "
                       f"rip would need doing again. Close anyway?", parent=self):
            return
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
        self._run("Looking for a disc", lambda p: al.pick_drive(drives, drive_has_disc, configured),
                  self.drive_menu.set, busy=False)
        self._list_nas()
        self.say("Ready. Rip each disc on the Rip disc tab, then Match and copy when all are in.")

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
        self._run("Resuming the rename", lambda p: resume_renaming(plan), done)

    # ---- top bar -----------------------------------------------------------------------------

    def _find(self):
        name = self.show_entry.get().strip()
        if not name:
            messagebox.showinfo(TITLE, "Type the show's name first.", parent=self)
            return
        client = self._client_or_ask()
        if client is None:
            return
        self.say(f"Searching TMDB for {name}...")

        def done(results):
            if not results:
                self.say(f"TMDB has nothing called {name}.")
                return
            shows = [al.Show(r.tmdb_id, r.name, r.year) for r in results[:15]]
            self._shows = {al.show_choice(s): s for s in shows}
            labels = list(self._shows)
            self.show_menu.configure(values=labels)
            self.show_menu.set(labels[0])
            self._pick_show(labels[0])
            if len(shows) > 1:
                self.say(f"TMDB found {len(shows)} shows. If {labels[0]} is the wrong one, pick another "
                         f"from the list.")
        self._run("Searching TMDB", lambda p: client.search_show(name), done)

    def _pick_show(self, label):
        show = self._shows.get(label)
        if show is not None:
            self.show = show
            self.say(f"Show: {al.show_choice(show)}")

    # ---- rip ---------------------------------------------------------------------------------

    def _scanner(self) -> DiscScanner:
        return DiscScanner(drive=self.drive_menu.get(), makemkv_path=resolve_makemkv_path(self.config_), quiet=True)

    def _scan(self):
        show = self._need_show()
        if show is None:
            return
        scanner = self._scanner()
        root = rip_root(self.config_)
        self.rip_plan = None
        self.scan_tree.delete(*self.scan_tree.get_children())
        self.say(f"Scanning the disc in {scanner.drive} (this takes a minute)...")

        def work(progress):
            titles, label = scanner.scan_with_label()
            if not titles:
                raise al.UserError(f"MakeMKV found no titles on the disc in {scanner.drive}. Is a disc in?")
            return al.plan_rip(titles, label, show, root)

        def done(plan):
            self.rip_plan = plan
            for r in plan.rows:
                self.scan_tree.insert("", "end", values=(r.index, r.length, "yes" if r.rip else "skip", r.why),
                                      tags=() if r.rip else ("skip",))
            self.again_var.set(False)
            self.rip_status.configure(text=f"Disc {plan.label}. {plan.summary}")
            self.say(f"Disc {plan.label}: {len(plan.rows)} titles. {plan.summary}")
        self._run("Scanning the disc", work, done)

    def _rip(self):
        plan, show = self.rip_plan, self.show
        if plan is None or show is None:
            return
        ok, season = self._season()
        if not ok:
            return
        again = self.again_var.get()
        scanner = self._scanner()
        encoder = HandBrakeEncoder(resolve_handbrake_path(self.config_), quiet=True)
        self.rip_status.configure(text="Ripping, encoding and transcribing. This takes a while; the log below "
                                       "shows where it is.")

        def work(progress):
            from dvd_processor.transcribe import transcribe_disc
            folder = al.run_rip(plan, scanner, encoder, transcribe_disc, show, season, again, progress)
            return folder, eject(scanner.drive)

        def done(result):
            folder, ejected = result
            self.rip_plan = None
            msg = "Disc done. Put in the next disc and scan, or go to Match when all discs are in."
            self.rip_status.configure(text=msg)
            self.say(f"Disc done: {folder}" + (" (ejected)" if ejected else ""))
            self.say(msg)
        self._run("Ripping", work, done)

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
        self.match_result = None
        self.match_table.clear()
        self.say(f"Matching {show.name}" + (f" season {season}" if season else "") + "...")

        def work(progress):
            from dvd_processor.transcribe import transcribe_disc
            from dvd_processor.transcript_matcher import match_transcripts
            return al.run_match(show, season, root, load, transcribe_disc, match_transcripts, progress)

        def done(result):
            self.match_result = result
            self.match_table.load(result.rows, result.all_episodes)
            self.say(al.match_summary(result))
            self.say(al.unused_text(result.rows, result.episodes))
        self._run("Matching", work, done)

    def _copy(self):
        r = self.match_result
        if r is None:
            return
        try:
            root = self._nas_root()
            items = al.copy_items(r.rows, r.show, root)
        except Exception as exc:
            self._show_error("Copying", exc)
            return
        if not items:
            return
        if not messagebox.askyesno(
                TITLE, f"Copy {len(items)} episodes to\n{root}?\n\nFor example:\n{items[0].dest}\n\n"
                       f"Nothing already there is overwritten, and the originals in C:\\Video stay.", parent=self):
            return
        self.say(al.unused_text(r.rows, r.episodes))

        def done(summary):
            text = al.copy_summary_text(summary)
            self.say(text)
            (messagebox.showwarning if summary.failed else messagebox.showinfo)(TITLE, text, parent=self)
        self._run("Copying", lambda p: al.run_copy(items, p), done)

    # ---- fix ---------------------------------------------------------------------------------

    def _list_nas(self):
        try:
            root = self._nas_root()
        except Exception as exc:
            self.say(f"NAS root problem: {al.friendly_error(exc)}")
            return

        def work(progress):
            try:
                return al.list_nas_shows(root)
            except al.UserError as exc:
                progress(f"{exc} The Fix tab needs the NAS.")
                return []

        def done(names):
            values = names or [NO_FOLDER]
            current = self.nas_menu.get()
            self.nas_menu.configure(values=values)
            self.nas_menu.set(current if current in names else values[0])
        self._run("Listing the NAS", work, done, busy=False)

    def _pick_nas_folder(self, folder):
        if folder != NO_FOLDER and not self.show_entry.get().strip():
            self.show_entry.insert(0, al.name_from_folder(folder))

    def _fix_match(self):
        folder = self.nas_menu.get()
        if folder == NO_FOLDER:
            messagebox.showinfo(TITLE, "No show folder picked. Is the NAS on? Click Refresh.", parent=self)
            return
        show = self._need_show()
        if show is None:
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

        def work(progress):
            from dvd_processor.transcribe import transcribe_files
            from dvd_processor.transcript_matcher import match_transcripts
            return al.run_fix_match(show, folder, season, root, load, transcribe_files, match_transcripts, progress)

        def done(result):
            self.fix_result = result
            self.fix_table.load(result.rows, result.all_episodes)
            self.say(al.match_summary(result))
            self.say(al.unused_text(result.rows, result.episodes))
            if result.strays:
                self.say("Left alone (not episodes): " + ", ".join(p.name for p in result.strays))
        self._run("Matching on the NAS", work, done)

    def _rename(self):
        r = self.fix_result
        if r is None:
            return
        moves, right = al.rename_moves(r)
        if not messagebox.askyesno(
                TITLE, f"Rename {len(moves) - right} files in {r.show_dir}?\n{right} are already right.\n\n"
                       f"The plan is saved first, so an interrupted rename can be resumed.", parent=self):
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

        def work(progress):
            try:
                return al.run_rename(r, progress)
            finally:  # a stopped rename leaves a plan to resume: show the banner either way
                self._queue.put(("done", "", (lambda _x: self._check_unfinished(), None), False))
        self._run("Renaming", work, done)


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
