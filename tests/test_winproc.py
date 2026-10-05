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

    @unittest.skipIf(WINDOWS, "POSIX liveness check")
    def test_posix_uses_signal_zero(self) -> None:
        with mock.patch("os.kill", side_effect=PermissionError) as kill:
            self.assertTrue(winproc.pid_alive(12345))                   # someone else's process
        kill.assert_called_once_with(12345, 0)
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(winproc.pid_alive(12345))


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


if __name__ == "__main__":
    unittest.main()
