"""Transcribe a disc folder's titles with faster-whisper, for the transcript matcher.

small.en on the GPU (float16), falling back to the CPU (int8) when CUDA will not load.
For each title, uses its MP4 encode in <disc>/MP4 when that exists (that is what goes to the
NAS), else its MKV rip, so a partly encoded disc never loses titles. Titles under 5 minutes
are menus or extras and are not transcribed; a title whose length cannot be read is
transcribed anyway. Transcripts are cached as <disc>/transcripts/<key>.txt, where the key is
the MP4-style name ("A1 T00") whether the MKV or the MP4 was used, so a rerun only does what
is missing. Needs the "transcribe" extra.
"""
import glob
import logging
import os
import site
from pathlib import Path
from typing import Callable, Optional

from dvd_processor.handbrake import mp4_name, mp4_path
from dvd_processor.transcript_matcher import TitleTranscript

log = logging.getLogger(__name__)

MODEL_NAME = "small.en"
MIN_TRANSCRIBE_SECONDS = 300
TRANSCRIPTS_DIR = "transcripts"

_model = None
_dll_dirs_added = False


def add_cuda_dll_dirs() -> list[str]:
    """Put site-packages nvidia/*/bin on the DLL search path, before faster_whisper is imported.

    Without this, CUDA on Windows fails to find cublas64_12.dll from the pip wheels.
    """
    global _dll_dirs_added
    added: list[str] = []
    if _dll_dirs_added:
        return added
    roots = list(site.getsitepackages())
    try:
        roots.append(site.getusersitepackages())
    except AttributeError:
        pass
    for sp in roots:
        for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            added.append(d)
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    _dll_dirs_added = True
    return added


def _load_whisper(device: str, compute_type: str):
    from faster_whisper import WhisperModel
    return WhisperModel(MODEL_NAME, device=device, compute_type=compute_type)


def _warm_up(model) -> None:
    """Run one second of silence through the model. A missing CUDA library often only shows
    up on the first transcription, not when the model loads."""
    import numpy as np
    segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)
    list(segments)


def get_model(loader: Callable[[str, str], object] = _load_whisper):
    """The whisper model, loaded once per process: GPU float16, else CPU int8."""
    global _model
    if _model is not None:
        return _model
    add_cuda_dll_dirs()
    try:
        model = loader("cuda", "float16")
        _warm_up(model)
        log.info("whisper %s on the GPU", MODEL_NAME)
    except Exception as exc:
        log.warning("CUDA not available (%s), using the CPU", exc)
        model = loader("cpu", "int8")
    _model = model
    return _model


def reset_model() -> None:
    global _model
    _model = None


ENCODING_SUFFIX = ".encoding.mp4"  # HandBrakeEncoder's temporary output


def title_key(path: Path) -> str:
    """'A1_t00.mkv' and 'MP4/A1 T00.mp4' are the same title: 'A1 T00'."""
    path = Path(path)
    return mp4_name(path.stem) if path.suffix.lower() == ".mkv" else path.stem


def title_files(disc: Path) -> list[Path]:
    """One file per title, in title order: its MP4 encode if there is one, else its MKV.

    MP4s with no MKV beside them (the rips already cleared) are included too. HandBrake's
    temporary *.encoding.mp4 files never are.
    """
    disc = Path(disc)
    by_key: dict[str, Path] = {}
    for mkv in sorted(disc.glob("*.mkv")):
        mp4 = mp4_path(mkv)
        by_key[title_key(mkv)] = mp4 if mp4.exists() else mkv
    for mp4 in sorted((disc / "MP4").glob("*.mp4")):
        if mp4.name.lower().endswith(ENCODING_SUFFIX):
            continue
        by_key.setdefault(mp4.stem, mp4)
    return [by_key[k] for k in sorted(by_key)]


def duration(path: Path) -> float:
    import av
    with av.open(str(path)) as c:
        return c.duration / 1e6 if c.duration else 0.0


def transcript_path(disc: Path, key: str) -> Path:
    return Path(disc) / TRANSCRIPTS_DIR / f"{key}.txt"


def cached_transcript(disc: Path, path: Path) -> Optional[Path]:
    """The cached transcript for this title under its key, or under the file's own stem
    (an older cache made from the MKV name), if either exists."""
    for name in dict.fromkeys([title_key(path), Path(path).stem]):
        p = transcript_path(disc, name)
        if p.exists():
            return p
    return None


def transcribe_file(path: Path, model=None) -> str:
    model = model if model is not None else get_model()
    segments, _ = model.transcribe(str(path), vad_filter=True, beam_size=1)
    return " ".join(s.text.strip() for s in segments)


def transcribe_disc(disc: Path,
                    get_duration: Callable[[Path], float] = duration,
                    model_getter: Callable[[], object] = get_model,
                    progress: Optional[Callable[[str], None]] = None) -> list[TitleTranscript]:
    """Transcribe what is missing and return every title on the disc, in title order.

    Titles under MIN_TRANSCRIBE_SECONDS come back with empty text; a title whose duration
    cannot be read (0) is transcribed anyway. The model is only loaded when something needs
    transcribing. TitleTranscript.stem is the title key ("A1 T00").
    """
    disc = Path(disc)
    out = []
    for f in title_files(disc):
        try:
            seconds = get_duration(f)
        except Exception as exc:
            log.warning("could not read the length of %s (%s), transcribing anyway", f, exc)
            seconds = 0.0
        key = title_key(f)
        txt = transcript_path(disc, key)
        cached = cached_transcript(disc, f)
        text = ""
        if cached is not None:
            text = cached.read_text(encoding="utf-8")
        elif seconds == 0 or seconds >= MIN_TRANSCRIBE_SECONDS:
            text = transcribe_file(f, model_getter())
            txt.parent.mkdir(parents=True, exist_ok=True)
            tmp = txt.with_suffix(".txt.part")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(txt)
            if progress:
                progress(f"{disc.name}/{key} {seconds / 60:.1f} min transcribed")
        out.append(TitleTranscript(str(disc), key, round(seconds, 1), text))
    return out
