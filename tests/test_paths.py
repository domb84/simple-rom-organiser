import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from romorg import paths


class PathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_override_and_subdirs(self) -> None:
        with mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": self.tmp.name}):
            self.assertEqual(paths.data_dir(), Path(self.tmp.name))
            self.assertEqual(paths.dats_dir(), Path(self.tmp.name) / "dats")
            self.assertTrue(paths.dats_dir().is_dir())
            self.assertTrue(paths.cache_dir().is_dir())
            self.assertEqual(paths.nointro_dir(), Path(self.tmp.name) / "nointro")
            self.assertTrue(paths.nointro_dir().is_dir())
            self.assertEqual(paths.config_path(), Path(self.tmp.name) / "config.json")

    def test_xdg_data_home(self) -> None:
        env = {"XDG_DATA_HOME": self.tmp.name}
        with mock.patch.dict(os.environ, env), mock.patch.object(paths.sys, "platform", "linux"):
            os.environ.pop("ROMORG_DATA_DIR", None)
            self.assertEqual(paths.data_dir(), Path(self.tmp.name) / paths.APP_NAME)

    def test_config_roundtrip(self) -> None:
        with mock.patch.dict(os.environ, {"ROMORG_DATA_DIR": self.tmp.name}):
            self.assertEqual(paths.load_config(), {})
            paths.save_config({"last_dir": "/x/ü", "last_dat": "A"})
            self.assertEqual(paths.load_config(), {"last_dir": "/x/ü", "last_dat": "A"})
            paths.config_path().write_text("{broken", encoding="utf-8")
            self.assertEqual(paths.load_config(), {})
            self.assertEqual([p.name for p in Path(self.tmp.name).glob(".config-*")], [])


if __name__ == "__main__":
    unittest.main()
