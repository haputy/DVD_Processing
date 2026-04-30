import pytest
from dvd_processor.matcher import match_titles_to_episodes, MatchResult
from dvd_processor.disc import DiscTitle
from dvd_processor.tmdb import TmdbEpisode


def make_title(index: int, mins: int, secs: int = 0) -> DiscTitle:
    return DiscTitle(index=index, duration_secs=mins * 60 + secs)


def make_episode(number: int, runtime_mins: int) -> TmdbEpisode:
    return TmdbEpisode(number=number, title=f"Episode {number}", runtime_secs=runtime_mins * 60)


def test_exact_duration_match():
    titles = [make_title(0, 22), make_title(1, 23)]
    episodes = [make_episode(1, 22), make_episode(2, 23)]
    results = match_titles_to_episodes(titles, episodes)
    assert results[0].episode.number == 1
    assert results[1].episode.number == 2


def test_closest_match_wins():
    titles = [make_title(0, 21, 43), make_title(1, 22, 5)]
    episodes = [make_episode(1, 22), make_episode(2, 23)]
    results = match_titles_to_episodes(titles, episodes)
    assert results[0].episode.number == 1
    assert results[1].episode.number == 2


def test_short_titles_filtered_out():
    titles = [make_title(0, 5), make_title(1, 22), make_title(2, 22)]
    episodes = [make_episode(1, 22), make_episode(2, 22)]
    results = match_titles_to_episodes(titles, episodes)
    assert len(results) == 2
    assert all(r.title.duration_secs >= 600 for r in results)


def test_long_titles_filtered_out():
    titles = [make_title(0, 22), make_title(1, 95)]
    episodes = [make_episode(1, 22)]
    results = match_titles_to_episodes(titles, episodes)
    assert len(results) == 1
    assert results[0].title.duration_secs < 5400


def test_match_flagged_when_difference_exceeds_two_minutes():
    titles = [make_title(0, 22)]
    episodes = [make_episode(1, 25)]  # 3 min difference
    results = match_titles_to_episodes(titles, episodes)
    assert results[0].low_confidence is True


def test_match_not_flagged_within_two_minutes():
    titles = [make_title(0, 22, 30)]
    episodes = [make_episode(1, 22)]  # 30s difference
    results = match_titles_to_episodes(titles, episodes)
    assert results[0].low_confidence is False


def test_episode_with_no_runtime_matched_sequentially():
    titles = [make_title(0, 22), make_title(1, 23)]
    episodes = [
        TmdbEpisode(number=1, title="Ep 1", runtime_secs=None),
        TmdbEpisode(number=2, title="Ep 2", runtime_secs=None),
    ]
    results = match_titles_to_episodes(titles, episodes)
    assert results[0].episode.number == 1
    assert results[1].episode.number == 2
    assert all(r.no_runtime_data for r in results)
