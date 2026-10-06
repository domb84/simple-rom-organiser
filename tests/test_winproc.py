"""romorg.winproc: the process liveness check (never a signal on Windows) and where "next to the app" is."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from romorg import chdtool, tempspace, winproc

WINDOWS = sys.platform.startswith("win")


class PidAliveTest(unittest.TestCase):
    def test_own_pid_is_alive(self) -> None:
        self.assertTrue(winproc.pid_alive(os.getpid()))
        self.assertTrue(tempspace._pid_alive(os.getpid()))
        self.assertTrue(chdtool._pid_alive(os.getpid()))

    def test_an_exited_child_is_dead(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"], **winproc.popen_kwargs())
        proc.wait()
        # on Windows the Popen object may still hold a handle: the process object exists, but it has exited
        self.assertFalse(winproc.pid_alive(proc.pid))
        self.assertFalse(tempspace._pid_alive(proc.pid))

    def test_a_running_child_is_alive(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                **winproc.popen_kwargs())
        try:
            self.assertTrue(winproc.pid_alive(proc.pid))
        finally:
            proc.stdin.close()
            proc.wait()

    def test_no_pid_is_dead(self) -> None:
        for pid in (0, -1):
            self.assertFalse(winproc.pid_alive(pid))

    @unittest.skipUnless(WINDOWS, "Windows liveness check")
    def test_windows_never_calls_os_kill(self) -> None:
        # os.kill(pid, 0) on Windows sends CTRL_C_EVENT to the console instead of probing the process
        with mock.patch("os.kill", side_effect=AssertionError("os.kill must not be used on Windows")):
            self.assertTrue(winproc.pid_alive(os.getpid()))
            self.assertFalse(winproc.pid_alive(0x7FFFFFF0))               # no such process

    @unittest.skipUnless(WINDOWS, "Windows liveness check")
    def test_windows_access_denied_counts_as_alive(self) -> None:
        self.assertTrue(winproc.pid_alive(4))                            # "System": may not be opened, always runs

    @unittest.skipUnless(WINDOWS, "Windows liveness check")
    def test_windows_open_failure_branches(self) -> None:
        # OpenProcess fails: ERROR_ACCESS_DENIED (5) means the process exists; ERROR_INVALID_PARAMETER (87) means none
        k32 = mock.MagicMock()
        k32.OpenProcess.return_value = 0
        with mock.patch("ctypes.WinDLL", return_value=k32):
            with mock.patch("ctypes.get_last_error", return_value=5):
                self.assertTrue(winproc.pid_alive(1234))
            with mock.patch("ctypes.get_last_error", return_value=87):
                self.assertFalse(winproc.pid_alive(1234))
        k32.OpenProcess.assert_called_with(0x1000, False, 1234)

    @unittest.skipUnless(WINDOWS, "Windows liveness check")
    def test_windows_failed_probe_counts_as_alive(self) -> None:
        # the callers sweep a dead owner's scratch folder: a probe that cannot run must not declare the owner dead
        import ctypes

        for exc in (OSError("no kernel32"), AttributeError("x"), ctypes.ArgumentError("bad arg")):
            with mock.patch.object(winproc, "_win_pid_alive", side_effect=exc):
                self.assertTrue(winproc.pid_alive(1234))
                self.assertTrue(tempspace._pid_alive(1234))
                self.assertTrue(chdtool._pid_alive(1234))

    @unittest.skipUnless(WINDOWS, "Windows liveness check")
    def test_windows_pid_beyond_32_bits_is_dead(self) -> None:
        # a DWORD argument would truncate 2**32 + 4 to pid 4 (System, always alive)
        with mock.patch.object(winproc, "_win_pid_alive", side_effect=AssertionError("must not probe")):
            self.assertFalse(winproc.pid_alive(2**32 + 4))
        self.assertTrue(winproc.pid_alive(4))

    @unittest.skipIf(WINDOWS, "POSIX liveness check")
    def test_posix_uses_signal_zero(self) -> None:
        with mock.patch("os.kill", side_effect=PermissionError) as kill:
            self.assertTrue(winproc.pid_alive(12345))                   # someone else's process
        kill.assert_called_once_with(12345, 0)
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(winproc.pid_alive(12345))
        with mock.patch("os.kill", side_effect=OSError(22, "unexpected")):
            self.assertTrue(winproc.pid_alive(12345))                   # unknown: keep the owner's files
        with mock.patch("os.kill", side_effect=OverflowError):
            self.assertFalse(winproc.pid_alive(2**70))                  # beyond pid_t: no such process


class AppDirsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.top = Path(tmp.name).resolve() / "Simple_ROM_Organiser"
        (self.top / "app" / "romorg").mkdir(parents=True)
        (self.top / "app" / "native").mkdir()
        (self.top / "python").mkdir()

    def test_zip_top_folder_is_next_to_the_app(self) -> None:
        # the .zip: <top>\python\python.exe runs <top>\app\romorg; "next to the app" is <top>
        fake = self.top / "app" / "romorg" / "winproc.py"
        with mock.patch.object(winproc, "__file__", str(fake)):
            self.assertIn(self.top, winproc.app_dirs())

    def test_a_source_tree_adds_no_parent_folder(self) -> None:
        fake = self.top.parent / "checkout" / "romorg" / "winproc.py"
        with mock.patch.object(winproc, "__file__", str(fake)):
            dirs = winproc.app_dirs()
        self.assertNotIn(self.top.parent, dirs)
        self.assertNotIn(fake.parent.parent, dirs)

    def test_frozen_exe_folder_comes_first(self) -> None:
        exe = self.top / "Simple_ROM_Organiser-win64.exe"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", str(exe)):
            self.assertEqual(winproc.app_dirs()[0], self.top)

    def test_chdman_exe_in_the_zip_top_folder_is_found(self) -> None:
        tool = self.top / "romorg-test-tool.exe"
        tool.write_bytes(b"MZ")
        fake = self.top / "app" / "romorg" / "winproc.py"
        with mock.patch.object(winproc, "__file__", str(fake)), mock.patch.object(winproc, "IS_WINDOWS", True), \
                mock.patch("romorg.winproc.shutil.which", return_value=None):
            self.assertEqual(winproc.find_tool(("romorg-test-tool",)), str(tool))

    def test_program_dirs_is_empty_off_windows(self) -> None:
        with mock.patch.object(winproc, "IS_WINDOWS", False):
            self.assertEqual(winproc.program_dirs(), [])


class LongPathHintTest(unittest.TestCase):
    long = "C:\\" + "x" * 300

    def exc(self, winerror: int) -> OSError:
        e = OSError(2, "The system cannot find the path specified")
        e.winerror = winerror
        return e

    def test_hint_on_windows_for_a_long_path(self) -> None:
        with mock.patch.object(winproc, "IS_WINDOWS", True):
            msg = winproc.long_path_hint(self.exc(3), self.long)
            self.assertIn("LongPathsEnabled", msg)
            self.assertIn("cannot find the path", msg)

    def test_no_hint_for_a_short_path_other_error_or_other_system(self) -> None:
        with mock.patch.object(winproc, "IS_WINDOWS", True):
            self.assertNotIn("LongPathsEnabled", winproc.long_path_hint(self.exc(3), "C:\short"))
            self.assertNotIn("LongPathsEnabled", winproc.long_path_hint(self.exc(5), self.long))
        with mock.patch.object(winproc, "IS_WINDOWS", False):
            self.assertNotIn("LongPathsEnabled", winproc.long_path_hint(self.exc(3), self.long))


if __name__ == "__main__":
    unittest.main()
