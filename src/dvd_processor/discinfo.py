"""disc.json: which show a ripped disc folder belongs to, so later steps can find it.

Written into each disc folder under the rip root (config rip_root, default C:\\Video) at rip
time. Matching gathers every disc folder of a show and works on them all at once.

"complete" is false while a rip is under way (or after one failed part way), and such a
folder is left out of matching. "season" is optional: the season the disc holds, when known.
"""
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

DISC_JSON = "disc.json"
DEFAULT_RIP_ROOT = r"C:\Video"


@dataclass
class DiscInfo:
    show: str
    tmdb_id: Optional[int]
    label: str
    ripped: str  # ISO date and time
    complete: bool = True
    season: Optional[int] = None
    # Every title's length in seconds, in disc order. Discs of one show often share a volume
    # label (every Northern Exposure disc is NORTHERN_EXPOSURE), so this is what tells a
    # re-rip of the same disc from the next disc in the set.
    titles: Optional[list] = None
    # "tv" or "movie". TMDB numbers movies and TV shows separately, so a movie's id can equal
    # a show's: a movie folder never counts as a disc of a show.
    kind: str = "tv"

    def belongs_to(self, show: Optional[str], tmdb_id: Optional[int]) -> bool:
        """Same show: by TMDB id when both have one, else by name ignoring case."""
        if tmdb_id is not None and self.tmdb_id is not None:
            return self.tmdb_id == tmdb_id
        return show is not None and self.show.casefold() == show.casefold()


def rip_root(config=None) -> Path:
    root = config.get("rip_root") if config is not None else None
    return Path(root or DEFAULT_RIP_ROOT)


def write_disc_info(folder: Path, show: str, tmdb_id: Optional[int], label: Optional[str] = None,
                    ripped: Optional[str] = None, complete: bool = True,
                    season: Optional[int] = None, titles: Optional[list] = None, kind: str = "tv") -> DiscInfo:
    folder = Path(folder)
    info = DiscInfo(
        show=show,
        tmdb_id=tmdb_id,
        label=label or folder.name,
        ripped=ripped or datetime.now().isoformat(timespec="seconds"),
        complete=complete,
        season=season,
        titles=[int(x) for x in titles] if titles is not None else None,
        kind=kind,
    )
    save_disc_info(folder, info)
    return info


def save_disc_info(folder: Path, info: DiscInfo) -> None:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (DISC_JSON + ".tmp")
    tmp.write_text(json.dumps(asdict(info), indent=2), encoding="utf-8")
    tmp.replace(folder / DISC_JSON)


def read_disc_info(folder: Path) -> Optional[DiscInfo]:
    p = Path(folder) / DISC_JSON
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        season = d.get("season")
        return DiscInfo(show=d["show"], tmdb_id=d.get("tmdb_id"), label=d.get("label", Path(folder).name),
                        ripped=d.get("ripped", ""), complete=bool(d.get("complete", True)),
                        season=int(season) if season is not None else None,
                        titles=[int(x) for x in d["titles"]] if d.get("titles") is not None else None,
                        kind=d.get("kind") or "tv")
    except (json.JSONDecodeError, KeyError, OSError, ValueError, TypeError):
        return None


def natural_key(name: str):
    """Sort key that puts DISC_2 before DISC_10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def disc_folders_for_show(show: Optional[str] = None, tmdb_id: Optional[int] = None,
                          root: Optional[Path] = None,
                          on_skip: Optional[Callable[[Path, str], None]] = None) -> list[Path]:
    """Complete disc folders under root whose disc.json names this show, in natural name
    order (DISC_2 before DISC_10), which is the disc order the matcher's prior relies on.

    Matched by TMDB id when given, else by show name ignoring case. Folders of this show
    whose rip did not complete are left out and reported through on_skip(folder, reason).
    """
    root = Path(root) if root else Path(DEFAULT_RIP_ROOT)
    if not root.is_dir():
        return []
    out = []
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        info = read_disc_info(folder)
        if info is None or info.kind != "tv" or not info.belongs_to(show, tmdb_id):
            continue
        if not info.complete:
            if on_skip:
                on_skip(folder, "its rip did not complete")
            continue
        out.append(folder)
    return sorted(out, key=lambda f: natural_key(f.name))
