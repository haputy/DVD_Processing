"""Match transcribed disc titles to episodes by what is said in them.

Each transcript is scored against every episode's reference text (title, TMDB overview and
Wikipedia summary) with TF-IDF, with the show's regular character names dropped so the plot
words carry it. A gentle disc-order prior helps where dialogue is thin. Then one assignment
is chosen for the whole show at once, so each episode is used once and an episode the DVD
files elsewhere still lands where it belongs.

Ported from the episode-matcher prototype (match.py), proven on NewsRadio and Arrested
Development on 17 October 2026. Keep the constants unless re-proven on real discs.
"""
import re
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
from scipy.optimize import linear_sum_assignment
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer

from dvd_processor.episodes import Episode

MIN_SECONDS = 600      # shorter titles are extras, not episodes
ORDER_WEIGHT = 0.05    # disc-order prior, against TF-IDF scores that run about 0.02 to 0.27
ORDER_DECAY = 1.5      # places in the disc sequence over which the prior fades
MAX_DF = 0.5
CONFIDENT_MARGIN = 0.02
MIN_SIMILARITY = 0.01  # below this the dialogue says nothing, so disc order alone decided
TOKEN_PATTERN = r"[a-zA-Z][a-zA-Z']{2,}"

FLAG_CHECK = "CHECK"
FLAG_EXTRA = "EXTRA"


@dataclass
class TitleTranscript:
    disc: str          # disc folder
    stem: str          # title file stem, e.g. "A1 T00"
    seconds: float     # 0 when unknown
    text: str


@dataclass
class TranscriptMatch:
    title: TitleTranscript
    episode: Optional[Episode]
    score: float       # TF-IDF similarity to the chosen episode
    margin: float      # combined score over the best other episode
    flag: str          # "" when confident, CHECK to review, EXTRA when too short to be an episode
    note: str = ""     # why a row is CHECK when it is not just a thin margin


def reference_text(ep: Episode) -> str:
    """Title three times, then the TMDB overview, then the Wikipedia summary.

    Wikipedia's guest-star notes name actors, not characters, so its summary is cut from
    "Guest star". The TMDB overview is kept whole.
    """
    wiki = re.split(r"Guest star", ep.wiki_summary)[0]
    return (ep.title + ". ") * 3 + " ".join(s for s in (ep.overview, wiki) if s)


def is_episode_length(t: TitleTranscript) -> bool:
    return t.seconds == 0 or t.seconds >= MIN_SECONDS


def match_transcripts(titles: list[TitleTranscript], episodes: list[Episode],
                      regulars: Iterable[str] = ()) -> list[TranscriptMatch]:
    """One result per title, in the order given (which should be disc order).

    Titles too short to be episodes come back EXTRA. Titles with no transcript text, and
    every title when no usable words remain at all, come back CHECK with no episode.
    """
    results: list[Optional[TranscriptMatch]] = []
    candidates = []
    for i, t in enumerate(titles):
        if not is_episode_length(t):
            results.append(TranscriptMatch(t, None, 0.0, 0.0, FLAG_EXTRA))
        elif not t.text.strip():
            results.append(TranscriptMatch(t, None, 0.0, 0.0, FLAG_CHECK, "no transcript text"))
        else:
            results.append(None)
            candidates.append(i)

    def unmatched(note: str) -> list[TranscriptMatch]:
        for i in candidates:
            results[i] = TranscriptMatch(titles[i], None, 0.0, 0.0, FLAG_CHECK, note)
        return results  # type: ignore[return-value]

    if not candidates:
        return results  # type: ignore[return-value]
    if not episodes:
        return unmatched("no episodes")

    texts = [titles[i].text for i in candidates]
    edocs = [reference_text(e) for e in episodes]
    stop = list(ENGLISH_STOP_WORDS | {w.lower() for w in regulars})
    vec = TfidfVectorizer(stop_words=stop, sublinear_tf=True, min_df=1, max_df=MAX_DF,
                          token_pattern=TOKEN_PATTERN)
    try:
        vec.fit(texts + edocs)
    except ValueError:  # "no terms remain": every word was a stop word or too common
        return unmatched("no usable words to match on")
    sim = (vec.transform(texts) @ vec.transform(edocs).T).toarray()

    # Discs mostly run in episode order, so a title's place in the disc sequence is a gentle
    # prior: a bonus that fades within a few places. Strong dialogue still overrules it, which
    # is how an episode the DVD moved (NewsRadio's Injury, Bitch Session) lands where it is.
    n_eps, n_titles = len(episodes), len(candidates)
    order = sorted(range(n_eps), key=lambda k: (episodes[k].season, episodes[k].number or 0))
    epos = np.empty(n_eps)
    epos[order] = np.arange(n_eps)
    tpos = np.arange(n_titles) * (n_eps - 1) / max(n_titles - 1, 1)
    score = sim + ORDER_WEIGHT * np.exp(-np.abs(tpos[:, None] - epos[None, :]) / ORDER_DECAY)

    rows, cols = linear_sum_assignment(-score)
    pick = dict(zip(rows, cols))
    for r, i in enumerate(candidates):
        j = pick.get(r)
        if j is None:  # more titles than episodes
            results[i] = TranscriptMatch(titles[i], None, 0.0, 0.0, FLAG_CHECK, "no episode left")
            continue
        others = np.delete(score[r], j)
        best_other = float(np.max(others)) if others.size else 0.0
        rank = int((score[r] > score[r, j]).sum()) + 1
        margin = float(score[r, j]) - best_other
        similarity = float(sim[r, j])
        note = ""
        if similarity < MIN_SIMILARITY:
            note = "dialogue does not match; placed by disc order only"
        elif rank != 1:
            note = "another episode scores higher"
        flag = "" if not note and margin > CONFIDENT_MARGIN else FLAG_CHECK
        results[i] = TranscriptMatch(titles[i], episodes[j], similarity, margin, flag, note)
    return results  # type: ignore[return-value]


def unused_episodes(matches: list[TranscriptMatch], episodes: list[Episode]) -> list[Episode]:
    """Episodes no title was assigned to, in TMDB order."""
    used = {id(m.episode) for m in matches if m.episode is not None}
    return [e for e in episodes if id(e) not in used]
