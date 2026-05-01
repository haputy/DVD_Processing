import subprocess
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DiscTitle:
    index: int
    duration_secs: int
    output_filename: str = ""


class DiscScanner:
    def __init__(self, drive: str, makemkv_path: str = "makemkvcon"):
        self.drive = drive
        self.makemkv_path = makemkv_path

    def scan(self) -> list[DiscTitle]:
        result = subprocess.run(
            [self.makemkv_path, "--robot", "info", f"disc:{self._drive_index()}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return self._parse_titles(result.stdout)

    def _drive_index(self) -> int:
        result = subprocess.run(
            [self.makemkv_path, "--robot", "info", "disc:9999"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
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
            if attr_id == 11:
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
        parts = s.split(":")
        h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
        return h * 3600 + m * 60 + sec

    def rip(self, title_indices: list[int], output_dir: Path) -> None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        for idx in title_indices:
            subprocess.run(
                [
                    self.makemkv_path,
                    "--robot",
                    "mkv",
                    f"disc:{self._drive_index()}",
                    str(idx),
                    str(output_dir),
                ],
                check=True,
            )
