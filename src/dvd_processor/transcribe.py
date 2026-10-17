"""Transcribe a disc folder's titles with faster-whisper, for the transcript matcher.

small.en on the GPU (float16), falling back to the CPU (int8) when CUDA will not load.
Uses the MP4 encodes in <disc>/MP4 when there are any (that is what goes to the NAS), else
the MKV rips. Titles under 5 minutes are menus or extras and are not transcribed.
Transcripts are cached as <disc>/transcripts/<title stem>.txt, so a rerun only does what is
missing. Needs the "transcribe" extra.
"""
import glob
import logging
import os
import site
from pathlib import Path
from typing import Callable, Optional

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


def title_files(disc: Path) -> list[Path]:
    """The disc's MP4 encodes, else its MKV rips, in name order."""
    disc = Path(disc)
    mp4s = sorted((disc / "MP4").glob("*.mp4"))
    return mp4s if mp4s else sorted(disc.glob("*.mkv"))


def duration(path: Path) -> float:
    import av
    with av.open(str(path)) as c:
        return c.duration / 1e6 if c.duration else 0.0


def transcript_path(disc: Path, stem: str) -> Path:
    return Path(disc) / TRANSCRIPTS_DIR / f"{stem}.txt"


def transcribe_file(path: Path, model=None) -> str:
    model = model if model is not None else get_model()
    segments, _ = model.transcribe(str(path), vad_filter=True, beam_size=1)
    return " ".join(s.text.strip() for s in segments)


def transcribe_disc(disc: Path,
                    get_duration: Callable[[Path], float] = duration,
                    model_getter: Callable[[], object] = get_model,
                    progress: Optional[Callable[[str], None]] = None) -> list[TitleTranscript]:
    """Transcribe what is missing and return every title on the disc, in name order.

    Titles under MIN_TRANSCRIBE_SECONDS come back with empty text. The model is only loaded
    when something needs transcribing.
    """
    disc = Path(disc)
    out = []
    for f in title_files(disc):
        seconds = get_duration(f)
        txt = transcript_path(disc, f.stem)
        text = ""
        if txt.exists():
            text = txt.read_text(encoding="utf-8")
        elif seconds >= MIN_TRANSCRIBE_SECONDS:
            text = transcribe_file(f, model_getter())
            txt.parent.mkdir(parents=True, exist_ok=True)
            tmp = txt.with_suffix(".txt.part")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(txt)
            if progress:
                progress(f"{disc.name}/{f.stem} {seconds / 60:.1f} min transcribed")
        out.append(TitleTranscript(str(disc), f.stem, round(seconds, 1), text))
    return out
