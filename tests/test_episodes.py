from unittest.mock import MagicMock

import requests

from dvd_processor.episodes import (
    Episode, attach_wiki_summaries, build_episodes, clean, parse_episode_list, wikipedia_episodes,
)
from dvd_processor.tmdb import TmdbEpisode, TmdbShowDetails

LIST_PAGE = """
== Series overview ==
Some text.
=== Season 1 (1995) ===
{{Episode list
 | EpisodeNumber = 1
 | EpisodeNumber2 = 1
 | Title = Pilot
 | OriginalAirDate = {{Start date|1995|3|21}}
 | ShortSummary = Dave arrives at [[WNYX|the station]].<ref>cite</ref> Guest stars Someone.
}}
{{Episode list
 | EpisodeNumber = 2
 | EpisodeNumber2 = 2
 | Title = ''Inappropriate''
 | ShortSummary = Lisa and Dave keep a secret.<br/>It does not last.
}}
=== Season 2 (1995-96) ===
{{Episode list
 | EpisodeNumber = 3
 | EpisodeNumber2 = 1
 | Title = The Shrink
 | ShortSummary = A therapist visits.
}}
"""


def test_clean_strips_markup():
    assert clean("A [[Link|word]] ''here''<ref>x</ref> {{tpl}}<br/>end") == "A word here end"


def test_parse_episode_list_reads_seasons_and_summaries():
    eps = parse_episode_list(LIST_PAGE)
    assert [(e["season"], e["episode"], e["title"]) for e in eps] == [
        (1, 1, "Pilot"), (1, 2, "Inappropriate"), (2, 1, "The Shrink")]
    assert eps[0]["summary"] == "Dave arrives at the station. Guest stars Someone."
    assert eps[1]["summary"] == "Lisa and Dave keep a secret. It does not last."


def test_wikipedia_falls_back_to_season_pages():
    calls = []

    def fetch(page):
        calls.append(page)
        if page.startswith("List of"):
            raise LookupError(page)
        return "{{Episode list\n | EpisodeNumber2 = 1\n | Title = Ep " + page[-1] + "\n | ShortSummary = S\n}}"

    eps = wikipedia_episodes("Show", [1, 2], fetch=fetch)
    assert calls == ["List of Show episodes", "Show season 1", "Show season 2"]
    assert [(e["season"], e["title"]) for e in eps] == [(1, "Ep 1"), (2, "Ep 2")]


def test_attach_by_title_with_fuzzy_fallback_and_tmdb_numbers():
    episodes = [Episode(1, 1, "Pilot"), Episode(1, 2, "Inappropriate"), Episode(2, 1, "Shrink"),
                Episode(2, 2, "Nothing Like It")]
    wiki = parse_episode_list(LIST_PAGE)
    assert attach_wiki_summaries(episodes, wiki) == 3
    assert episodes[2].wiki_summary == "A therapist visits."
    assert (episodes[2].season, episodes[2].number) == (2, 1)
    assert episodes[3].wiki_summary == ""


def test_attach_prefers_same_season_for_repeated_titles():
    episodes = [Episode(2, 1, "Christmas")]
    wiki = [{"season": 1, "title": "Christmas", "summary": "one"},
            {"season": 2, "title": "Christmas", "summary": "two"}]
    attach_wiki_summaries(episodes, wiki)
    assert episodes[0].wiki_summary == "two"


def _client():
    client = MagicMock()
    client.get_show.return_value = TmdbShowDetails(tmdb_id=5, name="NewsRadio", year="1995", seasons=[1, 2])
    client.get_season_episodes.side_effect = lambda tid, s: {
        1: [TmdbEpisode(1, "Pilot", 1320, "TMDB pilot"), TmdbEpisode(2, "Inappropriate", 1320, "")],
        2: [TmdbEpisode(1, "Shrink", 1320, "")],
    }[s]
    return client


def test_build_episodes_enriches_from_wikipedia():
    eps = build_episodes(_client(), 5, fetch=lambda page: LIST_PAGE)
    assert [(e.season, e.number, e.title) for e in eps] == [(1, 1, "Pilot"), (1, 2, "Inappropriate"), (2, 1, "Shrink")]
    assert eps[0].overview == "TMDB pilot"
    assert eps[0].wiki_summary.startswith("Dave arrives")


def test_build_episodes_survives_wikipedia_failure():
    def broken(page):
        raise requests.ConnectionError("offline")

    eps = build_episodes(_client(), 5, fetch=broken)
    assert len(eps) == 3
    assert all(e.wiki_summary == "" for e in eps)


def test_wiki_request_follows_redirects(monkeypatch):
    from dvd_processor import episodes
    seen = {}

    def fake_get(url, params, headers, timeout):
        seen.update(params)
        resp = MagicMock()
        resp.json.return_value = {"parse": {"wikitext": ""}}
        return resp

    monkeypatch.setattr(episodes.requests, "get", fake_get)
    episodes.fetch_wikitext("List of X episodes")
    assert seen["redirects"] == 1


def test_season_pages_fill_seasons_the_list_page_lacks():
    calls = []

    def fetch(page):
        calls.append(page)
        if page.startswith("List of"):
            return LIST_PAGE  # seasons 1 and 2 only
        return "{{Episode list\n | EpisodeNumber2 = 1\n | Title = Late One\n | ShortSummary = S\n}}"

    eps = wikipedia_episodes("Show", [1, 2, 3], fetch=fetch)
    assert calls == ["List of Show episodes", "Show season 3"]
    assert [(e["season"], e["title"]) for e in eps][-1] == (3, "Late One")


def test_fuzzy_attach_skips_entries_claimed_exactly():
    # "Led Zeppelin II" must not take "Led Zeppelin"'s summary when Wikipedia spells it differently
    episodes = [Episode(2, 18, "Led Zeppelin"), Episode(2, 21, "Led Zepelin II")]
    wiki = [{"season": 2, "title": "Led Zeppelin", "summary": "first"},
            {"season": 2, "title": "Led Zeppelin II", "summary": "second"}]
    assert attach_wiki_summaries(episodes, wiki) == 2
    assert episodes[0].wiki_summary == "first"
    assert episodes[1].wiki_summary == "second"
    # With only the first in Wikipedia, the near miss gets nothing rather than a stolen summary
    episodes = [Episode(2, 18, "Led Zeppelin"), Episode(2, 21, "Led Zeppelin I")]
    attach_wiki_summaries(episodes, wiki[:1])
    assert episodes[1].wiki_summary == ""


def test_regulars_for_show_adds_config_extras():
    from dvd_processor.episodes import regulars_for_show
    client = MagicMock()
    client.get_regulars.return_value = ["Dave", "Nelson"]
    config = {"regulars_extra": {"newsradio": ["WNYX", "Max", "Dave"], "Other": ["Nope"]}}
    assert regulars_for_show(client, 5, "NewsRadio", config) == ["Dave", "Nelson", "WNYX", "Max"]
    client.get_regulars.side_effect = requests.ConnectionError("offline")
    assert regulars_for_show(client, 5, "NewsRadio", config) == ["WNYX", "Max", "Dave"]
