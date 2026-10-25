from dataclasses import dataclass, field
import re
from typing import Optional
import requests

BASE = "https://api.themoviedb.org/3"
TIMEOUT = 30  # seconds


@dataclass
class TmdbShow:
    tmdb_id: int
    name: str
    year: str


@dataclass
class TmdbMovie:
    tmdb_id: int
    name: str
    year: str


@dataclass
class TmdbEpisode:
    number: int
    title: str
    runtime_secs: Optional[int]
    overview: str = ""


@dataclass
class TmdbShowDetails:
    tmdb_id: int
    name: str
    year: str
    seasons: list[int] = field(default_factory=list)  # season numbers, specials (0) left out
    number_of_episodes: int = 0


# Credits that are not a character, so never a regular's name.
_NOT_CHARACTERS = {"self", "himself", "herself", "themselves", "narrator", "host", "various"}


class TmdbClient:
    def __init__(self, api_key: str):
        self._key = api_key

    def _get(self, path: str, **params) -> dict:
        r = requests.get(f"{BASE}{path}", params={"api_key": self._key, **params}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def search_show(self, name: str) -> list[TmdbShow]:
        data = self._get("/search/tv", query=name)
        return [
            TmdbShow(
                tmdb_id=r["id"],
                name=r["name"],
                year=r.get("first_air_date", "")[:4],
            )
            for r in data.get("results", [])
        ]

    def search_movie(self, name: str) -> list[TmdbMovie]:
        data = self._get("/search/movie", query=name)
        return [
            TmdbMovie(
                tmdb_id=r["id"],
                name=r.get("title") or r.get("original_title") or "",
                year=(r.get("release_date") or "")[:4],
            )
            for r in data.get("results", [])
        ]

    def get_season_episodes(self, tmdb_id: int, season: int) -> list[TmdbEpisode]:
        data = self._get(f"/tv/{tmdb_id}/season/{season}")
        episodes = []
        for ep in data.get("episodes", []):
            runtime = ep.get("runtime")
            episodes.append(TmdbEpisode(
                number=ep["episode_number"],
                title=ep["name"],
                runtime_secs=runtime * 60 if runtime is not None else None,
                overview=ep.get("overview") or "",
            ))
        return episodes

    def get_show(self, tmdb_id: int) -> TmdbShowDetails:
        data = self._get(f"/tv/{tmdb_id}")
        seasons = sorted(
            s["season_number"] for s in data.get("seasons", [])
            if s.get("season_number", 0) > 0
        )
        return TmdbShowDetails(
            tmdb_id=data.get("id", tmdb_id),
            name=data.get("name", ""),
            year=(data.get("first_air_date") or "")[:4],
            seasons=seasons,
            number_of_episodes=data.get("number_of_episodes") or 0,
        )

    def get_regulars(self, tmdb_id: int, min_share: float = 0.25) -> list[str]:
        """Names (first names and surnames) of the main cast's characters, for the matcher's
        stop list.

        Uses aggregate_credits (every season), keeping cast in at least min_share of the
        show's episodes so guest stars stay in; falls back to /credits (the current main cast).
        """
        names: list[str] = []
        try:
            data = self._get(f"/tv/{tmdb_id}/aggregate_credits")
            cast = data.get("cast", [])
            total = max((c.get("total_episode_count") or 0 for c in cast), default=0)
            for c in cast:
                if total and (c.get("total_episode_count") or 0) < min_share * total:
                    continue
                for role in c.get("roles", []):
                    names += character_names(role.get("character", ""))
        except requests.RequestException:
            data = self._get(f"/tv/{tmdb_id}/credits")
            for c in data.get("cast", []):
                names += character_names(c.get("character", ""))
        seen: dict[str, None] = {}
        for n in names:
            seen.setdefault(n, None)
        return list(seen)


def character_names(character: str) -> list[str]:
    """Every name word of a credited character: 'Dr. Lisa Miller / Narrator (voice)' ->
    ['Lisa', 'Miller']. Titles, nicknames in quotes and parenthesised notes are dropped."""
    out = []
    for part in re.split(r"\s*/\s*|\s*,\s*", character):
        part = re.sub(r"\(.*?\)|\"[^\"]*\"", "", part).strip()
        part = re.sub(r"^(Dr|Mr|Mrs|Ms|Miss)\.?\s+", "", part)
        if not part or part.lower() in _NOT_CHARACTERS:
            continue
        for word in part.split():
            word = word.strip(".'\"")
            if word.lower() in _NOT_CHARACTERS or word.lower() in {"jr", "sr", "ii", "iii"}:
                continue
            if re.match(r"[A-Za-z]", word):
                out.append(word)
    return out
