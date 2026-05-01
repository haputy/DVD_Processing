import sys
import click
from pathlib import Path
from rich.console import Console
from rich.prompt import Prompt, IntPrompt

from dvd_processor.config import Config
from dvd_processor.tmdb import TmdbClient
from dvd_processor.disc import DiscScanner
from dvd_processor.matcher import match_titles_to_episodes
from dvd_processor.confirm import show_confirmation_table, parse_corrections, apply_corrections
from dvd_processor.renamer import rename_ripped_files
from dvd_processor.handbrake import HandBrakeEncoder

console = Console()


def ensure_api_key(config: Config) -> str:
    key = config.get("tmdb_api_key")
    if not key:
        key = Prompt.ask("Enter your TMDB API key (free at themoviedb.org)")
        config.set("tmdb_api_key", key)
    return key


def select_show(client: TmdbClient, show_name: str):
    results = client.search_show(show_name)
    if not results:
        console.print(f"[red]No results found for '{show_name}'.[/red]")
        sys.exit(1)
    display = results[:10]
    console.print("\nFound shows:")
    for i, show in enumerate(display, start=1):
        console.print(f"  {i}. {show.name} ({show.year})")
    choice = IntPrompt.ask("Select show number", default=1)
    choice = max(1, min(choice, len(display)))
    return display[choice - 1]


@click.command()
@click.option("--drive", default=None, help="Optical drive letter (e.g. E:)")
@click.option("--output", default=None, type=click.Path(), help="Output directory")
@click.option("--min-duration", default=10, help="Minimum title duration in minutes")
@click.option("--transcode", is_flag=True, help="Encode output with HandBrake after ripping")
@click.option("--dry-run", is_flag=True, help="Scan and match only, do not rip")
def main(drive, output, min_duration, transcode, dry_run):
    """Rip a TV show DVD and name episodes for Jellyfin."""
    config = Config()

    drive = drive or config.get("default_drive", "E:")
    output_dir = Path(output or config.get("default_output", "."))
    makemkv_path = config.get("makemkv_path", "makemkvcon")
    handbrake_path = config.get("handbrake_path", "HandBrakeCLI")

    api_key = ensure_api_key(config)
    tmdb = TmdbClient(api_key=api_key)

    show_name_input = Prompt.ask("Show name")
    season = IntPrompt.ask("Season number")

    show = select_show(tmdb, show_name_input)
    console.print(f"\nFetching episode data for [bold]{show.name}[/bold] Season {season}...")
    episodes = tmdb.get_season_episodes(show.tmdb_id, season)
    if not episodes:
        console.print("[red]No episodes found for that season.[/red]")
        sys.exit(1)

    console.print(f"\nScanning disc in drive [bold]{drive}[/bold]...")
    scanner = DiscScanner(drive=drive, makemkv_path=makemkv_path)
    titles = scanner.scan()
    if not titles:
        console.print("[red]No titles found on disc. Check the drive and try again.[/red]")
        sys.exit(1)

    console.print(f"Found [bold]{len(titles)}[/bold] titles on disc.")

    matches = match_titles_to_episodes(
        titles, episodes, min_duration_secs=min_duration * 60
    )

    while True:
        show_confirmation_table(matches, season)
        user_input = Prompt.ask("\nConfirm mapping")
        if user_input.strip().lower() == "ok":
            break
        corrections = parse_corrections(user_input)
        if corrections:
            matches = apply_corrections(matches, episodes, corrections)
        elif user_input.strip():
            console.print("[yellow]Type 'ok' to confirm or '1=3' to reassign titles.[/yellow]")

    if dry_run:
        console.print("[yellow]Dry run — skipping rip.[/yellow]")
        return

    console.print(f"\nRipping [bold]{len(matches)}[/bold] titles to [bold]{output_dir}[/bold]...")
    scanner.rip(
        title_indices=[m.title.index for m in matches],
        output_dir=output_dir,
    )

    rename_ripped_files(show.name, season, matches, output_dir)

    if transcode:
        console.print("\nTranscoding with HandBrake...")
        encoder = HandBrakeEncoder(handbrake_path=handbrake_path)
        encoder.encode_all(output_dir)

    console.print(f"\n[green]Done! {len(matches)} episodes saved to {output_dir}[/green]")
