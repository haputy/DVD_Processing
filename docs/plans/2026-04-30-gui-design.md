# DVD Processor GUI Design Document

**Date:** 2026-04-30  
**Status:** Approved

## Overview

A CustomTkinter desktop GUI for the DVD processor that supports the full pipeline: scan disc, search TMDB, propose episode mapping, correct assignments, and rip — all without touching the CLI.

## Layout

Single window, three horizontal bands:

### Top Band — Settings
Drive letter dropdown (auto-detected optical drives), Output folder with Browse button, MakeMKV path field (pre-filled from config), Transcode checkbox.

### Middle Band — Two Columns
**Left column:** Show name field, Season number, Start Episode (optional), Search TMDB button, scrollable episode list.

**Right column:** Scan Disc button, scrollable disc title table (Title #, Duration, Filename). Short titles (< 10 min) shown in grey.

**Full-width below both columns:** Propose Mapping button generates matching table — Title # | Duration | → | Episode | TMDB Runtime | Diff | Notes. Episode cells are editable via dropdown.

### Bottom Band — Actions and Log
Dry Run button, Rip button, progress bar, scrolling real-time log.

---

## Interactions and Data Flow

1. **On launch** — Load config, auto-detect optical drives, pre-fill all fields.
2. **Scan Disc** — Background thread, populates disc title table.
3. **Search TMDB** — Background thread, popup for multiple results, populates episode list.
4. **Propose Mapping** — Duration matching if runtimes differ; sequential from start episode if all same duration. Low-confidence rows highlighted yellow. Episode cells editable via dropdown.
5. **Rip / Dry Run** — Background thread, MakeMKV output streams to log, progress bar advances per title. Rename runs on completion.
6. **Auto-save config** — Output dir, drive, TMDB key persisted on rip.

---

## Technical Details

### New Files
- `src/dvd_processor/gui.py` — CustomTkinter UI (~400 lines)
- `launch.bat` — project root, user copies to desktop

### Dependencies
- `customtkinter` added to `pyproject.toml`

### Threading
All blocking calls (MakeMKV, TMDB) run in `threading.Thread`. Log area uses a thread-safe `queue.Queue` polled via `after()`.

### Error Handling
- MakeMKV not found → red error in log, Rip disabled
- TMDB key missing → popup prompt, saves to config
- No disc → empty state message in titles table
- Rip failure mid-batch → log shows failed title, continues remaining

### Launch
```bat
@echo off
cd /d "C:\github\DVD_Processing"
python -m dvd_processor.gui
pause
```
User copies `launch.bat` to desktop. No terminal visible on launch (use `pythonw` variant for production).
