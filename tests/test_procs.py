"""Stopping makemkvcon, HandBrakeCLI and copies part way."""
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dvd_processor import nas, procs
from dvd_processor.handbrake import HandBrakeEncoder
from dvd_processor.procs import Cancelled, ProcessRunner

SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def test_runner_runs_like_subprocess_run():
    r = ProcessRunner()
    out = r.run([sys.executable, "-c", "print('hi')"], stdout=subprocess.PIPE, text=True, check=True)
    assert out.returncode == 0 and out.stdout.strip() == "hi"
    with pytest.raises(subprocess.CalledProcessError):
        r.run([sys.executable, "-c", "raise SystemExit(3)"], check=True)


def test_cancel_kills_the_running_process_at_once():
    r = ProcessRunner()
    threading.Timer(0.5, r.cancel).start()
    start = time.monotonic()
    with pytest.raises(Cancelled):
        r.run(SLEEPER, check=True)
    assert time.monotonic() - start < 10
    assert r.running() == 0
    with pytest.raises(Cancelled):  # and nothing new starts
        r.run(SLEEPER)


def test_cancelled_is_a_keyboard_interrupt_so_ctrl_c_clean_up_applies():
    assert issubclass(Cancelled, KeyboardInterrupt) and not issubclass(Cancelled, Exception)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_processes_go_in_the_kill_on_close_job():
    p = subprocess.Popen(SLEEPER)
    try:
        assert procs.add_to_job(p)
    finally:
        p.kill()
        p.wait()


def test_cancelled_encode_removes_its_encoding_file(tmp_path):
    mkv = tmp_path / "A1_t00.mkv"
    mkv.write_text("rip")
    out = tmp_path / "MP4" / "A1 T00.mp4"

    def runner(cmd, check, **kw):
        Path(cmd[cmd.index("--output") + 1]).write_text("half")
        raise Cancelled()
    with pytest.raises(Cancelled):
        HandBrakeEncoder("hb", runner=runner).encode(mkv, out)
    assert list((tmp_path / "MP4").iterdir()) == []


def test_cancelled_copy_removes_its_part_file(tmp_path, tmp_config_dir, monkeypatch):
    monkeypatch.setattr(nas, "COPY_CHUNK", 4)
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x" * 40)
    dest = (tmp_path / "nas" / "Show" / "Season 1" / "Show S01 E01.mp4").as_posix()
    calls = []

    def stop():
        calls.append(1)
        return len(calls) > 3
    with pytest.raises(Cancelled):
        nas.copy_episode(src, dest, allow_drive=True, should_stop=stop)
    assert not Path(dest).exists() and not Path(dest + ".part").exists()
    # and a copy that is not stopped is whole, with the timestamps kept
    assert nas.copy_episode(src, dest, allow_drive=True, should_stop=lambda: False) == nas.COPIED
    assert Path(dest).read_bytes() == b"x" * 40
    assert int(Path(dest).stat().st_mtime) == int(src.stat().st_mtime)
