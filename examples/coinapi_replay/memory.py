"""Dependency-free process memory sampling with constant-size retention.

Windows private bytes measure committed private memory. Linux private bytes
measure resident private pages from smaps_rollup, so the result labels the
definition explicitly. OS peak working set covers the process lifetime;
sampled_peak covers only the samples taken by this helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys


@dataclass(frozen=True, slots=True)
class ProcessMemory:
    working_set_bytes: int
    private_bytes: int | None
    os_peak_working_set_bytes: int
    private_kind: str

    def counters(self):
        return {
            "working_set_bytes": self.working_set_bytes,
            "private_bytes": self.private_bytes,
            "os_peak_working_set_bytes": self.os_peak_working_set_bytes,
        }


def _windows_memory(pid):
    import ctypes
    from ctypes import wintypes

    class MemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(MemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000 | 0x0010, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return ProcessMemory(
            counters.WorkingSetSize,
            counters.PrivateUsage,
            counters.PeakWorkingSetSize,
            "committed",
        )
    finally:
        kernel.CloseHandle(handle)


def _linux_memory(pid):
    directory = Path("/proc") / str(pid)

    def values(path):
        result = {}
        for line in path.read_text(encoding="ascii").splitlines():
            name, _, value = line.partition(":")
            fields = value.split()
            if len(fields) == 2 and fields[1] == "kB":
                result[name] = int(fields[0]) * 1024
        return result

    status = values(directory / "status")
    private = None
    try:
        pages = values(directory / "smaps_rollup")
        private = sum(
            pages.get(name, 0)
            for name in ("Private_Clean", "Private_Dirty", "Private_Hugetlb")
        )
    except OSError:
        # RSS remains available where the kernel restricts private page data.
        pass
    return ProcessMemory(status["VmRSS"], private, status["VmHWM"], "resident")


def read_process_memory(pid: int) -> ProcessMemory:
    if sys.platform == "win32":
        return _windows_memory(pid)
    if sys.platform.startswith("linux"):
        return _linux_memory(pid)
    raise NotImplementedError(
        f"Process memory sampling is unsupported on {sys.platform}"
    )


class ProcessMemorySampler:
    """The caller chooses sampling cadence; no histories or threads are retained."""

    def __init__(self, pid: int, *, reader=read_process_memory):
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("Process ID must be a positive integer")
        self.pid, self.reader = pid, reader
        self.samples = self.failed_samples = 0
        self.initial = self.final = self.peak = None
        self.private_kind = None
        self.error = None

    def sample(self):
        try:
            reading = self.reader(self.pid)
        except (OSError, NotImplementedError, KeyError, ValueError) as exc:
            self.failed_samples += 1
            self.error = f"{type(exc).__name__}: {exc}"
            return
        counters = reading.counters()
        self.samples += 1
        self.private_kind = reading.private_kind
        if self.initial is None:
            self.initial = counters.copy()
            self.peak = counters.copy()
        else:
            for name, value in counters.items():
                if value is not None:
                    previous = self.peak[name]
                    self.peak[name] = (
                        max(value, previous) if previous is not None else value
                    )
        self.final = counters

    def summary(self):
        return {
            "pid": self.pid,
            "platform": sys.platform,
            "samples": self.samples,
            "failed_samples": self.failed_samples,
            "private_kind": self.private_kind,
            "error": self.error,
            "initial": self.initial.copy() if self.initial is not None else None,
            "final": self.final.copy() if self.final is not None else None,
            "sampled_peak": self.peak.copy() if self.peak is not None else None,
        }
