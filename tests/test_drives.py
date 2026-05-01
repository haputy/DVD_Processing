from dvd_processor.drives import get_optical_drives


def test_returns_list():
    result = get_optical_drives()
    assert isinstance(result, list)


def test_fallback_when_no_drives(monkeypatch):
    import ctypes
    monkeypatch.setattr(ctypes.windll.kernel32, "GetLogicalDrives", lambda: 0)
    result = get_optical_drives()
    assert len(result) >= 1  # always returns fallback
