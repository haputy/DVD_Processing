from dvd_processor.confirm import parse_corrections, apply_corrections
from dvd_processor.disc import DiscTitle
from dvd_processor.tmdb import TmdbEpisode
from dvd_processor.matcher import MatchResult


def make_match(title_idx, ep_num) -> MatchResult:
    return MatchResult(
        title=DiscTitle(index=title_idx, duration_secs=1320, output_filename=f"Title_t0{title_idx}.mkv"),
        episode=TmdbEpisode(number=ep_num, title=f"Episode {ep_num}", runtime_secs=1320),
        difference_secs=0,
        low_confidence=False,
        no_runtime_data=False,
    )


def test_parse_ok_returns_empty_corrections():
    assert parse_corrections("ok") == {}


def test_parse_single_correction():
    assert parse_corrections("1=3") == {1: 3}


def test_parse_multiple_corrections():
    result = parse_corrections("1=3 2=1")
    assert result == {1: 3, 2: 1}


def test_apply_corrections_reassigns_episodes():
    matches = [make_match(0, 1), make_match(1, 2), make_match(2, 3)]
    episodes = [m.episode for m in matches]
    corrected = apply_corrections(matches, episodes, {1: 3, 3: 1})
    assert corrected[0].episode.number == 3
    assert corrected[2].episode.number == 1
