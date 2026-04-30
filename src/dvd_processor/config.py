import json
from pathlib import Path


class Config:
    def __init__(self):
        self.config_dir = Path.home() / ".dvd_processor"
        self.config_dir.mkdir(exist_ok=True)
        self.config_path = self.config_dir / "config.json"
        self._data = self._load()

    def _load(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {}

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value):
        self._data[key] = value
        self.config_path.write_text(json.dumps(self._data, indent=2))
