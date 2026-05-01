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
