"""Review a transcript match before anything is copied or renamed.

Shows the match as a table (disc, title, minutes, SxxExx, episode title, score, margin,
flag, note) and takes corrections typed at the prompt:

    ok              accept and go on
    3=S02E05        give row 3 that episode instead
    skip 3          leave row 3 out (several: skip 3 5 7)
    unskip 3        put it back

Several commands can go on one line: "3=S02E05 4=S02E06 skip 9".
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
    corrections: dict[int, tuple[int, int]] = field(default_factory=dict)
    skips: set[int] = field(default_factory=set)
    unskips: set[int] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


def parse_review(text: str) -> ReviewCommand:
    cmd = ReviewCommand()
    words = text.replace(",", " ").split()
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
            cmd.corrections[int(m[1])] = (int(m[2]), int(m[3]))
            mode = None
            continue
        if mode and w.isdigit():
            (cmd.skips if mode == "skip" else cmd.unskips).add(int(w))
            continue
        cmd.errors.append(f"did not understand {w!r}")
    return cmd


def apply_review(rows: list[ReviewRow], episodes: list[Episode], cmd: ReviewCommand) -> list[str]:
    """Apply corrections and skips in place. Returns any problems, for the prompt."""
    errors = list(cmd.errors)
    by_number = {(e.season, e.number): e for e in episodes}

    def row(n: int) -> Optional[ReviewRow]:
        if 1 <= n <= len(rows):
            return rows[n - 1]
        errors.append(f"there is no row {n}")
        return None

    for n, (s, e) in cmd.corrections.items():
        r = row(n)
        if r is None:
            continue
        ep = by_number.get((s, e))
        if ep is None:
            errors.append(f"no episode S{s:02d}E{e:02d}")
            continue
        r.episode, r.corrected, r.skipped = ep, True, False
    for n in cmd.skips:
        r = row(n)
        if r is not None:
            r.skipped = True
    for n in cmd.unskips:
        r = row(n)
        if r is not None:
            r.skipped = False
    return errors


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
