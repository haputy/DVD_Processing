"""Pins the transcript matcher's assignment on a small synthetic show (made-up word lists,
not real dialogue): tests/fixtures/synthetic_matcher.json.

- T00, T03: plain dialogue matches.
- T01, T02: the dialogue is equally close to Regatta and Squall, so the disc-order prior
  decides: the earlier title gets the earlier episode.
- T04, T05: the disc has Circus and Election the other way round from TMDB; strong dialogue
  overrules the prior.
- T06: a 95 second menu, an extra.
"""
import json
from pathlib import Path

import pytest

pytest.importorskip("sklearn")

from dvd_processor import transcript_matcher as tm
from dvd_processor.episodes import Episode
from dvd_processor.transcript_matcher import TitleTranscript, match_transcripts

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "synthetic_matcher.json").read_text(encoding="utf-8"))


def _episodes():
    return [Episode(**e) for e in FIXTURE["episodes"]]


def _titles(order=None):
    ts = [TitleTranscript("DISC_1", t["stem"], t["seconds"], t["text"]) for t in FIXTURE["titles"]]
    return [ts[i] for i in order] if order else ts


def _assigned(results):
    return {r.title.stem: (r.episode.season, r.episode.number) if r.episode else None for r in results}


def test_pinned_assignment_and_flags():
    got = match_transcripts(_titles(), _episodes(), FIXTURE["regulars"])
    assert [{"stem": r.title.stem,
             "episode": [r.episode.season, r.episode.number] if r.episode else None,
             "flag": r.flag} for r in got] == FIXTURE["expected"]


def test_disc_order_prior_breaks_a_dialogue_tie(monkeypatch):
    eps = _episodes()
    got = match_transcripts(_titles(), eps, FIXTURE["regulars"])
    assert _assigned(got)["A1 T01"] == (1, 2)
    assert _assigned(got)["A1 T02"] == (1, 3)
    # Swap the two tied titles on the disc: each episode follows the disc position, not the text
    swapped = match_transcripts(_titles([0, 2, 1, 3, 4, 5, 6]), eps, FIXTURE["regulars"])
    assert _assigned(swapped)["A1 T02"] == (1, 2)
    assert _assigned(swapped)["A1 T01"] == (1, 3)
    # Without the prior the two are a dead heat
    monkeypatch.setattr(tm, "ORDER_WEIGHT", 0.0)
    flat = match_transcripts(_titles(), eps, FIXTURE["regulars"])
    assert abs(flat[1].margin) < 1e-9 and abs(flat[2].margin) < 1e-9


def test_strong_dialogue_overrules_disc_order():
    got = match_transcripts(_titles(), _episodes(), FIXTURE["regulars"])
    by = {r.title.stem: r for r in got}
    # T04 sits where Election (E05) would be, but talks about the circus
    assert by["A1 T04"].episode.title == "Circus"
    assert by["A1 T05"].episode.title == "Election"
    assert by["A1 T04"].flag == "" and by["A1 T05"].flag == ""
    assert by["A1 T06"].flag == tm.FLAG_EXTRA
