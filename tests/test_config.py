# tests/test_config.py
import json
from pathlib import Path
from dvd_processor.config import Config


def test_config_dir_created(tmp_config_dir):
    cfg = Config()
    assert cfg.config_dir.exists()


def test_save_and_load_api_key(tmp_config_dir):
    cfg = Config()
    cfg.set("tmdb_api_key", "abc123")
    cfg2 = Config()
    assert cfg2.get("tmdb_api_key") == "abc123"


def test_get_missing_key_returns_none(tmp_config_dir):
    cfg = Config()
    assert cfg.get("nonexistent") is None


def test_get_missing_key_returns_default(tmp_config_dir):
    cfg = Config()
    assert cfg.get("nonexistent", "fallback") == "fallback"


def test_config_file_is_json(tmp_config_dir):
    cfg = Config()
    cfg.set("tmdb_api_key", "xyz")
    raw = json.loads(cfg.config_path.read_text())
    assert raw["tmdb_api_key"] == "xyz"
