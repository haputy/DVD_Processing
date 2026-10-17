import os

import pytest

from dvd_processor import nas
from dvd_processor.nas import (
    COPIED, EXISTS, SIZE_MISMATCH, RenameError, copy_episode, episode_dest, nas_tv_root,
    rename_plan_apply, resume_renaming, safe_name, same_path, show_folder,
)


@pytest.fixture
def log(tmp_config_dir):
    return tmp_config_dir / ".dvd_processor" / "actions.log"


def test_default_root_and_config_override():
    assert nas_tv_root() == "//DXP2800-4CC5/Jellyfin/TVShows"
    assert nas_tv_root({"nas_tv_root": "\\\\NAS\\TV\\"}) == "//NAS/TV"


def test_episode_dest_matches_nas_naming():
    assert episode_dest("NewsRadio", "1995", 1, 1) == \
        "//DXP2800-4CC5/Jellyfin/TVShows/NewsRadio (1995)/Season 1/NewsRadio S01 E01.mp4"
    assert episode_dest("Show", "2003", 3, 12, root="X:/TV") == "X:/TV/Show (2003)/Season 3/Show S03 E12.mp4"


def test_unsafe_characters_removed_from_show_names():
    assert safe_name('Who: "Really"? / Maybe*') == "Who Really Maybe"
    assert show_folder("M*A*S*H", "1972") == "MASH (1972)"
    assert safe_name("Mr. Show.") == "Mr. Show"


def test_same_path_ignores_slash_style_and_case():
    assert same_path("//DXP2800-4CC5/Jellyfin/TVShows/a.mp4", r"\\dxp2800-4cc5\Jellyfin\TVShows\A.mp4")
    assert not same_path("//NAS/a.mp4", "//NAS/b.mp4")


# ---- copy ----------------------------------------------------------------------------------

def test_copy_episode_creates_folders_and_logs(tmp_path, log):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x" * 1000)
    dest = (tmp_path / "TV" / "Show (2000)" / "Season 1" / "Show S01 E01.mp4").as_posix()
    assert copy_episode(src, dest) == COPIED
    assert os.path.getsize(dest) == 1000
    assert not os.path.exists(dest + ".part")
    assert src.exists()
    assert "copied" in log.read_text(encoding="utf-8")


def test_copy_episode_never_overwrites(tmp_path, log):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"new")
    dest = tmp_path / "dest.mp4"
    dest.write_bytes(b"old")
    assert copy_episode(src, dest) == EXISTS
    assert dest.read_bytes() == b"old"


def test_copy_episode_size_mismatch_keeps_nothing(tmp_path, log, monkeypatch):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"abc")
    dest = tmp_path / "out" / "dest.mp4"
    monkeypatch.setattr(nas.shutil, "copy2", lambda a, b: open(b, "wb").write(b"a"))
    assert copy_episode(src, dest) == SIZE_MISMATCH
    assert not dest.exists()
    assert not (tmp_path / "out" / "dest.mp4.part").exists()


def test_copy_episode_missing_source(tmp_path, log):
    with pytest.raises(FileNotFoundError):
        copy_episode(tmp_path / "nope.mp4", tmp_path / "d.mp4")


# ---- rename --------------------------------------------------------------------------------

def _season(tmp_path, names):
    d = tmp_path / "Show (2000)" / "Season 1"
    d.mkdir(parents=True)
    for n in names:
        (d / f"Show S01 E{n:02d}.mp4").write_text(f"content of {n}")
    return d


def _p(d, n):
    return (d / f"Show S01 E{n:02d}.mp4").as_posix()


def test_rename_swap(tmp_path, log):
    d = _season(tmp_path, [4, 5, 6])
    moves = [(_p(d, 5), _p(d, 6)), (_p(d, 6), _p(d, 5)), (_p(d, 4), _p(d, 4).replace("/", "\\"))]
    report = rename_plan_apply(moves)
    assert (d / "Show S01 E06.mp4").read_text() == "content of 5"
    assert (d / "Show S01 E05.mp4").read_text() == "content of 6"
    assert (d / "Show S01 E04.mp4").read_text() == "content of 4"
    assert len(report.renamed) == 2 and len(report.already_right) == 1
    assert not list(d.glob("*.renaming"))
    text = log.read_text(encoding="utf-8")
    assert text.count("temp ") == 2 and text.count("renamed -> ") == 2


def test_rename_refuses_before_moving_anything(tmp_path, log):
    d = _season(tmp_path, [1, 2, 3])
    # E03 would land on E02, which exists and is not moving
    with pytest.raises(RenameError):
        rename_plan_apply([(_p(d, 3), _p(d, 2))])
    with pytest.raises(RenameError):
        rename_plan_apply([(_p(d, 1), _p(d, 7)), (_p(d, 3), _p(d, 7))])
    with pytest.raises(RenameError):
        rename_plan_apply([(_p(d, 9), _p(d, 8))])
    assert sorted(p.name for p in d.iterdir()) == ["Show S01 E01.mp4", "Show S01 E02.mp4", "Show S01 E03.mp4"]


def test_interrupted_rename_resumes(tmp_path, log, monkeypatch):
    d = _season(tmp_path, [1, 2, 3])
    moves = [(_p(d, 1), _p(d, 2)), (_p(d, 2), _p(d, 3)), (_p(d, 3), _p(d, 1))]
    real_rename = os.rename
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 5:  # three temps done, one final rename done, then the network drops
            raise OSError("network name no longer available")
        real_rename(a, b)

    monkeypatch.setattr(nas.os, "rename", flaky)
    with pytest.raises(OSError):
        rename_plan_apply(moves)
    monkeypatch.setattr(nas.os, "rename", real_rename)
    assert len(list(d.glob("*.renaming"))) == 2

    # A fresh apply refuses while temps are about
    with pytest.raises(RenameError):
        rename_plan_apply(moves)

    report = resume_renaming(moves)
    assert len(report.renamed) == 2 and report.left_as_temp == []
    assert (d / "Show S01 E02.mp4").read_text() == "content of 1"
    assert (d / "Show S01 E03.mp4").read_text() == "content of 2"
    assert (d / "Show S01 E01.mp4").read_text() == "content of 3"
    assert not list(d.glob("*.renaming"))

    # A second resume finds nothing left to do
    assert resume_renaming(moves).renamed == []


def test_resume_never_overwrites(tmp_path, log):
    d = _season(tmp_path, [1, 2])
    os.rename(_p(d, 1), _p(d, 1) + ".renaming")
    report = resume_renaming([(_p(d, 1), _p(d, 2))])
    assert report.left_as_temp == [_p(d, 1) + ".renaming"]
    assert (d / "Show S01 E02.mp4").read_text() == "content of 2"
