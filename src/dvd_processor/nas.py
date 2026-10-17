"""Put matched episodes on the Jellyfin NAS, or rename the ones already there.

Destination: <root>/<Show (Year)>/Season <n>/<Show> S01 E01.mp4, the naming already in use
on the NAS (note the space before E). Paths are built with forward slashes, which survive
shells and Python alike, and compared with slashes normalised: a lost backslash stranded 40
files mid-rename on 17 October 2026.

The NAS root must be a UNC path (//server/share/...). A drive path such as D:/TV is only
accepted when set on purpose (config nas_tv_root, or allow_drive=True); a path with a single
leading slash is always refused, since that is what a lost backslash turns a UNC path into.

Copies never overwrite and go through <dest>.part with a size check. Renames go through
<source>.renaming in two passes, so files that swap names never overwrite each other. The
move list and how far it got are saved as ~/.dvd_processor/rename-<timestamp>.json before
the first file moves, so an interrupted run can be finished with resume_renaming. Every
step is logged to ~/.dvd_processor/actions.log.
"""
import json
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


class RootError(ValueError):
    pass


_UNC = re.compile(r"^//[^/]+/[^/]+")
_DRIVE = re.compile(r"^[A-Za-z]:/")


def check_root(root: str, allow_drive: bool = False) -> str:
    """The root with forward slashes and no trailing slash. Raises RootError unless it is a
    UNC path, or (with allow_drive) a drive path like D:/TV."""
    r = str(root).replace("\\", "/").rstrip("/")
    if _UNC.match(r):
        return r
    if allow_drive and _DRIVE.match(r + "/"):
        return r
    raise RootError(f"not a NAS root (expected //server/share/...): {root!r}")


def nas_tv_root(config=None) -> str:
    """Config nas_tv_root, else the default. A drive path in config counts as set on purpose."""
    root = config.get("nas_tv_root") if config is not None else None
    return check_root(root or DEFAULT_NAS_TV_ROOT, allow_drive=bool(root))


def safe_name(name: str) -> str:
    """Remove characters Windows and SMB will not take; trailing dots and spaces too."""
    return re.sub(r"\s+", " ", UNSAFE_CHARS.sub("", name)).strip().rstrip(". ")


def show_folder(show: str, year: Optional[str] = None) -> str:
    s = safe_name(show)
    return f"{s} ({year})" if year else s


def episode_filename(show: str, season: int, episode: int, ext: str = ".mp4") -> str:
    return f"{safe_name(show)} S{season:02d} E{episode:02d}{ext}"


def episode_dest(show: str, year: Optional[str], season: int, episode: int,
                 ext: str = ".mp4", root: Optional[str] = None, allow_drive: bool = False) -> str:
    """<root>/<Show (Year)>/Season <n>/<Show> S01 E01.mp4. Pass allow_drive=True only with a
    root that came from nas_tv_root(config) or was otherwise chosen on purpose."""
    root = check_root(root or DEFAULT_NAS_TV_ROOT, allow_drive=allow_drive)
    return f"{root}/{show_folder(show, year)}/Season {season}/{episode_filename(show, season, episode, ext)}"


def canon(p) -> str:
    """Comparable form of a path whichever slashes and case it arrived with, for safety
    checks (Windows and SMB names are case-insensitive)."""
    return os.path.normcase(os.path.normpath(str(p).replace("\\", "/")))


def canon_exact(p) -> str:
    """Slashes normalised, case kept: for deciding whether a rename is needed at all."""
    return os.path.normpath(str(p).replace("\\", "/"))


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

def copy_episode(source, dest, log_path: Optional[Path] = None, allow_drive: bool = False) -> str:
    """Copy source to dest via dest.part, check the size, rename into place.

    dest must be on a UNC path (//server/share/...); a drive path only with allow_drive.
    A root that lost its leading slash raises RootError before anything is copied.

    Returns COPIED, EXISTS (dest already there, left alone) or SIZE_MISMATCH (the .part is
    removed and dest is not created). Raises FileNotFoundError if source is missing. If the
    copy itself fails, the .part is removed and the error raised.
    """
    source, dest = str(source), str(dest)
    check_root(dest, allow_drive=allow_drive)
    if not os.path.exists(source):
        raise FileNotFoundError(source)
    if os.path.exists(dest):
        log_action(f"skip, already there: {dest}", log_path)
        return EXISTS
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + PART
    try:
        shutil.copy2(source, part)
    except BaseException:
        _remove_quietly(part)
        log_action(f"copy failed, partial removed: {source} -> {dest}", log_path)
        raise
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


def _remove_quietly(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---- rename --------------------------------------------------------------------------------

PHASE_PASS1 = "pass1"   # sources are being moved to .renaming
PHASE_PASS2 = "pass2"   # every source is a .renaming; temps are being moved to destinations
PHASE_DONE = "done"


@dataclass
class RenameReport:
    renamed: list[tuple[str, str]] = field(default_factory=list)
    already_right: list[str] = field(default_factory=list)
    left_as_temp: list[str] = field(default_factory=list)  # resume_renaming only
    plan_path: Optional[Path] = None


class RenameError(RuntimeError):
    pass


def _plan_dir(log_path: Optional[Path]) -> Path:
    return (Path(log_path) if log_path else actions_log_path()).parent


def _write_plan(path: Path, moves: list[tuple[str, str]], phase: str) -> None:
    data = {"phase": phase, "moves": [{"source": s, "dest": d} for s, d in moves]}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_rename_plan(path) -> tuple[list[tuple[str, str]], str]:
    """(moves, phase) from a saved rename-<timestamp>.json."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [(m["source"], m["dest"]) for m in data["moves"]], data.get("phase", PHASE_PASS2)


def unfinished_rename_plans(log_path: Optional[Path] = None) -> list[Path]:
    """Saved rename plans that did not reach the end, oldest first."""
    out = []
    for p in sorted(_plan_dir(log_path).glob("rename-*.json")):
        try:
            if load_rename_plan(p)[1] != PHASE_DONE:
                out.append(p)
        except (OSError, ValueError, KeyError):
            continue
    return out


def _new_plan_path(log_path: Optional[Path]) -> Path:
    d = _plan_dir(log_path)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    p = d / f"rename-{stamp}.json"
    n = 1
    while p.exists():
        n += 1
        p = d / f"rename-{stamp}-{n}.json"
    return p


def _pass1(todo: list[tuple[str, str]], log_path: Optional[Path]) -> None:
    """Move each source that is still there, and has no temp yet, to its temp."""
    for src, _ in todo:
        tmp = src + RENAMING
        if os.path.exists(tmp) or not os.path.exists(src):
            continue
        os.rename(src, tmp)
        log_action(f"temp {src} -> {tmp}", log_path)


def _pass2(moves: list[tuple[str, str]], report: RenameReport, log_path: Optional[Path],
           verb: str) -> None:
    for src, dst in moves:
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
        log_action(f"{verb} -> {dst}", log_path)


def rename_plan_apply(moves: list[tuple[str, str]], log_path: Optional[Path] = None) -> RenameReport:
    """Rename each (source, dest), in two passes through <source>.renaming.

    Everything is checked before the first file moves: every source exists, no two moves
    share a destination, and no destination is occupied by a file that is not itself moving.
    These checks ignore case. Files already at their destination, case included, are
    skipped, so a case-only rename still happens. The move list is saved before pass 1.
    """
    report = RenameReport()
    todo = []
    for src, dst in moves:
        src, dst = str(src), str(dst)
        if canon_exact(src) == canon_exact(dst):
            report.already_right.append(dst)
        else:
            todo.append((src, dst))

    missing = [s for s, _ in todo if not os.path.exists(s)]
    if missing:
        msg = "sources missing, nothing renamed:\n" + "\n".join(missing)
        unfinished = unfinished_rename_plans(log_path)
        if unfinished:
            msg += ("\n\nAn earlier rename did not finish, which may be why. Finish it with "
                    "resume_renaming (dvd-process resume) from:\n" + "\n".join(str(p) for p in unfinished))
        raise RenameError(msg)
    dests = [canon(d) for _, d in todo] + [canon(d) for d in report.already_right]
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
    if not todo:
        log_action(f"rename: nothing to do, {len(report.already_right)} already right", log_path)
        return report

    plan = _new_plan_path(log_path)
    report.plan_path = plan
    _write_plan(plan, todo, PHASE_PASS1)
    log_action(f"rename plan saved: {plan}", log_path)
    _pass1(todo, log_path)
    _write_plan(plan, todo, PHASE_PASS2)
    _pass2(todo, report, log_path, "renamed")
    if report.left_as_temp:
        raise RenameError("destinations appeared during the rename; temps kept, run resume_renaming "
                          "after checking:\n" + "\n".join(report.left_as_temp))
    _write_plan(plan, todo, PHASE_DONE)
    log_action(f"rename: {len(report.renamed)} done, {len(report.already_right)} already right", log_path)
    return report


def resume_renaming(plan, log_path: Optional[Path] = None) -> RenameReport:
    """Finish an interrupted rename_plan_apply.

    plan is the saved rename-<timestamp>.json (preferred), or the move list itself, in which
    case the newest saved plan with the same moves is used when there is one. If pass 1 had
    not finished, it is finished first (each source still there with no temp goes to its
    temp), then each <source>.renaming is moved to its destination. Never overwrites: a temp
    whose destination exists is left where it is and reported. Without a saved plan only
    pass 2 is done, since a source could then be a finished move's new file.
    """
    report = RenameReport()
    plan_path: Optional[Path] = None
    if isinstance(plan, (str, Path)):
        plan_path = Path(plan)
        moves, phase = load_rename_plan(plan_path)
    else:
        moves = [(str(s), str(d)) for s, d in plan if canon_exact(s) != canon_exact(d)]
        phase = PHASE_PASS2
        key = [(canon(s), canon(d)) for s, d in moves]
        for p in sorted(_plan_dir(log_path).glob("rename-*.json"), reverse=True):
            try:
                saved, saved_phase = load_rename_plan(p)
            except (OSError, ValueError, KeyError):
                continue
            if [(canon(s), canon(d)) for s, d in saved] == key:
                plan_path, phase, moves = p, saved_phase, saved
                break
        else:
            log_action("resume: no saved plan for these moves, finishing pass 2 only", log_path)
    report.plan_path = plan_path
    if phase == PHASE_DONE:
        log_action(f"resume: {plan_path} already finished", log_path)
        return report
    if phase == PHASE_PASS1:
        _pass1(moves, log_path)
        if plan_path:
            _write_plan(plan_path, moves, PHASE_PASS2)
    _pass2(moves, report, log_path, "restored")
    if plan_path and not report.left_as_temp:
        _write_plan(plan_path, moves, PHASE_DONE)
    log_action(f"resume: {len(report.renamed)} moved, {len(report.left_as_temp)} left as {RENAMING}", log_path)
    return report
