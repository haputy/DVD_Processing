import io

from rich.console import Console

from dvd_processor.episodes import Episode
from dvd_processor.review import (
    FLAG_FIXED, FLAG_SKIP, apply_review, check_rows, duplicate_episodes, parse_review,
    rows_from_matches, show_review_table,
)
from dvd_processor.transcript_matcher import TitleTranscript, TranscriptMatch

EPS = [Episode(1, 1, "Pilot"), Episode(1, 2, "Two"), Episode(2, 5, "Five")]


def _rows():
    t = lambda s: TitleTranscript("C:/Video/D1", s, 1320, "text", f"C:/Video/D1/MP4/{s}.mp4")
    return rows_from_matches([
        TranscriptMatch(t("A1 T00"), EPS[0], 0.2, 0.1, ""),
        TranscriptMatch(t("A1 T01"), EPS[1], 0.03, 0.01, "CHECK"),
        TranscriptMatch(t("A1 T02"), None, 0.0, 0.0, "EXTRA"),
    ])


def test_parse_ok_corrections_and_skips():
    assert parse_review("ok").ok
    cmd = parse_review("2=S02E05 skip 1 3, unskip 4 1=s1e2")
    assert not cmd.ok
    assert cmd.corrections == {2: (2, 5), 1: (1, 2)}
    assert cmd.skips == {1, 3} and cmd.unskips == {4}
    assert cmd.actions == [("set", 2, (2, 5)), ("skip", 1), ("skip", 3), ("unskip", 4), ("set", 1, (1, 2))]
    assert parse_review("banana").errors


def test_spaces_around_equals_and_inside_the_code():
    for text in ["2 = S02E05", "2= S02E05", "2 =S02E05", "2 = S02 E05", "2=s2 e5"]:
        assert parse_review(text).corrections == {2: (2, 5)}, text


def test_apply_corrections_and_skips():
    rows = _rows()
    msgs = apply_review(rows, EPS, parse_review("2=S02E05 skip 1"))
    assert msgs == []
    assert rows[1].episode.title == "Five" and rows[1].flag == FLAG_FIXED
    assert rows[0].flag == FLAG_SKIP and not rows[0].will_act
    assert apply_review(rows, EPS, parse_review("9=S01E01 1=S09E09"))  # bad row, bad episode


def test_applied_in_the_order_typed():
    rows = _rows()
    apply_review(rows, EPS, parse_review("skip 1 1=S01E01"))
    assert rows[0].will_act  # the later assignment un-skips it
    rows = _rows()
    apply_review(rows, EPS, parse_review("1=S01E01 skip 1"))
    assert not rows[0].will_act


def test_rows_without_an_episode_explain_themselves():
    rows = _rows()
    [msg] = apply_review(rows, EPS, parse_review("skip 3"))
    assert "row 3 has no episode" in msg and "3=SxxEyy" in msg
    [msg] = apply_review(rows, EPS, parse_review("unskip 3"))
    assert "nothing to put back" in msg
    [msg] = apply_review(rows, EPS, parse_review("3=S02E05"))
    assert "extra" in msg and "will be included" in msg
    assert rows[2].will_act


def test_check_rows_lists_acting_check_rows():
    rows = _rows()
    assert check_rows(rows) == [2]
    apply_review(rows, EPS, parse_review("skip 2"))
    assert check_rows(rows) == []
    rows = _rows()
    apply_review(rows, EPS, parse_review("2=S01E02"))  # a hand correction clears CHECK
    assert check_rows(rows) == []


def test_duplicates_detected():
    rows = _rows()
    apply_review(rows, EPS, parse_review("2=S01E01"))
    assert duplicate_episodes(rows) == {(1, 1)}
    apply_review(rows, EPS, parse_review("skip 1"))
    assert duplicate_episodes(rows) == set()


def test_table_renders_all_columns():
    buf = io.StringIO()
    show_review_table(_rows(), Console(file=buf, width=200), "t")
    out = buf.getvalue()
    for word in ["Disc", "Margin", "Note", "S01E01", "Pilot", "CHECK", "EXTRA", "D1", "22.0"]:
        assert word in out
