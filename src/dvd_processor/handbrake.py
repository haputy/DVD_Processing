"""Encode a disc folder's MKV rips to MP4 the way Andy's HandBrake GUI does.

Preset "Fast 1080p30" (H.264, AAC stereo, MP4, DVD resolution kept), output in an MP4
subfolder of the disc folder, named with underscores turned to spaces and title case:
A1_t00.mkv becomes MP4/A1 T00.mp4. The MKV rips are left alone.
"""
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_PRESET = "Fast 1080p30"
HANDBRAKE_VERSION = "1.9.2"


def default_handbrake_path() -> str:
    """The 1.9.2 install under %LOCALAPPDATA%\\HandBrakeCLI if it is there, else HandBrakeCLI on PATH."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        p = Path(local) / "HandBrakeCLI" / HANDBRAKE_VERSION / "HandBrakeCLI.exe"
        if p.exists():
            return str(p)
    return "HandBrakeCLI"


def resolve_handbrake_path(config=None) -> str:
    """Config key handbrake_path when set, else the default."""
    configured = config.get("handbrake_path") if config is not None else None
    return configured or default_handbrake_path()


def mp4_name(stem: str) -> str:
    """'A1_t00' -> 'A1 T00', as the HandBrake GUI names its output."""
    return stem.replace("_", " ").title()


def mp4_path(mkv: Path) -> Path:
    mkv = Path(mkv)
    return mkv.parent / "MP4" / (mp4_name(mkv.stem) + ".mp4")


@dataclass
class EncodeResult:
    source: Path
    output: Path
    skipped: bool  # True when the MP4 was already there


class HandBrakeEncoder:
    def __init__(self, handbrake_path: Optional[str] = None, preset: str = DEFAULT_PRESET):
        self.handbrake_path = handbrake_path or default_handbrake_path()
        self.preset = preset

    def encode(self, input_path: Path, output_path: Path) -> None:
        """Encode to a temporary name beside the output, then rename, so a stopped encode never
        leaves a partial file that looks finished."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = output_path.with_name(output_path.stem + ".encoding" + output_path.suffix)
        try:
            subprocess.run(
                [
                    self.handbrake_path,
                    "--input", str(input_path),
                    "--output", str(tmp),
                    "--preset", self.preset,
                ],
                check=True,
            )
            tmp.replace(output_path)
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise

    def encode_all(self, directory: Path, suffix: str = ".mkv") -> list[EncodeResult]:
        results = []
        for src in sorted(Path(directory).glob(f"*{suffix}")):
            out = mp4_path(src)
            if out.exists():
                results.append(EncodeResult(src, out, skipped=True))
                continue
            self.encode(src, out)
            results.append(EncodeResult(src, out, skipped=False))
        return results
