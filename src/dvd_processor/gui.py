import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from dvd_processor.config import Config
from dvd_processor.disc import DiscScanner
from dvd_processor.drives import get_optical_drives
from dvd_processor.matcher import MatchResult, match_titles_to_episodes
from dvd_processor.renamer import build_output_filename, rename_ripped_files
from dvd_processor.tmdb import TmdbClient

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class DVDProcessorApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("DVD Processor for Jellyfin")
        self.geometry("1100x780")
        self.minsize(900, 650)

        self._config = Config()
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._titles = []
        self._episodes = []
        self._matches: list[MatchResult] = []

        self._build_ui()
        self._load_config()
        self.after(100, self._poll_log)

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_settings_bar()
        self._build_middle()
        self._build_bottom_bar()

    def _build_settings_bar(self):
        bar = ctk.CTkFrame(self)
        bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        bar.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(bar, text="Drive:").grid(row=0, column=0, padx=(10, 4), pady=8)
        self._drive_var = ctk.StringVar()
        drives = get_optical_drives()
        self._drive_menu = ctk.CTkOptionMenu(bar, variable=self._drive_var, values=drives, width=80)
        self._drive_menu.grid(row=0, column=1, padx=(0, 16), pady=8)
        if drives:
            self._drive_var.set(drives[0])

        ctk.CTkLabel(bar, text="Output:").grid(row=0, column=2, padx=(0, 4), pady=8)
        self._output_var = ctk.StringVar()
        ctk.CTkEntry(bar, textvariable=self._output_var, width=300).grid(
            row=0, column=3, padx=(0, 4), pady=8, sticky="ew"
        )
        ctk.CTkButton(bar, text="Browse", width=70, command=self._browse_output).grid(
            row=0, column=4, padx=(0, 16), pady=8
        )
        self._transcode_var = ctk.BooleanVar()
        ctk.CTkCheckBox(bar, text="Transcode (HandBrake)", variable=self._transcode_var).grid(
            row=0, column=5, padx=(0, 10), pady=8
        )

    def _build_middle(self):
        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        mid.grid_columnconfigure(0, weight=1)
        mid.grid_columnconfigure(1, weight=1)
        mid.grid_rowconfigure(0, weight=1)
        self._build_left_panel(mid)
        self._build_right_panel(mid)
        self._build_mapping_panel(mid)

    def _build_left_panel(self, parent):
        frame = ctk.CTkFrame(parent)
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(5, weight=1)

        ctk.CTkLabel(frame, text="Show Name:").grid(row=0, column=0, padx=10, pady=(10, 2), sticky="w")
        self._show_var = ctk.StringVar()
        ctk.CTkEntry(frame, textvariable=self._show_var, placeholder_text="e.g. NewsRadio").grid(
            row=1, column=0, padx=10, pady=(0, 6), sticky="ew"
        )

        inner = ctk.CTkFrame(frame, fg_color="transparent")
        inner.grid(row=2, column=0, padx=10, pady=(0, 6), sticky="ew")
        ctk.CTkLabel(inner, text="Season:").grid(row=0, column=0, padx=(0, 4))
        self._season_var = ctk.StringVar(value="1")
        ctk.CTkEntry(inner, textvariable=self._season_var, width=50).grid(row=0, column=1, padx=(0, 16))
        ctk.CTkLabel(inner, text="Start Episode:").grid(row=0, column=2, padx=(0, 4))
        self._start_ep_var = ctk.StringVar(value="1")
        ctk.CTkEntry(inner, textvariable=self._start_ep_var, width=50).grid(row=0, column=3)

        ctk.CTkButton(frame, text="Search TMDB", command=self._search_tmdb).grid(
            row=3, column=0, padx=10, pady=(0, 6)
        )
        ctk.CTkLabel(frame, text="Episodes from TMDB:").grid(row=4, column=0, padx=10, pady=(4, 2), sticky="w")
        self._episode_list = ctk.CTkTextbox(frame, state="disabled")
        self._episode_list.grid(row=5, column=0, padx=10, pady=(0, 10), sticky="nsew")

    def _build_right_panel(self, parent):
        frame = ctk.CTkFrame(parent)
        frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        ctk.CTkButton(frame, text="Scan Disc", command=self._scan_disc).grid(
            row=0, column=0, padx=10, pady=(10, 6)
        )
        ctk.CTkLabel(frame, text="Disc Titles:").grid(row=1, column=0, padx=10, pady=(4, 2), sticky="w")
        self._titles_box = ctk.CTkTextbox(frame, state="disabled")
        self._titles_box.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="nsew")

    def _build_mapping_panel(self, parent):
        frame = ctk.CTkFrame(parent)
        frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(frame, text="Propose Mapping", command=self._propose_mapping).grid(
            row=0, column=0, padx=10, pady=(10, 6)
        )
        self._mapping_box = ctk.CTkTextbox(frame, state="disabled", height=160)
        self._mapping_box.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="ew")

    def _build_bottom_bar(self):
        bar = ctk.CTkFrame(self)
        bar.grid(row=2, column=0, sticky="ew", padx=10, pady=(5, 0))
        bar.grid_columnconfigure(2, weight=1)

        self._dry_run_btn = ctk.CTkButton(
            bar, text="Dry Run", width=100, fg_color="grey",
            command=lambda: self._start_rip(dry_run=True)
        )
        self._dry_run_btn.grid(row=0, column=0, padx=(10, 6), pady=8)

        self._rip_btn = ctk.CTkButton(
            bar, text="Rip Episodes", width=120,
            command=lambda: self._start_rip(dry_run=False)
        )
        self._rip_btn.grid(row=0, column=1, padx=(0, 10), pady=8)

        self._progress = ctk.CTkProgressBar(bar)
        self._progress.set(0)
        self._progress.grid(row=0, column=2, padx=(0, 10), pady=8, sticky="ew")

        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=3, column=0, sticky="ew", padx=10, pady=(5, 10))
        log_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(log_frame, text="Log:").grid(row=0, column=0, padx=10, pady=(6, 2), sticky="w")
        self._log_box = ctk.CTkTextbox(log_frame, state="disabled", height=120)
        self._log_box.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="ew")

    # ── Config ────────────────────────────────────────────────────────────

    def _load_config(self):
        self._output_var.set(self._config.get("default_output", ""))
        saved = self._config.get("default_drive")
        if saved:
            self._drive_var.set(saved)

    def _save_config(self):
        self._config.set("default_output", self._output_var.get())
        self._config.set("default_drive", self._drive_var.get())

    # ── Helpers ───────────────────────────────────────────────────────────

    def _browse_output(self):
        path = filedialog.askdirectory()
        if path:
            self._output_var.set(path)

    def _log(self, msg: str):
        self._log_queue.put(msg)

    def _poll_log(self):
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self._log_box.configure(state="normal")
                self._log_box.insert("end", msg + "\n")
                self._log_box.see("end")
                self._log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def _set_text(self, widget, text: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _ensure_api_key(self) -> str | None:
        key = self._config.get("tmdb_api_key")
        if not key:
            dialog = ctk.CTkInputDialog(text="Enter your TMDB API key:", title="TMDB API Key")
            key = dialog.get_input()
            if key:
                self._config.set("tmdb_api_key", key)
        return key or None

    # ── Disc scan ─────────────────────────────────────────────────────────

    def _scan_disc(self):
        self._log("Scanning disc...")
        self._set_text(self._titles_box, "Scanning...")
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        try:
            makemkv = self._config.get("makemkv_path", r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe")
            scanner = DiscScanner(drive=self._drive_var.get(), makemkv_path=makemkv)
            self._titles = scanner.scan()
            lines = []
            for t in self._titles:
                h = t.duration_secs // 3600
                m = (t.duration_secs % 3600) // 60
                s = t.duration_secs % 60
                tag = "  [short - will be skipped]" if t.duration_secs < 600 else ""
                lines.append(f"Title {t.index:2d}  {h:02d}:{m:02d}:{s:02d}  {t.output_filename}{tag}")
            self._log(f"Found {len(self._titles)} titles.")
            self.after(0, lambda: self._set_text(self._titles_box, "\n".join(lines)))
        except Exception as e:
            self._log(f"Scan error: {e}")
            self.after(0, lambda: self._set_text(self._titles_box, f"Error: {e}"))

    # ── TMDB ──────────────────────────────────────────────────────────────

    def _search_tmdb(self):
        show = self._show_var.get().strip()
        if not show:
            messagebox.showwarning("Missing", "Enter a show name first.")
            return
        key = self._ensure_api_key()
        if not key:
            return
        self._log(f"Searching TMDB for '{show}'...")
        self._set_text(self._episode_list, "Searching...")
        threading.Thread(target=self._do_search, args=(show, key), daemon=True).start()

    def _do_search(self, show: str, key: str):
        try:
            client = TmdbClient(api_key=key)
            results = client.search_show(show)
            if not results:
                self.after(0, lambda: self._set_text(self._episode_list, "No results found."))
                return
            picked = results[0]
            self._log(f"Using: {picked.name} ({picked.year})")
            season = int(self._season_var.get() or "1")
            self._episodes = client.get_season_episodes(picked.tmdb_id, season)
            lines = [
                f"E{ep.number:02d}  {ep.title}  "
                f"({ep.runtime_secs // 60 if ep.runtime_secs else '?'} min)"
                for ep in self._episodes
            ]
            self.after(0, lambda: self._set_text(self._episode_list, "\n".join(lines)))
            self._log(f"Loaded {len(self._episodes)} episodes for Season {season}.")
        except Exception as e:
            self._log(f"TMDB error: {e}")
            self.after(0, lambda: self._set_text(self._episode_list, f"Error: {e}"))

    # ── Mapping ───────────────────────────────────────────────────────────

    def _propose_mapping(self):
        if not self._titles:
            messagebox.showwarning("Missing", "Scan the disc first.")
            return
        if not self._episodes:
            messagebox.showwarning("Missing", "Search TMDB first.")
            return

        try:
            start = int(self._start_ep_var.get() or "1")
        except ValueError:
            start = 1

        candidates = [t for t in self._titles if t.duration_secs >= 600]
        ep_pool = [ep for ep in self._episodes if ep.number >= start]

        durations = [ep.runtime_secs for ep in ep_pool if ep.runtime_secs]
        all_same = len(set(durations)) <= 1

        if all_same:
            self._matches = [
                MatchResult(
                    title=title, episode=ep,
                    difference_secs=0, low_confidence=False,
                    no_runtime_data=not ep.runtime_secs,
                )
                for title, ep in zip(candidates, ep_pool)
            ]
            method = "sequential (same duration)"
        else:
            self._matches = match_titles_to_episodes(candidates, ep_pool)
            method = "duration matched"

        lines = [f"{'#':<4} {'Duration':<10} {'':3} {'Ep':<5} Title"]
        lines.append("-" * 55)
        for i, m in enumerate(self._matches, 1):
            h = m.title.duration_secs // 3600
            mn = (m.title.duration_secs % 3600) // 60
            s = m.title.duration_secs % 60
            flag = " ⚠ low confidence" if m.low_confidence else ""
            lines.append(f"{i:<4} {h:02d}:{mn:02d}:{s:02d}   →  E{m.episode.number:02d}  {m.episode.title}{flag}")

        self._set_text(self._mapping_box, "\n".join(lines))
        self._log(f"Proposed {len(self._matches)} matches ({method}).")

    # ── Rip ───────────────────────────────────────────────────────────────

    def _start_rip(self, dry_run: bool):
        if not self._matches:
            messagebox.showwarning("Missing", "Propose a mapping first.")
            return
        output = self._output_var.get().strip()
        if not output and not dry_run:
            messagebox.showwarning("Missing", "Set an output directory first.")
            return
        self._save_config()
        self._progress.set(0)
        self._rip_btn.configure(state="disabled")
        self._dry_run_btn.configure(state="disabled")
        threading.Thread(target=self._do_rip, args=(dry_run, output), daemon=True).start()

    def _do_rip(self, dry_run: bool, output: str):
        try:
            season = int(self._season_var.get() or "1")
            show = self._show_var.get()
            if dry_run:
                self._log("Dry run — files that would be created:")
                for m in self._matches:
                    self._log(f"  {build_output_filename(show, season, m)}")
                self._log("Dry run complete.")
            else:
                makemkv = self._config.get("makemkv_path", r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe")
                scanner = DiscScanner(drive=self._drive_var.get(), makemkv_path=makemkv)
                output_dir = Path(output)
                total = len(self._matches)
                for i, m in enumerate(self._matches):
                    self._log(f"Ripping title {m.title.index} ({i + 1}/{total})...")
                    scanner.rip([m.title.index], output_dir)
                    self.after(0, lambda v=(i + 1) / total: self._progress.set(v))
                rename_ripped_files(show, season, self._matches, output_dir)
                self._log(f"Done! {total} episodes saved to {output_dir}")
                self.after(0, lambda: self._progress.set(1.0))
        except Exception as e:
            self._log(f"Error: {e}")
        finally:
            self.after(0, lambda: self._rip_btn.configure(state="normal"))
            self.after(0, lambda: self._dry_run_btn.configure(state="normal"))


def run():
    app = DVDProcessorApp()
    app.mainloop()
