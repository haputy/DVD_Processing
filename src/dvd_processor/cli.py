"""dvd-process: rip TV discs, match titles to episodes by their dialogue, put them on the NAS.

    dvd-process rip --show "NewsRadio"        one disc: rip, encode, transcribe
    dvd-process add --show "NewsRadio" C:\\Video\\NEWSRADIO_S3D1 ...   register existing rips
    dvd-process match --show "NewsRadio"      match every disc of the show, copy to the NAS
    dvd-process fix --show "Arrested Development" --nas-folder "Arrested Development (2003)"
    dvd-process resume                        finish an interrupted NAS rename
    dvd-process scan                          list the titles on the disc in the drive
    dvd-process gui                           the "DVD to Jellyfin" window

Commands that change files ask first unless given --yes, and log to
~/.dvd_processor/actions.log.
"""
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt

from dvd_processor import app_logic, workflow
from dvd_processor.config import Config
from dvd_processor.disc import DiscScanner, parse_disc_label, resolve_makemkv_path
from dvd_processor.discinfo import disc_folders_for_show, rip_root, write_disc_info
from dvd_processor.episodes import build_episodes, regulars_for_show
from dvd_processor.handbrake import HandBrakeEncoder, resolve_handbrake_path
from dvd_processor.nas import (
    RenameError, RootError, canon_exact, check_root, load_rename_plan, log_action, nas_tv_root,
    rename_plan_apply, resume_renaming, unfinished_rename_plans,
)
from dvd_processor.review import (
    apply_review, check_rows, duplicate_episodes, episode_code, parse_review, rows_from_matches,
    show_review_table,
)
from dvd_processor.tmdb import TmdbClient, TmdbShowDetails

console = Console()


# ---- helpers -------------------------------------------------------------------------------

def ensure_api_key(config: Config) -> str:
    key = config.get("tmdb_api_key")
    if not key:
        key = Prompt.ask("Enter your TMDB API key (free at themoviedb.org)")
        config.set("tmdb_api_key", key)
    return key


def make_client(config: Config) -> TmdbClient:
    return TmdbClient(api_key=ensure_api_key(config))


def resolve_show(client: TmdbClient, name: str, yes: bool, tmdb_id: Optional[int] = None) -> TmdbShowDetails:
    """The show on TMDB. With --yes only an unambiguous result is taken: the first result
    must match the name exactly (ignoring case) and be the only one that does. Otherwise
    a numbered pick. --tmdb-id names the show exactly and skips the search (two shows are
    called exactly "Northern Exposure")."""
    if tmdb_id is not None:
        show = client.get_show(tmdb_id)
        console.print(f"Show: [bold]{show.name}[/bold] ({show.year}), TMDB {show.tmdb_id}, "
                      f"{len(show.seasons)} seasons")
        return show
    results = client.search_show(name)
    if not results:
        console.print(f"[red]No TMDB results for '{name}'.[/red]")
        sys.exit(1)
    pick = results[0]
    if yes:
        exact = [r for r in results if r.name.casefold() == name.strip().casefold()]
        if len(exact) != 1 or exact[0] is not results[0]:
            console.print(f"[red]TMDB search for '{name}' is ambiguous under --yes:[/red]")
            for r in results[:10]:
                console.print(f"  {r.name} ({r.year or '?'}), TMDB {r.tmdb_id}")
            console.print("Run again with --tmdb-id, or without --yes to pick one.")
            sys.exit(1)
    elif len(results) > 1:
        shown = results[:10]
        console.print("\nTMDB found:")
        for i, s in enumerate(shown, start=1):
            console.print(f"  {i}. {s.name} ({s.year or '?'})")
        n = IntPrompt.ask("Which show", default=1)
        pick = shown[max(1, min(n, len(shown))) - 1]
    show = client.get_show(pick.tmdb_id)
    console.print(f"Show: [bold]{show.name}[/bold] ({show.year}), TMDB {show.tmdb_id}, "
                  f"{len(show.seasons)} seasons")
    return show


def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    return Confirm.ask(question, default=False)


def say(msg: str) -> None:
    console.print(msg)


def _transcriber():
    """transcribe_disc, imported only when needed (it pulls in the transcribe extra)."""
    from dvd_processor.transcribe import transcribe_disc
    return transcribe_disc


def _file_transcriber():
    from dvd_processor.transcribe import transcribe_files
    return transcribe_files


def _matcher():
    from dvd_processor.transcript_matcher import match_transcripts
    return match_transcripts


def make_encoder(config: Config) -> HandBrakeEncoder:
    return HandBrakeEncoder(resolve_handbrake_path(config))


def encode_and_transcribe(folder: Path, config: Config) -> None:
    todo = workflow.missing_encodes(folder)
    if todo:
        console.print(f"Encoding {len(todo)} title(s) with HandBrake into {folder / 'MP4'}...")
        for r in make_encoder(config).encode_all(folder):
            if not r.skipped:
                log_action(f"encoded {r.source} -> {r.output}")
    else:
        console.print("All titles already have MP4s, nothing to encode.")
    console.print("Transcribing...")
    titles = _transcriber()(folder, progress=say)
    console.print(f"{len(titles)} title(s) transcribed or already cached in {folder / 'transcripts'}.")


def review_loop(rows, all_episodes, yes: bool, title: str) -> bool:
    """Show the table and take corrections until ok. False if the user stops."""
    show_review_table(rows, console, title)
    if yes:
        if duplicate_episodes(rows):
            console.print("[red]Two rows have the same episode; run again without --yes to fix them.[/red]")
            sys.exit(1)
        flagged = check_rows(rows)
        if flagged:
            console.print("[red]These rows are flagged CHECK, so nothing was done under --yes: "
                          + ", ".join(f"{n} ({rows[n - 1].match.title.stem}, "
                                      f"{episode_code(rows[n - 1].episode)})" for n in flagged)
                          + ".[/red]")
            console.print("Run again without --yes to review them.")
            sys.exit(1)
        return True
    console.print("Type [bold]ok[/bold] to accept, [bold]3=S02E05[/bold] to change a row, "
                  "[bold]skip 3[/bold] to leave one out, or [bold]q[/bold] to stop.")
    while True:
        text = Prompt.ask("Review").strip()
        if text.lower() in ("q", "quit", "exit"):
            return False
        cmd = parse_review(text)
        if cmd.ok:
            dupes = duplicate_episodes(rows)
            if dupes:
                console.print("[red]These episodes are on more than one row: "
                              + ", ".join(f"S{s:02d}E{e:02d}" for s, e in sorted(dupes)) + "[/red]")
                continue
            return True
        for err in apply_review(rows, all_episodes, cmd):
            console.print(f"[yellow]{err}[/yellow]")
        show_review_table(rows, console, title)


def print_unused(rows, episodes) -> None:
    used = {(r.episode.season, r.episode.number) for r in rows if r.will_act}
    unused = [e for e in episodes if (e.season, e.number) not in used]
    if unused:
        console.print("Episodes with no title: " + ", ".join(f"{episode_code(e)} {e.title}" for e in unused))


def refuse_surplus(titles, episodes, yes: bool) -> None:
    if yes and workflow.too_many_titles(titles, episodes):
        console.print(f"[red]{len(titles)} titles for {len(episodes)} episodes: more titles than episodes "
                      f"by over {workflow.MAX_SURPLUS_TITLES}, so nothing was done under --yes. "
                      f"Check --season and the discs, or run without --yes.[/red]")
        sys.exit(1)


def choose_root(dest_root: Optional[str], config: Config) -> str:
    try:
        return check_root(dest_root, allow_drive=True) if dest_root else nas_tv_root(config)
    except RootError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)


# ---- commands ------------------------------------------------------------------------------

@click.group()
def main():
    """Rip TV show DVDs and name the episodes for Jellyfin."""


@main.command()
@click.option("--drive", default=None, help="Optical drive letter (e.g. E:)")
def scan(drive):
    """List the titles on the disc in the drive and their lengths."""
    config = Config()
    drive = drive or config.get("default_drive", "E:")
    makemkv = resolve_makemkv_path(config)
    scanner = DiscScanner(drive=drive, makemkv_path=makemkv)
    console.print(f"\nScanning disc in drive [bold]{drive}[/bold]...")
    raw = subprocess.run(
        [makemkv, "--robot", "info", f"disc:{scanner._drive_index()}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    titles = scanner._parse_titles(raw.stdout)
    if not titles:
        console.print("[yellow]No titles parsed. Raw MakeMKV output:[/yellow]")
        console.print(raw.stdout or raw.stderr or "(no output)")
        sys.exit(1)
    label = parse_disc_label(raw.stdout)
    console.print(f"\nDisc [bold]{label or '(no label)'}[/bold], {len(titles)} titles:\n")
    for t in titles:
        h, m, s = t.duration_secs // 3600, (t.duration_secs % 3600) // 60, t.duration_secs % 60
        console.print(f"  Title {t.index:2d}  {h:02d}:{m:02d}:{s:02d}  {t.output_filename}")


@main.command()
@click.option("--show", "show_name", required=True, help="TV show name, looked up on TMDB")
@click.option("--tmdb-id", type=int, default=None,
              help="The show's TMDB id, when the name matches more than one show")
@click.option("--drive", default=None, help="Optical drive letter (e.g. E:)")
@click.option("--season", type=int, default=None, help="The season this disc holds, if known")
@click.option("--again", is_flag=True, help="Rip a disc whose folder already exists for this show")
@click.option("--yes", "-y", is_flag=True, help="Take the TMDB match and do not ask")
def rip(show_name, tmdb_id, drive, season, again, yes):
    """Rip one disc: every title of 10 minutes or more, then encode and transcribe."""
    config = Config()
    client = make_client(config)
    show = resolve_show(client, show_name, yes, tmdb_id)
    drive = drive or config.get("default_drive", "E:")
    scanner = DiscScanner(drive=drive, makemkv_path=resolve_makemkv_path(config))
    console.print(f"\nScanning disc in drive [bold]{drive}[/bold]...")
    titles, label = scanner.scan_with_label()
    keep = workflow.episode_titles(titles)
    if not keep:
        console.print("[red]No titles of 10 minutes or more on this disc.[/red]")
        sys.exit(1)
    dropped = [t for t in titles if t.duration_secs >= workflow.MIN_RIP_SECONDS and t not in keep]
    for t in dropped:
        console.print(f"Skipping title {t.index} ({t.duration_secs // 60} min): looks like a play-all of the episodes")
    fingerprint = [t.duration_secs for t in titles]
    root = rip_root(config)
    if not label:
        existing = len(disc_folders_for_show(show.name, show.tmdb_id, root))
        label = workflow.fallback_label(show.name, existing)
    before = workflow.already_ripped(root, label, show.name, show.tmdb_id, fingerprint)
    if before is not None and not again:
        console.print(f"[red]{before} already holds this disc of {show.name}, so it looks already ripped. "
                      f"Use --again to rip it into a new folder.[/red]")
        sys.exit(1)
    folder = workflow.unique_disc_folder(root, label)
    console.print(f"{len(keep)} of {len(titles)} titles are 10 minutes or more:")
    for t in keep:
        console.print(f"  Title {t.index:2d}  {t.duration_secs // 60} min")
    console.print(f"Rip into [bold]{folder}[/bold]")
    if not confirm("Rip, encode and transcribe?", yes):
        return
    folder.mkdir(parents=True)
    write_disc_info(folder, show.name, show.tmdb_id, label, complete=False, season=season,
                    titles=fingerprint)
    log_action(f"rip {show.name} disc {label} -> {folder}, titles {[t.index for t in keep]}")
    result = workflow.rip_titles(scanner, [t.index for t in keep], folder)
    if not result.complete:
        console.print(f"[red]Rip stopped at title {result.failed}: {result.error}[/red]")
        console.print(f"Finished titles: {result.finished or 'none'}")
        for p in result.failed_files:
            console.print(f"  partial file kept as {p}")
        console.print(f"{folder} is marked incomplete, so match will leave it out. "
                      f"Rip the disc again with --again.")
        sys.exit(1)
    encode_and_transcribe(folder, config)
    console.print(f"\n[green]Disc done: {folder}[/green]")
    console.print(f"Next: put in the next disc and run [bold]dvd-process rip --show \"{show_name}\"[/bold],"
                  f" or when all discs are done, [bold]dvd-process match --show \"{show_name}\"[/bold].")


@main.command()
@click.option("--show", "show_name", required=True, help="TV show name, looked up on TMDB")
@click.option("--tmdb-id", type=int, default=None,
              help="The show's TMDB id, when the name matches more than one show")
@click.option("--season", type=int, default=None, help="The season these discs hold, if known")
@click.option("--yes", "-y", is_flag=True, help="Take the TMDB match and do not ask")
@click.argument("folders", nargs=-1, required=True, type=click.Path(file_okay=False))
def add(show_name, tmdb_id, season, yes, folders):
    """Register existing rip folders for a show, encode missing MP4s and transcribe."""
    config = Config()
    paths = [Path(f) for f in folders]
    missing = [p for p in paths if not p.is_dir()]
    if missing:
        console.print("[red]Not folders: " + ", ".join(map(str, missing)) + "[/red]")
        sys.exit(1)
    client = make_client(config)
    show = resolve_show(client, show_name, yes, tmdb_id)
    for p in paths:
        console.print(f"  {p}: {len(list(p.glob('*.mkv')))} MKV, "
                      f"{len(workflow.missing_encodes(p))} to encode")
    if not confirm(f"Register these {len(paths)} folder(s) as {show.name}, encode and transcribe?", yes):
        return
    for p in paths:
        try:
            _, what = workflow.register_disc(p, show.name, show.tmdb_id, season)
        except ValueError as exc:
            console.print(f"[red]{exc}; skipped[/red]")
            continue
        console.print(f"\n[bold]{p.name}[/bold]: {what}")
        encode_and_transcribe(p, config)
    console.print(f"\nNext: [bold]dvd-process match --show \"{show_name}\"[/bold]")


@main.command()
@click.option("--show", "show_name", required=True, help="TV show name, looked up on TMDB")
@click.option("--tmdb-id", type=int, default=None,
              help="The show's TMDB id, when the name matches more than one show")
@click.option("--season", type=int, default=None, help="Only consider this season's episodes")
@click.option("--yes", "-y", is_flag=True, help="Accept the match as it stands and copy without asking")
@click.option("--dest-root", default=None, help="Copy here instead of the NAS TV root (for testing)")
def match(show_name, tmdb_id, season, yes, dest_root):
    """Match every disc of a show to its episodes by dialogue, review, copy to the NAS."""
    config = Config()
    root = choose_root(dest_root, config)
    client = make_client(config)
    show = resolve_show(client, show_name, yes, tmdb_id)
    folders = disc_folders_for_show(
        show.name, show.tmdb_id, rip_root(config),
        on_skip=lambda f, why: console.print(f"[yellow]Leaving out {f}: {why}[/yellow]"))
    folders, untagged = workflow.discs_for_season(folders, season)
    if untagged:
        console.print(f"[yellow]Not tagged with a season, used anyway: {', '.join(f.name for f in untagged)} "
                      f"(tag them with add --season).[/yellow]")
    if not folders:
        console.print(f"[red]No disc folders for {show.name} under {rip_root(config)}. "
                      f"Rip with 'dvd-process rip' or register with 'dvd-process add'.[/red]")
        sys.exit(1)
    console.print("Discs: " + ", ".join(f.name for f in folders))
    if any(workflow.missing_encodes(f) for f in folders):
        try:
            app_logic.encode_missing(folders, make_encoder(config), progress=say)
        except app_logic.UserError as exc:
            console.print(f"[red]{exc}[/red]")
            sys.exit(1)
    console.print("Fetching episodes (TMDB, plus Wikipedia summaries)...")
    all_episodes = build_episodes(client, show.tmdb_id, show.name)
    episodes = workflow.limit_to_season(all_episodes, season)
    if not episodes:
        console.print("[red]No episodes to match against.[/red]")
        sys.exit(1)
    regulars = regulars_for_show(client, show.tmdb_id, show.name, config)
    titles = workflow.gather_titles(folders, _transcriber(), progress=say)
    refuse_surplus(titles, episodes, yes)
    rows = rows_from_matches(_matcher()(titles, episodes, regulars))
    title = f"{show.name}: {len(titles)} titles, {len(episodes)} episodes"
    if not review_loop(rows, all_episodes, yes, title):
        console.print("Stopped, nothing copied.")
        return
    print_unused(rows, episodes)
    items = workflow.copy_plan(rows, show.name, show.year, root)
    if not items:
        console.print("Nothing to copy.")
        return
    console.print(f"{len(items)} episode(s) to copy under [bold]{root}[/bold], e.g. {items[0].dest}")
    if app_logic.mkv_note(items):
        console.print(f"[yellow]{app_logic.mkv_note(items)}[/yellow]")
    if not confirm("Copy now?", yes):
        return
    log_action(f"match {show.name}: copying {len(items)} to {root}")
    summary = workflow.copy_all(items, progress=say)
    console.print(f"\n[green]{len(summary.copied)} copied[/green], {len(summary.already_there)} already there, "
                  f"[red]{len(summary.failed)} failed[/red]")
    for item, why in summary.failed:
        console.print(f"  [red]row {item.row}: {item.source}: {why}[/red]")


@main.command()
@click.option("--show", "show_name", required=True, help="TV show name, looked up on TMDB")
@click.option("--tmdb-id", type=int, default=None,
              help="The show's TMDB id, when the name matches more than one show")
@click.option("--nas-folder", required=True, help='The show\'s folder on the NAS, e.g. "Arrested Development (2003)"')
@click.option("--season", type=int, default=None, help="Only consider this season's episodes")
@click.option("--yes", "-y", is_flag=True, help="Accept the match and rename without asking")
@click.option("--dest-root", default=None, help="Use this TV root instead of the NAS (for testing)")
def fix(show_name, tmdb_id, nas_folder, season, yes, dest_root):
    """Rename a show already on the NAS by matching its files' dialogue."""
    config = Config()
    root = choose_root(dest_root, config)
    try:
        nas_folder = workflow.clean_nas_folder(nas_folder)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)
    show_dir = f"{root}/{nas_folder}"
    if not Path(show_dir).is_dir():
        console.print(f"[red]No folder {show_dir}[/red]")
        sys.exit(1)
    client = make_client(config)
    show = resolve_show(client, show_name, yes, tmdb_id)
    nas = workflow.scan_nas_show(Path(show_dir), season)
    transcribe_files = _file_transcriber()
    titles = []
    for folder, videos in nas.seasons:
        titles += transcribe_files(videos, workflow.nas_transcript_dir(show.name, folder), str(folder),
                                   progress=say, check_source=True)
    if not titles:
        console.print("[red]No video files in Season folders.[/red]")
        sys.exit(1)
    all_episodes = build_episodes(client, show.tmdb_id, show.name)
    episodes = workflow.limit_to_season(all_episodes, season)
    regulars = regulars_for_show(client, show.tmdb_id, show.name, config)
    refuse_surplus(titles, episodes, yes)
    rows = rows_from_matches(_matcher()(titles, episodes, regulars))
    if not review_loop(rows, all_episodes, yes, f"{show.name} on the NAS"):
        console.print("Stopped, nothing renamed.")
        return
    print_unused(rows, episodes)
    moves = workflow.rename_moves(rows, show.name, show_dir, nas.season_folders())
    right = sum(canon_exact(s) == canon_exact(d) for s, d in moves)
    stray = list(nas.strays) + [Path(r.match.title.path) for r in rows if not r.will_act]
    if stray:
        console.print("Left alone (not episodes, or not matched):")
        for p in stray:
            console.print(f"  {p}")
    console.print(f"{len(moves) - right} file(s) to rename, {right} already right.")
    if len(moves) == right:
        return
    if not confirm("Rename now?", yes):
        return
    try:
        report = rename_plan_apply(moves)
    except RenameError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)
    moved = workflow.move_nas_transcripts(show.name, report.renamed)
    console.print(f"[green]{len(report.renamed)} renamed[/green], {len(report.already_right)} already right "
                  f"({moved} cached transcripts moved with them).")


@main.command()
def gui():
    """Open the "DVD to Jellyfin" window."""
    from dvd_processor.gui import run
    run()


@main.command()
@click.option("--yes", "-y", is_flag=True, help="Resume without asking when there is only one")
def resume(yes):
    """Finish an interrupted NAS rename."""
    plans = unfinished_rename_plans()
    if not plans:
        console.print("No unfinished renames.")
        return
    for i, p in enumerate(plans, start=1):
        moves, phase = load_rename_plan(p)
        console.print(f"  {i}. {p.name}: {len(moves)} files, stopped in {phase}"
                      + (f", e.g. {moves[0][1]}" if moves else ""))
    plan = plans[0]
    if len(plans) > 1 and yes:
        console.print("[red]More than one unfinished rename; run without --yes to choose.[/red]")
        sys.exit(1)
    if len(plans) > 1:
        n = IntPrompt.ask("Which one", default=1)
        plan = plans[max(1, min(n, len(plans))) - 1]
    if not confirm(f"Resume {plan.name}?", yes):
        return
    report = resume_renaming(plan)
    console.print(f"[green]{len(report.renamed)} moved[/green], {len(report.left_as_temp)} left as .renaming")
    for t in report.left_as_temp:
        console.print(f"  [yellow]{t}[/yellow] (its destination exists)")
