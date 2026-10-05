import errno
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import RecentLogs, SyncService
from settings import DirectorySettings
from sync import Config, Rule


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "torrent/f1").mkdir(parents=True)
        (self.root / "media/f1").mkdir(parents=True)
        self.settings = DirectorySettings(self.root)
        self.config = Config(frozenset({".mkv"}), (Rule("2026", Path(".")),))
        self.service = SyncService(None, RecentLogs(), self.settings, self.config, 120)
        self.selection = {"source": "torrent/f1", "destination": "media/f1"}

    def test_setup_persists_and_restores_without_leaving_probe_files(self):
        self.assertFalse(self.service.status()["configured"])
        self.assertIsNone(self.service.scan_now())
        self.service.configure(self.selection)
        self.assertEqual(json.loads(self.settings.path.read_text()), self.selection)
        self.assertEqual(list((self.root / "torrent/f1").iterdir()), [])
        self.assertEqual(list((self.root / "media/f1").iterdir()), [])
        restored = SyncService(None, RecentLogs(), self.settings, self.config)
        restored.restore_settings()
        self.assertTrue(restored.status()["configured"])
        self.assertEqual(restored.selected, self.selection)
        self.assertEqual(restored.sync.torrent, self.root / "torrent/f1")
        self.assertEqual(restored.sync.data, self.root / "media/f1")

    def test_invalid_paths_keep_working_settings(self):
        self.service.configure(self.selection)
        original = self.service.sync
        (self.root / "alias").symlink_to(self.root.parent, target_is_directory=True)
        (self.root / "torrent/f1/nested").mkdir()
        for source, destination in (
            ("../outside", "media/f1"), ("/tmp", "media/f1"),
            ("alias", "media/f1"), ("missing", "media/f1"),
            ("torrent/f1", "torrent/f1"), ("torrent/f1", "torrent/f1/nested"),
            (".", "media/f1"), ("torrent\\f1", "media/f1"),
        ):
            with self.subTest(source=source, destination=destination):
                with self.assertRaises((ValueError, OSError)):
                    self.service.configure({"source": source, "destination": destination})
                self.assertIs(self.service.sync, original)
                self.assertEqual(json.loads(self.settings.path.read_text()), self.selection)

    def test_failed_probe_preserves_settings_and_cleans_up(self):
        self.service.configure(self.selection)
        original = self.service.sync
        with patch("settings.os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
            with self.assertRaisesRegex(ValueError, "one mount at /app/data"):
                self.service.configure(self.selection)
        self.assertIs(self.service.sync, original)
        self.assertEqual(list((self.root / "torrent/f1").iterdir()), [])
        self.assertEqual(list((self.root / "media/f1").iterdir()), [])

    def test_failed_save_preserves_settings_and_cleans_up(self):
        self.service.configure(self.selection)
        original = self.service.sync
        with patch("settings.os.replace", side_effect=PermissionError("read-only")):
            with self.assertRaises(PermissionError):
                self.service.configure({"source": "media/f1", "destination": "torrent/f1"})
        self.assertIs(self.service.sync, original)
        self.assertEqual(json.loads(self.settings.path.read_text()), self.selection)
        self.assertEqual(list(self.root.glob(".foldersync-*")), [])

    def test_configuration_cannot_change_during_scan(self):
        with self.service.scan_lock:
            with self.assertRaisesRegex(RuntimeError, "scan or configuration"):
                self.service.configure(self.selection)
        self.assertFalse(self.settings.path.exists())

    def test_corrupt_or_missing_saved_directories_allow_setup(self):
        for raw in ("not json", json.dumps({"source": "gone", "destination": "media/f1"})):
            with self.subTest(raw=raw):
                self.settings.path.write_text(raw)
                self.service.restore_settings()
                self.assertFalse(self.service.status()["configured"])
                self.assertIn("need attention", self.service.last_error)
        self.service.configure(self.selection)
        self.assertIsNone(self.service.last_error)


if __name__ == "__main__":
    unittest.main()
