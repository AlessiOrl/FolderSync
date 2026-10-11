import errno
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from sync import Config, LinkSync, Rule, load_config, load_directories, normalized_f1_name


class LinkSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.torrent = self.root / "torrent"
        self.data = self.root / "data"
        self.torrent.mkdir()
        self.data.mkdir()
        (self.data / "season 26").mkdir()
        self.config = Config(frozenset({".mp4", ".mkv", ".avi", ".mov"}), (Rule("2026", Path("season 26")),))
        self.now = 0.0
        self.sync = LinkSync(self.data, self.torrent, self.config, clock=lambda: self.now)

    def advance(self):
        self.now += 120

    def source_file(self, relative="F1 gran prix 2026/bahrein.mp4", content=b"video"):
        source = self.torrent / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        return source

    def test_example_waits_two_scans_and_links_same_inode(self):
        source = self.source_file()
        destination = self.data / "season 26" / "bahrein.mp4"

        self.assertEqual(self.sync.scan_once().pending, 1)
        self.assertFalse(destination.exists())
        self.assertEqual(self.sync.scan_once().pending, 1)
        self.advance()
        self.assertEqual(self.sync.scan_once().linked, 1)
        self.assertEqual((source.stat().st_dev, source.stat().st_ino), (destination.stat().st_dev, destination.stat().st_ino))
        self.assertEqual(self.sync.scan_once().already_linked, 1)

    def test_pending_check_links_after_ten_seconds_without_discovering_new_files(self):
        source = self.source_file()
        self.assertEqual(self.sync.scan_once().pending, 1)
        self.now = 9
        self.assertFalse(self.sync.pending_ready())
        self.source_file("2026/new.mp4")
        self.now = 10
        self.assertTrue(self.sync.pending_ready())
        with patch("sync.os.walk", side_effect=AssertionError("Unexpected full scan")):
            self.assertEqual(self.sync.scan_once(pending_only=True).linked, 1)
        self.assertTrue(source.samefile(self.data / "season 26" / source.name))
        self.assertFalse((self.data / "season 26" / "new.mp4").exists())
        self.assertFalse(self.sync.pending_ready())
        self.now = 120
        self.assertEqual(self.sync.scan_once().pending, 1)

    def test_pending_change_restarts_ten_second_wait(self):
        source = self.source_file()
        self.sync.scan_once()
        self.now = 10
        source.write_bytes(b"changed video")
        self.assertEqual(self.sync.scan_once(pending_only=True).pending, 1)
        self.now = 19
        self.assertFalse(self.sync.pending_ready())
        self.now = 20
        self.assertTrue(self.sync.pending_ready())
        self.assertEqual(self.sync.scan_once(pending_only=True).linked, 1)

    def test_link_errors_preserve_source_and_recover(self):
        source = self.source_file()
        destination = self.data / "season 26" / source.name
        self.sync.scan_once()
        self.advance()
        for code in (errno.EXDEV, errno.EACCES):
            with self.subTest(errno=code):
                with patch("sync.os.link", side_effect=OSError(code, "link failed")):
                    with self.assertLogs("folder_sync", level="ERROR") as logs:
                        result = self.sync.scan_once()
                self.assertEqual(result.failed, 1)
                self.assertEqual(result.linked, 0)
                self.assertEqual(result.skipped, 0)
                self.assertFalse(destination.exists())
                self.assertEqual(source.read_bytes(), b"video")
                self.assertIn("Cannot link", self.sync.last_error)
                self.assertEqual("Mount the shared host parent once" in logs.output[0],
                                 code == errno.EXDEV)
        self.assertEqual(self.sync.scan_once().linked, 1)
        self.assertIsNone(self.sync.last_error)
        self.assertTrue(source.samefile(destination))

    def test_change_resets_wait(self):
        source = self.source_file()
        self.sync.scan_once()
        self.advance()
        source.write_bytes(b"longer video")
        self.assertEqual(self.sync.scan_once().pending, 1)
        self.assertFalse((self.data / "season 26" / "bahrein.mp4").exists())
        self.advance()
        self.assertEqual(self.sync.scan_once().linked, 1)

    def test_first_rule_case_insensitive_recursive_and_video_only(self):
        (self.data / "other").mkdir()
        config = replace(self.config, rules=(Rule("PRIX", Path("other")), Rule("2026", Path("season 26"))))
        sync = LinkSync(self.data, self.torrent, config, clock=lambda: self.now)
        source = self.source_file("F1 grand PRIX 2026/nested/bahrein.MP4")
        (source.parent / "notes.txt").write_text("metadata")
        sync.scan_once()
        self.advance()
        result = sync.scan_once()
        self.assertEqual(result.linked, 1)
        self.assertTrue((self.data / "other" / "bahrein.MP4").exists())
        self.assertFalse((self.data / "season 26" / "bahrein.MP4").exists())
        self.assertFalse((self.data / "other" / "notes.txt").exists())

    def test_unmatched_conflict_and_recreated_folder_leave_files_untouched(self):
        unmatched = self.source_file("unknown/video.mp4", b"unmatched")
        matched = self.source_file()
        destination = self.data / "season 26" / "bahrein.mp4"
        destination.write_bytes(b"existing")
        self.sync.scan_once()
        self.advance()
        self.assertEqual(self.sync.scan_once().skipped, 2)
        self.assertEqual(destination.read_bytes(), b"existing")
        self.assertEqual(matched.read_bytes(), b"video")
        self.assertEqual(unmatched.read_bytes(), b"unmatched")

        (self.data / "season 26").rename(self.data / "renamed")
        result = self.sync.scan_once()
        self.assertEqual((result.linked, result.skipped), (1, 1))
        self.assertTrue((self.data / "season 26" / "bahrein.mp4").samefile(matched))
        self.assertEqual((self.data / "renamed" / "bahrein.mp4").read_bytes(), b"existing")

    def test_2027_rule_creates_season_27_when_missing(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config.json")
        sync = LinkSync(self.data, self.torrent, config, clock=lambda: self.now)
        source = self.source_file("Formula1.2027.Round01.Australia.Race.mkv")
        destination = self.data / "Season 2701" / "S2701E01 - Australia Race.mkv"

        self.assertFalse(destination.parent.exists())
        self.assertEqual(sync.scan_once().pending, 1)
        self.assertTrue(destination.parent.is_dir())
        self.advance()
        self.assertEqual(sync.scan_once().linked, 1)
        self.assertTrue(destination.samefile(source))

    def test_missing_nested_folder_is_created_inside_destination(self):
        config = replace(self.config, rules=(Rule("2026", Path("new/season 26")),))
        sync = LinkSync(self.data, self.torrent, config, clock=lambda: self.now)
        source = self.source_file()
        self.assertEqual(sync.scan_once().pending, 1)
        self.advance()
        self.assertEqual(sync.scan_once().linked, 1)
        self.assertTrue((self.data / "new" / "season 26" / source.name).samefile(source))

    def test_symlink_outside_destination_is_not_created(self):
        (self.data / "outside").symlink_to(self.root, target_is_directory=True)
        config = replace(self.config, rules=(Rule("2026", Path("outside/new")),))
        sync = LinkSync(self.data, self.torrent, config, clock=lambda: self.now)
        self.source_file()
        self.assertEqual(sync.scan_once().skipped, 1)
        self.assertFalse((self.root / "new").exists())

    def test_source_removal_does_not_remove_link(self):
        source = self.source_file()
        self.sync.scan_once()
        self.advance()
        self.sync.scan_once()
        destination = self.data / "season 26" / "bahrein.mp4"
        source.unlink()
        self.sync.scan_once()
        self.assertEqual(destination.read_bytes(), b"video")

    def test_symlink_source_is_ignored(self):
        source = self.source_file()
        (source.parent / "link.mp4").symlink_to(source)
        self.sync.scan_once()
        self.advance()
        result = self.sync.scan_once()
        self.assertEqual(result.linked, 1)
        self.assertFalse((self.data / "season 26" / "link.mp4").exists())

    def test_dot_destination_uses_mounted_root(self):
        source = self.source_file()
        config = replace(self.config, rules=(Rule("2026", Path(".")),))
        sync = LinkSync(self.data, self.torrent, config, clock=lambda: self.now)
        self.assertEqual(sync.scan_once().pending, 1)
        self.advance()
        self.assertEqual(sync.scan_once().linked, 1)
        destination = self.data / source.name
        self.assertEqual((source.stat().st_dev, source.stat().st_ino),
                         (destination.stat().st_dev, destination.stat().st_ino))

    def test_json_config_allows_mount_root_and_rejects_traversal(self):
        config_path = self.root / "config.json"
        raw = {"rules": [{"contains": "2026", "destination": "."}]}
        config_path.write_text(json.dumps(raw))
        config = load_config(config_path)
        self.assertIn(".mp4", config.extensions)
        self.assertEqual(config.rules[0].destination, Path("."))

        config_path.write_text(json.dumps({"rules": [{"contains": "2026", "destination": "./Season 2026"}]}))
        self.assertEqual(load_config(config_path).rules[0].destination, Path("Season 2026"))

        config_path.write_text(json.dumps({"rules": [{"contains": "2026", "destination": "../elsewhere"}]}))
        with self.assertRaises(ValueError):
            load_config(config_path)

        for destination in ("Season 2026 ", " Season 2026", "Season 2026/ Other"):
            config_path.write_text(json.dumps({
                "rules": [{"contains": "2026", "destination": destination}],
            }))
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(ValueError, "leading or trailing whitespace"):
                    load_config(config_path)


class Formula1NamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.source.mkdir()
        (self.destination / "Season 2026").mkdir(parents=True)
        self.config = Config(frozenset({".mkv"}), (Rule("2026", Path("Season 2026")),))
        self.now = 0.0

    def sync(self):
        return LinkSync(self.destination, self.source, self.config, clock=lambda: self.now,
                        registry_path=self.root / ".foldersync-links.json")

    def make_source(self, name, data=b"video"):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def ready_scan(self, sync):
        sync.scan_once()
        self.now += 120
        return sync.scan_once()

    def test_parser_examples_multiword_aliases_and_unknowns(self):
        examples = {
            "Formula1 Formula1 2026 Round16 Bahrain Qualifying.mkv":
                "S2616E01 - Bahrain Qualify.mkv",
            "Formula1.2026.Round16.Bahrain.Post-Qualifying Show.F1TV.WEB-DL.1080p.mkv":
                "S2616E01 - Bahrain Post Qualifying Show.mkv",
            "Formula1.2026.Round16.Bahrain.Qualifying.F1TV.WEB-DL.2160p.mkv":
                "S2616E01 - Bahrain Qualify.mkv",
            "Formula1.2026.Round15.Azerbaijan.Race.F1LIVE.F1TV.1080p.mkv":
                "S2615E01 - Azerbaijan Race.mkv",
            "Formula1.2026.Round12.Saudi.Arabia.Sprint.Qualifying.F1TV.mkv":
                "S2612E01 - Saudi Arabia Sprint Qualifying.mkv",
            "Formula1 2026 Round16 Bahrain Quali.mkv":
                "S2616E01 - Bahrain Qualify.mkv",
            "Formula1 2026 Round07 Spain Barcelona FP3 F1TV WEB-DL 2160p.mkv":
                "S2607E01 - Spain Barcelona Practice 3.mkv",
            "Formula1 2026 Round12 Netherlands Pre-Qualifying Show F1TV.mkv":
                "S2612E01 - Netherlands Pre Qualifying Show.mkv",
            "Formula1 2026 Round13 Italy Drivers Press Conference SKYF1UHD.mkv":
                "S2613E01 - Italy Drivers Press Conference.mkv",
        }
        for original, expected in examples.items():
            with self.subTest(original=original):
                title, reason = normalized_f1_name(original, self.config)
                self.assertIsNone(reason)
                self.assertEqual(title.episode_name(1), expected)
        self.assertIsNone(normalized_f1_name("Formula1.2026.Round16.Bahrain.Special.mkv", self.config)[0])
        self.assertEqual(normalized_f1_name("other.2026.mkv", self.config), (None, None))

    def test_monza_sessions_share_round_folder_and_increment(self):
        names = (
            "Formula1.2026.Round01.Monza.Practice.1.mp4",
            "Formula1.2026.Round01.Monza.Practice.2.mp4",
            "Formula1.2026.Round01.Monza.Qualifying.mp4",
        )
        config = load_config(Path(__file__).resolve().parents[1] / "config.json")
        sources = [self.make_source(f"2026/{name}") for name in names]
        sync = LinkSync(self.destination, self.source, config, clock=lambda: self.now,
                        registry_path=self.root / ".foldersync-links.json")
        self.assertEqual(self.ready_scan(sync).linked, 3)
        folder = self.destination / "Season 2601"
        for number, (source, event) in enumerate(zip(sources,
                ("Practice 1", "Practice 2", "Qualify")), start=1):
            self.assertTrue((folder / f"S2601E{number:02d} - Monza {event}.mp4").samefile(source))
        restarted = LinkSync(self.destination, self.source, config, clock=lambda: self.now,
                             registry_path=self.root / ".foldersync-links.json")
        self.assertEqual(self.ready_scan(restarted).already_linked, 3)
        self.assertEqual(len(list(folder.glob("*.mp4"))), 3)
        self.assertEqual(len(list(folder.glob("*.nfo"))), 3)

    def test_pending_link_creates_episode_nfo_automatically(self):
        source = self.make_source("2026/Formula1.2026.Round16.Bahrain.Qualifying.F1TV.mkv")
        sync = self.sync()
        self.assertEqual(sync.scan_once().pending, 1)
        self.now += 120
        result = sync.scan_once(pending_only=True)
        target = self.destination / "Season 2616" / "S2616E01 - Bahrain Qualify.mkv"
        nfo = target.with_suffix(".nfo")
        self.assertEqual((result.linked, result.nfo_created, result.nfo_failed), (1, 1, 0))
        self.assertTrue(target.samefile(source))
        self.assertEqual(ET.parse(nfo).getroot().findtext("title"), "Bahrain Qualify")
        self.assertEqual(ET.parse(nfo).getroot().findtext("season"), "2616")
        self.assertEqual(ET.parse(nfo).getroot().findtext("episode"), "1")
        self.assertFalse(source.with_suffix(".nfo").exists())
        self.assertEqual(source.read_bytes(), b"video")

    def test_nfo_failure_does_not_undo_link(self):
        source = self.make_source("2026/Formula1.2026.Round16.Bahrain.Race.F1TV.mkv")
        sync = self.sync()
        self.assertEqual(sync.scan_once().pending, 1)
        self.now += 120
        with patch("sync.ensure_nfo", side_effect=PermissionError("read-only destination")):
            result = sync.scan_once(pending_only=True)
        target = self.destination / "Season 2616" / "S2616E01 - Bahrain Race.mkv"
        self.assertEqual((result.linked, result.nfo_failed), (1, 1))
        self.assertTrue(target.samefile(source))
        self.assertFalse(target.with_suffix(".nfo").exists())
        self.assertIn("Cannot create episode NFO", sync.last_error)

    def test_full_scan_backfills_existing_destination_without_source(self):
        folder = self.destination / "Season 2616"
        folder.mkdir()
        video = folder / "S2616E04 - Bahrain Race.mkv"
        video.write_bytes(b"existing video")
        custom_video = folder / "S2616E05 - Bahrain Qualify.mkv"
        custom_video.write_bytes(b"another video")
        custom_nfo = custom_video.with_suffix(".nfo")
        custom_nfo.write_text("custom metadata")

        first = self.sync().scan_once()
        self.assertEqual((first.nfo_created, first.nfo_existing, first.nfo_failed), (1, 1, 0))
        self.assertEqual(ET.parse(video.with_suffix(".nfo")).getroot().findtext("episode"), "4")
        second = self.sync().scan_once()
        self.assertEqual((second.nfo_created, second.nfo_existing), (0, 2))
        self.assertEqual(custom_nfo.read_text(), "custom metadata")
        self.assertEqual(video.read_bytes(), b"existing video")

    def test_prefixless_episode_is_migrated_without_changing_its_number(self):
        source = self.make_source("2026/Formula1.2026.Round16.Bahrain.Race.F1TV.mkv")
        folder = self.destination / "Season 2616"
        folder.mkdir()
        old = folder / "2616E03 - Bahrain Race.mkv"
        old.hardlink_to(source)
        self.assertEqual(self.ready_scan(self.sync()).already_linked, 1)
        target = folder / "S2616E03 - Bahrain Race.mkv"
        self.assertFalse(old.exists())
        self.assertTrue(target.samefile(source))

        next_source = self.make_source(
            "2026/other/Formula1.2026.Round16.Bahrain.Qualifying.F1TV.mkv")
        self.assertEqual(self.ready_scan(self.sync()).linked, 1)
        self.assertTrue((folder / "S2616E04 - Bahrain Qualify.mkv").samefile(next_source))

    def test_duplicates_get_incremental_episodes_and_survive_restart(self):
        first = self.make_source("2026/a/Formula1.2026.Round16.Bahrain.Race.F1TV.2160p.mkv", b"one")
        second = self.make_source("2026/b/Formula1.2026.Round16.Bahrain.Race.F1TV.1080p.mkv", b"two")
        sync = self.sync()
        self.assertEqual(self.ready_scan(sync).linked, 2)
        destinations = sorted((self.destination / "Season 2616").glob("*.mkv"))
        self.assertEqual(len(destinations), 2)
        self.assertEqual(len(list((self.destination / "Season 2616").glob("*.nfo"))), 2)
        self.assertTrue((self.destination / "Season 2616" / "S2616E01 - Bahrain Race.mkv").samefile(first))
        self.assertTrue((self.destination / "Season 2616" / "S2616E02 - Bahrain Race.mkv").samefile(second))
        restarted = self.sync()
        self.assertEqual(restarted.scan_once().pending, 2)
        self.now += 120
        self.assertEqual(restarted.scan_once().already_linked, 2)
        self.assertEqual(sorted((self.destination / "Season 2616").glob("*.mkv")), destinations)
        saved = json.loads((self.root / ".foldersync-links.json").read_text())
        self.assertEqual(len(next(iter(saved["pairs"].values()))), 2)

    def test_reported_release_uses_short_destination_name(self):
        source = self.make_source(
            "2026/Formula1.2026.Round16.Bahrain.Qualifying.F1LIVE.F1TV."
            "WEB-DL.1080p.H264.English-MWR/"
            "Formula1.2026.Round16.Bahrain.Qualifying.F1LIVE.F1TV."
            "WEB-DL.1080p.H264.English-MWR.mkv"
        )
        self.assertEqual(self.ready_scan(self.sync()).linked, 1)
        target = self.destination / "Season 2616" / "S2616E01 - Bahrain Qualify.mkv"
        self.assertTrue(target.samefile(source))
        self.assertFalse((target.parent / "Formula1 2026 Round16 Bahrain Qualifying.mkv").exists())

    def test_saved_old_title_is_renamed_and_registry_updated(self):
        source = self.make_source("2026/Formula1.2026.Round16.Bahrain.Qualifying.F1TV.mkv")
        old = self.destination / "Season 2026" / "Formula1 2026 Round16 Bahrain Qualifying.mkv"
        old.hardlink_to(source)
        registry_path = self.root / ".foldersync-links.json"
        pair_key = json.dumps([str(self.source), str(self.destination)], ensure_ascii=False)
        registry_path.write_text(json.dumps({"version": 1, "pairs": {
            pair_key: {source.relative_to(self.source).as_posix():
                       old.relative_to(self.destination).as_posix()}
        }}))
        self.assertEqual(self.ready_scan(self.sync()).already_linked, 1)
        target = self.destination / "Season 2616" / "S2616E01 - Bahrain Qualify.mkv"
        self.assertFalse(old.exists())
        self.assertTrue(target.samefile(source))
        saved = json.loads(registry_path.read_text())
        self.assertEqual(saved["pairs"][pair_key][source.relative_to(self.source).as_posix()],
                         target.relative_to(self.destination).as_posix())

    def test_untracked_previous_round_title_moves_to_round_folder(self):
        source = self.make_source("2026/Formula1.2026.Round16.Bahrain.Qualifying.F1TV.mkv")
        old = self.destination / "Season 2026" / "Round16 Bahrain Qualifying.mkv"
        old.hardlink_to(source)
        self.assertEqual(self.ready_scan(self.sync()).already_linked, 1)
        target = self.destination / "Season 2616" / "S2616E01 - Bahrain Qualify.mkv"
        self.assertFalse(old.exists())
        self.assertTrue(target.samefile(source))

    def test_untracked_old_title_is_renamed(self):
        source = self.make_source("2026/Formula1.2026.Round16.Bahrain.Qualifying.F1TV.mkv")
        old = self.destination / "Season 2026" / "Formula1 2026 Round16 Bahrain Qualifying.mkv"
        old.hardlink_to(source)
        self.assertEqual(self.ready_scan(self.sync()).already_linked, 1)
        self.assertFalse(old.exists())
        self.assertTrue((self.destination / "Season 2616" / "S2616E01 - Bahrain Qualify.mkv").samefile(source))

    def test_existing_link_is_migrated_without_changing_source(self):
        source = self.make_source("2026/Formula1.2026.Round16.Bahrain.Qualifying.F1TV.mkv")
        old = self.destination / "Season 2026" / source.name
        old.hardlink_to(source)
        sync = self.sync()
        self.assertEqual(self.ready_scan(sync).already_linked, 1)
        clean = self.destination / "Season 2616" / "S2616E01 - Bahrain Qualify.mkv"
        self.assertFalse(old.exists())
        self.assertTrue(clean.samefile(source))
        self.assertTrue(source.exists())
        restarted = self.sync()
        self.assertEqual(restarted.scan_once().pending, 1)
        self.now += 120
        self.assertEqual(restarted.scan_once().already_linked, 1)

    def test_configured_names_and_aliases_are_used(self):
        config_path = self.root / "custom.json"
        config_path.write_text(json.dumps({
            "rules": [{"contains": "2026", "destination": "Season 2026"}],
            "formula1": {
                "grand_prix": ["Custom Coast"],
                "events": {"Timed Session": "Time-Trial"},
            },
        }))
        config = load_config(config_path)
        title, reason = normalized_f1_name(
            "Formula1.2026.Round1.Custom.Coast.Timed.Session.mkv", config)
        self.assertIsNone(reason)
        self.assertEqual(title.episode_name(1), "S2601E01 - Custom Coast Time Trial.mkv")
        self.assertIsNone(normalized_f1_name(
            "Formula1.2026.Round1.Bahrain.Race.mkv", config)[0])

    def test_missing_saved_destination_is_recreated_at_same_name(self):
        source = self.make_source("2026/Formula1.2026.Round16.Bahrain.Race.F1TV.mkv")
        self.assertEqual(self.ready_scan(self.sync()).linked, 1)
        target = self.destination / "Season 2616" / "S2616E01 - Bahrain Race.mkv"
        target.unlink()
        restarted = self.sync()
        self.assertEqual(restarted.scan_once().pending, 1)
        self.now += 120
        self.assertEqual(restarted.scan_once().linked, 1)
        self.assertTrue(target.samefile(source))
        self.assertEqual(source.name, "Formula1.2026.Round16.Bahrain.Race.F1TV.mkv")

    def test_unknown_name_uses_original_and_stale_match_is_reported(self):
        source = self.make_source("2026/Formula1.2026.Round16.Bahrain.Special.F1TV.mkv")
        sync = self.sync()
        self.assertEqual(self.ready_scan(sync).linked, 1)
        target = self.destination / "Season 2026" / source.name
        self.assertTrue(target.samefile(source))
        source.unlink()
        source.write_bytes(b"replacement")
        restarted = self.sync()
        restarted.scan_once()
        self.now += 120
        with self.assertLogs("folder_sync", level="WARNING") as logs:
            result = restarted.scan_once()
        self.assertEqual(result.skipped, 1)
        self.assertIn("Saved destination conflicts", " ".join(logs.output))
        self.assertEqual(target.read_bytes(), b"video")
        self.assertEqual(source.read_bytes(), b"replacement")


class DirectoryPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.source.mkdir()
        self.destination.mkdir()

    def test_accepts_separate_mount_paths(self):
        self.assertEqual(load_directories(self.source, self.destination),
                         (self.destination, self.source))

    def test_environment_paths_support_one_shared_mount(self):
        with patch.dict("os.environ", {"SOURCE_DIR": str(self.source),
                                      "DESTINATION_DIR": str(self.destination)}):
            self.assertEqual(load_directories(), (self.destination, self.source))

    def test_rejects_missing_mount(self):
        with self.assertRaises(FileNotFoundError):
            load_directories(self.source, self.root / "missing")

    def test_rejects_overlapping_directories(self):
        nested = self.destination / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, "overlap"):
            load_directories(nested, self.destination)

    def test_rejects_symlink_to_same_directory(self):
        alias = self.root / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "overlap"):
            load_directories(self.source, alias)


if __name__ == "__main__":
    unittest.main()
