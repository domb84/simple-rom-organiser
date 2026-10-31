"""Run a command and measure it with everything it starts: wall time, CPU seconds, processes, peak memory.

Shared by the benchmark scripts in this folder (not used by the app). The CPU time comes from
``getrusage(RUSAGE_CHILDREN)`` (reaped descendants) and the peak is the largest single process.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass
class Measured:
    returncode: int
    stdout: str
    stderr: str
    wall: float                       # seconds, from start to exit
    cpu: Optional[float]              # user + system seconds of the command and its descendants
    processes: Optional[int]          # processes started
    peak_mb: Optional[float]          # the largest one


def run(cmd: Sequence[str], env: Optional[dict] = None, cwd: Optional[str] = None) -> Measured:
    """Run ``cmd`` to completion (stdout / stderr captured as text) and measure it."""
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
