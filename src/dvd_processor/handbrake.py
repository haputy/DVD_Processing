import subprocess
from pathlib import Path


class HandBrakeEncoder:
    def __init__(self, handbrake_path: str = "HandBrakeCLI"):
        self.handbrake_path = handbrake_path

    def encode(self, input_path: Path, output_path: Path) -> None:
        subprocess.run(
            [
                self.handbrake_path,
                "--input", str(input_path),
                "--output", str(output_path),
                "--preset", "H.265 MKV 1080p",
            ],
            check=True,
        )

    def encode_all(self, directory: Path, suffix: str = ".mkv") -> None:
        for src in sorted(directory.glob(f"*{suffix}")):
            tmp = src.with_suffix(".h265.mkv")
            self.encode(src, tmp)
            src.unlink()
            tmp.rename(src)
