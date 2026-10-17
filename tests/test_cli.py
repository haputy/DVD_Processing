"""The dvd-process commands end to end, with TMDB, Wikipedia, MakeMKV, HandBrake and whisper
replaced by fakes, and every folder under tmp_path."""
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

pytest.importorskip("sklearn")

from dvd_processor import cli
from dvd_processor.disc import DiscTitle
from dvd_processor.discinfo import read_disc_info, write_disc_info
from dvd_processor.episodes import Episode
from dvd_processor.handbrake import EncodeResult
from dvd_processor.tmdb import TmdbShow, TmdbShowDetails
from dvd_processor.transcript_matcher import TitleTranscript

EPISODES = [
    Episode(1, 1, "Lighthouse", wiki_summary="lighthouse keeper brass lamp fog horn"),
    Episode(1, 2, "Bakery", wiki_summary="croissants baguettes sourdough bakery"),
    Episode(1, 3, "Circus", wiki_summary="circus trapeze acrobats clowns elephant"),
    Episode(2, 1, "Election", wiki_summary="mayoral election ballots recount posters"),
]
TEXTS = {
    "lighthouse": "the lighthouse lamp needs brass polish, sound the fog horn",
    "bakery": "fresh croissants and baguettes, feed the sourdough starter",
    "circus": "trapeze acrobats and clowns, the circus elephant escaped",
    "election": "count the ballots, recount the election, mayoral posters",
}


class FakeClient:
    def __init__(self, *a, **k):
        pass

    def search_show(self, name):
        return [TmdbShow(42, "Seaside", "1999"), TmdbShow(43, "Seaside (2020)", "2020")]

    def get_show(self, tmdb_id):
        return TmdbShowDetails(tmdb_id, "Seaside", "1999", [1, 2], 4)

    def get_regulars(self, tmdb_id):
        return ["Hank"]


@pytest.fixture
def env(tmp_path, tmp_config_dir, monkeypatch):
    rips = tmp_path / "Video"
    rips.mkdir()
    cfg = tmp_config_dir / ".dvd_processor"
    cfg.mkdir(exist_ok=True)
    (cfg / "config.json").write_text(json.dumps({"tmdb_api_key": "k", "rip_root": str(rips)}))
    monkeypatch.setattr(cli, "TmdbClient", FakeClient)
    monkeypatch.setattr(cli, "build_episodes", lambda client, tid, name: [Episode(**vars(e)) for e in EPISODES])
    return tmp_path


def _disc(root, name, words, show="Seaside", tmdb_id=42):
    d = root / "Video" / name
    (d / "MP4").mkdir(parents=True)
    for i, w in enumerate(words):
        (d / f"A1_t0{i}.mkv").write_text("rip")
        (d / "MP4" / f"A1 T0{i}.mp4").write_text(w)  # the file content names its dialogue
    write_disc_info(d, show, tmdb_id, name, "2026-10-17T10:00:00")
    return d


def fake_transcribe_disc(folder, progress=None):
    folder = Path(folder)
    out = []
    for mp4 in sorted((folder / "MP4").glob("*.mp4")):
        out.append(TitleTranscript(str(folder), mp4.stem, 1320, TEXTS[mp4.read_text()], str(mp4)))
    (folder / "transcripts").mkdir(exist_ok=True)
    return out


def run(args, input=None):
    return CliRunner().invoke(cli.main, args, input=input, catch_exceptions=False)


def test_match_copies_after_review_with_correction_and_skip(env, monkeypatch):
    monkeypatch.setattr(cli, "_transcriber", lambda: fake_transcribe_disc)
    _disc(env, "SEASIDE_D1", ["lighthouse", "circus"])
    _disc(env, "SEASIDE_D2", ["bakery", "election"])
    dest = env / "nas"
    # pick show 1, swap rows 1 and 2's episodes by hand, skip row 4, accept, confirm copy
    result = run(["match", "--show", "Seaside", "--dest-root", str(dest)],
                 input="1\n1=S01E03 2=S01E01 skip 4\nok\ny\n")
    assert result.exit_code == 0, result.output
    season1 = dest / "Seaside (1999)" / "Season 1"
    assert (season1 / "Seaside S01 E03.mp4").read_text() == "lighthouse"
    assert (season1 / "Seaside S01 E01.mp4").read_text() == "circus"
    assert (season1 / "Seaside S01 E02.mp4").read_text() == "bakery"
    assert not (dest / "Seaside (1999)" / "Season 2").exists()
    assert "3 copied" in result.output and "0 failed" in result.output


def test_match_yes_with_season_limit(env, monkeypatch):
    monkeypatch.setattr(cli, "_transcriber", lambda: fake_transcribe_disc)
    _disc(env, "SEASIDE_D1", ["lighthouse", "bakery", "circus"])
    dest = env / "nas"
    result = run(["match", "--show", "Seaside", "--season", "1", "--yes", "--dest-root", str(dest)])
    assert result.exit_code == 0, result.output
    names = sorted(p.name for p in (dest / "Seaside (1999)" / "Season 1").iterdir())
    assert names == ["Seaside S01 E01.mp4", "Seaside S01 E02.mp4", "Seaside S01 E03.mp4"]
    # Running again copies nothing new
    again = run(["match", "--show", "Seaside", "--season", "1", "--yes", "--dest-root", str(dest)])
    assert "0 copied" in again.output and "3 already there" in again.output


def test_match_refuses_a_lost_backslash_root(env):
    result = CliRunner().invoke(cli.main, ["match", "--show", "Seaside", "--yes",
                                           "--dest-root", "/DXP2800-4CC5/Jellyfin/TVShows"])
    assert result.exit_code == 1
    assert "not a NAS root" in result.output


def test_match_quit_copies_nothing(env, monkeypatch):
    monkeypatch.setattr(cli, "_transcriber", lambda: fake_transcribe_disc)
    _disc(env, "SEASIDE_D1", ["lighthouse"])
    dest = env / "nas"
    result = run(["match", "--show", "Seaside", "--dest-root", str(dest)], input="1\nq\n")
    assert "nothing copied" in result.output
    assert not dest.exists()


def test_add_registers_encodes_missing_and_transcribes(env, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_transcriber", lambda: (lambda f, progress=None: calls.append(Path(f)) or []))

    class Encoder:
        def encode_all(self, folder):
            calls.append(("encode", Path(folder)))
            return [EncodeResult(Path(folder) / "A1_t01.mkv", Path(folder) / "MP4" / "A1 T01.mp4", False)]

    monkeypatch.setattr(cli, "make_encoder", lambda config: Encoder())
    d1 = env / "Video" / "OLD_1"
    d1.mkdir()
    (d1 / "A1_t00.mkv").write_text("")
    (d1 / "MP4").mkdir()
    (d1 / "MP4" / "A1 T00.mp4").write_text("")
    d2 = env / "Video" / "OLD_2"
    d2.mkdir()
    (d2 / "A1_t01.mkv").write_text("")
    result = run(["add", "--show", "Seaside", str(d1), str(d2)], input="1\ny\n")
    assert result.exit_code == 0, result.output
    assert read_disc_info(d1).show == "Seaside" and read_disc_info(d2).tmdb_id == 42
    assert ("encode", d1) not in calls and ("encode", d2) in calls  # d1 already has its MP4
    assert d1 in calls and d2 in calls


def test_add_declined_changes_nothing(env):
    d1 = env / "Video" / "OLD_1"
    d1.mkdir()
    run(["add", "--show", "Seaside", str(d1)], input="1\nn\n")
    assert read_disc_info(d1) is None


def test_rip_never_reuses_a_disc_folder(env, monkeypatch):
    ripped = []

    class Scanner:
        def __init__(self, drive, makemkv_path):
            pass

        def scan_with_label(self):
            return [DiscTitle(0, 1320, "A1_t00.mkv"), DiscTitle(1, 120, "A1_t01.mkv"),
                    DiscTitle(2, 1300, "A1_t02.mkv")], "SEASIDE_D1"

        def rip(self, indices, output_dir):
            ripped.append((indices, Path(output_dir)))
            for i in indices:
                (Path(output_dir) / f"A1_t0{i}.mkv").write_text("rip")

    monkeypatch.setattr(cli, "DiscScanner", Scanner)
    monkeypatch.setattr(cli, "encode_and_transcribe", lambda folder, config: None)
    (env / "Video" / "SEASIDE_D1").mkdir()
    result = run(["rip", "--show", "Seaside", "--drive", "E:", "--yes"])
    assert result.exit_code == 0, result.output
    assert ripped == [([0], env / "Video" / "SEASIDE_D1_2"), ([2], env / "Video" / "SEASIDE_D1_2")]
    info = read_disc_info(env / "Video" / "SEASIDE_D1_2")
    assert info.label == "SEASIDE_D1" and info.complete
    assert list((env / "Video" / "SEASIDE_D1").iterdir()) == []
    assert "dvd-process match" in result.output


def test_rip_uses_fallback_label(env, monkeypatch):
    class Scanner:
        def __init__(self, drive, makemkv_path):
            pass

        def scan_with_label(self):
            return [DiscTitle(0, 1320, "t.mkv")], ""

        def rip(self, indices, output_dir):
            pass

    monkeypatch.setattr(cli, "DiscScanner", Scanner)
    monkeypatch.setattr(cli, "encode_and_transcribe", lambda folder, config: None)
    _disc(env, "OTHER", ["lighthouse"])
    run(["rip", "--show", "Seaside", "--yes"])
    assert (env / "Video" / "SEASIDE_D2" / "disc.json").exists()


def _nas_show(env):
    show = env / "nas" / "Seaside (1999)"
    s1 = show / "Season 1"
    s1.mkdir(parents=True)
    # On the NAS in the wrong order: E01 holds the bakery episode, E02 the lighthouse one
    (s1 / "Seaside S01 E01.mp4").write_text("bakery")
    (s1 / "Seaside S01 E02.mp4").write_text("lighthouse")
    (s1 / "Seaside S01 E03.mp4").write_text("circus")
    (s1 / "folder.jpg").write_text("")
    return show


def fake_transcribe_files(files, cache_dir, disc, progress=None, check_source=False):
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    return [TitleTranscript(disc, Path(f).stem, 1320, TEXTS[Path(f).read_text()], str(f)) for f in files]


def test_fix_renames_on_the_given_root_and_caches_off_it(env, monkeypatch, tmp_config_dir):
    monkeypatch.setattr(cli, "_file_transcriber", lambda: fake_transcribe_files)
    show = _nas_show(env)
    result = run(["fix", "--show", "Seaside", "--nas-folder", "Seaside (1999)", "--season", "1",
                  "--dest-root", str(env / "nas")], input="1\nok\ny\n")
    assert result.exit_code == 0, result.output
    s1 = show / "Season 1"
    assert (s1 / "Seaside S01 E01.mp4").read_text() == "lighthouse"
    assert (s1 / "Seaside S01 E02.mp4").read_text() == "bakery"
    assert (s1 / "Seaside S01 E03.mp4").read_text() == "circus"
    assert (s1 / "folder.jpg").exists() and "folder.jpg" in result.output
    assert "2 renamed" in result.output
    assert (tmp_config_dir / ".dvd_processor" / "nas-transcripts" / "Seaside" / "Season 1").is_dir()
    assert not any(p.suffix == ".txt" for p in show.rglob("*"))


def test_fix_declined_renames_nothing(env, monkeypatch):
    monkeypatch.setattr(cli, "_file_transcriber", lambda: fake_transcribe_files)
    show = _nas_show(env)
    run(["fix", "--show", "Seaside", "--nas-folder", "Seaside (1999)", "--season", "1",
         "--dest-root", str(env / "nas")], input="1\nok\nn\n")
    assert (show / "Season 1" / "Seaside S01 E01.mp4").read_text() == "bakery"


def test_resume_finishes_an_interrupted_rename(env, monkeypatch):
    from dvd_processor import nas
    d = env / "nas" / "Season 1"
    d.mkdir(parents=True)
    a, b = (d / "x E05.mp4"), (d / "x E06.mp4")
    a.write_text("5")
    b.write_text("6")
    real = os.rename
    calls = {"n": 0}

    def stop(x, y):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("dropped")
        real(x, y)

    monkeypatch.setattr(nas.os, "rename", stop)
    with pytest.raises(OSError):
        nas.rename_plan_apply([(a.as_posix(), b.as_posix()), (b.as_posix(), a.as_posix())])
    monkeypatch.setattr(nas.os, "rename", real)
    result = run(["resume", "--yes"])
    assert "2 moved" in result.output
    assert b.read_text() == "5" and a.read_text() == "6"
    assert "No unfinished renames" in run(["resume"]).output
