import pytest

from dvd_processor import workflow
from dvd_processor.discinfo import read_disc_info, write_disc_info
from dvd_processor.episodes import Episode
from dvd_processor.review import rows_from_matches
from dvd_processor.transcript_matcher import TitleTranscript, TranscriptMatch


def test_unique_disc_folder_never_reuses(tmp_path):
    assert workflow.unique_disc_folder(tmp_path, "NEWSRADIO_S3D1") == tmp_path / "NEWSRADIO_S3D1"
    (tmp_path / "NEWSRADIO_S3D1").mkdir()
    (tmp_path / "NEWSRADIO_S3D1_2").mkdir()
    assert workflow.unique_disc_folder(tmp_path, "NEWSRADIO_S3D1") == tmp_path / "NEWSRADIO_S3D1_3"
    assert workflow.unique_disc_folder(tmp_path, 'Bad: "Label"') == tmp_path / "Bad_Label"


def test_fallback_label():
    assert workflow.fallback_label("NewsRadio", 2) == "NEWSRADIO_D3"
    assert workflow.fallback_label("Mr. Show!", 0) == "MR_SHOW_D1"


def test_register_disc(tmp_path, tmp_config_dir):
    d = tmp_path / "D1"
    d.mkdir()
    info, what = workflow.register_disc(d, "NewsRadio", 5)
    assert what == "registered" and read_disc_info(d).tmdb_id == 5
    assert workflow.register_disc(d, "newsradio", 5)[1] == "already registered"
    with pytest.raises(ValueError):
        workflow.register_disc(d, "Other Show", 9)
    with pytest.raises(FileNotFoundError):
        workflow.register_disc(tmp_path / "nope", "NewsRadio", 5)


def test_missing_encodes(tmp_path):
    (tmp_path / "A1_t00.mkv").write_text("")
    (tmp_path / "A1_t01.mkv").write_text("")
    (tmp_path / "MP4").mkdir()
    (tmp_path / "MP4" / "A1 T00.mp4").write_text("")
    assert [p.name for p in workflow.missing_encodes(tmp_path)] == ["A1_t01.mkv"]


def _rows(paths):
    eps = [Episode(3, 1, "President"), Episode(3, 2, "Review")]
    ms = [TranscriptMatch(TitleTranscript("D", f"T{i}", 1320, "x", p), eps[i] if i < 2 else None, 0.1, 0.1,
                          "" if i < 2 else "EXTRA") for i, p in enumerate(paths)]
    return rows_from_matches(ms)


def test_copy_plan_and_copy_all(tmp_path, tmp_config_dir):
    srcs = []
    for n in range(3):
        p = tmp_path / "disc" / "MP4" / f"A1 T0{n}.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * (n + 1))
        srcs.append(str(p))
    rows = _rows(srcs)
    root = (tmp_path / "nas").as_posix()
    items = workflow.copy_plan(rows, "NewsRadio", "1995", root)
    assert [i.dest for i in items] == [
        f"{root}/NewsRadio (1995)/Season 3/NewsRadio S03 E01.mp4",
        f"{root}/NewsRadio (1995)/Season 3/NewsRadio S03 E02.mp4",
    ]
    summary = workflow.copy_all(items)
    assert len(summary.copied) == 2 and not summary.failed
    again = workflow.copy_all(items)
    assert len(again.already_there) == 2
    with pytest.raises(Exception):
        workflow.copy_plan(rows, "NewsRadio", "1995", "/lost/backslash")


def test_copy_all_counts_failures(tmp_path, tmp_config_dir):
    rows = _rows([str(tmp_path / "missing.mp4")])
    items = workflow.copy_plan(rows, "S", "2000", tmp_path.as_posix())
    summary = workflow.copy_all(items)
    assert len(summary.failed) == 1


def test_scan_nas_show_and_rename_moves(tmp_path):
    show = tmp_path / "Show (2003)"
    (show / "Season 1").mkdir(parents=True)
    (show / "Season 2").mkdir()
    for n in (10, 2, 1):
        (show / "Season 1" / f"Show S01 E{n}.mp4").write_text("")
    (show / "Season 1" / "cover.jpg").write_text("")
    (show / "Season 2" / "Show S02 E01.mkv").write_text("")
    (show / "poster.jpg").write_text("")
    nas = workflow.scan_nas_show(show)
    assert [f.name for f in nas.seasons[0][1]] == ["Show S01 E1.mp4", "Show S01 E2.mp4", "Show S01 E10.mp4"]
    assert sorted(p.name for p in nas.strays) == ["cover.jpg", "poster.jpg"]
    rows = _rows([str(nas.seasons[0][1][1]), str(nas.seasons[0][1][0])])
    moves = workflow.rename_moves(rows, "Show", show.as_posix())
    assert moves[0] == (nas.seasons[0][1][1].as_posix(), f"{show.as_posix()}/Season 3/Show S03 E01.mp4")


def test_nas_transcripts_live_in_home(tmp_config_dir):
    p = workflow.nas_transcript_dir("Arrested Development", "//NAS/TV/AD (2003)/Season 1")
    assert p == tmp_config_dir / ".dvd_processor" / "nas-transcripts" / "Arrested Development" / "Season 1"
