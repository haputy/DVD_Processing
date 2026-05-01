from dataclasses import dataclass
from dvd_processor.disc import DiscTitle
from dvd_processor.tmdb import TmdbEpisode

LOW_CONFIDENCE_THRESHOLD_SECS = 120  # 2 minutes


@dataclass
class MatchResult:
    title: DiscTitle
    episode: TmdbEpisode
    difference_secs: int
    low_confidence: bool
    no_runtime_data: bool


def match_titles_to_episodes(
    titles: list[DiscTitle],
    episodes: list[TmdbEpisode],
    min_duration_secs: int = 600,
    max_duration_secs: int = 5400,
) -> list[MatchResult]:
    candidates = [
        t for t in titles
        if min_duration_secs <= t.duration_secs <= max_duration_secs
    ]

    all_missing_runtime = all(ep.runtime_secs is None for ep in episodes)

    if all_missing_runtime:
        return [
            MatchResult(
                title=title,
                episode=episode,
                difference_secs=0,
                low_confidence=False,
                no_runtime_data=True,
            )
            for title, episode in zip(candidates, episodes)
        ]

    results = []
    remaining_episodes = list(episodes)

    # Episodes with None runtime are treated as 0 seconds in the distance calculation,
    # making them unlikely to match unless no better candidate exists.
    # The no_runtime_data flag is set per-result when this occurs.
    for title in candidates:
        if not remaining_episodes:
            break
        best = min(
            remaining_episodes,
            key=lambda ep: abs((ep.runtime_secs or 0) - title.duration_secs),
        )
        diff = abs((best.runtime_secs or 0) - title.duration_secs)
        results.append(MatchResult(
            title=title,
            episode=best,
            difference_secs=diff,
            low_confidence=diff > LOW_CONFIDENCE_THRESHOLD_SECS,
            no_runtime_data=best.runtime_secs is None,
        ))
        remaining_episodes.remove(best)

    return results
