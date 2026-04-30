import pytest
from unittest.mock import patch
from dvd_processor.disc import DiscScanner, DiscTitle

SAMPLE_MAKEMKV_OUTPUT = """\
MSG:1005,0,1,"MakeMKV v1.17.5 win(x64-release) started",""
TCOUNT:3
TINFO:0,2,0,"Title_1"
TINFO:0,11,0,"00:21:43"
TINFO:0,27,0,"Title_t00.mkv"
TINFO:1,2,0,"Title_2"
TINFO:1,11,0,"00:22:05"
TINFO:1,27,0,"Title_t01.mkv"
TINFO:2,2,0,"Title_3"
TINFO:2,11,0,"00:05:30"
TINFO:2,27,0,"Title_t02.mkv"
"""


def test_parse_titles_from_output():
    scanner = DiscScanner(drive="E:", makemkv_path="makemkvcon")
    titles = scanner._parse_titles(SAMPLE_MAKEMKV_OUTPUT)
    assert len(titles) == 3
    assert titles[0].index == 0
    assert titles[0].duration_secs == 21 * 60 + 43
    assert titles[1].duration_secs == 22 * 60 + 5
    assert titles[2].duration_secs == 5 * 60 + 30


def test_scan_calls_makemkvcon(tmp_path):
    scanner = DiscScanner(drive="E:", makemkv_path="makemkvcon")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = SAMPLE_MAKEMKV_OUTPUT
        mock_run.return_value.returncode = 0
        titles = scanner.scan()
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert "info" in call_args
    assert len(titles) == 3


def test_rip_calls_makemkvcon_with_title_indices(tmp_path):
    scanner = DiscScanner(drive="E:", makemkv_path="makemkvcon")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        scanner.rip(title_indices=[0, 2], output_dir=tmp_path)
    assert mock_run.call_count == 2
    first_call_args = mock_run.call_args_list[0][0][0]
    assert "mkv" in first_call_args
    assert "0" in first_call_args
