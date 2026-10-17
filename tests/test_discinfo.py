import json

from dvd_processor.discinfo import (
    DISC_JSON, disc_folders_for_show, read_disc_info, rip_root, write_disc_info,
)


def test_rip_root_default_and_config():
    assert str(rip_root()) == r"C:\Video"
    assert rip_root({"rip_root": "D:/Rips"}).name == "Rips"


def test_write_and_read_round_trip(tmp_path):
    folder = tmp_path / "NEWSRADIO_DISC_1"
    info = write_disc_info(folder, "NewsRadio", 1234, ripped="2026-10-17T10:00:00")
    assert info.label == "NEWSRADIO_DISC_1"
    raw = json.loads((folder / DISC_JSON).read_text(encoding="utf-8"))
    assert raw == {"show": "NewsRadio", "tmdb_id": 1234, "label": "NEWSRADIO_DISC_1",
                   "ripped": "2026-10-17T10:00:00"}
    assert read_disc_info(folder) == info


def test_read_missing_or_broken(tmp_path):
    assert read_disc_info(tmp_path) is None
    (tmp_path / DISC_JSON).write_text("{not json", encoding="utf-8")
    assert read_disc_info(tmp_path) is None


def test_disc_folders_for_show_in_natural_order(tmp_path):
    for name in ["NR_DISC_10", "NR_DISC_2", "NR_DISC_1"]:
        write_disc_info(tmp_path / name, "NewsRadio", 1234)
    write_disc_info(tmp_path / "AD_1", "Arrested Development", 4589)
    (tmp_path / "no_json").mkdir()
    (tmp_path / "loose.mkv").write_text("")
    by_name = disc_folders_for_show("newsradio", root=tmp_path)
    assert [f.name for f in by_name] == ["NR_DISC_1", "NR_DISC_2", "NR_DISC_10"]
    assert [f.name for f in disc_folders_for_show(tmdb_id=4589, root=tmp_path)] == ["AD_1"]
    assert disc_folders_for_show("Nothing", root=tmp_path) == []
    assert disc_folders_for_show("NewsRadio", root=tmp_path / "missing") == []
