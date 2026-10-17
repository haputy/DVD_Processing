from dvd_processor.disc import DiscTitle
from dvd_processor.discinfo import read_disc_info, write_disc_info
from dvd_processor import workflow


def titles(*secs):
    return [DiscTitle(index=i, duration_secs=s) for i, s in enumerate(secs)]


# Northern Exposure season 3 disc 1 side A, as MakeMKV reported it on 17 October 2026.
NORTHERN_EXPOSURE_S3D1 = titles(2765, 2749, 2716, 2756, 10986, 365, 136)


def test_play_all_title_is_skipped():
    keep = workflow.episode_titles(NORTHERN_EXPOSURE_S3D1)
    assert [t.index for t in keep] == [0, 1, 2, 3]


def test_play_all_of_two_episodes_is_skipped():
    keep = workflow.episode_titles(titles(1320, 1310, 2630))
    assert [t.index for t in keep] == [0, 1]


def test_movie_disc_keeps_its_one_long_title():
    keep = workflow.episode_titles(titles(7200, 300, 120))
    assert [t.index for t in keep] == [0]


def test_long_episode_among_normal_ones_is_kept():
    # A double-length finale is long, but nowhere near the other titles added up.
    keep = workflow.episode_titles(titles(1320, 1310, 1330, 1300, 2640))
    assert [t.index for t in keep] == [0, 1, 2, 3, 4]


def test_same_disc_tolerates_a_second_or_two():
    assert workflow.same_disc([2765, 2749], [2766, 2748])
    assert not workflow.same_disc([2765, 2749], [2765, 2700])
    assert not workflow.same_disc([2765], [2765, 2749])
    assert not workflow.same_disc(None, [2765])


def test_next_disc_with_the_same_label_is_not_already_ripped(tmp_path):
    write_disc_info(tmp_path / "NORTHERN_EXPOSURE", "Northern Exposure", 1, "NORTHERN_EXPOSURE",
                    titles=[2765, 2749, 2716, 2756, 10986, 365, 136])
    side_b = [2700, 2731, 2760, 2744, 10935, 200]
    assert workflow.already_ripped(tmp_path, "NORTHERN_EXPOSURE", "Northern Exposure", 1, side_b) is None
    assert workflow.unique_disc_folder(tmp_path, "NORTHERN_EXPOSURE").name == "NORTHERN_EXPOSURE_2"


def test_same_disc_found_under_any_folder_of_the_show(tmp_path):
    fp = [2700, 2731, 2760, 2744, 10935, 200]
    write_disc_info(tmp_path / "NORTHERN_EXPOSURE", "Northern Exposure", 1, "NORTHERN_EXPOSURE",
                    titles=[2765, 2749, 2716, 2756, 10986, 365, 136])
    write_disc_info(tmp_path / "NORTHERN_EXPOSURE_2", "Northern Exposure", 1, "NORTHERN_EXPOSURE", titles=fp)
    found = workflow.already_ripped(tmp_path, "NORTHERN_EXPOSURE", "Northern Exposure", 1, fp)
    assert found is not None and found.name == "NORTHERN_EXPOSURE_2"


def test_another_show_with_the_same_titles_is_not_this_disc(tmp_path):
    fp = [1320, 1310]
    write_disc_info(tmp_path / "DISC", "Other Show", 2, "DISC", titles=fp)
    assert workflow.already_ripped(tmp_path, "DISC", "Northern Exposure", 1, fp) is None


def test_folder_ripped_before_fingerprints_counts_by_label(tmp_path):
    write_disc_info(tmp_path / "NEWSRADIO_S3D1", "NewsRadio", 3, "NEWSRADIO_S3D1")
    assert workflow.already_ripped(tmp_path, "NEWSRADIO_S3D1", "NewsRadio", 3, [1350, 1351]) is not None
    assert workflow.already_ripped(tmp_path, "NEWSRADIO_S3D1", "NewsRadio", 3) is not None


def test_fingerprint_round_trips_through_disc_json(tmp_path):
    write_disc_info(tmp_path, "Northern Exposure", 1, "NORTHERN_EXPOSURE", titles=[2765, 136])
    assert read_disc_info(tmp_path).titles == [2765, 136]
