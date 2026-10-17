"""Run makemkvcon and HandBrakeCLI so they can be stopped, and stop with the window.

Every process started through a ProcessRunner goes into one Windows Job Object created with
KILL_ON_JOB_CLOSE. The job's handle belongs to this Python process, so when the process ends
(the window is closed, or it crashes) Windows kills whatever is still running in the job.
ProcessRunner.cancel() stops the running processes straight away and makes the next
cancellation point raise Cancelled.

Cancelled is a KeyboardInterrupt, so the existing clean-up for Ctrl+C applies to it: a
half-ripped MKV becomes .failed (workflow.rip_titles), a half-encoded .encoding.mp4 is
removed (HandBrakeEncoder.encode), and a half-copied .part is removed (nas.copy_episode).
"""
import logging
import os
import subprocess
import threading
from typing import Optional

log = logging.getLogger(__name__)


class Cancelled(KeyboardInterrupt):
    """The work was stopped on purpose (the Cancel button, or closing the window)."""


# ---- the Windows job object ----------------------------------------------------------------

_job_handle = None
_job_lock = threading.Lock()


def _make_job():
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # extended limits
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    return job


def add_to_job(proc: subprocess.Popen) -> bool:
    """Put a started process in the kill-on-close job. False (logged) if Windows refused or
    this is not Windows; the process then still stops with cancel(), just not on a crash."""
    global _job_handle
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        with _job_lock:
            if _job_handle is None:
                _job_handle = _make_job()
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if not k32.AssignProcessToJobObject(_job_handle, int(proc._handle)):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        return True
    except Exception as exc:
        log.warning("could not put process %s in the job object: %s", proc.pid, exc)
        return False


# ---- the runner ----------------------------------------------------------------------------

class ProcessRunner:
    """A stand-in for subprocess.run that can be cancelled from another thread. One per
    piece of work; DiscScanner and HandBrakeEncoder take its run method as runner."""

    def __init__(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()

    @property
    def cancelled(self) -> bool:
        return self._stop.is_set()

    def check(self) -> None:
        """A cancellation point: raises Cancelled once cancel() has been called."""
        if self._stop.is_set():
            raise Cancelled()

    def cancel(self) -> None:
        """Stop the running processes now; the next cancellation point raises Cancelled."""
        self._stop.set()
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except OSError:
                pass

    def run(self, cmd, check: bool = False, **kwargs) -> subprocess.CompletedProcess:
        self.check()
        proc = subprocess.Popen(cmd, **kwargs)
        add_to_job(proc)
        with self._lock:
            self._procs.add(proc)
        try:
            if self._stop.is_set():  # cancelled while it was starting
                proc.kill()
            out, err = proc.communicate()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            with self._lock:
                self._procs.discard(proc)
        if self._stop.is_set():
            raise Cancelled()
        if check and proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    def running(self) -> int:
        with self._lock:
            return len(self._procs)

