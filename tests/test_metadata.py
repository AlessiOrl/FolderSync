import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from metadata import MARKER, prepare, prepare_show, regenerate, validate_config
from sync import Config, LinkSync, Rule, F1Name


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.show = self.root / "Formula 1"
        self.folder = self.show / "Season 2615"
        self.folder.mkdir(parents=True)
        self.video = self.folder / "S2615E01 - Azerbaijan Race.mkv"
        self.f1 = F1Name("2615", "Azerbaijan", "Race", ".mkv")

    def test_metadata_and_artwork_exist_at_the_instant_video_is_linked(self):
        from PIL import Image
        Image.new("RGB", (600, 900)).save(self.show / "selected.png")
        Image.new("RGB", (1600, 900)).save(self.show / "wide.png")
        source = self.root / "source"
        source.mkdir()
        (source / "Formula1.2026.Round15.Azerbaijan.Race.mkv").write_bytes(b"video")
        metadata = {"background": "wide.png", "events": {"2615": {
            "poster": "selected.png", "sessions": {"Race": {"date": "2026-09-26", "description": "Custom description & details"}}}}}
        config = Config(frozenset({".mkv"}), (Rule("2026", Path(".")),), metadata=metadata)
        sync = LinkSync(self.show, source, config, stable_seconds=0)
        import os
        original = os.link
        def checked_link(src, dst, **kwargs):
            if Path(dst).suffix == ".mkv":
                self.assertFalse(self.video.exists())
                episode = ET.parse(self.video.with_suffix(".nfo")).getroot()
                self.assertEqual(episode.findtext("aired"), "2026-09-26")
                self.assertEqual(episode.findtext("plot"), "Custom description & details")
                show = ET.parse(self.show / "tvshow.nfo").getroot()
                self.assertEqual(show.findtext("year"), "2026")
                self.assertEqual(show.findtext("premiered"), "2026-01-01")
                self.assertEqual(show.find("namedseason").text, "2026 Azerbaijan Grand Prix")
                self.assertEqual([g.text for g in show.findall("genre")], ["Sports", "Motorsport"])
                self.assertTrue((self.folder / "Season2615.png").exists())
                self.assertTrue((self.show / "background.png").exists())
            return original(src, dst, **kwargs)
        with patch("os.link", side_effect=checked_link):
            self.assertEqual(sync.scan_once().linked, 1)

    def test_season_names_accumulate_and_user_metadata_is_preserved(self):
        prepare(self.video, self.f1, 2026, {})
        other = self.show / "Season 2616"
        other.mkdir()
        prepare(other / "S2616E01 - Monza Race.mkv", F1Name("2616", "Monza", "Race", ".mkv"), 2026, {})
        root = ET.parse(self.show / "tvshow.nfo").getroot()
        self.assertEqual(len(root.findall("namedseason")), 2)
        self.assertIsNone(ET.parse(self.video.with_suffix(".nfo")).getroot().find("aired"))
        self.video.with_suffix(".nfo").write_text("user metadata")
        prepare(self.video, self.f1, 2026, {})
        self.assertEqual(self.video.with_suffix(".nfo").read_text(), "user metadata")

    def test_invalid_dates_and_artwork_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_config({"events": {"2615": {"sessions": {"Race": {"date": "2025-09-26"}}}}})
        with self.assertRaises(ValueError):
            prepare(self.video, self.f1, 2026, {"background": "../outside.png"})

    def test_show_uses_lowest_season_year_independent_of_scan_order(self):
        prepare(self.video, self.f1, 2026, {})
        for code, expected in (("2715", "2026"), ("2515", "2025"), ("2815", "2025")):
            folder = self.show / f"Season {code}"
            folder.mkdir()
            prepare(folder / f"S{code}E01 - Azerbaijan Race.mkv",
                    F1Name(code, "Azerbaijan", "Race", ".mkv"), 2000 + int(code[:2]), {})
            root = ET.parse(self.show / "tvshow.nfo").getroot()
            self.assertEqual(root.findtext("year"), expected)
            self.assertEqual(root.findtext("premiered"), f"{expected}-01-01")
            self.assertEqual(len(root.findall("year")), 1)
            self.assertEqual(len(root.findall("premiered")), 1)
        prepare(self.video, self.f1, 2026, {})
        self.assertEqual(ET.parse(self.show / "tvshow.nfo").findtext("year"), "2025")

    def test_preexisting_season_folders_and_integer_codes_supply_earliest_year(self):
        (self.show / "Season 2415").mkdir()
        (self.show / "Season 2415" / "S2415E01 - Azerbaijan Race.mkv").write_bytes(b"video")
        for name in ("Season 0000", "Season 0127", "Season 2026", "Season 23"):
            (self.show / name).mkdir()
        (self.show / "Season 2026" / "S26E01 - Azerbaijan Race.mkv").write_bytes(b"legacy video")
        prepare(self.video, self.f1, 2026, {})
        self.assertEqual(ET.parse(self.show / "tvshow.nfo").findtext("year"), "2024")
        root = ET.parse(self.show / "tvshow.nfo").getroot()
        ET.SubElement(root, "namedseason", number="115").text = "2001 Azerbaijan Grand Prix"
        (self.show / "tvshow.nfo").write_bytes(MARKER + ET.tostring(root))
        prepare_show(self.show, {})
        self.assertEqual(ET.parse(self.show / "tvshow.nfo").findtext("year"), "2001")

    def test_full_scan_backfills_show_without_source_and_preserves_user_show(self):
        path = self.show / "tvshow.nfo"
        path.write_bytes(MARKER + b'<tvshow><title>Formula 1</title><namedseason number="2515">Old GP</namedseason></tvshow>')
        source = self.root / "source"
        source.mkdir()
        sync = LinkSync(self.show, source, Config(frozenset({".mkv"}), (), metadata={"description": "Expanded synopsis"}), stable_seconds=0)
        self.assertEqual(sync.scan_once().nfo_failed, 0)
        root = ET.parse(path).getroot()
        self.assertEqual(root.findtext("premiered"), "2025-01-01")
        self.assertEqual(root.findtext("plot"), "Expanded synopsis")
        path.write_text("user show metadata")
        self.assertEqual(sync.scan_once().nfo_failed, 0)
        self.assertEqual(path.read_text(), "user show metadata")

    def test_strict_poster_mode_keeps_failed_lookup_pending(self):
        from posters import PosterWorker
        worker = PosterWorker({"enabled": False, "require_before_link": True})
        worker.configure(self.show)
        worker.observe(self.folder, 2026, "Azerbaijan")
        self.assertFalse(worker.ready(self.folder))
        worker.close()

    def test_regeneration_rebuilds_missing_and_old_metadata_without_sources(self):
        from nfo import episode_xml
        self.video.write_bytes(b"video")
        self.video.with_suffix(".nfo").write_bytes(episode_xml(2615, 1, "Azerbaijan Race").encode())
        (self.show / "tvshow.nfo").write_bytes(MARKER + b"<broken")
        (self.folder / "season.nfo").write_bytes(MARKER + b"<broken")
        poster = self.folder / "Season2615.jpg"
        poster.write_bytes(b"existing poster")
        signature = self.video.stat()
        config = Config(frozenset({".mkv"}), (), metadata={"description": "New series summary"})
        result = regenerate(self.show, config, lambda message: None)
        self.assertEqual(result["updated"], 3)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(ET.parse(self.show / "tvshow.nfo").findtext("year"), "2026")
        self.assertEqual(ET.parse(self.video.with_suffix(".nfo")).findtext("year"), "2026")
        self.assertIn("Azerbaijan", ET.parse(self.video.with_suffix(".nfo")).findtext("plot"))
        self.assertEqual(poster.read_bytes(), b"existing poster")
        self.assertEqual((signature.st_ino, signature.st_mtime_ns), (self.video.stat().st_ino, self.video.stat().st_mtime_ns))
        repeat = regenerate(self.show, config, lambda message: None)
        self.assertEqual(repeat["updated"], 0)
        self.assertEqual(repeat["unchanged"], 3)

    def test_regeneration_preserves_authored_files_and_reports_unparsed(self):
        self.video.write_bytes(b"video")
        paths = [self.show / "tvshow.nfo", self.folder / "season.nfo", self.video.with_suffix(".nfo")]
        for path in paths:
            path.write_text("custom metadata")
        (self.folder / "unknown.mkv").write_bytes(b"video")
        result = regenerate(self.show, Config(frozenset({".mkv"}), ()), lambda message: None)
        self.assertEqual(result["preserved"], 3)
        self.assertEqual(result["unparsed"], 1)
        for path in paths:
            self.assertEqual(path.read_text(), "custom metadata")

    def test_regeneration_continues_after_write_error(self):
        self.video.write_bytes(b"video")
        other = self.folder / "S2615E02 - Azerbaijan Qualify.mkv"
        other.write_bytes(b"video")
        from metadata import write_xml
        def fail_one(path, *args, **kwargs):
            if path == self.video.with_suffix(".nfo"):
                raise OSError("read-only episode")
            return write_xml(path, *args, **kwargs)
        messages = []
        with patch("metadata.write_xml", side_effect=fail_one):
            result = regenerate(self.show, Config(frozenset({".mkv"}), ()), messages.append)
        self.assertEqual(result["errors"], 1)
        self.assertTrue(other.with_suffix(".nfo").exists())
        self.assertTrue(any("read-only episode" in message for message in messages))

    def test_regeneration_stays_within_destination_and_skips_symlinks(self):
        self.video.write_bytes(b"video")
        config = Config(frozenset({".mkv"}), ())
        regenerate(self.folder, config, lambda message: None)
        self.assertFalse((self.show / "tvshow.nfo").exists())
        self.assertFalse((self.folder / "season.nfo").exists())
        outside = self.root / "outside"
        outside.mkdir()
        external = outside / "S2615E01 - Azerbaijan Race.mkv"
        external.write_bytes(b"outside")
        (self.show / "linked-folder").symlink_to(outside, target_is_directory=True)
        self.video.with_suffix(".nfo").unlink()
        self.video.with_suffix(".nfo").symlink_to(outside / "external.nfo")
        result = regenerate(self.show, config, lambda message: None)
        self.assertEqual(result["preserved"], 1)
        self.assertFalse(external.with_suffix(".nfo").exists())
        self.assertFalse((outside / "external.nfo").exists())
