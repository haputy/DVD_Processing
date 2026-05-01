import re
from pathlib import Path
from dvd_processor.matcher import MatchResult

UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')


def build_output_filename(show_name: str, season: int, match: MatchResult) -> str:
    ep = match.episode
    safe_title = UNSAFE_CHARS.sub("", ep.title).strip()
    return f"{show_name} - S{season:02d}E{ep.number:02d} - {safe_title}.mkv"


def rename_ripped_files(
    show_name: str,
    season: int,
    matches: list[MatchResult],
    output_dir: Path,
) -> None:
    output_dir = Path(output_dir)
    for match in matches:
        src = output_dir / match.title.output_filename
        if not src.exists():
            continue
        dest = output_dir / build_output_filename(show_name, season, match)
        src.rename(dest)
