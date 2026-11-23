"""读当前进程的常驻内存（MB）。

不引入 psutil：桌面端依赖已经够重，这里只用 Windows API / Linux proc。
"""

from __future__ import annotations

import ctypes
import os
import sys


if sys.platform == "win32":
    from ctypes import wintypes

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
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
        ]

    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    _psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE


def rss_mb() -> float:
    """当前进程常驻内存，单位 MB。读不到时返回 0。"""
    try:
        if sys.platform == "win32":
            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            ok = _psapi.GetProcessMemoryInfo(
                _kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            )
            return round(counters.WorkingSetSize / (1024 * 1024), 1) if ok else 0.0
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return 0.0
