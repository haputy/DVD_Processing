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
    assert al.pick_prompt(2) == "(pick one of 2)" and al.pick_prompt(1) == "(pick it from the list)"


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
    assert "Finished titles: [0, 1]" in msg and "Go again" in msg and "marked incomplete" in msg
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


# ---- Go: one disc from the drive to the NAS ------------------------------------------------

class GoScanner(FakeScanner):
    def __init__(self, titles=None, label="NEWSRADIO_S3D1", fail_on=None):
        super().__init__(fail_on)
        self.titles, self.label, self.ripped = _scan() if titles is None else titles, label, []

    def scan_with_label(self):
        return self.titles, self.label

    def rip(self, indices, folder):
        self.ripped += indices
        super().rip(indices, folder)


class SizedEncoder(FakeEncoder):
    """MP4s of different sizes per title, and different bytes per disc, as real encodes are."""

    def encode(self, src, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        tag = src.parent.name.encode()  # discs differ in content even where sizes agree
        out.write_bytes(tag + b"x" * (1000 + int(src.stem[-2:]) - len(tag)))
        self.done.append(src.name)


def go_transcriber(folder, progress=None):
    return [TitleTranscript(str(folder), p.stem, 1320, "words", str(p))
            for p in sorted((Path(folder) / "MP4").glob("*.mp4"))]


def go_matcher(flags=None, picks=None):
    """In-order matches; flags[i] flags title i, picks[i] gives it episodes[picks[i]]."""
    seen = {}

    def matcher(titles, episodes, regulars):
        seen["titles"], seen["episodes"] = [t.stem for t in titles], [(e.season, e.number) for e in episodes]
        out = []
        for i, t in enumerate(titles):
            j = (picks or {}).get(i, i)
            ep = episodes[j] if j < len(episodes) else None
            out.append(TranscriptMatch(t, ep, 0.2, 0.05, (flags or {}).get(i, "") if ep else "CHECK"))
        return out
    matcher.seen = seen
    return matcher


def _go(tmp_path, scanner=None, matcher=None, season=3, episodes=EPS, ejected=None, said=None):
    nas = tmp_path / "nas"
    nas.mkdir(exist_ok=True)
    return al.run_go(SHOW, season, scanner or GoScanner(), SizedEncoder(), go_transcriber,
                     lambda s: (episodes, ["Dave"]), matcher or go_matcher(), tmp_path / "Video",
                     nas.as_posix(), progress=(said.append if said is not None else al._quiet),
                     eject=(lambda: ejected.append(1) or True) if ejected is not None else None)


def _season3(tmp_path):
    return tmp_path / "nas" / "NewsRadio (1995)" / "Season 3"


def test_go_takes_a_clean_disc_all_the_way_to_the_nas(tmp_path, tmp_config_dir):
    ejected, said = [], []
    r = _go(tmp_path, ejected=ejected, said=said)
    assert r.problem == "" and len(r.summary.copied) == 4 and not r.summary.failed
    assert sorted(p.name for p in _season3(tmp_path).iterdir()) == [
        "NewsRadio S03 E01.mp4", "NewsRadio S03 E02.mp4", "NewsRadio S03 E03.mp4", "NewsRadio S03 E04.mp4"]
    assert read_disc_info(r.folder).complete and ejected == [1]
    assert "4 copied, 0 already there, 0 failed." in al.go_text(r)
    assert "Copying 4 of 4: NewsRadio S03 E04.mp4" in said


def test_go_copies_nothing_when_a_match_is_check(tmp_path, tmp_config_dir):
    r = _go(tmp_path, matcher=go_matcher(flags={2: "CHECK"}))
    assert "flagged CHECK" in r.problem and r.summary is None
    assert not (tmp_path / "nas" / "NewsRadio (1995)").exists()
    # the review table: sure rows ticked, the CHECK row unticked for Andy to look at
    assert [row.will_act for row in r.match.rows] == [True, True, False, True]
    assert "stopped for review" in al.go_text(r)


def test_go_copies_nothing_when_two_titles_get_one_episode(tmp_path, tmp_config_dir):
    r = _go(tmp_path, matcher=go_matcher(picks={1: 0}))
    assert "share an episode" in r.problem and r.summary is None
    assert not (tmp_path / "nas" / "NewsRadio (1995)").exists()


def test_go_copies_nothing_when_titles_outnumber_episodes(tmp_path, tmp_config_dir):
    titles = [DiscTitle(i, 1320 + i) for i in range(8)]  # 8 titles, 5 episodes: over by more than 2
    r = _go(tmp_path, scanner=GoScanner(titles), matcher=go_matcher(),
            episodes=EPS + [Episode(3, 5, "Five")])
    assert "more titles than episodes" in r.problem
    assert not (tmp_path / "nas" / "NewsRadio (1995)").exists()


def test_go_again_with_the_same_disc_rips_nothing_and_copies_nothing_twice(tmp_path, tmp_config_dir):
    _go(tmp_path)
    scanner, matcher = GoScanner(), go_matcher()
    r = _go(tmp_path, scanner=scanner, matcher=matcher)
    assert scanner.ripped == [] and matcher.seen == {}
    assert r.match is None and len(r.on_nas) == 4
    assert "Nothing left to copy" in al.go_text(r)
    assert len(list(_season3(tmp_path).iterdir())) == 4


def test_go_leaves_episodes_already_on_the_nas_out_of_the_match(tmp_path, tmp_config_dir):
    other = tmp_path / "nas" / "NewsRadio (1995)" / "Season 03"
    other.mkdir(parents=True)
    (other / "NewsRadio S03E02.mp4").write_bytes(b"old")  # another naming, another size
    titles = [DiscTitle(0, 1320), DiscTitle(1, 1325), DiscTitle(2, 1318)]
    matcher = go_matcher()
    r = _go(tmp_path, scanner=GoScanner(titles), matcher=matcher)
    assert matcher.seen["episodes"] == [(3, 1), (3, 3), (3, 4)]
    assert r.problem == "" and (other / "NewsRadio S03E02.mp4").read_bytes() == b"old"
    assert sorted(p.name for p in _season3(tmp_path).iterdir()) == [
        "NewsRadio S03 E01.mp4", "NewsRadio S03 E03.mp4", "NewsRadio S03 E04.mp4"]


def test_go_never_overwrites_what_appeared_on_the_nas(tmp_path, tmp_config_dir, monkeypatch):
    # an episode not seen by the NAS check (it arrived during the match) is still left alone
    real = al.nas_show_state
    monkeypatch.setattr(al, "nas_show_state", lambda show, root: (real(show, root), _season3(tmp_path).mkdir(
        parents=True), (_season3(tmp_path) / "NewsRadio S03 E01.mp4").write_bytes(b"theirs"))[0])
    r = _go(tmp_path)
    assert len(r.summary.already_there) == 1 and len(r.summary.copied) == 3
    assert (_season3(tmp_path) / "NewsRadio S03 E01.mp4").read_bytes() == b"theirs"


def test_go_after_an_interrupted_rip_rips_again_into_a_new_folder(tmp_path, tmp_config_dir):
    with pytest.raises(al.UserError):
        _go(tmp_path, scanner=GoScanner(fail_on=2))
    scanner = GoScanner()
    r = _go(tmp_path, scanner=scanner)
    assert scanner.ripped == [0, 1, 2, 3]
    assert r.folder.name == "NEWSRADIO_S3D1_2" and len(r.summary.copied) == 4


def test_go_after_the_nas_was_away_carries_on_without_ripping(tmp_path, tmp_config_dir):
    nas = tmp_path / "nas"
    with pytest.raises(al.UserError) as e:  # no NAS folder at all: unreachable
        al.run_go(SHOW, 3, GoScanner(), SizedEncoder(), go_transcriber, lambda s: (EPS, []), go_matcher(),
                  tmp_path / "Video", nas.as_posix())
    assert "press Go again" in str(e.value)
    scanner = GoScanner()
    r = _go(tmp_path, scanner=scanner, season=None)  # the season comes from disc.json
    assert scanner.ripped == [] and len(r.summary.copied) == 4
    assert r.folder.name == "NEWSRADIO_S3D1"


def test_go_with_a_disc_that_has_no_episodes(tmp_path, tmp_config_dir):
    with pytest.raises(al.UserError):
        _go(tmp_path, scanner=GoScanner([DiscTitle(0, 100)]))
    with pytest.raises(al.UserError):
        _go(tmp_path, scanner=GoScanner([]))


def test_go_rip_only_rips_and_ejects_so_the_drive_is_free_sooner(tmp_path, tmp_config_dir):
    said, ejected = [], []
    ripped = al.go_rip(SHOW, 3, GoScanner(), tmp_path / "Video", progress=said.append,
                       eject=lambda: ejected.append(1) or True)
    assert ejected == [1] and al.EJECTED in said
    assert read_disc_info(ripped.folder).complete
    assert not (ripped.folder / "MP4").exists()  # encoding waits for go_finish


def test_two_discs_ripped_back_to_back_finish_one_after_the_other(tmp_path, tmp_config_dir):
    (tmp_path / "nas").mkdir()
    eps = EPS + [Episode(3, 5, "Five"), Episode(3, 6, "Six")]
    disc1 = al.go_rip(SHOW, 3, GoScanner([DiscTitle(0, 1320), DiscTitle(1, 1325), DiscTitle(2, 1318)], "D1"),
                      tmp_path / "Video")
    disc2 = al.go_rip(SHOW, 3, GoScanner([DiscTitle(0, 1321), DiscTitle(1, 1330), DiscTitle(2, 1319)], "D2"),
                      tmp_path / "Video")
    nas = (tmp_path / "nas").as_posix()

    def finish(ripped, matcher):
        return al.go_finish(ripped, SizedEncoder(), go_transcriber, lambda s: (eps, []), matcher, nas)
    first, second = go_matcher(), go_matcher()
    assert len(finish(disc1, first).summary.copied) == 3
    assert len(finish(disc2, second).summary.copied) == 3
    assert first.seen["episodes"] == [(3, n) for n in range(1, 7)]
    assert second.seen["episodes"] == [(3, 4), (3, 5), (3, 6)]  # what disc 1 copied is out
    assert len(list(_season3(tmp_path).iterdir())) == 6


# ---- titles far off the disc's episode length ----------------------------------------------

def _t(stem, minutes, disc="C:/Video/D1", text="words"):
    return TitleTranscript(disc, stem, minutes * 60, text)


def test_episode_length_is_the_largest_group_of_like_lengths():
    # Northern Exposure season 2 disc 2: two episodes, a play-all, three featurettes
    mins = [94.7, 47.2, 47.5, 14.5, 11.1, 15.5]
    assert al.episode_length([m * 60 for m in mins]) == 47.5 * 60
    assert al.episode_length([47 * 60]) is None            # nothing to compare with
    assert al.episode_length([47 * 60, 5 * 60]) is None    # under 10 minutes does not count


def test_titles_far_off_the_episode_length_are_extras_and_never_matched():
    titles = [_t("B4 T02", 94.7), _t("C1 T00", 47.2), _t("D1 T01", 47.5), _t("E3 T05", 14.5),
              _t("X", 5)]
    seen = []

    def matcher(ts, episodes, regulars):
        seen.extend(t.stem for t in ts)
        return [TranscriptMatch(t, episodes[i] if t.seconds >= 600 else None, 0.2, 0.05,
                                "" if t.seconds >= 600 else "EXTRA") for i, t in enumerate(ts)]
    rows = al.build_rows(al.match_by_length(titles, EPS, [], matcher))
    assert seen == ["C1 T00", "D1 T01", "X"]
    assert [r.match.title.stem for r in rows] == ["B4 T02", "C1 T00", "D1 T01", "E3 T05", "X"]
    assert [r.match.flag for r in rows] == ["EXTRA", "", "", "EXTRA", "EXTRA"]
    assert "much longer" in al.row_note(rows[0]) and "play-all" in al.row_note(rows[0])
    assert "much shorter" in al.row_note(rows[3]) and "about 48 min" in al.row_note(rows[3])
    assert al.row_note(rows[4]) == "under 10 minutes, probably an extra"
    # an extra is copied only once its episode is picked by hand
    assert al.set_include(rows, 4, True) is not None
    al.set_episode(rows, 4, al.episode_label(EPS[3]), EPS)
    assert rows[3].will_act


def test_each_disc_is_judged_on_its_own_titles():
    titles = [_t("A", 46, "D1"), _t("B", 46, "D1"), _t("C", 22, "D2"), _t("D", 23, "D2")]
    assert al.length_flags(titles) == {}


def test_go_never_copies_a_short_title_on_its_own(tmp_path, tmp_config_dir):
    # disc 6 of Northern Exposure season 3: three episodes and a 12.8-minute extra
    titles = [DiscTitle(0, 2742), DiscTitle(1, 2760), DiscTitle(2, 2738), DiscTitle(10, 768)]

    def transcriber(folder, progress=None):
        out = go_transcriber(folder)
        for t, d in zip(out, sorted(titles, key=lambda d: d.index)):
            t.seconds = d.duration_secs
        return out
    nas = tmp_path / "nas"
    nas.mkdir()
    r = al.run_go(SHOW, 3, GoScanner(titles), SizedEncoder(), transcriber, lambda s: (EPS, []), go_matcher(),
                  tmp_path / "Video", nas.as_posix())
    assert r.problem == "" and len(r.summary.copied) == 3
    assert [row.match.flag for row in r.match.rows].count("EXTRA") == 1


# ---- why a row was matched -----------------------------------------------------------------

SLOW = Episode(2, 7, "Slow Dance", "A falling satellite hits Rick.",
               "A satellite plunges out of orbit and crashes into Rick. Guest star Someone Famous")
BIG_KISS = Episode(2, 2, "The Big Kiss", "Chris looks for his muse.", "Chris kisses a woman and loses his voice.")


def test_shared_words_put_the_telling_ones_first_and_drop_the_regulars():
    said = "The satellite fell. Rick was under the satellite. Maggie cried. Chris lost his voice."
    words = al.shared_words(said, SLOW, [SLOW, BIG_KISS], regulars=["Maggie O'Connell", "Rick"])
    assert words[0] == "satellite" and "rick" not in words and "maggie" not in words
    assert al.shared_words(said, BIG_KISS, [SLOW, BIG_KISS], regulars=["Chris Stevens"]) == ["voice"]


def test_dialogue_lines_pick_the_sentences_with_the_words_in_order():
    said = "Hello there. The satellite came down. Nothing here. A satellite in orbit, imagine. Bye."
    assert al.dialogue_lines(said, ["satellite", "orbit"], n=2) == [
        "The satellite came down.", "A satellite in orbit, imagine."]
    assert al.dialogue_lines(said, []) == []


def test_row_detail_shows_both_descriptions_and_the_dialogue():
    title = TitleTranscript("C:/Video/NE_11", "C1 T00", 2832, "Rick is gone. The satellite hit him. Dance with me.")
    row = al.build_rows([TranscriptMatch(title, SLOW, 0.031, 0.008, "CHECK", "", BIG_KISS)])[0]
    text = al.row_detail(row, [SLOW, BIG_KISS], regulars=["Rick"])
    assert text.startswith("NE_11/C1 T00, 47.2 min. Score 0.031, margin 0.008. CHECK: close call")
    assert "Picked: S02E07 Slow Dance" in text and "Runner-up: S02E02 The Big Kiss" in text
    assert "TMDB: A falling satellite hits Rick." in text
    assert "Wikipedia: A satellite plunges out of orbit" in text and "Someone Famous" not in text
    assert "Words in common with the dialogue: dance, satellite" in text
    assert '"The satellite hit him."' in text
    # after a pick by hand the matcher's own pick is still shown
    al.set_episode([row], 1, al.episode_label(BIG_KISS), [SLOW, BIG_KISS])
    text = al.row_detail(row, [SLOW, BIG_KISS])
    assert "Set by hand: S02E02 The Big Kiss" in text and "The matcher's pick: S02E07 Slow Dance" in text


def test_row_detail_for_an_extra_quotes_the_start_of_the_dialogue():
    row = al.build_rows([TranscriptMatch(_t("E3 T05", 14.5, text="This is Maurice. A crime wave."), None, 0, 0,
                                         "EXTRA", "14.5 min, much shorter")])[0]
    text = al.row_detail(row, [SLOW])
    assert "EXTRA: 14.5 min, much shorter" in text and "From the dialogue (start): This is Maurice." in text


# ---- movies --------------------------------------------------------------------------------

BRAZIL = al.Show(68, "Brazil", "1985", kind=al.MOVIE)


def _movie_disc():
    # the film, a trailer, two featurettes
    return [DiscTitle(0, 8520), DiscTitle(1, 150), DiscTitle(2, 1500), DiscTitle(3, 900)]


def _movie_go(tmp_path, titles=None, pick=None, fingerprint=None, ejected=None):
    scanner = GoScanner(titles or _movie_disc(), "BRAZIL")
    ripped = al.go_rip_movie(BRAZIL, scanner, tmp_path / "Video", eject=(lambda: ejected.append(1) or True)
                             if ejected is not None else None, pick=pick, pick_fingerprint=fingerprint)
    return scanner, ripped


def test_a_movie_rips_only_the_feature_and_lands_in_its_own_folder(tmp_path, tmp_config_dir):
    ejected = []
    scanner, ripped = _movie_go(tmp_path, ejected=ejected)
    assert scanner.ripped == [0] and ejected == [1]
    assert [r.rip for r in ripped.plan.rows] == [True, False, False, False]
    info = read_disc_info(ripped.folder)
    assert info.kind == "movie" and info.complete and info.tmdb_id == 68
    movies = tmp_path / "Movies"
    movies.mkdir()
    r = al.go_finish_movie(ripped, SizedEncoder(), movies.as_posix())
    assert r.status == "copied"
    assert (movies / "Brazil (1985)" / "Brazil (1985).mp4").exists()
    assert "copied to" in al.movie_text(r)


def test_a_movie_already_in_the_library_is_never_copied_again(tmp_path, tmp_config_dir):
    movies = tmp_path / "Movies"
    (movies / "Brazil (1985)").mkdir(parents=True)
    (movies / "Brazil (1985)" / "Brazil.1985.mkv").write_bytes(b"theirs")  # another name
    _, ripped = _movie_go(tmp_path)
    r = al.go_finish_movie(ripped, SizedEncoder(), movies.as_posix())
    assert r.status == "exists" and "already in the Movies library" in al.movie_text(r)
    assert sorted(p.name for p in (movies / "Brazil (1985)").iterdir()) == ["Brazil.1985.mkv"]


def test_go_again_with_a_ripped_movie_does_not_rip_it_again(tmp_path, tmp_config_dir):
    _, first = _movie_go(tmp_path)
    scanner, again = _movie_go(tmp_path)
    assert scanner.ripped == [] and again.folder == first.folder


def test_two_cuts_of_the_film_wait_for_a_pick(tmp_path, tmp_config_dir):
    titles = [DiscTitle(0, 8520), DiscTitle(1, 9420), DiscTitle(2, 900)]   # theatrical, director's cut
    scanner, pick = _movie_go(tmp_path, titles)
    assert isinstance(pick, al.MoviePick) and scanner.ripped == []
    assert [t.index for t in pick.candidates] == [0, 1]
    assert "pick one" in pick.plan.rows[0].why and "pick one" in pick.plan.rows[1].why
    scanner, ripped = _movie_go(tmp_path, titles, pick=0, fingerprint=pick.plan.fingerprint)
    assert scanner.ripped == [0] and isinstance(ripped, al.Ripped)


def test_a_pick_from_another_disc_is_ignored(tmp_path, tmp_config_dir):
    titles = [DiscTitle(0, 8520), DiscTitle(1, 9420)]
    _, pick = _movie_go(tmp_path, titles, pick=1, fingerprint=[100, 200])
    assert isinstance(pick, al.MoviePick)


def test_a_movie_folder_is_never_a_disc_of_a_show_with_the_same_tmdb_id(tmp_path, tmp_config_dir):
    write_disc_info(tmp_path / "MOVIE", "Something", 3968, kind="movie", titles=[t.duration_secs for t in _scan()])
    plan = al.plan_rip(_scan(), "NEWSRADIO_S3D1", SHOW, tmp_path)   # NewsRadio is TMDB 3968 too
    assert plan.already is None
    from dvd_processor.discinfo import disc_folders_for_show
    assert disc_folders_for_show("NewsRadio", 3968, tmp_path) == []


def test_movie_destination_naming():
    from dvd_processor.nas import movie_dest
    assert movie_dest("Brazil", "1985") == "//DXP2800-4CC5/Jellyfin/Movies/Brazil (1985)/Brazil (1985).mp4"
    assert movie_dest("Star Wars: A New Hope", "1977").endswith("/Star Wars A New Hope (1977).mp4")
