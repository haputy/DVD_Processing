"""Review a transcript match before anything is copied or renamed.

Shows the match as a table (disc, title, minutes, SxxExx, episode title, score, margin,
flag, note) and takes corrections typed at the prompt:

    ok              accept and go on
    3=S02E05        give row 3 that episode instead ("3 = S02 E05" works too)
    skip 3          leave row 3 out (several: skip 3 5 7)
    unskip 3        put it back

Several commands can go on one line, "3=S02E05 4=S02E06 skip 9", and are applied in the
order typed.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

from rich.console import Console
from rich.table import Table

from dvd_processor.episodes import Episode
from dvd_processor.transcript_matcher import FLAG_CHECK, FLAG_EXTRA, TranscriptMatch

FLAG_FIXED = "FIXED"
FLAG_SKIP = "SKIP"

_CORRECTION = re.compile(r"^(\d+)=s(\d+)\s*e(\d+)$", re.I)


@dataclass
class ReviewRow:
    match: TranscriptMatch
    episode: Optional[Episode]
    skipped: bool = False
    corrected: bool = False

    @property
    def flag(self) -> str:
        if self.skipped:
            return FLAG_SKIP
        if self.corrected:
            return FLAG_FIXED
        return self.match.flag

    @property
    def will_act(self) -> bool:
        """True when this row will be copied or renamed."""
        return self.episode is not None and not self.skipped


def rows_from_matches(matches: list[TranscriptMatch]) -> list[ReviewRow]:
    return [ReviewRow(m, m.episode) for m in matches]


@dataclass
class ReviewCommand:
    ok: bool = False
    # In the order typed: ("set", row, (season, episode)), ("skip", row), ("unskip", row)
    actions: list[tuple] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def corrections(self) -> dict[int, tuple[int, int]]:
        return {a[1]: a[2] for a in self.actions if a[0] == "set"}

    @property
    def skips(self) -> set[int]:
        return {a[1] for a in self.actions if a[0] == "skip"}

    @property
    def unskips(self) -> set[int]:
        return {a[1] for a in self.actions if a[0] == "unskip"}


def parse_review(text: str) -> ReviewCommand:
    """Parse one line typed at the review prompt. Spaces around "=" and between the season
    and episode are fine: "3 = S02 E05"."""
    cmd = ReviewCommand()
    text = re.sub(r"\s*=\s*", "=", text.replace(",", " "))
    text = re.sub(r"(?i)\b(s\d+)\s+(e\d+)\b", r"\1\2", text)
    words = text.split()
    if [w.lower() for w in words] == ["ok"]:
        cmd.ok = True
        return cmd
    mode = None
    for w in words:
        low = w.lower()
        if low in ("skip", "unskip"):
            mode = low
            continue
        m = _CORRECTION.match(w)
        if m:
            cmd.actions.append(("set", int(m[1]), (int(m[2]), int(m[3]))))
            mode = None
            continue
        if mode and w.isdigit():
            cmd.actions.append((mode, int(w)))
            continue
        cmd.errors.append(f"did not understand {w!r}")
    return cmd


def apply_review(rows: list[ReviewRow], episodes: list[Episode], cmd: ReviewCommand) -> list[str]:
    """Apply corrections and skips in place, in the order they were typed. Returns messages
    for the prompt: problems, and explanations where a command could not do what it says."""
    msgs = list(cmd.errors)
    by_number = {(e.season, e.number): e for e in episodes}

    def row(n: int) -> Optional[ReviewRow]:
        if 1 <= n <= len(rows):
            return rows[n - 1]
        msgs.append(f"there is no row {n}")
        return None

    for action in cmd.actions:
        kind, n = action[0], action[1]
        r = row(n)
        if r is None:
            continue
        if kind == "set":
            s, e = action[2]
            ep = by_number.get((s, e))
            if ep is None:
                msgs.append(f"no episode S{s:02d}E{e:02d}")
                continue
            if r.episode is None:
                why = "an extra (too short to be an episode)" if r.match.flag == FLAG_EXTRA else "not matched"
                msgs.append(f"row {n} was {why}; it now gets S{s:02d}E{e:02d} {ep.title} and will be included")
            r.episode, r.corrected, r.skipped = ep, True, False
        elif kind == "skip":
            if r.episode is None:
                msgs.append(f"row {n} has no episode, so it is left out already; "
                            f"to include it, give it one with {n}=SxxEyy")
                continue
            r.skipped = True
        elif kind == "unskip":
            if r.episode is None:
                msgs.append(f"row {n} has no episode, so there is nothing to put back; "
                            f"give it one with {n}=SxxEyy")
                continue
            if not r.skipped:
                msgs.append(f"row {n} was not skipped")
            r.skipped = False
    return msgs


def check_rows(rows: list[ReviewRow]) -> list[int]:
    """1-based numbers of rows that will act while still flagged CHECK."""
    return [i for i, r in enumerate(rows, start=1) if r.will_act and r.flag == FLAG_CHECK]


def duplicate_episodes(rows: list[ReviewRow]) -> set[tuple[int, int]]:
    """Episodes given to more than one row that will be acted on."""
    seen: dict[tuple[int, int], int] = {}
    for r in rows:
        if r.will_act:
            k = (r.episode.season, r.episode.number)
            seen[k] = seen.get(k, 0) + 1
    return {k for k, n in seen.items() if n > 1}


def episode_code(ep: Optional[Episode]) -> str:
    return f"S{ep.season:02d}E{ep.number:02d}" if ep else ""


def show_review_table(rows: list[ReviewRow], console: Console, title: str = "Proposed episodes") -> None:
    dupes = duplicate_episodes(rows)
    table = Table(title=title, show_lines=False)
    for name, kw in [("#", {"style": "cyan", "justify": "right"}), ("Disc", {}), ("Title", {}),
                     ("Min", {"justify": "right"}), ("Episode", {}), ("Episode title", {"max_width": 34}),
                     ("Score", {"justify": "right"}), ("Margin", {"justify": "right"}),
                     ("Flag", {}), ("Note", {"max_width": 40})]:
        table.add_column(name, **kw)
    for i, r in enumerate(rows, start=1):
        m = r.match
        flag = r.flag
        style = {FLAG_CHECK: "yellow", FLAG_EXTRA: "dim", FLAG_FIXED: "cyan", FLAG_SKIP: "dim"}.get(flag, "")
        note = "set by hand" if r.corrected else m.note
        if r.will_act and (r.episode.season, r.episode.number) in dupes:
            note = "[red]same episode as another row[/red]"
        minutes = f"{m.title.seconds / 60:.1f}" if m.title.seconds else "?"
        has_scores = r.episode is not None and not r.corrected and m.episode is not None
        table.add_row(
            str(i),
            _short_disc(m.title.disc),
            m.title.stem,
            minutes,
            episode_code(r.episode),
            r.episode.title if r.episode else "",
            f"{m.score:.3f}" if has_scores else "",
            f"{m.margin:.3f}" if has_scores else "",
            f"[{style}]{flag}[/{style}]" if style and flag else flag,
            note,
        )
    console.print(table)


def _short_disc(disc: str) -> str:
    parts = re.split(r"[\\/]", disc.rstrip("\\/"))
    return parts[-1] if parts else disc
