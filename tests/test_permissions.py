import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

from metadata import copy_art, write_xml
from nfo import ensure_nfo
from permissions import apply_permissions, policy, repair_tree
from sync import Config, LinkSync, Rule


class PermissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"PGID": "100", "FILE_MODE": "0764",
                                             "DIRECTORY_MODE": "0764"})
        environment.start()
        self.addCleanup(environment.stop)
        # Verify the root ownership request without requiring root to run tests.
        ownership = patch("permissions.os.fchown")
        self.chown = ownership.start()
        self.addCleanup(ownership.stop)

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_startup_repairs_existing_files_and_skips_symlinks(self):
        destination = self.root / "destination"
        nested = destination / "season"
        nested.mkdir(parents=True)
        media = nested / "video.mkv"
        media.write_bytes(b"video")
        original = self.root / "source-video.mkv"
        os.link(media, original)
        outside = self.root / "outside"
        outside.mkdir()
        private = outside / "private"
        private.write_bytes(b"private")
        private.chmod(0o600)
        (nested / "external-file").symlink_to(private)
        (nested / "external-directory").symlink_to(outside, target_is_directory=True)
        media.chmod(0o600)
        repair_tree(destination)
        self.assertEqual(self.mode(destination), 0o0764)
        self.assertEqual(self.mode(nested), 0o0764)
        self.assertEqual(self.mode(media), 0o764)
        self.assertEqual(self.mode(original), 0o764)
        self.assertEqual(self.mode(private), 0o600)
        self.assertTrue(media.samefile(original))
        self.assertTrue(self.chown.called)
        self.assertTrue(all(call.args[1:] == (0, 100) for call in self.chown.call_args_list))

    def test_atomic_metadata_and_new_hardlinks_get_permissions(self):
        source = self.root / "source"
        destination = self.root / "destination"
        source.mkdir()
        destination.mkdir()
        video = source / "S1E01 - Example.mkv"
        video.write_bytes(b"video")
        config = Config(frozenset({".mkv"}), (Rule("", Path("nested/deeper")),))
        sync = LinkSync(destination, source, config, stable_seconds=0)
        self.assertEqual(sync.scan_once().linked, 1)
        linked = destination / "nested/deeper" / video.name
        self.assertTrue(video.samefile(linked))
        self.assertEqual(self.mode(linked), 0o764)
        self.assertEqual(self.mode(linked.parent), 0o0764)
        self.assertEqual(self.mode(linked.parent.parent), 0o0764)
        self.assertEqual(self.mode(sync.registry_path), 0o764)
        self.assertEqual(self.mode(linked.with_suffix(".nfo")), 0o764)
        metadata = destination / "season.nfo"
        write_xml(metadata, ET.Element("season"), update=True)
        metadata.chmod(0o600)
        write_xml(metadata, ET.Element("updated"), update=True)
        self.assertEqual(self.mode(metadata), 0o764)

    def test_poster_and_settings_publication_get_permissions(self):
        from posters import atomic_json, publish_image
        from settings import DirectorySettings
        poster = self.root / "Season2618.jpg"
        self.assertTrue(publish_image(self.root, poster.name, b"poster"))
        sidecar = self.root / "poster.json"
        atomic_json(sidecar, {"poster": poster.name})
        settings = DirectorySettings(self.root)
        settings.save({"source": "source", "destination": "destination"})
        for path in (poster, sidecar, settings.path):
            self.assertEqual(self.mode(path), 0o764)

    def test_custom_artwork_and_basic_nfo_get_permissions(self):
        from PIL import Image
        artwork = self.root / "custom.png"
        Image.new("RGB", (10, 10)).save(artwork)
        target = self.root / "poster.png"
        copy_art(self.root, Path("custom.png"), target)
        self.assertEqual(self.mode(target), 0o764)
        video = self.root / "S1E01 - Example.mkv"
        self.assertEqual(ensure_nfo(video), "created")
        self.assertEqual(self.mode(video.with_suffix(".nfo")), 0o764)

    def test_disabled_policy_preserves_existing_permissions(self):
        path = self.root / "private"
        path.write_bytes(b"private")
        path.chmod(0o600)
        with patch.dict(os.environ, {}, clear=True):
            repair_tree(self.root)
            apply_permissions(path)
        self.assertEqual(self.mode(path), 0o600)
        self.chown.assert_not_called()

    def test_invalid_configuration_is_rejected(self):
        for key, value in (("PGID", "users"), ("PGID", "-1"), ("PGID", "4294967295"),
                           ("FILE_MODE", "0888"), ("DIRECTORY_MODE", "")):
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                with self.assertRaises(ValueError):
                    policy()


if __name__ == "__main__":
    unittest.main()
