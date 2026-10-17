"""Safety rules of the dvd-process commands: --yes never acts on doubt, failed or repeated
rips are caught, seasons are respected, and the NAS folder cannot escape its root."""
import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

pytest.importorskip("sklearn")

from dvd_processor import cli, nas
from dvd_processor.disc import DiscTitle
from dvd_processor.discinfo import read_disc_info, write_disc_info
from dvd_processor.tmdb import TmdbShow
from dvd_processor.transcript_matcher import TitleTranscript

from test_cli import TEXTS, FakeClient, _disc, env, fake_transcribe_disc, run  # noqa: F401

TEXTS["nonsense"] = "zebra quokka narwhal okapi"


def invoke(args, input=None):
    return CliRunner().invoke(cli.main, args, input=input)


# ---- 1: --yes ------------------------------------------------------------------------------

def test_yes_refuses_check_rows_and_copies_nothing(env, monkeypatch):
    monkeypatch.setattr(cli, "_transcriber", lambda: fake_transcribe_disc)
    _disc(env, "SEASIDE_D1", ["lighthouse", "nonsense", "circus"])
    dest = env / "nas"
    result = invoke(["match", "--show", "Seaside", "--season", "1", "--yes", "--dest-root", str(dest)])
    assert result.exit_code == 1
    assert "flagged CHECK" in result.output and "A1 T01" in result.output
    assert not dest.exists()


def test_yes_refuses_ambiguous_tmdb_search(env, monkeypatch):
    class Twins(FakeClient):
        def search_show(self, name):
            return [TmdbShow(1, "Seaside", "1999"), TmdbShow(2, "Seaside", "2020")]

    class NotFirst(FakeClient):
        def search_show(self, name):
            return [TmdbShow(1, "Seaside Stories", "1999"), TmdbShow(2, "Seaside", "2020")]

    for client in (Twins, NotFirst):
        monkeypatch.setattr(cli, "TmdbClient", client)
        result = invoke(["match", "--show", "Seaside", "--yes", "--dest-root", str(env / "nas")])
        assert result.exit_code == 1 and "ambiguous" in result.output


def test_yes_refuses_far_more_titles_than_episodes(env, monkeypatch):
    monkeypatch.setattr(cli, "_transcriber", lambda: fake_transcribe_disc)
    # Season 2 has one episode; five titles are far too many
    _disc(env, "SEASIDE_D1", ["lighthouse", "bakery", "circus", "election", "nonsense"])
    result = invoke(["match", "--show", "Seaside", "--season", "2", "--yes", "--dest-root", str(env / "nas")])
    assert result.exit_code == 1 and "more titles than episodes" in result.output
    assert not (env / "nas").exists()


# ---- 2, 3: rips ----------------------------------------------------------------------------

def _scanner(label, fail_on=None, interrupt=False, ripped=None):
    class Scanner:
        def __init__(self, drive, makemkv_path):
            pass

        def scan_with_label(self):
            return [DiscTitle(0, 1320, "A1_t00.mkv"), DiscTitle(1, 1320, "A1_t01.mkv"),
                    DiscTitle(2, 1320, "A1_t02.mkv")], label

        def rip(self, indices, output_dir):
            for i in indices:
                (Path(output_dir) / f"A1_t0{i}.mkv").write_text("partial" if i == fail_on else "rip")
                if i == fail_on:
                    if interrupt:
                        raise KeyboardInterrupt
                    raise subprocess.CalledProcessError(1, "makemkvcon")
                if ripped is not None:
                    ripped.append(i)
    return Scanner


@pytest.mark.parametrize("interrupt", [False, True])
def test_failed_rip_is_marked_incomplete_and_left_out_of_match(env, monkeypatch, interrupt):
    monkeypatch.setattr(cli, "DiscScanner", _scanner("SEASIDE_D1", fail_on=1, interrupt=interrupt))
    monkeypatch.setattr(cli, "encode_and_transcribe", lambda folder, config: pytest.fail("should not encode"))
    result = invoke(["rip", "--show", "Seaside", "--yes"])
    assert result.exit_code == 1
    folder = env / "Video" / "SEASIDE_D1"
    assert "Finished titles: [0]" in result.output
    assert sorted(p.name for p in folder.glob("A1_*")) == ["A1_t00.mkv", "A1_t01.mkv.failed"]
    assert read_disc_info(folder).complete is False
    monkeypatch.setattr(cli, "_transcriber", lambda: fake_transcribe_disc)
    result = invoke(["match", "--show", "Seaside", "--yes", "--dest-root", str(env / "nas")])
    assert "Leaving out" in result.output and "did not complete" in result.output


def test_same_disc_again_is_refused_unless_again(env, monkeypatch):
    monkeypatch.setattr(cli, "encode_and_transcribe", lambda folder, config: None)
    monkeypatch.setattr(cli, "DiscScanner", _scanner("SEASIDE_D1"))
    assert invoke(["rip", "--show", "Seaside", "--yes"]).exit_code == 0
    result = invoke(["rip", "--show", "Seaside", "--yes"])
    assert result.exit_code == 1 and "already ripped" in result.output
    assert not (env / "Video" / "SEASIDE_D1_2").exists()
    assert invoke(["rip", "--show", "Seaside", "--yes", "--again"]).exit_code == 0
    assert read_disc_info(env / "Video" / "SEASIDE_D1_2").complete


def test_label_clash_with_another_show_gets_a_suffix(env, monkeypatch):
    monkeypatch.setattr(cli, "encode_and_transcribe", lambda folder, config: None)
    monkeypatch.setattr(cli, "DiscScanner", _scanner("DISC_1"))
    write_disc_info(env / "Video" / "DISC_1", "Other Show", 99)
    assert invoke(["rip", "--show", "Seaside", "--yes", "--season", "2"]).exit_code == 0
    info = read_disc_info(env / "Video" / "DISC_1_2")
    assert info.show == "Seaside" and info.season == 2


# ---- 5: seasons ----------------------------------------------------------------------------

def test_match_season_uses_tagged_discs_and_warns_about_untagged(env, monkeypatch):
    monkeypatch.setattr(cli, "_transcriber", lambda: fake_transcribe_disc)
    d1 = _disc(env, "SEASIDE_D1", ["lighthouse", "bakery", "circus"])
    d2 = _disc(env, "SEASIDE_D2", ["election"])
    d3 = _disc(env, "SEASIDE_D3", ["bakery"])
    for d, s in ((d1, 1), (d2, 2)):
        info = read_disc_info(d)
        write_disc_info(d, info.show, info.tmdb_id, info.label, info.ripped, season=s)
    result = invoke(["match", "--show", "Seaside", "--season", "2", "--dest-root", str(env / "nas")],
                    input="1\nq\n")
    assert "SEASIDE_D1" not in result.output.split("Discs:")[1].splitlines()[0]
    assert "SEASIDE_D2" in result.output and "Not tagged with a season" in result.output
    assert "SEASIDE_D3" in result.output


def test_add_tags_the_season(env, monkeypatch):
    monkeypatch.setattr(cli, "_transcriber", lambda: (lambda f, progress=None: []))
    d = env / "Video" / "OLD"
    d.mkdir()
    assert invoke(["add", "--show", "Seaside", "--season", "3", "--yes", str(d)]).exit_code == 0
    assert read_disc_info(d).season == 3


# ---- 4, 5, 6, 11: fix ----------------------------------------------------------------------

def _transcribe_by_content(files, cache_dir, disc, progress=None, check_source=False):
    assert check_source
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        (Path(cache_dir) / f"{Path(f).stem}.txt").write_text(TEXTS[Path(f).read_text()], encoding="utf-8")
        out.append(TitleTranscript(disc, Path(f).stem, 1320, TEXTS[Path(f).read_text()], str(f)))
    return out


def test_fix_keeps_existing_season_folder_names_and_limits_to_season(env, monkeypatch, tmp_config_dir):
    monkeypatch.setattr(cli, "_file_transcriber", lambda: _transcribe_by_content)
    show = env / "nas" / "Seaside (1999)"
    s1, s2 = show / "Season 01", show / "season 2"
    s1.mkdir(parents=True)
    s2.mkdir()
    (s1 / "a.mp4").write_text("bakery")
    (s1 / "b.mp4").write_text("lighthouse")
    (s1 / "c.mp4").write_text("circus")
    (s2 / "untouched.mp4").write_text("election")
    result = invoke(["fix", "--show", "Seaside", "--nas-folder", "/Seaside (1999)/", "--season", "1",
                     "--yes", "--dest-root", str(env / "nas")])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in s1.iterdir()) == ["Seaside S01 E01.mp4", "Seaside S01 E02.mp4",
                                                    "Seaside S01 E03.mp4"]
    assert (s1 / "Seaside S01 E01.mp4").read_text() == "lighthouse"
    assert not (show / "Season 1").exists()
    assert [p.name for p in s2.iterdir()] == ["untouched.mp4"]
    # The cached transcripts moved with the files
    cache = tmp_config_dir / ".dvd_processor" / "nas-transcripts" / "Seaside" / "Season 01"
    assert sorted(p.name for p in cache.glob("*.txt")) == ["Seaside S01 E01.txt", "Seaside S01 E02.txt",
                                                           "Seaside S01 E03.txt"]
    assert "lighthouse" in (cache / "Seaside S01 E01.txt").read_text(encoding="utf-8")
    assert not (tmp_config_dir / ".dvd_processor" / "nas-transcripts" / "Seaside" / "season 2").exists()


@pytest.mark.parametrize("bad", ["..", "../Other", "Seaside/../..", "/", ""])
def test_nas_folder_cannot_escape_the_root(env, bad):
    result = invoke(["fix", "--show", "Seaside", "--nas-folder", bad, "--dest-root", str(env / "nas")])
    assert result.exit_code == 1
    assert "not a show folder name" in result.output


# ---- 10: resume ----------------------------------------------------------------------------

def test_resume_yes_with_several_plans_lists_and_stops(env, tmp_config_dir):
    d = tmp_config_dir / ".dvd_processor"
    for n in (1, 2):
        (d / f"rename-2026101{n}-000000.json").write_text(json.dumps(
            {"phase": "pass2", "moves": [{"source": f"//N/s/{n}.mp4", "dest": f"//N/s/x{n}.mp4"}]}))
    result = invoke(["resume", "--yes"])
    assert result.exit_code == 1
    assert "rename-20261011" in result.output and "rename-20261012" in result.output
