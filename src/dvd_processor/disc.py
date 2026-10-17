import os
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MAKEMKV_DEFAULT = r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe"


@dataclass
class DiscTitle:
    index: int
    duration_secs: int
    output_filename: str = ""


def resolve_makemkv_path(config=None) -> str:
    """Config key makemkv_path, else the usual install, else makemkvcon on PATH."""
    configured = config.get("makemkv_path") if config is not None else None
    if configured:
        return configured
    return MAKEMKV_DEFAULT if os.path.exists(MAKEMKV_DEFAULT) else "makemkvcon"


def parse_disc_label(output: str) -> str:
    """The disc's volume name from makemkvcon's CINFO lines (32, else the disc name, 2)."""
    found = {}
    for line in output.splitlines():
        m = re.match(r'CINFO:(\d+),\d+,"(.*)"', line)
        if m:
            found[int(m[1])] = m[2].strip()
    return found.get(32) or found.get(2) or ""


def volume_label(drive: str) -> str:
    """The Windows volume label of a drive, or "" if it cannot be read."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        root = drive.rstrip("\\/") + "\\"
        ok = ctypes.windll.kernel32.GetVolumeInformationW(root, buf, len(buf), None, None, None, None, 0)
        return buf.value.strip() if ok else ""
    except Exception:
        return ""


CREATE_NO_WINDOW = 0x08000000


class DiscScanner:
    def __init__(self, drive: str, makemkv_path: str = "makemkvcon", quiet: bool = False, runner=None):
        """quiet (for the window): no console window for makemkvcon and its output dropped.
        runner: a stand-in for subprocess.run (procs.ProcessRunner.run) so a rip can be stopped."""
        self.drive = drive
        self.makemkv_path = makemkv_path
        self.quiet = quiet
        self.runner = runner

    def _run(self, *args, **kwargs):
        return (self.runner or subprocess.run)(*args, **kwargs)

    def _no_window(self) -> dict:
        return {"creationflags": CREATE_NO_WINDOW} if self.quiet and os.name == "nt" else {}

    def scan(self) -> list[DiscTitle]:
        return self.scan_with_label()[0]

    def scan_with_label(self) -> tuple[list[DiscTitle], str]:
        """The disc's titles and its label (from CINFO, else the drive's volume label)."""
        result = self._run(
            [self.makemkv_path, "--robot", "info", f"disc:{self._drive_index()}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            **self._no_window(),
        )
        self.last_output = result.stdout
        label = parse_disc_label(result.stdout) or volume_label(self.drive)
        return self._parse_titles(result.stdout), label

    def _drive_index(self) -> int:
        result = self._run(
            [self.makemkv_path, "--robot", "info", "disc:9999"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **self._no_window(),
        )
        drive_upper = self.drive.upper().rstrip("\\")
        for line in result.stdout.splitlines():
            if not line.startswith("DRV:"):
                continue
            parts = line.split(",")
            if len(parts) < 6:
                continue
            index = int(parts[0].split(":")[1])
            letter = parts[5].strip('"').upper().rstrip("\\")
            if letter == drive_upper:
                return index
        return 0

    def _parse_titles(self, output: str) -> list[DiscTitle]:
        titles: dict[int, dict] = {}
        for line in output.splitlines():
            m = re.match(r'TINFO:(\d+),(\d+),\d+,"(.*)"', line)
            if not m:
                continue
            idx, attr_id, value = int(m[1]), int(m[2]), m[3]
            if idx not in titles:
                titles[idx] = {}
            if attr_id == 9:
                titles[idx]["duration_secs"] = self._parse_duration(value)
            elif attr_id == 27:
                titles[idx]["output_filename"] = value

        result = []
        for idx in sorted(titles):
            t = titles[idx]
            if "duration_secs" in t:
                result.append(DiscTitle(
                    index=idx,
                    duration_secs=t["duration_secs"],
                    output_filename=t.get("output_filename", ""),
                ))
        return result

    def _parse_duration(self, s: str) -> int:
        try:
            parts = s.split(":")
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            return 0
        except (ValueError, TypeError):
            return 0

    def rip(self, title_indices: list[int], output_dir: Path) -> None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        for idx in title_indices:
            self._run(
                [
                    self.makemkv_path,
                    "--robot",
                    "mkv",
                    f"disc:{self._drive_index()}",
                    str(idx),
                    str(output_dir),
                ],
                check=True,
                **({"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, **self._no_window()}
                   if self.quiet else {}),
            )
