from types import SimpleNamespace

import pytest

from dvd_processor import transcribe
from dvd_processor.transcribe import get_model, title_files, transcribe_disc, transcript_path


class FakeModel:
    def __init__(self, device="cuda", fail=False):
        self.device = device
        self.fail = fail
        self.calls = []

    def transcribe(self, audio, **kw):
        if self.fail:
            raise RuntimeError("Library cublas64_12.dll is not found")
        self.calls.append((audio, kw))
        return iter([SimpleNamespace(text=" Hello there. "), SimpleNamespace(text="General Kenobi.")]), None


@pytest.fixture(autouse=True)
def fresh_model(monkeypatch):
    transcribe.reset_model()
    monkeypatch.setattr(transcribe, "add_cuda_dll_dirs", lambda: [])
    yield
    transcribe.reset_model()


def test_model_prefers_gpu_and_loads_once():
    loads = []

    def loader(device, compute):
        loads.append((device, compute))
        return FakeModel(device)

    m1 = get_model(loader)
    m2 = get_model(loader)
    assert m1 is m2
    assert loads == [("cuda", "float16")]


def test_model_falls_back_to_cpu_when_cuda_load_fails():
    loads = []

    def loader(device, compute):
        loads.append((device, compute))
        if device == "cuda":
            raise RuntimeError("CUDA driver missing")
        return FakeModel(device)

    assert get_model(loader).device == "cpu"
    assert loads == [("cuda", "float16"), ("cpu", "int8")]


def test_model_falls_back_when_cuda_fails_on_first_use():
    def loader(device, compute):
        return FakeModel(device, fail=(device == "cuda"))

    assert get_model(loader).device == "cpu"


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    return p


def test_title_files_prefer_mp4(tmp_path):
    _touch(tmp_path / "A1_t00.mkv")
    assert [p.name for p in title_files(tmp_path)] == ["A1_t00.mkv"]
    _touch(tmp_path / "MP4" / "A1 T01.mp4")
    _touch(tmp_path / "MP4" / "A1 T00.mp4")
    assert [p.name for p in title_files(tmp_path)] == ["A1 T00.mp4", "A1 T01.mp4"]


def test_transcribe_disc_caches_and_skips_short_titles(tmp_path):
    _touch(tmp_path / "MP4" / "A1 T00.mp4")
    _touch(tmp_path / "MP4" / "A1 T01.mp4")
    _touch(tmp_path / "MP4" / "A1 T02.mp4")
    lengths = {"A1 T00": 1385.14, "A1 T01": 120.0, "A1 T02": 1360.0}
    cached = transcript_path(tmp_path, "A1 T02")
    cached.parent.mkdir(parents=True)
    cached.write_text("already done", encoding="utf-8")
    model = FakeModel()

    got = transcribe_disc(tmp_path, get_duration=lambda f: lengths[f.stem], model_getter=lambda: model)

    assert [(t.stem, t.seconds, t.text) for t in got] == [
        ("A1 T00", 1385.1, "Hello there. General Kenobi."),
        ("A1 T01", 120.0, ""),
        ("A1 T02", 1360.0, "already done"),
    ]
    assert len(model.calls) == 1
    assert model.calls[0][1] == {"vad_filter": True, "beam_size": 1}
    assert transcript_path(tmp_path, "A1 T00").read_text(encoding="utf-8") == "Hello there. General Kenobi."
    assert not transcript_path(tmp_path, "A1 T01").exists()
    assert sorted(p.name for p in (tmp_path / "transcripts").iterdir()) == ["A1 T00.txt", "A1 T02.txt"]


def test_transcribe_disc_does_not_load_model_when_all_cached(tmp_path):
    _touch(tmp_path / "MP4" / "A1 T00.mp4")
    transcript_path(tmp_path, "A1 T00").parent.mkdir(parents=True)
    transcript_path(tmp_path, "A1 T00").write_text("cached", encoding="utf-8")

    def no_model():
        raise AssertionError("model should not load")

    got = transcribe_disc(tmp_path, get_duration=lambda f: 1300.0, model_getter=no_model)
    assert got[0].text == "cached"
