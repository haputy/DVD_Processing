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


def test_title_files_prefer_each_titles_mp4(tmp_path):
    _touch(tmp_path / "A1_t00.mkv")
    _touch(tmp_path / "A1_t01.mkv")
    assert [p.name for p in title_files(tmp_path)] == ["A1_t00.mkv", "A1_t01.mkv"]
    # Partly encoded disc: T00 done, T01 still encoding. Neither title is lost.
    _touch(tmp_path / "MP4" / "A1 T00.mp4")
    _touch(tmp_path / "MP4" / "A1 T01.encoding.mp4")
    assert [p.relative_to(tmp_path).as_posix() for p in title_files(tmp_path)] ==         ["MP4/A1 T00.mp4", "A1_t01.mkv"]
    # An MP4 whose MKV was cleared away still counts
    _touch(tmp_path / "MP4" / "A1 T02.mp4")
    assert [p.name for p in title_files(tmp_path)] == ["A1 T00.mp4", "A1_t01.mkv", "A1 T02.mp4"]


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


def test_cache_key_is_stable_between_mkv_and_mp4(tmp_path):
    _touch(tmp_path / "A1_t00.mkv")
    model = FakeModel()
    got = transcribe_disc(tmp_path, get_duration=lambda f: 1300.0, model_getter=lambda: model)
    assert got[0].stem == "A1 T00"
    assert transcript_path(tmp_path, "A1 T00").exists()
    # After encoding, the MP4 is used but the cached transcript is found
    _touch(tmp_path / "MP4" / "A1 T00.mp4")
    got = transcribe_disc(tmp_path, get_duration=lambda f: 1300.0, model_getter=lambda: model)
    assert len(model.calls) == 1
    assert got[0].text == "Hello there. General Kenobi."


def test_older_cache_under_mkv_stem_is_used(tmp_path):
    _touch(tmp_path / "A1_t00.mkv")
    old = transcript_path(tmp_path, "A1_t00")
    old.parent.mkdir(parents=True)
    old.write_text("old cache", encoding="utf-8")

    def no_model():
        raise AssertionError("model should not load")

    got = transcribe_disc(tmp_path, get_duration=lambda f: 1300.0, model_getter=no_model)
    assert got[0].text == "old cache"


def test_unknown_duration_is_transcribed(tmp_path):
    _touch(tmp_path / "A1_t00.mkv")
    _touch(tmp_path / "A1_t01.mkv")
    model = FakeModel()

    def length(f):
        if f.stem == "A1_t01":
            raise OSError("cannot open")
        return 0.0

    got = transcribe_disc(tmp_path, get_duration=length, model_getter=lambda: model)
    assert [(t.seconds, bool(t.text)) for t in got] == [(0.0, True), (0.0, True)]
    assert len(model.calls) == 2


def test_title_files_ignore_case_and_sort_naturally(tmp_path):
    _touch(tmp_path / "A1_t00.mkv")
    _touch(tmp_path / "MP4" / "A1 t00.mp4")  # same title, different case
    _touch(tmp_path / "MP4" / "A1 T9.mp4")
    _touch(tmp_path / "MP4" / "A1 T10.mp4")
    got = title_files(tmp_path)
    assert len(got) == 3
    assert [transcribe.title_key(f).casefold() for f in got] == ["a1 t00", "a1 t9", "a1 t10"]
    assert transcribe.title_key(got[0]) in ("A1 T00", "A1 t00")
