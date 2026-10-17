"""The window's logic, without Tk."""
import subprocess
from pathlib import Path

import pytest

from dvd_processor import app_logic as al
from dvd_processor.disc import DiscTitle
from dvd_processor.discinfo import read_disc_info, write_disc_info
from dvd_processor.episodes import Episode
from dvd_processor.nas import RenameError
from dvd_processor.transcript_matcher import TitleTranscript, TranscriptMatch

EPS = [Episode(3, 1, "The Bumpy Road to Love"), Episode(3, 2, "Arcade"), Episode(3, 3, "Super Karate Monkey Death Car"),
       Episode(3, 4, "Rose Bowl")]
SHOW = al.Show(3968, "NewsRadio", "1995")


def _match(stem, ep, flag="", note="", seconds=1320, path="", margin=0.05, disc="C:/Video/D1"):
    return TranscriptMatch(TitleTranscript(disc, stem, seconds, "words", path), ep, 0.2, margin, flag, note)


def _rows():
    return al.build_rows([
        _match("A1 T00", EPS[0]),
        _match("A1 T01", EPS[1], "CHECK", margin=0.01),
        _match("A1 T02", EPS[2], "CHECK", "another episode scores higher"),
        _match("A1 T03", None, "EXTRA", seconds=120),
    ])


# ---- show, season, drive -------------------------------------------------------------------

def test_show_choice_names_year_and_id():
    assert al.show_choice(SHOW) == "NewsRadio (1995) · TMDB 3968"
    assert al.show_choice(al.Show(1, "X", "")) == "X (?) · TMDB 1"


def test_parse_season():
    assert al.parse_season("") is None
    assert al.parse_season(" 3 ") == 3
    for bad in ("x", "0", "-1", "2.5"):
        with pytest.raises(al.UserError):
            al.parse_season(bad)


def test_pick_drive_prefers_the_one_with_a_disc():
    assert al.pick_drive(["D:", "E:"], lambda d: d == "E:") == "E:"
    assert al.pick_drive(["D:", "E:"], lambda d: False, "e:") == "E:"
    assert al.pick_drive(["D:", "E:"], lambda d: False) == "D:"

    def boom(d):
        raise OSError
    assert al.pick_drive(["D:"], boom) == "D:"


# ---- rip plan ------------------------------------------------------------------------------

def _scan():
    # four 22-minute episodes, a play-all and a menu
    return [DiscTitle(0, 1320), DiscTitle(1, 1325), DiscTitle(2, 1318), DiscTitle(3, 1330),
            DiscTitle(4, 5293), DiscTitle(5, 45)]


def test_plan_rip_marks_play_all_and_extras(tmp_path):
    plan = al.plan_rip(_scan(), "NEWSRADIO_S3D1", SHOW, tmp_path)
    assert [r.rip for r in plan.rows] == [True, True, True, True, False, False]
    assert "play-all" in plan.rows[4].why and "under 10 minutes" in plan.rows[5].why
    assert plan.rows[0].length == "0:22:00"
    assert plan.folder == tmp_path / "NEWSRADIO_S3D1"
    assert plan.already is None and plan.can_rip()
    assert "Will rip 4 of 6 titles" in plan.summary


def test_plan_rip_knows_a_disc_already_ripped_by_its_fingerprint(tmp_path, tmp_config_dir):
    write_disc_info(tmp_path / "SOME_OTHER_NAME", "NewsRadio", 3968,
                    titles=[1320, 1325, 1318, 1330, 5293, 45])
    plan = al.plan_rip(_scan(), "NEWSRADIO_S3D1", SHOW, tmp_path)
    assert plan.already == tmp_path / "SOME_OTHER_NAME"
    assert not plan.can_rip() and plan.can_rip(again=True)
    assert "already ripped" in plan.summary


def test_plan_rip_without_a_label_uses_the_fallback(tmp_path):
    plan = al.plan_rip(_scan(), "", SHOW, tmp_path)
    assert plan.label == "NEWSRADIO_D1"


def test_plan_rip_with_nothing_long_enough(tmp_path):
    plan = al.plan_rip([DiscTitle(0, 100)], "X", SHOW, tmp_path)
    assert not plan.can_rip(again=True) and "nothing to rip" in plan.summary


class FakeScanner:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on

    def rip(self, indices, folder):
        idx = indices[0]
        (Path(folder) / f"A1_t{idx:02d}.mkv").write_text("rip")
        if idx == self.fail_on:
            raise subprocess.CalledProcessError(1, ["makemkvcon"])


class FakeEncoder:
    def __init__(self):
        self.done = []

    def encode(self, src, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("mp4")
        self.done.append(src.name)


def test_run_rip_rips_encodes_and_transcribes_with_short_progress(tmp_path, tmp_config_dir):
    plan = al.plan_rip(_scan(), "NEWSRADIO_S3D1", SHOW, tmp_path)
    said, enc = [], FakeEncoder()

    def transcriber(folder, progress):
        progress("transcribing NEWSRADIO_S3D1/A1 T00 (22.0 min)")
        return [1, 2, 3, 4]
    folder = al.run_rip(plan, FakeScanner(), enc, transcriber, SHOW, 3, progress=said.append)
    info = read_disc_info(folder)
    assert info.complete and info.season == 3 and info.titles == plan.fingerprint
    assert enc.done == ["A1_t00.mkv", "A1_t01.mkv", "A1_t02.mkv", "A1_t03.mkv"]
    assert "Ripping title 2 (3 of 4)" in said
    assert "Encoding 2 of 4 (A1_t01.mkv)" in said
    assert "Transcribing title 1: NEWSRADIO_S3D1/A1 T00 (22.0 min)" in said


def test_run_rip_stopping_part_way_is_a_plain_error(tmp_path, tmp_config_dir):
    plan = al.plan_rip(_scan(), "D1", SHOW, tmp_path)
    with pytest.raises(al.UserError) as e:
        al.run_rip(plan, FakeScanner(fail_on=2), FakeEncoder(), None, SHOW, None)
    assert "stopped at title 2" in str(e.value) and "Finished titles: [0, 1]" in str(e.value)
    assert not read_disc_info(tmp_path / "D1").complete


def test_run_rip_refuses_an_already_ripped_disc(tmp_path, tmp_config_dir):
    write_disc_info(tmp_path / "D1", "NewsRadio", 3968, titles=[t.duration_secs for t in _scan()])
    plan = al.plan_rip(_scan(), "D1", SHOW, tmp_path)
    with pytest.raises(al.UserError):
        al.run_rip(plan, FakeScanner(), FakeEncoder(), None, SHOW, None)


# ---- the table -----------------------------------------------------------------------------

def test_episode_labels_and_choices():
    assert al.episode_label(EPS[0]) == "S03E01 The Bumpy Road to Love"
    assert al.episode_label(None) == "none"
    choices = al.episode_choices(EPS)
    assert choices[0] == "none" and choices[1] == "S03E01 The Bumpy Road to Love" and len(choices) == 5
    assert al.episode_from_label("S03E02 Arcade", EPS) is EPS[1]
    assert al.episode_from_label("none", EPS) is None


def test_check_rows_start_unticked_and_say_why():
    rows = _rows()
    views = al.row_views(rows)
    assert [v.include for v in views] == [True, False, False, False]
    assert [v.flag for v in views] == ["", "CHECK", "CHECK", "EXTRA"]
    assert [v.highlight for v in views] == ["", "check", "check", "extra"]
    assert "close call" in views[1].note
    assert views[2].note == "another episode scores higher"
    assert "extra" in views[3].note
    assert views[0].disc == "D1" and views[0].minutes == "22.0" and views[0].score == "0.200"
    assert views[3].episode == "none" and views[3].score == ""


def test_changing_the_episode_marks_fixed_and_ticks():
    rows = _rows()
    al.set_episode(rows, 2, "S03E04 Rose Bowl", EPS)
    v = al.row_views(rows)[1]
    assert v.include and v.flag == "FIXED" and v.highlight == "fixed" and v.note == "set by hand"
    assert v.score == ""  # the scores were for the matcher's pick
    al.set_episode(rows, 4, "S03E03 Super Karate Monkey Death Car", EPS)  # an extra can be given one
    assert rows[3].will_act
    al.set_episode(rows, 1, "none", EPS)
    assert not rows[0].will_act and al.row_views(rows)[0].flag == "FIXED"


def test_set_include():
    rows = _rows()
    assert al.set_include(rows, 2, True) is None and rows[1].will_act
    assert al.set_include(rows, 1, False) is None and not rows[0].will_act
    assert "no episode" in al.set_include(rows, 4, True)
    assert not rows[3].will_act


def test_duplicates_are_named_and_block_the_copy():
    rows = _rows()
    assert al.duplicate_problem(rows) == ""
    al.set_episode(rows, 2, "S03E01 The Bumpy Road to Love", EPS)
    msg = al.duplicate_problem(rows)
    assert "rows 1 and 2 are both S03E01 The Bumpy Road to Love" in msg and "before copying" in msg
    assert "before renaming" in al.duplicate_problem(rows, "renaming")
    views = al.row_views(rows)
    assert views[0].highlight == "duplicate" and views[0].note == "same episode as row 2"
    al.set_include(rows, 2, False)  # unticked rows do not count
    assert al.duplicate_problem(rows) == ""


def test_unused_episodes_and_summary():
    rows = _rows()
    assert [e.number for e in al.unused_episodes(rows, EPS)] == [2, 3, 4]
    assert al.unused_text(rows, EPS).startswith("Episodes with no title: S03E02 Arcade")
    assert al.match_summary(al.MatchResult(SHOW, rows, EPS, EPS)) == \
        "4 titles, 1 ticked, 2 to check (highlighted, unticked), 1 extras."


# ---- match and copy ------------------------------------------------------------------------

def _disc(root, name, season=None, complete=True, n=2):
    d = root / name
    write_disc_info(d, "NewsRadio", 3968, complete=complete, season=season)
    return d


def test_run_match_gathers_the_discs_and_builds_rows(tmp_path, tmp_config_dir):
    _disc(tmp_path, "S3D2", 3)
    _disc(tmp_path, "S3D1", 3)
    _disc(tmp_path, "S4D1", 4)
    _disc(tmp_path, "BROKEN", 3, complete=False)
    seen, said = [], []

    def transcriber(folder, progress):
        seen.append(folder.name)
        return [TitleTranscript(str(folder), f"T{folder.name}", 1320, "words")]

    def matcher(titles, episodes, regulars):
        assert regulars == ["Dave"] and [e.season for e in episodes] == [3, 3, 3, 3]
        return [TranscriptMatch(t, episodes[i], 0.2, 0.05, "") for i, t in enumerate(titles)]

    result = al.run_match(SHOW, 3, tmp_path, lambda s: (EPS + [Episode(4, 1, "Sweeps")], ["Dave"]),
                          transcriber, matcher, progress=said.append)
    assert seen == ["S3D1", "S3D2"]
    assert len(result.rows) == 2 and len(result.all_episodes) == 5 and len(result.episodes) == 4
    assert any("Leaving out BROKEN" in s for s in said)


def test_run_match_with_no_discs(tmp_path, tmp_config_dir):
    with pytest.raises(al.UserError):
        al.run_match(SHOW, None, tmp_path, lambda s: (EPS, []), None, None)


def test_copy_items_and_run_copy(tmp_path, tmp_config_dir):
    srcs = []
    for n in range(2):
        p = tmp_path / "disc" / "MP4" / f"A1 T0{n}.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        srcs.append(str(p))
    rows = al.build_rows([_match("A1 T00", EPS[0], path=srcs[0]), _match("A1 T01", EPS[1], path=srcs[1])])
    root = (tmp_path / "nas").as_posix()
    items = al.copy_items(rows, SHOW, root)
    assert items[0].dest == f"{root}/NewsRadio (1995)/Season 3/NewsRadio S03 E01.mp4"
    said = []
    summary = al.run_copy(items, progress=said.append)
    assert said == ["Copying 1 of 2: NewsRadio S03 E01.mp4", "Copying 2 of 2: NewsRadio S03 E02.mp4"]
    assert al.copy_summary_text(summary) == "2 copied, 0 already there, 0 failed."
    assert al.copy_summary_text(al.run_copy(items)) == "0 copied, 2 already there, 0 failed."


# ---- fix -----------------------------------------------------------------------------------

def _nas(tmp_path):
    root = tmp_path / "TV"
    season = root / "NewsRadio (1995)" / "Season 03"
    season.mkdir(parents=True)
    for n in (1, 2):
        (season / f"NewsRadio S03 E0{n}.mp4").write_bytes(b"x" * n)
    (root / "Other Show").mkdir()
    return root


def test_list_nas_shows_and_name_from_folder(tmp_path):
    root = _nas(tmp_path)
    assert al.list_nas_shows(str(root)) == ["NewsRadio (1995)", "Other Show"]
    assert al.name_from_folder("Arrested Development (2003)") == "Arrested Development"
    with pytest.raises(al.UserError):
        al.list_nas_shows(str(tmp_path / "missing"))


def test_fix_match_and_rename_swap(tmp_path, tmp_config_dir):
    root = _nas(tmp_path)

    def file_transcriber(videos, cache_dir, disc, progress, check_source):
        assert check_source and str(cache_dir).startswith(str(tmp_config_dir))
        return [TitleTranscript(disc, v.stem, 1320, "w", str(v)) for v in videos]

    def matcher(titles, episodes, regulars):  # the files are in each other's places
        return [TranscriptMatch(titles[0], episodes[1], 0.2, 0.05, ""),
                TranscriptMatch(titles[1], episodes[0], 0.2, 0.05, "")]

    result = al.run_fix_match(SHOW, "NewsRadio (1995)", 3, root.as_posix(), lambda s: (EPS[:2], []),
                              file_transcriber, matcher)
    moves, right = al.rename_moves(result)
    assert right == 0 and moves[0][1].endswith("/Season 03/NewsRadio S03 E02.mp4")
    out = al.run_rename(result)
    assert out.renamed == 2
    season = root / "NewsRadio (1995)" / "Season 03"
    assert (season / "NewsRadio S03 E01.mp4").read_bytes() == b"xx"
    assert (season / "NewsRadio S03 E02.mp4").read_bytes() == b"x"


def test_fix_match_refuses_dotdot(tmp_path):
    with pytest.raises(al.UserError):
        al.run_fix_match(SHOW, "../x", None, tmp_path.as_posix(), None, None, None)


# ---- errors --------------------------------------------------------------------------------

def test_friendly_errors():
    assert al.friendly_error(al.UserError("plain")) == "plain"
    assert al.friendly_error(RenameError("stuck")) == "stuck"
    msg = al.friendly_error(subprocess.CalledProcessError(3, ["C:/x/HandBrakeCLI.exe", "-i"]))
    assert msg == "HandBrakeCLI stopped with an error (exit code 3)."
    assert "Could not find" in al.friendly_error(FileNotFoundError(2, "nope", "makemkvcon64.exe"))
    import requests
    assert "Could not reach TMDB" in al.friendly_error(requests.ConnectionError("x"))
    assert al.friendly_error(ValueError("odd")).startswith("Something went wrong")
