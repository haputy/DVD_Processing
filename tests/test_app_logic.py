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


# ---- review fixes: show pick, folder check, confirm text, rip safety ------------------------

def test_auto_pick_follows_the_cli_yes_rule():
    ne = [al.Show(1, "Northern Exposure", "1990"), al.Show(2, "Northern Exposure", "2023")]
    assert al.auto_pick(ne, "northern exposure") is None          # two exact names
    assert al.auto_pick([SHOW], " newsradio ") == 0
    assert al.auto_pick([al.Show(5, "NewsRadio Live", "")], "NewsRadio") is None  # not exact
    assert al.auto_pick([al.Show(5, "Other", ""), SHOW], "NewsRadio") is None     # exact but not first
    assert al.auto_pick([], "x") is None
    assert al.pick_prompt(2) == "(pick one of 2 shows)" and al.pick_prompt(1) == "(pick the show)"


def test_folder_matches_show():
    assert al.folder_matches_show("NewsRadio (1995)", "NewsRadio")
    assert al.folder_matches_show("Mr. Show (1995)", "Mr Show")
    assert not al.folder_matches_show("3rd Rock from the Sun (1996)", "NewsRadio")


def test_confirm_texts_count_ticked_check_rows():
    rows = _rows()
    items = [workflow_item()]
    assert "CHECK" not in al.copy_confirm_text(items, "//nas/TV", rows)
    al.set_include(rows, 2, True)                       # a CHECK row ticked as it is
    assert "1 ticked row is still flagged CHECK" in al.copy_confirm_text(items, "//nas/TV", rows)
    al.set_include(rows, 3, True)
    assert "2 ticked rows are still flagged CHECK" in al.rename_confirm_text([("a", "b")], 0, "//nas/TV/X", rows)
    al.set_episode(rows, 2, "S03E04 Rose Bowl", EPS)    # a picked episode is FIXED, no longer CHECK
    assert al.still_check(rows) == 1


def workflow_item():
    from dvd_processor.workflow import CopyItem
    return CopyItem(1, "C:/Video/D1/MP4/A1 T00.mp4", "//nas/TV/NewsRadio (1995)/Season 3/NewsRadio S03 E01.mp4")


def test_rip_plan_keeps_the_show_and_rip_refuses_another(tmp_path, tmp_config_dir):
    plan = al.plan_rip(_scan(), "D1", SHOW, tmp_path)
    assert plan.show is SHOW
    with pytest.raises(al.UserError) as e:
        al.run_rip(plan, FakeScanner(), FakeEncoder(), None, al.Show(4499, "Northern Exposure", "1990"), None)
    assert "Scan the disc again" in str(e.value)
    assert not (tmp_path / "D1").exists()


def test_rip_ejects_after_the_last_title_and_before_encoding(tmp_path, tmp_config_dir):
    plan = al.plan_rip(_scan(), "D1", SHOW, tmp_path)
    said = []

    def eject():
        said.append("EJECT")
        return True
    al.run_rip(plan, FakeScanner(), FakeEncoder(), lambda f, progress: [], SHOW, None,
               progress=said.append, eject=eject)
    i = said.index("EJECT")
    assert said[i - 1] == "Ripping title 3 (4 of 4)"
    assert said[i + 1] == al.EJECTED
    assert said[i + 2].startswith("Encoding 1 of 4")


def test_cancelled_rip_keeps_the_partial_as_failed_and_stays_incomplete(tmp_path, tmp_config_dir):
    from dvd_processor.procs import Cancelled

    class StoppedScanner(FakeScanner):
        def rip(self, indices, folder):
            (Path(folder) / f"A1_t{indices[0]:02d}.mkv").write_text("half")
            if indices[0] == 1:
                raise Cancelled()
    plan = al.plan_rip(_scan(), "D1", SHOW, tmp_path)
    ejected = []
    with pytest.raises(Cancelled) as e:
        al.run_rip(plan, StoppedScanner(), FakeEncoder(), None, SHOW, None, eject=lambda: ejected.append(1))
    assert "stopped at title 1" in str(e.value)
    assert (tmp_path / "D1" / "A1_t01.mkv.failed").exists()
    assert not read_disc_info(tmp_path / "D1").complete
    assert ejected == []
    assert al.friendly_error(e.value).startswith("The rip was stopped")


def test_run_copy_stops_when_asked(tmp_path, tmp_config_dir):
    from dvd_processor.procs import Cancelled
    src = tmp_path / "A1 T00.mp4"
    src.write_bytes(b"x" * 10)
    rows = al.build_rows([_match("A1 T00", EPS[0], path=str(src))])
    items = al.copy_items(rows, SHOW, (tmp_path / "nas").as_posix())
    with pytest.raises(Cancelled):
        al.run_copy(items, should_stop=lambda: True)
    assert not list((tmp_path / "nas").rglob("*.part"))
    assert not Path(items[0].dest).exists()


# ---- second review: stop between titles, encode before matching, MKV warning ----------------

def test_auto_pick_sees_a_second_exact_name_beyond_the_first_15():
    results = [SHOW] + [al.Show(i, f"Other {i}", "") for i in range(20)] + [al.Show(77, "NewsRadio", "2030")]
    assert al.auto_pick(results, "NewsRadio") is None
    assert al.auto_pick(results[:15], "NewsRadio") == 0  # why it must run before the cut


def test_a_stop_between_titles_is_handled_like_one_mid_title(tmp_path, tmp_config_dir):
    from dvd_processor.procs import Cancelled
    plan = al.plan_rip(_scan(), "D1", SHOW, tmp_path)
    calls = []

    def progress(msg):
        if msg.startswith("Ripping title"):
            calls.append(msg)
            if len(calls) == 3:  # Cancel pressed while title 1 was finishing
                raise Cancelled()
    with pytest.raises(Cancelled) as e:
        al.run_rip(plan, FakeScanner(), FakeEncoder(), None, SHOW, None, progress=progress)
    msg = str(e.value)
    assert "Finished titles: [0, 1]" in msg and "Rip again" in msg and "marked incomplete" in msg
    assert not read_disc_info(tmp_path / "D1").complete
    assert sorted(p.name for p in (tmp_path / "D1").glob("*.mkv")) == ["A1_t00.mkv", "A1_t01.mkv"]


def _mkv_disc(root, name, n=2, encoded=0):
    d = root / name
    write_disc_info(d, "NewsRadio", 3968)
    for i in range(n):
        (d / f"A1_t0{i}.mkv").write_text("rip")
    (d / "MP4").mkdir()
    for i in range(encoded):
        (d / "MP4" / f"A1 T0{i}.mp4").write_text("mp4")
    return d


def test_encode_missing_encodes_only_what_has_no_mp4(tmp_path, tmp_config_dir):
    d1 = _mkv_disc(tmp_path, "D1", 2, encoded=1)
    d2 = _mkv_disc(tmp_path, "D2", 1, encoded=1)
    enc, said = FakeEncoder(), []
    assert al.encode_missing([d1, d2], enc, said.append) == 1
    assert enc.done == ["A1_t01.mkv"]
    assert said == ["D1: 1 titles have no MP4 yet, encoding them first", "Encoding 1 of 1 (A1_t01.mkv)"]
    assert al.encode_missing([d1, d2], None) == 0  # nothing left to do needs no encoder


def test_encode_missing_failure_stops_with_the_disc_named(tmp_path, tmp_config_dir):
    d1 = _mkv_disc(tmp_path, "D1", 1)

    class Broken:
        def encode(self, src, out):
            raise subprocess.CalledProcessError(2, ["HandBrakeCLI.exe"])
    with pytest.raises(al.UserError) as e:
        al.encode_missing([d1], Broken())
    assert "Encoding D1 failed: HandBrakeCLI stopped with an error (exit code 2)." in str(e.value)
    assert "Nothing was matched or copied" in str(e.value)
    with pytest.raises(al.UserError):
        al.encode_missing([d1], None)


def test_run_match_encodes_before_transcribing(tmp_path, tmp_config_dir):
    _mkv_disc(tmp_path, "D1", 1)
    order = []

    class Enc(FakeEncoder):
        def encode(self, src, out):
            order.append("encode")
            super().encode(src, out)

    def transcriber(folder, progress):
        order.append("transcribe")
        return [TitleTranscript(str(folder), "A1 T00", 1320, "w", str(folder / "MP4" / "A1 T00.mp4"))]

    def matcher(titles, episodes, regulars):
        return [TranscriptMatch(titles[0], episodes[0], 0.2, 0.05, "")]
    al.run_match(SHOW, None, tmp_path, lambda s: (EPS, []), transcriber, matcher, encoder=Enc())
    assert order == ["encode", "transcribe"]


def test_copying_an_mkv_is_said_first():
    from dvd_processor.workflow import CopyItem
    mp4 = CopyItem(1, "C:/Video/D1/MP4/A1 T00.mp4", "//nas/TV/S/Season 1/S S01 E01.mp4")
    mkv = CopyItem(2, "C:/Video/D1/A1_t01.mkv", "//nas/TV/S/Season 1/S S01 E02.mkv")
    assert al.mkv_note([mp4]) == ""
    note = al.mkv_note([mp4, mkv])
    assert "1 of these is an MKV rip" in note and "A1_t01.mkv" in note
    assert note in al.copy_confirm_text([mp4, mkv], "//nas/TV", [])
