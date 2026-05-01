from pathlib import Path
from dvd_processor.renamer import build_output_filename, rename_ripped_files
from dvd_processor.disc import DiscTitle
from dvd_processor.tmdb import TmdbEpisode
from dvd_processor.matcher import MatchResult


def make_match(title_idx: int, ep_number: int, ep_title: str) -> MatchResult:
    return MatchResult(
        title=DiscTitle(index=title_idx, duration_secs=1320, output_filename=f"Title_t0{title_idx}.mkv"),
        episode=TmdbEpisode(number=ep_number, title=ep_title, runtime_secs=1320),
        difference_secs=0,
        low_confidence=False,
        no_runtime_data=False,
    )


def test_basic_filename():
    name = build_output_filename("The Wire", 2, make_match(0, 4, "Hard Cases"))
    assert name == "The Wire - S02E04 - Hard Cases.mkv"


def test_double_digit_episode():
    name = build_output_filename("Seinfeld", 3, make_match(0, 12, "The Junior Mint"))
    assert name == "Seinfeld - S03E12 - The Junior Mint.mkv"


def test_special_chars_sanitized():
    name = build_output_filename("My Show", 1, make_match(0, 1, "The: Beginning / End"))
    assert ":" not in name
    assert "/" not in name


def test_rename_ripped_files(tmp_path):
    (tmp_path / "Title_t00.mkv").write_text("fake")
    (tmp_path / "Title_t01.mkv").write_text("fake")

    matches = [make_match(0, 1, "Pilot"), make_match(1, 2, "Episode Two")]
    rename_ripped_files("My Show", season=1, matches=matches, output_dir=tmp_path)

    assert (tmp_path / "My Show - S01E01 - Pilot.mkv").exists()
    assert (tmp_path / "My Show - S01E02 - Episode Two.mkv").exists()
    assert not (tmp_path / "Title_t00.mkv").exists()


def test_rename_skips_missing_output_filename(tmp_path):
    match = MatchResult(
        title=DiscTitle(index=0, duration_secs=1320, output_filename=""),
        episode=TmdbEpisode(number=1, title="Pilot", runtime_secs=1320),
        difference_secs=0,
        low_confidence=False,
        no_runtime_data=False,
    )
    rename_ripped_files("My Show", season=1, matches=[match], output_dir=tmp_path)
    # No files should be created or renamed, no exception raised
    assert list(tmp_path.iterdir()) == []
