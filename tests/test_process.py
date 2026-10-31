"""romorg.process: the process liveness check."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

from romorg import chdtool, process, tempspace


class PidAliveTest(unittest.TestCase):
    def test_own_pid_is_alive(self) -> None:
        self.assertTrue(process.pid_alive(os.getpid()))
        self.assertTrue(tempspace._pid_alive(os.getpid()))
        self.assertTrue(chdtool._pid_alive(os.getpid()))

    def test_an_exited_child_is_dead(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"], **process.popen_kwargs())
        proc.wait()
        self.assertFalse(process.pid_alive(proc.pid))
        self.assertFalse(tempspace._pid_alive(proc.pid))

    def test_a_running_child_is_alive(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                **process.popen_kwargs())
        try:
            self.assertTrue(process.pid_alive(proc.pid))
        finally:
            proc.stdin.close()
            proc.wait()

    def test_no_pid_is_dead(self) -> None:
        for pid in (0, -1):
            self.assertFalse(process.pid_alive(pid))

    def test_it_uses_signal_zero(self) -> None:
        with mock.patch("os.kill", side_effect=PermissionError) as kill:
            self.assertTrue(process.pid_alive(12345))                   # someone else's process
        kill.assert_called_once_with(12345, 0)
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(process.pid_alive(12345))
        with mock.patch("os.kill", side_effect=OSError(22, "unexpected")):
            self.assertTrue(process.pid_alive(12345))                   # unknown: keep the owner's files
        with mock.patch("os.kill", side_effect=OverflowError):
            self.assertFalse(process.pid_alive(2**70))                  # beyond pid_t: no such process


if __name__ == "__main__":
    unittest.main()
