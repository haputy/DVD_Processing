from dataclasses import dataclass
from typing import Optional
import requests

BASE = "https://api.themoviedb.org/3"


@dataclass
class TmdbShow:
    tmdb_id: int
    name: str
    year: str


@dataclass
class TmdbEpisode:
    number: int
    title: str
    runtime_secs: Optional[int]


class TmdbClient:
    def __init__(self, api_key: str):
        self._key = api_key

    def _get(self, path: str, **params) -> dict:
        r = requests.get(f"{BASE}{path}", params={"api_key": self._key, **params})
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

    def get_season_episodes(self, tmdb_id: int, season: int) -> list[TmdbEpisode]:
        data = self._get(f"/tv/{tmdb_id}/season/{season}")
        episodes = []
        for ep in data.get("episodes", []):
            runtime = ep.get("runtime")
            episodes.append(TmdbEpisode(
                number=ep["episode_number"],
                title=ep["name"],
                runtime_secs=runtime * 60 if runtime is not None else None,
            ))
        return episodes
