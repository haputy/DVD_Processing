import pytest

pytest.importorskip("sklearn")
pytest.importorskip("scipy")

from dvd_processor.episodes import Episode
from dvd_processor.transcript_matcher import (
    FLAG_CHECK, FLAG_EXTRA, TitleTranscript, match_transcripts, reference_text, unused_episodes,
)

EPISODES = [
    Episode(1, 1, "Pilot", wiki_summary="Dave starts the job and must fire the old director."),
    Episode(1, 2, "Volcano", overview="A volcano erupts near the station; lava everywhere."),
    Episode(1, 3, "Chess", wiki_summary="A chess tournament with grandmasters and checkmate."),
    Episode(1, 4, "Bakery", wiki_summary="The bakery downstairs sells croissants and baguettes."),
]


def title(stem, text, seconds=1320):
    return TitleTranscript("C:/Video/DISC_1", stem, seconds, text)


def test_reference_text_repeats_title_and_cuts_guest_stars():
    ep = Episode(1, 1, "Pilot", overview="Short.", wiki_summary="Plot here. Guest stars Famous Actor.")
    assert reference_text(ep) == "Pilot. Pilot. Pilot. Short. Plot here. "


def test_dialogue_beats_disc_order():
    # Disc order is Chess, Pilot, Bakery, Volcano: the dialogue must win over the order prior.
    titles = [
        title("A1 T00", "chess chess grandmasters checkmate tournament board pawn"),
        title("A1 T01", "fire director job fire starts new job director"),
        title("A1 T02", "croissants baguettes bakery bread croissants"),
        title("A1 T03", "volcano lava erupts lava volcano station"),
    ]
    got = match_transcripts(titles, EPISODES)
    assert [m.episode.title for m in got] == ["Chess", "Pilot", "Bakery", "Volcano"]
    assert all(m.flag == "" for m in got)
    assert all(m.score > 0 and m.margin > 0.02 for m in got)


def test_regulars_are_stop_words():
    eps = [Episode(1, 1, "One", wiki_summary="Dave volcano"), Episode(1, 2, "Two", wiki_summary="Lisa chess")]
    titles = [title("A", "lisa lisa lisa lisa volcano"), title("B", "dave dave dave dave chess")]
    # Without regulars the names pull each title to the wrong episode; with them, plot words win.
    assert [m.episode.title for m in match_transcripts(titles, eps)] == ["Two", "One"]
    assert [m.episode.title for m in match_transcripts(titles, eps, regulars=["Dave", "Lisa"])] == ["One", "Two"]


def test_short_titles_are_extras_and_keep_their_place():
    titles = [title("A1 T00", "chess checkmate", seconds=120),
              title("A1 T01", "volcano lava erupts")]
    got = match_transcripts(titles, EPISODES)
    assert got[0].flag == FLAG_EXTRA and got[0].episode is None
    assert got[1].episode.title == "Volcano"


def test_each_episode_used_once_and_surplus_titles_flagged():
    eps = EPISODES[:2]
    titles = [title("a", "volcano lava"), title("b", "volcano lava erupts"), title("c", "fire director")]
    got = match_transcripts(titles, eps)
    picked = [m.episode.title for m in got if m.episode]
    assert sorted(picked) == ["Pilot", "Volcano"]
    assert sum(m.episode is None and m.flag == FLAG_CHECK for m in got) == 1


def test_title_torn_between_two_episodes_is_flagged():
    titles = [title("a", "volcano lava erupts"), title("b", "checkmate croissants")]
    got = match_transcripts(titles, EPISODES)
    assert got[0].flag == ""
    assert got[1].flag == FLAG_CHECK and got[1].margin < 0.02


def test_unused_episodes():
    got = match_transcripts([title("a", "volcano lava erupts")], EPISODES)
    assert [e.title for e in unused_episodes(got, EPISODES)] == ["Pilot", "Chess", "Bakery"]


def test_empty_inputs():
    assert match_transcripts([], EPISODES) == []
    got = match_transcripts([title("a", "x")], [])
    assert got[0].episode is None and got[0].flag == FLAG_CHECK
