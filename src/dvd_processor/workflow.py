"""The steps behind the dvd-process commands, kept apart from click so they can be tested.

rip/add: a disc folder gets disc.json, MP4 encodes and transcripts.
match:   every disc folder of a show is matched at once, then copied to the NAS.
fix:     a show already on the NAS is transcribed from there and renamed in place.
"""
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from dvd_processor.discinfo import DiscInfo, natural_key, read_disc_info, write_disc_info
from dvd_processor.episodes import Episode
from dvd_processor.nas import (
    COPIED, EXISTS, SIZE_MISMATCH, check_root, copy_episode, episode_dest, episode_filename,
    log_action, safe_name,
)
from dvd_processor.review import ReviewRow

MIN_RIP_SECONDS = 600
VIDEO_SUFFIXES = {".mp4", ".mkv", ".m4v", ".avi"}


# ---- disc folders --------------------------------------------------------------------------

def safe_label(label: str) -> str:
    """A disc label usable as a folder name."""
    s = safe_name(label).replace(" ", "_")
    return s or "DISC"


def fallback_label(show: str, existing: int) -> str:
    """SHOW_D<n> when the disc gives no label: NewsRadio with 2 discs so far -> NEWSRADIO_D3."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", show).strip("_").upper() or "SHOW"
    return f"{slug}_D{existing + 1}"


def unique_disc_folder(root: Path, label: str) -> Path:
    """root/label, or root/label_2, label_3, ... so an existing disc folder is never reused."""
    root = Path(root)
    base = safe_label(label)
    p = root / base
    n = 1
    while p.exists():
        n += 1
        p = root / f"{base}_{n}"
    return p


def register_disc(folder: Path, show: str, tmdb_id: Optional[int]) -> tuple[DiscInfo, str]:
    """Write disc.json for an existing rip folder. Returns (info, what happened).

    An existing disc.json for the same show is kept (with its rip date). One for another
    show raises ValueError, so a folder is never silently moved between shows.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    info = read_disc_info(folder)
    if info is not None:
        same = (tmdb_id is not None and info.tmdb_id == tmdb_id) or info.show.casefold() == show.casefold()
        if not same:
            raise ValueError(f"{folder} is already registered to {info.show!r}")
        if info.tmdb_id is None and tmdb_id is not None:
            info = write_disc_info(folder, show, tmdb_id, info.label, info.ripped)
            return info, "updated"
        return info, "already registered"
    ripped = datetime.fromtimestamp(folder.stat().st_mtime).isoformat(timespec="seconds")
    info = write_disc_info(folder, show, tmdb_id, folder.name, ripped)
    log_action(f"registered {folder} as {show} (TMDB {tmdb_id})")
    return info, "registered"


def missing_encodes(folder: Path) -> list[Path]:
    """MKV rips in folder without an MP4 encode yet."""
    from dvd_processor.handbrake import mp4_path
    return [m for m in sorted(Path(folder).glob("*.mkv")) if not mp4_path(m).exists()]


# ---- gathering titles ----------------------------------------------------------------------

def gather_titles(folders: Iterable[Path], transcriber: Callable, progress=None) -> list:
    """Transcribe what is missing and return every title of every folder, in folder order."""
    out = []
    for f in folders:
        out += transcriber(Path(f), progress=progress)
    return out


# ---- copying to the NAS --------------------------------------------------------------------

@dataclass
class CopyItem:
    row: int            # 1-based row in the review table
    source: str
    dest: str


@dataclass
class CopySummary:
    copied: list[CopyItem] = field(default_factory=list)
    already_there: list[CopyItem] = field(default_factory=list)
    failed: list[tuple[CopyItem, str]] = field(default_factory=list)


def copy_plan(rows: list[ReviewRow], show: str, year: Optional[str], root: str) -> list[CopyItem]:
    """One copy per row that will act, named <root>/<Show (Year)>/Season n/<Show> SxxEyy.ext."""
    root = check_root(root, allow_drive=True)
    items = []
    for i, r in enumerate(rows, start=1):
        if not r.will_act:
            continue
        src = r.match.title.path
        ext = os.path.splitext(src)[1].lower() or ".mp4"
        dest = episode_dest(show, year, r.episode.season, r.episode.number, ext=ext, root=root,
                            allow_drive=True)
        items.append(CopyItem(i, src, dest))
    return items


def copy_all(items: list[CopyItem], progress=None) -> CopySummary:
    summary = CopySummary()
    for item in items:
        if progress:
            progress(f"copying {item.source} -> {item.dest}")
        try:
            status = copy_episode(item.source, item.dest, allow_drive=True)
        except Exception as exc:
            summary.failed.append((item, str(exc)))
            log_action(f"copy failed: {item.source} -> {item.dest}: {exc}")
            continue
        if status == COPIED:
            summary.copied.append(item)
        elif status == EXISTS:
            summary.already_there.append(item)
        elif status == SIZE_MISMATCH:
            summary.failed.append((item, "size mismatch after copy"))
    return summary


# ---- shows already on the NAS --------------------------------------------------------------

@dataclass
class NasShow:
    seasons: list[tuple[Path, list[Path]]]   # (season folder, its video files), in order
    strays: list[Path]                       # anything else, never moved


def scan_nas_show(show_dir: Path) -> NasShow:
    """Season folders of a show on the NAS and their video files, in natural order. Files
    that are not videos, and anything outside a Season folder, are strays."""
    show_dir = Path(show_dir)
    seasons, strays = [], []
    for entry in sorted(show_dir.iterdir(), key=lambda p: natural_key(p.name)):
        if entry.is_dir() and re.match(r"^Season \d+$", entry.name, re.I):
            videos = []
            for f in sorted(entry.iterdir(), key=lambda p: natural_key(p.name)):
                if f.is_file() and f.suffix.lower() in VIDEO_SUFFIXES:
                    videos.append(f)
                else:
                    strays.append(f)
            seasons.append((entry, videos))
        else:
            strays.append(entry)
    return NasShow(seasons, strays)


def nas_transcript_dir(show: str, season_folder: Path) -> Path:
    """~/.dvd_processor/nas-transcripts/<show>/<Season n>: never on the NAS itself."""
    return Path.home() / ".dvd_processor" / "nas-transcripts" / safe_name(show) / Path(season_folder).name


def rename_moves(rows: list[ReviewRow], show: str, show_dir: str) -> list[tuple[str, str]]:
    """(current path, Jellyfin name) for each row that will act."""
    show_dir = str(show_dir).replace("\\", "/").rstrip("/")
    moves = []
    for r in rows:
        if not r.will_act:
            continue
        src = r.match.title.path.replace("\\", "/")
        ext = os.path.splitext(src)[1].lower()
        dest = (f"{show_dir}/Season {r.episode.season}/"
                f"{episode_filename(show, r.episode.season, r.episode.number, ext)}")
        moves.append((src, dest))
    return moves


def limit_to_season(episodes: list[Episode], season: Optional[int]) -> list[Episode]:
    return [e for e in episodes if season is None or e.season == season]
