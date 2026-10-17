"""A show's episodes, numbered by TMDB, with Wikipedia summaries attached when there are any.

Jellyfin labels files by TMDB, so numbering always comes from TMDB. Wikipedia's "List of
<Show> episodes" page (or its "<Show> season N" pages) usually has a much fuller summary than
TMDB's overview, which gives the transcript matcher more plot words to work with. Wikipedia
is a bonus: if it fails or has nothing, the TMDB episodes are returned as they are.
"""
import difflib
import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from dvd_processor.tmdb import TmdbClient

log = logging.getLogger(__name__)

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_HEADERS = {"User-Agent": "dvd-processor/0.1 (andy@planetkrouton.com)"}
TITLE_CUTOFF = 0.8  # difflib ratio for "Shrink" and "The Shrink"


@dataclass
class Episode:
    season: int
    number: int
    title: str
    overview: str = ""
    wiki_summary: str = ""


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---- TMDB ----------------------------------------------------------------------------------

def fetch_tmdb_episodes(client: TmdbClient, tmdb_id: int, seasons: Optional[list[int]] = None) -> list[Episode]:
    """Every episode of every season (specials left out), in TMDB's order."""
    if seasons is None:
        seasons = client.get_show(tmdb_id).seasons
    out = []
    for s in seasons:
        for e in client.get_season_episodes(tmdb_id, s):
            out.append(Episode(season=s, number=e.number, title=e.title, overview=e.overview))
    return out


# ---- Wikipedia -----------------------------------------------------------------------------

def fetch_wikitext(page: str) -> str:
    r = requests.get(
        WIKI_API,
        params={"action": "parse", "page": page, "prop": "wikitext", "format": "json",
                "formatversion": "2", "redirects": 1},
        headers=WIKI_HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "parse" not in data:
        raise LookupError(f"no Wikipedia page {page!r}")
    return data["parse"]["wikitext"]


def clean(s: str) -> str:
    s = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    s = re.sub(r"<br\s*/?>", " ", s)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"'{2,}", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _field(block: str, name: str) -> str:
    m = re.search(r"\|\s*" + name + r"\s*=(.*?)(?=\n\s*\|\s*\w+\s*=|\n\}\})", block, re.S)
    return m.group(1).strip() if m else ""


def parse_episode_list(text: str) -> list[dict]:
    """Each {{Episode list}} block as {season, episode, title, summary}.

    Seasons come from the "Season N" section headings above the blocks.
    """
    eps, season = [], 0
    for m in re.finditer(r"^=+\s*(.*?)\s*=+\s*$|\{\{Episode list.*?\n\}\}", text, re.M | re.S):
        if m.group(1) is not None:
            s = re.search(r"Season\s+(\d+)", m.group(1))
            if s:
                season = int(s.group(1))
            continue
        b = m.group(0)
        n = re.search(r"EpisodeNumber2\s*=\s*(\d+)", b)
        eps.append({
            "season": season,
            "episode": int(n.group(1)) if n else None,
            "title": clean(_field(b, "Title")),
            "summary": clean(_field(b, "ShortSummary")),
        })
    return eps


def wikipedia_episodes(show_name: str, seasons: list[int],
                       fetch: Callable[[str], str] = fetch_wikitext) -> list[dict]:
    """The show's list page, plus "<Show> season N" pages for any season the list page does
    not cover (some list pages only link out to season pages). Raises if Wikipedia fails."""
    try:
        eps = parse_episode_list(fetch(f"List of {show_name} episodes"))
    except LookupError:
        eps = []
    covered = {e["season"] for e in eps if e.get("title")}
    for s in seasons:
        if s in covered:
            continue
        try:
            got = parse_episode_list(fetch(f"{show_name} season {s}"))
        except LookupError:
            continue
        for e in got:
            e["season"] = s
        eps += got
    return eps


def attach_wiki_summaries(episodes: list[Episode], wiki: list[dict]) -> int:
    """Set wiki_summary on each episode whose title Wikipedia also has. Returns how many matched.

    First by normalised title, preferring the same season when a title repeats. Then, for
    the episodes still without one, by difflib at TITLE_CUTOFF against the Wikipedia entries
    no exact match claimed, for small spelling differences ("Shrink" and "The Shrink").
    Numbering stays TMDB's.
    """
    entries = [w for w in wiki if w.get("title")]
    claimed: set[int] = set()
    pending = []
    hits = 0

    def pick(ep: Episode, options: list[int]) -> int:
        same = [k for k in options if entries[k].get("season") == ep.season]
        return (same or options)[0]

    for ep in episodes:
        key = norm(ep.title)
        options = [k for k, w in enumerate(entries) if norm(w["title"]) == key and k not in claimed]
        if not options:
            pending.append(ep)
            continue
        k = pick(ep, options)
        claimed.add(k)
        ep.wiki_summary = entries[k].get("summary", "")
        hits += 1

    for ep in pending:
        free = {}
        for k, w in enumerate(entries):
            if k not in claimed:
                free.setdefault(norm(w["title"]), []).append(k)
        close = difflib.get_close_matches(norm(ep.title), list(free), n=1, cutoff=TITLE_CUTOFF)
        if not close:
            continue
        k = pick(ep, free[close[0]])
        claimed.add(k)
        ep.wiki_summary = entries[k].get("summary", "")
        hits += 1
    return hits


def build_episodes(client: TmdbClient, tmdb_id: int, show_name: Optional[str] = None,
                   use_wikipedia: bool = True,
                   fetch: Callable[[str], str] = fetch_wikitext) -> list[Episode]:
    show = client.get_show(tmdb_id)
    episodes = fetch_tmdb_episodes(client, tmdb_id, show.seasons)
    if use_wikipedia:
        try:
            wiki = wikipedia_episodes(show_name or show.name, show.seasons, fetch=fetch)
            n = attach_wiki_summaries(episodes, wiki)
            log.info("Wikipedia summaries for %d of %d episodes", n, len(episodes))
        except Exception as exc:  # Wikipedia is optional; never let it stop a run
            log.warning("Wikipedia lookup failed, using TMDB overviews only: %s", exc)
    return episodes


def regulars_for_show(client: TmdbClient, tmdb_id: int, show_name: str, config=None) -> list[str]:
    """The show's regular character names from TMDB, plus any listed for it in the config
    key regulars_extra ({"NewsRadio": ["WNYX", "Max"]}, show name matched ignoring case).
    A TMDB failure leaves just the extras."""
    try:
        names = list(client.get_regulars(tmdb_id))
    except Exception as exc:
        log.warning("could not fetch regular cast names: %s", exc)
        names = []
    extra = (config.get("regulars_extra") if config is not None else None) or {}
    for show, words in extra.items():
        if show.casefold() == show_name.casefold():
            names += [w for w in words if w]
    return list(dict.fromkeys(names))
