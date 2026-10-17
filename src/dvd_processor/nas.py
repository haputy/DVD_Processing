"""Put matched episodes on the Jellyfin NAS, or rename the ones already there.

Destination: <root>/<Show (Year)>/Season <n>/<Show> S01 E01.mp4, the naming already in use
on the NAS (note the space before E). Paths are built with forward slashes, which survive
shells and Python alike, and compared with slashes normalised: a lost backslash stranded 40
files mid-rename on 17 October 2026.

Copies never overwrite and go through <dest>.part with a size check. Renames go through
<source>.renaming in two passes, so files that swap names never overwrite each other, and an
interrupted run can be finished with resume_renaming. Every step is logged to
~/.dvd_processor/actions.log.
"""
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_NAS_TV_ROOT = "//DXP2800-4CC5/Jellyfin/TVShows"
UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PART = ".part"
RENAMING = ".renaming"

COPIED = "copied"
EXISTS = "exists"
SIZE_MISMATCH = "size_mismatch"


def nas_tv_root(config=None) -> str:
    root = config.get("nas_tv_root") if config is not None else None
    return (root or DEFAULT_NAS_TV_ROOT).replace("\\", "/").rstrip("/")


def safe_name(name: str) -> str:
    """Remove characters Windows and SMB will not take; trailing dots and spaces too."""
    return re.sub(r"\s+", " ", UNSAFE_CHARS.sub("", name)).strip().rstrip(". ")


def show_folder(show: str, year: Optional[str] = None) -> str:
    s = safe_name(show)
    return f"{s} ({year})" if year else s


def episode_filename(show: str, season: int, episode: int, ext: str = ".mp4") -> str:
    return f"{safe_name(show)} S{season:02d} E{episode:02d}{ext}"


def episode_dest(show: str, year: Optional[str], season: int, episode: int,
                 ext: str = ".mp4", root: Optional[str] = None) -> str:
    root = (root or DEFAULT_NAS_TV_ROOT).replace("\\", "/").rstrip("/")
    return f"{root}/{show_folder(show, year)}/Season {season}/{episode_filename(show, season, episode, ext)}"


def canon(p) -> str:
    """Comparable form of a path, whichever slashes it arrived with."""
    return os.path.normcase(os.path.normpath(str(p).replace("\\", "/")))


def same_path(a, b) -> bool:
    return canon(a) == canon(b)


def actions_log_path() -> Path:
    return Path.home() / ".dvd_processor" / "actions.log"


def log_action(message: str, log_path: Optional[Path] = None) -> None:
    path = Path(log_path) if log_path else actions_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")


# ---- copy ----------------------------------------------------------------------------------

def copy_episode(source, dest, log_path: Optional[Path] = None) -> str:
    """Copy source to dest via dest.part, check the size, rename into place.

    Returns COPIED, EXISTS (dest already there, left alone) or SIZE_MISMATCH (the .part is
    removed and dest is not created). Raises FileNotFoundError if source is missing.
    """
    source, dest = str(source), str(dest)
    if not os.path.exists(source):
        raise FileNotFoundError(source)
    if os.path.exists(dest):
        log_action(f"skip, already there: {dest}", log_path)
        return EXISTS
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + PART
    shutil.copy2(source, part)
    if os.path.getsize(source) != os.path.getsize(part):
        os.remove(part)
        log_action(f"size mismatch copying {source} -> {dest}, nothing kept", log_path)
        return SIZE_MISMATCH
    if os.path.exists(dest):  # appeared while we copied: still never overwrite
        os.remove(part)
        log_action(f"skip, appeared during copy: {dest}", log_path)
        return EXISTS
    os.rename(part, dest)
    log_action(f"copied {source} -> {dest}", log_path)
    return COPIED


# ---- rename --------------------------------------------------------------------------------

@dataclass
class RenameReport:
    renamed: list[tuple[str, str]] = field(default_factory=list)
    already_right: list[str] = field(default_factory=list)
    left_as_temp: list[str] = field(default_factory=list)  # resume_renaming only


class RenameError(RuntimeError):
    pass


def rename_plan_apply(moves: list[tuple[str, str]], log_path: Optional[Path] = None) -> RenameReport:
    """Rename each (source, dest), in two passes through <source>.renaming.

    Everything is checked before the first file moves: every source exists, no two moves
    share a destination, and no destination is occupied by a file that is not itself moving.
    Files already at their destination are skipped.
    """
    report = RenameReport()
    todo = []
    for src, dst in moves:
        src, dst = str(src), str(dst)
        if same_path(src, dst):
            report.already_right.append(dst)
        else:
            todo.append((src, dst))

    missing = [s for s, _ in todo if not os.path.exists(s)]
    if missing:
        raise RenameError("sources missing, nothing renamed:\n" + "\n".join(missing))
    dests = [canon(d) for _, d in todo]
    if len(set(dests)) != len(dests):
        raise RenameError("two files planned for the same destination, nothing renamed")
    moving = {canon(s) for s, _ in todo}
    occupied = [d for _, d in todo if os.path.exists(d) and canon(d) not in moving]
    if occupied:
        raise RenameError("destinations already exist, nothing renamed:\n" + "\n".join(occupied))
    temps_in_way = [s + RENAMING for s, _ in todo if os.path.exists(s + RENAMING)]
    if temps_in_way:
        raise RenameError("temp files from an earlier run exist; run resume_renaming first:\n"
                          + "\n".join(temps_in_way))

    temps = []
    for src, dst in todo:
        tmp = src + RENAMING
        os.rename(src, tmp)
        temps.append((tmp, dst))
        log_action(f"temp {src} -> {tmp}", log_path)
    for (tmp, dst), (src, _) in zip(temps, todo):
        if os.path.exists(dst):
            raise RenameError(f"{dst} exists; temp file left at {tmp}. Run resume_renaming after checking.")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(tmp, dst)
        report.renamed.append((src, dst))
        log_action(f"renamed -> {dst}", log_path)
    log_action(f"rename: {len(report.renamed)} done, {len(report.already_right)} already right", log_path)
    return report


def resume_renaming(moves: list[tuple[str, str]], log_path: Optional[Path] = None) -> RenameReport:
    """Finish an interrupted rename_plan_apply: move each <source>.renaming to its destination.

    Never overwrites: a temp whose destination exists is left where it is and reported.
    """
    report = RenameReport()
    for src, dst in moves:
        src, dst = str(src), str(dst)
        tmp = src + RENAMING
        if not os.path.exists(tmp):
            continue
        if os.path.exists(dst):
            report.left_as_temp.append(tmp)
            log_action(f"NOT MOVED, destination exists: {dst} (temp kept at {tmp})", log_path)
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(tmp, dst)
        report.renamed.append((src, dst))
        log_action(f"restored -> {dst}", log_path)
    log_action(f"resume: {len(report.renamed)} moved, {len(report.left_as_temp)} left as {RENAMING}", log_path)
    return report
