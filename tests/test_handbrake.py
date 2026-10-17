from pathlib import Path
from unittest.mock import patch

import pytest

from dvd_processor import handbrake
from dvd_processor.handbrake import HandBrakeEncoder, default_handbrake_path, mp4_name, mp4_path, resolve_handbrake_path


def test_mp4_name_matches_handbrake_gui():
    assert mp4_name("A1_t00") == "A1 T00"
    assert mp4_name("title_t12") == "Title T12"


def test_mp4_path_goes_in_mp4_subfolder(tmp_path):
    assert mp4_path(tmp_path / "A1_t03.mkv") == tmp_path / "MP4" / "A1 T03.mp4"


def test_default_path_prefers_localappdata_install(tmp_path, monkeypatch):
    exe = tmp_path / "HandBrakeCLI" / "1.9.2" / "HandBrakeCLI.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_handbrake_path() == str(exe)


def test_default_path_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_handbrake_path() == "HandBrakeCLI"


def test_config_key_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
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
