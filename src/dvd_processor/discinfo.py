"""disc.json: which show a ripped disc folder belongs to, so later steps can find it.

Written into each disc folder under the rip root (config rip_root, default C:\\Video) at rip
time. Matching gathers every disc folder of a show and works on them all at once.
"""
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

DISC_JSON = "disc.json"
DEFAULT_RIP_ROOT = r"C:\Video"


@dataclass
class DiscInfo:
    show: str
    tmdb_id: Optional[int]
    label: str
    ripped: str  # ISO date and time


def rip_root(config=None) -> Path:
    root = config.get("rip_root") if config is not None else None
    return Path(root or DEFAULT_RIP_ROOT)


def write_disc_info(folder: Path, show: str, tmdb_id: Optional[int], label: Optional[str] = None,
                    ripped: Optional[str] = None) -> DiscInfo:
    folder = Path(folder)
    info = DiscInfo(
        show=show,
        tmdb_id=tmdb_id,
        label=label or folder.name,
        ripped=ripped or datetime.now().isoformat(timespec="seconds"),
    )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / DISC_JSON).write_text(json.dumps(asdict(info), indent=2), encoding="utf-8")
    return info


def read_disc_info(folder: Path) -> Optional[DiscInfo]:
    p = Path(folder) / DISC_JSON
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return DiscInfo(show=d["show"], tmdb_id=d.get("tmdb_id"), label=d.get("label", Path(folder).name),
                        ripped=d.get("ripped", ""))
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _natural(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def disc_folders_for_show(show: Optional[str] = None, tmdb_id: Optional[int] = None,
                          root: Optional[Path] = None) -> list[Path]:
    """Disc folders under root whose disc.json names this show, in natural name order
    (DISC_2 before DISC_10), which is the disc order the matcher's prior relies on.

    Matched by TMDB id when given, else by show name ignoring case.
    """
    root = Path(root) if root else Path(DEFAULT_RIP_ROOT)
    if not root.is_dir():
        return []
    out = []
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        info = read_disc_info(folder)
        if info is None:
            continue
        if tmdb_id is not None and info.tmdb_id is not None:
            if info.tmdb_id == tmdb_id:
                out.append(folder)
        elif show is not None and info.show.casefold() == show.casefold():
            out.append(folder)
    return sorted(out, key=lambda f: _natural(f.name))
