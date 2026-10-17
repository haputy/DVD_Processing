import ctypes
import string


def get_optical_drives() -> list[str]:
    drives = []
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if bitmask & 1:
                drive = f"{letter}:"
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\")
                if drive_type == 5:  # DRIVE_CDROM = 5
                    drives.append(drive)
            bitmask >>= 1
    except Exception:
        pass
    return drives or ["D:", "E:"]


def drive_has_disc(drive: str) -> bool:
    """True when the drive has a readable disc in it. Never shows Windows' "insert a disc"
    box: critical error dialogs are turned off around the check."""
    try:
        k32 = ctypes.windll.kernel32
        old = k32.SetErrorMode(1)  # SEM_FAILCRITICALERRORS
        try:
            buf = ctypes.create_unicode_buffer(261)
            root = drive.rstrip("\\/") + "\\"
            return bool(k32.GetVolumeInformationW(root, buf, len(buf), None, None, None, None, 0))
        finally:
            k32.SetErrorMode(old)
    except Exception:
        return False


def eject(drive: str) -> bool:
    """Open the drive's tray. False if Windows would not (best effort, never raises)."""
    try:
        mci = ctypes.windll.winmm.mciSendStringW
        letter = drive.rstrip("\\/")
        if mci(f"open {letter} type cdaudio alias dvdtray", None, 0, None) != 0:
            return False
        try:
            return mci("set dvdtray door open", None, 0, None) == 0
        finally:
            mci("close dvdtray", None, 0, None)
    except Exception:
        return False
