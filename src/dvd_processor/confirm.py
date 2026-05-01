from rich.table import Table
from rich.console import Console
from dvd_processor.matcher import MatchResult
from dvd_processor.tmdb import TmdbEpisode

console = Console()


def parse_corrections(user_input: str) -> dict[int, int]:
    if user_input.strip().lower() == "ok":
        return {}
    corrections = {}
    for part in user_input.strip().split():
        if "=" in part:
            left, right = part.split("=", 1)
            corrections[int(left)] = int(right)
    return corrections


def apply_corrections(
    matches: list[MatchResult],
    all_episodes: list[TmdbEpisode],
    corrections: dict[int, int],
) -> list[MatchResult]:
    ep_by_number = {ep.number: ep for ep in all_episodes}
    result = []
    for i, match in enumerate(matches):
        title_display_num = i + 1
        if title_display_num in corrections:
            new_ep_num = corrections[title_display_num]
            new_ep = ep_by_number.get(new_ep_num, match.episode)
            result.append(MatchResult(
                title=match.title,
                episode=new_ep,
                difference_secs=match.difference_secs,
                low_confidence=match.low_confidence,
                no_runtime_data=match.no_runtime_data,
            ))
        else:
            result.append(match)
    return result


def show_confirmation_table(matches: list[MatchResult], season: int) -> None:
    table = Table(title="Proposed Episode Mapping", show_lines=True)
    table.add_column("#", style="cyan", width=4)
    table.add_column("Title Duration", width=12)
    table.add_column("Episode", width=8)
    table.add_column("Episode Title", width=30)
    table.add_column("TMDB Runtime", width=12)
    table.add_column("Diff", width=8)
    table.add_column("Notes", width=20)

    for i, m in enumerate(matches, start=1):
        duration = f"{m.title.duration_secs // 3600:02d}:{(m.title.duration_secs % 3600) // 60:02d}:{m.title.duration_secs % 60:02d}"
        ep_id = f"E{m.episode.number:02d}"
        tmdb_rt = f"{m.episode.runtime_secs // 60} min" if m.episode.runtime_secs else "N/A"
        diff = f"{m.difference_secs}s" if not m.no_runtime_data else "N/A"
        notes = []
        if m.low_confidence:
            notes.append("[yellow]Low confidence[/yellow]")
        if m.no_runtime_data:
            notes.append("[yellow]No TMDB runtime[/yellow]")
        table.add_row(str(i), duration, ep_id, m.episode.title, tmdb_rt, diff, " ".join(notes))

    console.print(table)
    console.print("\nType [bold]ok[/bold] to confirm, or [bold]1=3 2=1[/bold] to reassign titles to episodes.")
