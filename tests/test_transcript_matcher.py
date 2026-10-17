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


def test_reference_text_repeats_title_and_cuts_guest_stars_from_wikipedia_only():
    ep = Episode(1, 1, "Pilot", overview="Guest star Bob drops by.", wiki_summary="Plot here. Guest stars Famous Actor.")
    assert reference_text(ep) == "Pilot. Pilot. Pilot. Guest star Bob drops by. Plot here. "
    assert reference_text(Episode(1, 1, "Pilot", wiki_summary="Plot.")) == "Pilot. Pilot. Pilot. Plot."


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


def test_no_dialogue_match_is_check_even_when_order_fits():
    # Title 1 shares no words with any episode; disc order alone puts it on Volcano
    titles = [title("a", "fire director job"), title("b", "zebra quokka narwhal")]
    got = match_transcripts(titles, EPISODES[:2])
    assert got[0].flag == ""
    assert got[1].episode.title == "Volcano"
    assert got[1].score < 0.01 and got[1].flag == FLAG_CHECK
    assert "disc order" in got[1].note


def test_empty_transcript_left_out_of_assignment():
    titles = [title("a", "   "), title("b", "volcano lava erupts", seconds=0)]
    got = match_transcripts(titles, EPISODES)
    assert got[0].episode is None and got[0].flag == FLAG_CHECK
    assert got[1].episode.title == "Volcano"


def test_no_terms_remain_gives_check_rows():
    eps = [Episode(1, 1, "The"), Episode(1, 2, "And")]
    got = match_transcripts([title("a", "the and of"), title("b", "it is")], eps)
    assert all(m.episode is None and m.flag == FLAG_CHECK for m in got)
