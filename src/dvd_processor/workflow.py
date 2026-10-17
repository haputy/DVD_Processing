"""The steps behind the dvd-process commands, kept apart from click so they can be tested.

rip/add: a disc folder gets disc.json, MP4 encodes and transcripts.
match:   every disc folder of a show is matched at once, then copied to the NAS.
fix:     a show already on the NAS is transcribed from there and renamed in place.
"""
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from dvd_processor.discinfo import (
    DiscInfo, natural_key, read_disc_info, save_disc_info, write_disc_info,
)
from dvd_processor.episodes import Episode
from dvd_processor.nas import (
    COPIED, EXISTS, SIZE_MISMATCH, check_root, copy_episode, episode_dest, episode_filename,
    log_action, safe_name,
)
from dvd_processor.review import ReviewRow

MIN_RIP_SECONDS = 600
VIDEO_SUFFIXES = {".mp4", ".mkv", ".m4v", ".avi"}
SEASON_FOLDER = re.compile(r"^season\s*0*(\d+)$", re.I)
MAX_SURPLUS_TITLES = 2  # under --yes, refuse when titles outnumber episodes by more than this


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


def already_ripped(root: Path, label: str, show: str, tmdb_id: Optional[int]) -> Optional[Path]:
    """root/label when it is already a disc folder of this show (the same disc ripped
    before), else None. A clash with another show or an unregistered folder is not this."""
    p = Path(root) / safe_label(label)
    info = read_disc_info(p) if p.is_dir() else None
    return p if info is not None and info.belongs_to(show, tmdb_id) else None


def register_disc(folder: Path, show: str, tmdb_id: Optional[int],
                  season: Optional[int] = None) -> tuple[DiscInfo, str]:
    """Write disc.json for an existing rip folder. Returns (info, what happened).

    An existing disc.json for the same show is kept (with its rip date), updated with the
    TMDB id or season when those are new. One for another show raises ValueError, so a folder
    is never silently moved between shows.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    info = read_disc_info(folder)
    if info is not None:
        if not info.belongs_to(show, tmdb_id) and info.show.casefold() != show.casefold():
            raise ValueError(f"{folder} is already registered to {info.show!r}")
        changed = False
        if info.tmdb_id is None and tmdb_id is not None:
            info.tmdb_id, changed = tmdb_id, True
        if season is not None and info.season != season:
            info.season, changed = season, True
        if changed:
            save_disc_info(folder, info)
            log_action(f"updated {folder}: TMDB {info.tmdb_id}, season {info.season}")
            return info, "updated"
        return info, "already registered"
    ripped = datetime.fromtimestamp(folder.stat().st_mtime).isoformat(timespec="seconds")
    info = write_disc_info(folder, show, tmdb_id, folder.name, ripped, season=season)
    log_action(f"registered {folder} as {show} (TMDB {tmdb_id}, season {season})")
    return info, "registered"


def missing_encodes(folder: Path) -> list[Path]:
    """MKV rips in folder without an MP4 encode yet."""
    from dvd_processor.handbrake import mp4_path
    return [m for m in sorted(Path(folder).glob("*.mkv")) if not mp4_path(m).exists()]


# ---- ripping -------------------------------------------------------------------------------

@dataclass
class RipResult:
    finished: list[int] = field(default_factory=list)   # title indices ripped
    failed: Optional[int] = None                        # the title that failed, if any
    failed_files: list[Path] = field(default_factory=list)  # its partial MKVs, now .failed
    error: str = ""

    @property
    def complete(self) -> bool:
        return self.failed is None


def rip_titles(scanner, indices: list[int], folder: Path) -> RipResult:
    """Rip titles one at a time into folder, marking disc.json complete only when all have.

    On a makemkvcon error or Ctrl+C, the partial MKV of the title in progress is renamed to
    .failed and the rip stops; disc.json stays incomplete so match leaves the folder out.
    """
    folder = Path(folder)
    result = RipResult()
    for idx in indices:
        before = {p.name for p in folder.glob("*.mkv")}
        try:
            scanner.rip([idx], folder)
        except (subprocess.CalledProcessError, OSError, KeyboardInterrupt) as exc:
            result.failed = idx
            result.error = "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
            for p in sorted(folder.glob("*.mkv")):
                if p.name not in before:
                    target = p.with_name(p.name + ".failed")
                    p.replace(target)
                    result.failed_files.append(target)
            log_action(f"rip of title {idx} into {folder} failed ({result.error}); "
                       f"finished {result.finished}")
            return result
        result.finished.append(idx)
        log_action(f"ripped title {idx} into {folder}")
    info = read_disc_info(folder)
    if info is not None:
        info.complete = True
        save_disc_info(folder, info)
    return result


# ---- gathering titles ----------------------------------------------------------------------

def gather_titles(folders: Iterable[Path], transcriber: Callable, progress=None) -> list:
    """Transcribe what is missing and return every title of every folder, in folder order."""
    out = []
    for f in folders:
        out += transcriber(Path(f), progress=progress)
    return out


def discs_for_season(folders: list[Path], season: Optional[int]) -> tuple[list[Path], list[Path]]:
    """(folders to use, untagged folders among them) for match --season. Discs tagged with
    another season are left out; untagged discs are kept but returned so the caller can warn."""
    if season is None:
        return list(folders), []
    use, untagged = [], []
    for f in folders:
        info = read_disc_info(f)
        if info is None or info.season is None:
            use.append(f)
            untagged.append(f)
        elif info.season == season:
            use.append(f)
    return use, untagged


def too_many_titles(titles: list, episodes: list[Episode]) -> bool:
    """True when the episode-length titles with a transcript outnumber the episodes by more
    than MAX_SURPLUS_TITLES: a sign the wrong discs or season are in play."""
    from dvd_processor.transcript_matcher import is_episode_length
    n = sum(1 for t in titles if is_episode_length(t) and t.text.strip())
    return n > len(episodes) + MAX_SURPLUS_TITLES


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

def clean_nas_folder(name: str) -> str:
    """The show's folder name with slashes stripped. Refuses '..' and anything empty, so
    --nas-folder can never point outside the TV root."""
    parts = [p for p in re.split(r"[\\/]+", name.strip()) if p]
    if not parts or any(p.strip() in ("..", ".") for p in parts):
        raise ValueError(f"not a show folder name: {name!r}")
    return "/".join(parts)


def season_number(folder_name: str) -> Optional[int]:
    """'Season 01', 'season 1' and 'Season1' are all season 1; anything else is None."""
    m = SEASON_FOLDER.match(folder_name.strip())
    return int(m[1]) if m else None


@dataclass
class NasShow:
    seasons: list[tuple[Path, list[Path]]]   # (season folder, its video files), in order
    strays: list[Path]                       # anything else, never moved

    def season_folders(self) -> dict[int, str]:
        """Season number -> existing folder name, for naming renames."""
        out: dict[int, str] = {}
        for folder, _ in self.seasons:
            out.setdefault(season_number(folder.name), folder.name)
        return out


def scan_nas_show(show_dir: Path, season: Optional[int] = None) -> NasShow:
    """Season folders of a show on the NAS and their video files, in natural order. Files
    that are not videos, and anything outside a Season folder, are strays. With season, only
    that season's folder is scanned."""
    show_dir = Path(show_dir)
    seasons, strays = [], []
    for entry in sorted(show_dir.iterdir(), key=lambda p: natural_key(p.name)):
        n = season_number(entry.name) if entry.is_dir() else None
        if n is None:
            strays.append(entry)
            continue
        if season is not None and n != season:
            continue
        videos = []
        for f in sorted(entry.iterdir(), key=lambda p: natural_key(p.name)):
            if f.is_file() and f.suffix.lower() in VIDEO_SUFFIXES:
                videos.append(f)
            else:
                strays.append(f)
        seasons.append((entry, videos))
    return NasShow(seasons, strays)


def nas_transcript_dir(show: str, season_folder) -> Path:
    """~/.dvd_processor/nas-transcripts/<show>/<season folder>: never on the NAS itself."""
    return Path.home() / ".dvd_processor" / "nas-transcripts" / safe_name(show) / Path(season_folder).name


def rename_moves(rows: list[ReviewRow], show: str, show_dir: str,
                 season_folders: Optional[dict[int, str]] = None) -> list[tuple[str, str]]:
    """(current path, Jellyfin name) for each row that will act. Each season goes into the
    show's existing folder for it ("Season 01" stays "Season 01"); "Season n" only when the
    show has none."""
    show_dir = str(show_dir).replace("\\", "/").rstrip("/")
    season_folders = season_folders or {}
    moves = []
    for r in rows:
        if not r.will_act:
            continue
        src = r.match.title.path.replace("\\", "/")
        ext = os.path.splitext(src)[1].lower()
        s = r.episode.season
        folder = season_folders.get(s, f"Season {s}")
        dest = f"{show_dir}/{folder}/{episode_filename(show, s, r.episode.number, ext)}"
        moves.append((src, dest))
    return moves


def move_nas_transcripts(show: str, renamed: list[tuple[str, str]]) -> int:
    """Move cached NAS transcripts along with their renamed files, so the cache follows the
    file rather than the name. Swaps are safe: everything is read before anything is written.
    Returns how many moved."""
    from dvd_processor.transcribe import sidecar_path, source_stamp
    pending = []
    for src, dst in renamed:
        old = nas_transcript_dir(show, Path(src).parent) / f"{Path(src).stem}.txt"
        if old.exists():
            pending.append((old, old.read_text(encoding="utf-8"), dst))
    for old, _, _ in pending:
        for p in (old, sidecar_path(old)):
            if p.exists():
                p.unlink()
    for old, text, dst in pending:
        new = nas_transcript_dir(show, Path(dst).parent) / f"{Path(dst).stem}.txt"
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text(text, encoding="utf-8")
        if os.path.exists(dst):
            sidecar_path(new).write_text(json.dumps(source_stamp(Path(dst))), encoding="utf-8")
    return len(pending)


def limit_to_season(episodes: list[Episode], season: Optional[int]) -> list[Episode]:
    return [e for e in episodes if season is None or e.season == season]
