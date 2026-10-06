"""Run a command and measure it with everything it starts: wall time, CPU seconds, processes, peak memory.

Shared by the benchmark scripts in this folder (not used by the app). On Windows the command runs inside a job
object, so the CPU time and peak memory include the worker processes it starts; on POSIX the CPU time comes from
``getrusage(RUSAGE_CHILDREN)`` (reaped descendants) and the peak is the largest single process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional, Sequence

IS_WINDOWS = os.name == "nt"


@dataclass
class Measured:
    returncode: int
    stdout: str
    stderr: str
    wall: float                       # seconds, from start to exit
    cpu: Optional[float]              # user + system seconds of the command and its descendants
    processes: Optional[int]          # processes started (Windows: counted by the job object)
    peak_mb: Optional[float]          # Windows: peak committed memory of all of them at once; POSIX: largest one


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _Basic(ctypes.Structure):           # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    class _Limits(ctypes.Structure):          # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _Extended(ctypes.Structure):        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [("BasicLimitInformation", _Limits), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


def run(cmd: Sequence[str], env: Optional[dict] = None, cwd: Optional[str] = None) -> Measured:
    """Run ``cmd`` to completion (stdout / stderr captured as text) and measure it."""
    if IS_WINDOWS:
        job = _k32.CreateJobObjectW(None, None)
        t0 = time.perf_counter()
        proc = subprocess.Popen(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
                                text=True, encoding="utf-8", errors="replace")
        # a Python child takes tens of milliseconds to start anything of its own: assigned in time
        assigned = bool(job) and bool(_k32.AssignProcessToJobObject(job, int(proc._handle)))  # type: ignore[attr-defined]
        out, err = proc.communicate()
        wall = time.perf_counter() - t0
        cpu = procs = peak = None
        if assigned:
            basic, ext = _Basic(), _Extended()
            if _k32.QueryInformationJobObject(job, 1, ctypes.byref(basic), ctypes.sizeof(basic), None):
                cpu = (basic.TotalUserTime + basic.TotalKernelTime) / 1e7
                procs = int(basic.TotalProcesses)
            if _k32.QueryInformationJobObject(job, 9, ctypes.byref(ext), ctypes.sizeof(ext), None):
                peak = ext.PeakJobMemoryUsed / (1 << 20)
        if job:
            _k32.CloseHandle(job)
        return Measured(proc.returncode, out, err, wall, cpu, procs, peak)
    import resource
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.perf_counter()
    proc = subprocess.run(list(cmd), capture_output=True, env=env, cwd=cwd, text=True, encoding="utf-8",
                          errors="replace")
    wall = time.perf_counter() - t0
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    # ru_maxrss: kilobytes on Linux, bytes on macOS
    peak = after.ru_maxrss / (1 << 20 if sys.platform == "darwin" else 1 << 10)
    return Measured(proc.returncode, proc.stdout, proc.stderr, wall, cpu, None, peak)
