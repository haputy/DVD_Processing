"""What the "DVD to Jellyfin" window does, kept free of any GUI toolkit so it can be tested.

The window (gui.py) only lays out widgets and calls these. Everything here reuses the steps
the CLI uses (workflow, review, transcript_matcher, nas); nothing about matching, copying or
renaming is done differently.

Rows in the review table are review.ReviewRow objects. In the window a row is "included"
when it will be copied (or renamed): ReviewRow.will_act, that is, it has an episode and is
not skipped. CHECK rows start unticked.
"""
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from dvd_processor import workflow
from dvd_processor.discinfo import disc_folders_for_show, write_disc_info
from dvd_processor.episodes import Episode
from dvd_processor.handbrake import mp4_path
from dvd_processor.nas import RenameError, RootError, canon_exact, log_action
from dvd_processor.procs import Cancelled
from dvd_processor.review import FLAG_FIXED, ReviewRow, check_rows, episode_code, rows_from_matches
from dvd_processor.transcript_matcher import CONFIDENT_MARGIN, FLAG_CHECK, FLAG_EXTRA

NO_EPISODE = "none"

Progress = Callable[[str], None]


class UserError(Exception):
    """A problem to show as it is, in plain words, with no traceback."""


def _quiet(_msg: str) -> None:
    pass


# ---- the show, the season and the drive ----------------------------------------------------

@dataclass
class Show:
    tmdb_id: int
    name: str
    year: str = ""


def show_choice(show) -> str:
    """'Northern Exposure (1990) · TMDB 4499', for the show dropdown."""
    return f"{show.name} ({show.year or '?'}) · TMDB {show.tmdb_id}"


def show_choices(results) -> list[str]:
    return [show_choice(r) for r in results]


def auto_pick(results, name: str) -> Optional[int]:
    """The result to pick without asking, the CLI's --yes rule: only when exactly one result
    has the exact name (ignoring case) and it is the first result. Else None: Andy picks."""
    want = (name or "").strip().casefold()
    exact = [i for i, r in enumerate(results) if r.name.casefold() == want]
    return 0 if exact == [0] else None


def pick_prompt(n: int) -> str:
    """What the show menu says while Andy still has to pick."""
    return "(pick the show)" if n == 1 else f"(pick one of {n} shows)"


def parse_season(text: str) -> Optional[int]:
    """'' -> None, '3' -> 3. Anything else raises UserError."""
    t = (text or "").strip()
    if not t:
        return None
    if not t.isdigit() or int(t) < 1:
        raise UserError("Season must be a number such as 3, or left empty.")
    return int(t)


def pick_drive(drives: list[str], has_disc: Callable[[str], bool],
               configured: Optional[str] = None) -> str:
    """The first drive with a disc in it, else the configured default drive, else the first."""
    for d in drives:
        try:
            if has_disc(d):
                return d
        except Exception:
            continue
    if configured:
        for d in drives:
            if d.upper().rstrip("\\") == configured.upper().rstrip("\\"):
                return d
    return drives[0] if drives else (configured or "E:")


# ---- rip -----------------------------------------------------------------------------------

@dataclass
class ScanRow:
    index: int
    seconds: int
    rip: bool
    why: str = ""

    @property
    def length(self) -> str:
        s = self.seconds
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


@dataclass
class RipPlan:
    show: Show                 # the show picked when the disc was scanned
    rows: list[ScanRow]
    keep: list                 # the DiscTitles to rip
    label: str
    root: Path
    folder: Path               # where it would go (rechecked when the rip starts)
    fingerprint: list[int]
    already: Optional[Path]    # the folder that already holds this disc, if any

    @property
    def summary(self) -> str:
        if not self.keep:
            return "No titles of 10 minutes or more on this disc, so nothing to rip."
        if self.already is not None:
            return (f"{self.already} already holds this disc, so it looks already ripped. "
                    f"Tick 'Rip again' to rip it into a new folder.")
        return f"Will rip {len(self.keep)} of {len(self.rows)} titles into {self.folder}."

    def can_rip(self, again: bool = False) -> bool:
        return bool(self.keep) and (self.already is None or again)


def plan_rip(titles: list, label: str, show: Show, root: Path) -> RipPlan:
    """What the CLI rip command would do with this scan: which titles, which folder, and
    whether the disc is already ripped (by its fingerprint of title lengths)."""
    root = Path(root)
    keep = workflow.episode_titles(titles)
    keep_ids = {id(t) for t in keep}
    rows = []
    for t in titles:
        if id(t) in keep_ids:
            rows.append(ScanRow(t.index, t.duration_secs, True))
        elif t.duration_secs < workflow.MIN_RIP_SECONDS:
            rows.append(ScanRow(t.index, t.duration_secs, False, "under 10 minutes: an extra or menu"))
        else:
            rows.append(ScanRow(t.index, t.duration_secs, False, "looks like a play-all of the episodes"))
    fingerprint = [t.duration_secs for t in titles]
    if not label:
        label = workflow.fallback_label(show.name, len(disc_folders_for_show(show.name, show.tmdb_id, root)))
    already = workflow.already_ripped(root, label, show.name, show.tmdb_id, fingerprint) if keep else None
    return RipPlan(show, rows, keep, label, root, workflow.unique_disc_folder(root, label), fingerprint, already)


def encode_folder(folder: Path, encoder, progress: Progress = _quiet) -> int:
    """Encode the MKVs that have no MP4 yet, one line of progress per title. Returns how many."""
    todo = workflow.missing_encodes(folder)
    for n, mkv in enumerate(todo, start=1):
        progress(f"Encoding {n} of {len(todo)} ({mkv.name})")
        out = mp4_path(mkv)
        encoder.encode(mkv, out)
        log_action(f"encoded {mkv} -> {out}")
    return len(todo)


def transcribe_progress(progress: Progress) -> Progress:
    """Turn transcribe.py's 'transcribing DISC/A1 T00 (22.0 min)' into a numbered line."""
    count = [0]

    def say(msg: str) -> None:
        count[0] += 1
        what = msg[len("transcribing "):] if msg.startswith("transcribing ") else msg
        progress(f"Transcribing title {count[0]}: {what}")
    return say


EJECTED = "Disc ejected, you can put in the next one; encoding continues."


def run_rip(plan: RipPlan, scanner, encoder, transcriber, show: Show, season: Optional[int],
            again: bool = False, progress: Progress = _quiet,
            eject: Optional[Callable[[], bool]] = None) -> Path:
    """The CLI rip command's steps after the scan: rip each title, encode, transcribe.
    The disc is ejected (eject()) as soon as the last title has ripped. Returns the disc
    folder. Raises UserError when the rip fails part way, Cancelled when it was stopped."""
    if plan.show.tmdb_id != show.tmdb_id:
        raise UserError(f"The disc was scanned for {show_choice(plan.show)}, but the show picked now is "
                        f"{show_choice(show)}. Scan the disc again.")
    if not plan.can_rip(again):
        raise UserError(plan.summary)
    folder = workflow.unique_disc_folder(plan.root, plan.label)
    folder.mkdir(parents=True)
    write_disc_info(folder, show.name, show.tmdb_id, plan.label, complete=False, season=season,
                    titles=plan.fingerprint)
    indices = [t.index for t in plan.keep]
    log_action(f"rip {show.name} disc {plan.label} -> {folder}, titles {indices}")
    progress(f"Ripping {len(indices)} titles into {folder}")
    result = workflow.rip_titles(scanner, indices, folder, progress=progress)
    if not result.complete:
        kept = "".join(f"\nPartial file kept as {p}" for p in result.failed_files)
        if result.error == "interrupted":
            raise Cancelled(f"The rip was stopped at title {result.failed}. Finished titles: "
                            f"{result.finished or 'none'}.{kept}\n{folder} is marked incomplete, so Match "
                            f"leaves it out. Scan again and rip with 'Rip again' ticked.")
        raise UserError(f"The rip stopped at title {result.failed}: {result.error}\n"
                        f"Finished titles: {result.finished or 'none'}.{kept}\n"
                        f"{folder} is marked incomplete, so Match leaves it out. "
                        f"Scan again and rip with 'Rip again' ticked.")
    if eject is not None:
        try:
            ejected = eject()
        except Exception:
            ejected = False
        progress(EJECTED if ejected else "Ripping done (the drive would not eject); encoding continues.")
    n = encode_folder(folder, encoder, progress)
    progress(f"Encoded {n} titles." if n else "Nothing to encode.")
    titles = transcriber(folder, progress=transcribe_progress(progress))
    progress(f"{len(titles)} titles transcribed or already cached.")
    return folder


# ---- the review table ----------------------------------------------------------------------

def episode_label(ep: Optional[Episode]) -> str:
    """'S03E01 The Bumpy Road to Love', or 'none'."""
    return f"{episode_code(ep)} {ep.title}" if ep is not None else NO_EPISODE


def episode_choices(episodes: list[Episode]) -> list[str]:
    return [NO_EPISODE] + [episode_label(e) for e in episodes]


def episode_from_label(label: str, episodes: list[Episode]) -> Optional[Episode]:
    if label == NO_EPISODE:
        return None
    for e in episodes:
        if episode_label(e) == label:
            return e
    raise UserError(f"No episode {label!r}")


def build_rows(matches) -> list[ReviewRow]:
    """The table's rows from match_transcripts, in disc order. CHECK rows start unticked."""
    rows = rows_from_matches(matches)
    for r in rows:
        if r.match.flag == FLAG_CHECK and r.episode is not None:
            r.skipped = True
    return rows


def display_flag(row: ReviewRow) -> str:
    """CHECK, EXTRA, FIXED or empty. An unticked row keeps its flag (the tick shows it)."""
    return FLAG_FIXED if row.corrected else row.match.flag


def row_note(row: ReviewRow) -> str:
    """Why a row is flagged, in plain words."""
    m = row.match
    if row.corrected:
        return "set by hand"
    if m.flag == FLAG_EXTRA:
        return "under 10 minutes, probably an extra"
    if m.flag == FLAG_CHECK:
        if m.note == "no episode left":
            return "more titles than episodes: no episode left for this one"
        if m.note:
            return m.note
        return f"close call: {m.margin:.3f} ahead of the next best (sure is over {CONFIDENT_MARGIN})"
    return ""


@dataclass
class RowView:
    include: bool
    disc: str
    title: str
    minutes: str
    episode: str
    score: str
    margin: str
    flag: str
    note: str
    highlight: str   # "check", "fixed", "extra", "duplicate" or ""


def _row_name(row: ReviewRow) -> str:
    return f"{Path(row.match.title.disc).name}/{row.match.title.stem}"


def row_views(rows: list[ReviewRow]) -> list[RowView]:
    dupes = duplicate_groups(rows)
    dupe_of = {}
    for numbers in dupes.values():
        for n in numbers:
            dupe_of[n] = [k for k in numbers if k != n]
    out = []
    for i, r in enumerate(rows, start=1):
        m = r.match
        has_scores = r.episode is not None and not r.corrected and m.episode is not None
        flag = display_flag(r)
        note = row_note(r)
        highlight = {FLAG_CHECK: "check", FLAG_FIXED: "fixed", FLAG_EXTRA: "extra"}.get(flag, "")
        if i in dupe_of:
            note = "same episode as row " + " and ".join(map(str, dupe_of[i]))
            highlight = "duplicate"
        out.append(RowView(
            include=r.will_act,
            disc=Path(m.title.disc).name,
            title=m.title.stem,
            minutes=f"{m.title.seconds / 60:.1f}" if m.title.seconds else "?",
            episode=episode_label(r.episode),
            score=f"{m.score:.3f}" if has_scores else "",
            margin=f"{m.margin:.3f}" if has_scores else "",
            flag=flag,
            note=note,
            highlight=highlight,
        ))
    return out


def set_episode(rows: list[ReviewRow], n: int, label: str, episodes: list[Episode]) -> None:
    """Row n (1-based) gets the episode picked in its dropdown: marked FIXED and ticked
    ('none' leaves it unticked, since there is nothing to copy)."""
    r = rows[n - 1]
    ep = episode_from_label(label, episodes)
    r.episode, r.corrected = ep, True
    r.skipped = ep is None


def set_include(rows: list[ReviewRow], n: int, include: bool) -> Optional[str]:
    """Tick or untick row n. Returns a message when it cannot be ticked."""
    r = rows[n - 1]
    if include and r.episode is None:
        return f"Row {n} has no episode. Pick one from its episode list first."
    r.skipped = not include
    return None


def duplicate_groups(rows: list[ReviewRow]) -> dict[tuple[int, int], list[int]]:
    """Episode -> the 1-based rows that are included and share it, for episodes on 2+ rows."""
    seen: dict[tuple[int, int], list[int]] = {}
    for i, r in enumerate(rows, start=1):
        if r.will_act:
            seen.setdefault((r.episode.season, r.episode.number), []).append(i)
    return {k: v for k, v in seen.items() if len(v) > 1}


def duplicate_problem(rows: list[ReviewRow], verb: str = "copying") -> str:
    """'' when no two included rows share an episode, else a message naming them."""
    groups = duplicate_groups(rows)
    if not groups:
        return ""
    parts = []
    for (s, e), numbers in sorted(groups.items()):
        ep = rows[numbers[0] - 1].episode
        parts.append(f"rows {' and '.join(map(str, numbers))} are both {episode_label(ep)}")
    return ("Two included rows share an episode: " + "; ".join(parts)
            + f". Untick one or pick another episode before {verb}.")


def included_count(rows: list[ReviewRow]) -> int:
    return sum(1 for r in rows if r.will_act)


def unused_episodes(rows: list[ReviewRow], episodes: list[Episode]) -> list[Episode]:
    """Episodes no included row has, in TMDB order."""
    used = {(r.episode.season, r.episode.number) for r in rows if r.will_act}
    return [e for e in episodes if (e.season, e.number) not in used]


def unused_text(rows: list[ReviewRow], episodes: list[Episode]) -> str:
    left = unused_episodes(rows, episodes)
    if not left:
        return "Every episode has a title."
    return "Episodes with no title: " + ", ".join(episode_label(e) for e in left)


# ---- match and copy ------------------------------------------------------------------------

@dataclass
class MatchResult:
    show: Show
    rows: list[ReviewRow]
    all_episodes: list[Episode]     # for the dropdowns
    episodes: list[Episode]         # the ones matched against (the season, if one was set)
    folders: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # warnings worth showing in the log
    show_dir: str = ""              # fix only: the show's folder on the NAS
    season_folders: dict = field(default_factory=dict)
    strays: list[Path] = field(default_factory=list)


def surplus_note(titles, episodes) -> list[str]:
    if workflow.too_many_titles(titles, episodes):
        return [f"{len(titles)} titles for {len(episodes)} episodes: more titles than episodes. "
                f"Check the season and the discs."]
    return []


def encode_missing(folders: list[Path], encoder, progress: Progress = _quiet) -> int:
    """Encode every title that has an MKV but no MP4 yet, so what is matched and copied is
    the MP4. Raises UserError naming the disc when an encode fails (a stop is passed on)."""
    total = 0
    for f in folders:
        todo = workflow.missing_encodes(f)
        if not todo:
            continue
        if encoder is None:
            raise UserError(f"{f.name} has {len(todo)} titles with no MP4 and no encoder to make them.")
        progress(f"{f.name}: {len(todo)} titles have no MP4 yet, encoding them first")
        try:
            total += encode_folder(f, encoder, progress)
        except Exception as exc:
            raise UserError(f"Encoding {f.name} failed: {friendly_error(exc)} Nothing was matched or "
                            f"copied.") from exc
    return total


def mkv_note(items) -> str:
    """'' or a warning that some of the files to copy are MKV rips, not MP4 encodes."""
    mkv = [i for i in items if str(i.source).lower().endswith(".mkv")]
    if not mkv:
        return ""
    return (f"{len(mkv)} of these {'are MKV rips' if len(mkv) > 1 else 'is an MKV rip'}, not MP4 "
            f"encodes, and will be copied as MKV: " + ", ".join(Path(i.source).name for i in mkv[:5])
            + (" ..." if len(mkv) > 5 else ""))


def run_match(show: Show, season: Optional[int], rip_root: Path,
              load_episodes: Callable[[Show], tuple[list[Episode], list[str]]],
              transcriber, matcher, progress: Progress = _quiet, encoder=None) -> MatchResult:
    """The CLI match command up to the review: the show's complete disc folders (season
    filter if set), encode any title still without an MP4, transcribe what is missing,
    match, and the table's rows. load_episodes(show) returns (every episode, the regulars'
    names)."""
    notes = []
    folders = disc_folders_for_show(
        show.name, show.tmdb_id, rip_root,
        on_skip=lambda f, why: notes.append(f"Leaving out {f.name}: {why}."))
    folders, untagged = workflow.discs_for_season(folders, season)
    if untagged:
        notes.append(f"Not tagged with a season, used anyway: {', '.join(f.name for f in untagged)}.")
    for n in notes:
        progress(n)
    if not folders:
        raise UserError(f"No ripped discs of {show.name} under {rip_root}. Rip them on the "
                        f"Rip disc tab first (or register old rips with 'dvd-process add').")
    progress("Discs: " + ", ".join(f.name for f in folders))
    progress("Fetching episodes from TMDB, with Wikipedia summaries...")
    all_episodes, regulars = load_episodes(show)
    episodes = workflow.limit_to_season(all_episodes, season)
    if not episodes:
        raise UserError(f"TMDB has no episodes for {show.name}"
                        + (f" season {season}." if season else "."))
    encode_missing(folders, encoder, progress)
    progress("Checking transcripts...")
    titles = workflow.gather_titles(folders, transcriber, progress=transcribe_progress(progress))
    extra = surplus_note(titles, episodes)
    for n in extra:
        progress(n)
    progress(f"Matching {len(titles)} titles against {len(episodes)} episodes...")
    rows = build_rows(matcher(titles, episodes, regulars))
    return MatchResult(show, rows, all_episodes, episodes, folders, notes + extra)


def match_summary(result: MatchResult) -> str:
    rows = result.rows
    checks = sum(1 for r in rows if r.match.flag == FLAG_CHECK)
    extras = sum(1 for r in rows if r.match.flag == FLAG_EXTRA)
    s = f"{len(rows)} titles, {included_count(rows)} ticked"
    if checks:
        s += f", {checks} to check (highlighted, unticked)"
    if extras:
        s += f", {extras} extras"
    return s + "."


def copy_items(rows: list[ReviewRow], show: Show, root: str):
    """The copies the ticked rows make, named the Jellyfin way under root."""
    return workflow.copy_plan(rows, show.name, show.year, root)


def run_copy(items, progress: Progress = _quiet, should_stop: Optional[Callable[[], bool]] = None):
    """Copy with copy_episode (never overwriting), one numbered line per file. With
    should_stop, a copy stops part way (procs.Cancelled) and its .part file is removed."""
    count = [0]

    def say(_msg: str) -> None:
        count[0] += 1
        item = items[count[0] - 1]
        progress(f"Copying {count[0]} of {len(items)}: {os.path.basename(item.dest)}")
    log_action(f"window: copying {len(items)} episodes")
    return workflow.copy_all(items, progress=say, should_stop=should_stop)


def still_check(rows: list[ReviewRow]) -> int:
    """How many ticked rows are still flagged CHECK (ticked without picking a new episode)."""
    return len(check_rows(rows))


def check_warning(rows: list[ReviewRow]) -> str:
    """'' or a paragraph for a confirm dialog saying how many ticked rows are still CHECK."""
    n = still_check(rows)
    if not n:
        return ""
    if n == 1:
        return "\n\n1 ticked row is still flagged CHECK: it was ticked without picking a new episode."
    return f"\n\n{n} ticked rows are still flagged CHECK: they were ticked without picking a new episode."


def copy_confirm_text(items, root: str, rows: list[ReviewRow]) -> str:
    return (f"Copy {len(items)} episodes to\n{root}?\n\nFor example:\n{items[0].dest}\n\n"
            f"Nothing already there is overwritten, and the originals in C:\\Video stay."
            + (f"\n\n{mkv_note(items)}" if mkv_note(items) else "")
            + check_warning(rows))


def rename_confirm_text(moves, right: int, show_dir: str, rows: list[ReviewRow]) -> str:
    return (f"Rename {len(moves) - right} files in {show_dir}?\n{right} are already right.\n\n"
            f"The plan is saved first, so an interrupted rename can be resumed." + check_warning(rows))


def copy_summary_text(summary) -> str:
    s = (f"{len(summary.copied)} copied, {len(summary.already_there)} already there, "
         f"{len(summary.failed)} failed.")
    for item, why in summary.failed:
        s += f"\nFailed: {os.path.basename(item.source)}: {why}"
    return s


# ---- fix a show on the NAS -----------------------------------------------------------------

def list_nas_shows(root: str) -> list[str]:
    """The show folders under the NAS TV root, sorted."""
    try:
        return sorted((p.name for p in Path(root).iterdir() if p.is_dir()), key=str.casefold)
    except OSError as exc:
        raise UserError(f"Could not read {root}: {exc.strerror or exc}") from exc


def _plain(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def folder_matches_show(folder: str, show_name: str) -> bool:
    """'NewsRadio (1995)' matches NewsRadio; spaces, punctuation and case do not count."""
    return _plain(name_from_folder(folder)) == _plain(show_name)


def name_from_folder(folder: str) -> str:
    """'Arrested Development (2003)' -> 'Arrested Development', for the show search box."""
    import re
    return re.sub(r"\s*\(\d{4}\)\s*$", "", folder).strip()


def run_fix_match(show: Show, nas_folder: str, season: Optional[int], root: str,
                  load_episodes: Callable[[Show], tuple[list[Episode], list[str]]],
                  file_transcriber, matcher, progress: Progress = _quiet) -> MatchResult:
    """The CLI fix command up to the review: transcribe the show's season folders on the
    NAS into the home cache (never onto the NAS), match, and the table's rows."""
    try:
        nas_folder = workflow.clean_nas_folder(nas_folder)
    except ValueError as exc:
        raise UserError(str(exc)) from exc
    show_dir = f"{str(root).rstrip('/')}/{nas_folder}"
    if not Path(show_dir).is_dir():
        raise UserError(f"No folder {show_dir}")
    nas = workflow.scan_nas_show(Path(show_dir), season)
    titles = []
    say = transcribe_progress(progress)
    for folder, videos in nas.seasons:
        progress(f"{folder.name}: {len(videos)} files")
        titles += file_transcriber(videos, workflow.nas_transcript_dir(show.name, folder), str(folder),
                                   progress=say, check_source=True)
    if not titles:
        raise UserError(f"No video files in the Season folders of {nas_folder}.")
    progress("Fetching episodes from TMDB, with Wikipedia summaries...")
    all_episodes, regulars = load_episodes(show)
    episodes = workflow.limit_to_season(all_episodes, season)
    if not episodes:
        raise UserError(f"TMDB has no episodes for {show.name}"
                        + (f" season {season}." if season else "."))
    notes = surplus_note(titles, episodes)
    for n in notes:
        progress(n)
    progress(f"Matching {len(titles)} files against {len(episodes)} episodes...")
    rows = build_rows(matcher(titles, episodes, regulars))
    return MatchResult(show, rows, all_episodes, episodes, notes=notes, show_dir=show_dir,
                       season_folders=nas.season_folders(), strays=list(nas.strays))


def rename_moves(result: MatchResult) -> tuple[list[tuple[str, str]], int]:
    """(moves for the ticked rows, how many of them are already right)."""
    moves = workflow.rename_moves(result.rows, result.show.name, result.show_dir, result.season_folders)
    right = sum(canon_exact(s) == canon_exact(d) for s, d in moves)
    return moves, right


@dataclass
class RenameOutcome:
    renamed: int
    already_right: int
    transcripts_moved: int


def run_rename(result: MatchResult, progress: Progress = _quiet) -> RenameOutcome:
    """Rename the ticked rows with rename_plan_apply (two passes, plan saved first), then move
    the cached transcripts along."""
    moves, right = rename_moves(result)
    if len(moves) == right:
        return RenameOutcome(0, right, 0)
    progress(f"Renaming {len(moves) - right} files ({right} already right)...")
    from dvd_processor.nas import rename_plan_apply
    report = rename_plan_apply(moves)
    moved = workflow.move_nas_transcripts(result.show.name, report.renamed)
    return RenameOutcome(len(report.renamed), len(report.already_right), moved)


def rename_plan_text(path) -> str:
    """'rename-20261017-145128.json: 40 files, stopped in pass2'."""
    from dvd_processor.nas import load_rename_plan
    try:
        moves, phase = load_rename_plan(path)
    except (OSError, ValueError, KeyError):
        return f"{Path(path).name}: could not be read"
    return f"{Path(path).name}: {len(moves)} files, stopped in {phase}"


# ---- errors --------------------------------------------------------------------------------

def friendly_error(exc: BaseException) -> str:
    """A readable one-paragraph message for an error, for the log and a dialog."""
    if isinstance(exc, Cancelled):
        return str(exc) or "Stopped."
    if isinstance(exc, (UserError, RenameError, RootError)):
        return str(exc)
    if isinstance(exc, subprocess.CalledProcessError):
        cmd = exc.cmd[0] if isinstance(exc.cmd, (list, tuple)) and exc.cmd else exc.cmd
        prog = Path(str(cmd)).stem
        return f"{prog} stopped with an error (exit code {exc.returncode})."
    if isinstance(exc, FileNotFoundError):
        return f"Could not find {exc.filename or exc}. Check the program paths in ~/.dvd_processor/config.json."
    try:
        import requests
        if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 401:
            return "TMDB refused the API key. Check tmdb_api_key in ~/.dvd_processor/config.json."
        if isinstance(exc, requests.RequestException):
            return "Could not reach TMDB or Wikipedia. Check the internet connection and try again."
    except ImportError:
        pass
    if isinstance(exc, ImportError):
        return (f"A part of the tool is not installed ({exc.name or exc}). "
                f"Run: python -m pip install -e .[transcribe]")
    if isinstance(exc, PermissionError):
        return f"Windows would not allow access to {exc.filename or 'a file'}."
    if isinstance(exc, OSError):
        return f"{exc.strerror or exc}" + (f": {exc.filename}" if exc.filename else "")
    return f"Something went wrong: {exc}"
