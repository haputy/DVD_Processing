from pathlib import Path
from unittest.mock import patch

import pytest

from dvd_processor import handbrake
from dvd_processor.handbrake import HandBrakeEncoder, default_handbrake_path, mp4_name, mp4_path, resolve_handbrake_path


def test_mp4_name_matches_real_gui_outputs():
    # Pairs from C:/Video (NewsRadio discs) and the Arrested Development rips
    assert mp4_name("A1_t00") == "A1 T00"
    assert mp4_name("B1_t10") == "B1 T10"
    assert mp4_name("D1_t11") == "D1 T11"


def test_mp4_name_matches_handbrake_title_case_edge_cases():
    # Outputs of HandBrake 1.9.2's own StringExtensions.ToTitleCase, called directly
    cases = {
        "MICHAEL CLAYTON-A1_t00": "Michael Clayton-A1 T00",
        "a1b c2d": "A1b C2d",
        "o'brien x": "O'brien X",
        "title_t00": "Title T00",
        "B3c_t02": "B3c T02",
        "x.y z": "X.Y Z",
        "3rd rock": "3Rd Rock",
        "ab12cd": "Ab12cd",
        "NEWSRADIO S3D1": "Newsradio S3d1",
    }
    for stem, want in cases.items():
        assert mp4_name(stem) == want, stem


def test_mp4_path_goes_in_mp4_subfolder(tmp_path):
    assert mp4_path(tmp_path / "A1_t03.mkv") == tmp_path / "MP4" / "A1 T03.mp4"


@pytest.fixture
def no_real_installs(tmp_path, monkeypatch):
    """Point home and LOCALAPPDATA at empty temp folders, so Andy's real install is not found."""
    home, local = tmp_path / "home", tmp_path / "local"
    home.mkdir(); local.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return home, local


def put_exe(base):
    exe = base / "HandBrakeCLI" / "1.9.2" / "HandBrakeCLI.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    return exe


def test_default_path_finds_localappdata_install(no_real_installs):
    _, local = no_real_installs
    exe = put_exe(local)
    assert default_handbrake_path() == str(exe)


def test_default_path_prefers_tools_over_localappdata(no_real_installs):
    # Store Python cannot see AppData\Local installs made outside it, so ~/Tools wins.
    home, local = no_real_installs
    put_exe(local)
    exe = put_exe(home / "Tools")
    assert default_handbrake_path() == str(exe)


def test_default_path_falls_back_to_path(no_real_installs):
    assert default_handbrake_path() == "HandBrakeCLI"


def test_config_key_wins(no_real_installs):
    assert resolve_handbrake_path({"handbrake_path": "D:/hb.exe"}) == "D:/hb.exe"
    assert resolve_handbrake_path({}) == "HandBrakeCLI"


def _fake_run(calls):
    def run(cmd, check):
        calls.append(cmd)
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text("encoded")
    return run


def test_encode_all_uses_preset_and_names(tmp_path):
    (tmp_path / "A1_t00.mkv").write_text("rip")
    (tmp_path / "A1_t01.mkv").write_text("rip")
    calls = []
    with patch.object(handbrake.subprocess, "run", side_effect=_fake_run(calls)):
        results = HandBrakeEncoder("hb").encode_all(tmp_path)
    assert [r.output.name for r in results] == ["A1 T00.mp4", "A1 T01.mp4"]
    assert all(not r.skipped for r in results)
    assert calls[0][0] == "hb"
    assert calls[0][calls[0].index("--preset") + 1] == "Fast 1080p30"
    assert (tmp_path / "MP4" / "A1 T00.mp4").read_text() == "encoded"
    # MKV rips stay, no temporary files left
    assert (tmp_path / "A1_t00.mkv").read_text() == "rip"
    assert sorted(p.name for p in (tmp_path / "MP4").iterdir()) == ["A1 T00.mp4", "A1 T01.mp4"]


def test_encode_all_skips_existing_outputs(tmp_path):
    (tmp_path / "A1_t00.mkv").write_text("rip")
    (tmp_path / "MP4").mkdir()
    (tmp_path / "MP4" / "A1 T00.mp4").write_text("old")
    calls = []
    with patch.object(handbrake.subprocess, "run", side_effect=_fake_run(calls)):
        results = HandBrakeEncoder("hb").encode_all(tmp_path)
    assert calls == []
    assert results[0].skipped
    assert (tmp_path / "MP4" / "A1 T00.mp4").read_text() == "old"


def test_failed_encode_leaves_no_output(tmp_path):
    (tmp_path / "A1_t00.mkv").write_text("rip")

    def fail(cmd, check):
        Path(cmd[cmd.index("--output") + 1]).write_text("partial")
        raise RuntimeError("HandBrake crashed")

    with patch.object(handbrake.subprocess, "run", side_effect=fail):
        with pytest.raises(RuntimeError):
            HandBrakeEncoder("hb").encode_all(tmp_path)
    assert list((tmp_path / "MP4").iterdir()) == []


def test_failed_cleanup_does_not_hide_the_real_error(tmp_path, monkeypatch):
    (tmp_path / "A1_t00.mkv").write_text("rip")

    def fail(cmd, check):
        Path(cmd[cmd.index("--output") + 1]).write_text("partial")
        raise KeyboardInterrupt

    def locked(self, *a, **k):
        raise PermissionError("file in use")

    monkeypatch.setattr(Path, "unlink", locked)
    with patch.object(handbrake.subprocess, "run", side_effect=fail):
        with pytest.raises(KeyboardInterrupt):
            HandBrakeEncoder("hb").encode_all(tmp_path)
