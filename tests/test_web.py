import errno
import json
import logging
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import INDEX_PATH, RecentLogs, SyncService, make_handler, run_scheduler
from settings import DirectorySettings
from sync import Config, LinkSync, Rule


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.torrent = root / "torrent"
        self.data = root / "data"
        self.torrent.mkdir()
        (self.data / "season 26").mkdir(parents=True)
        source = self.torrent / "F1 grand prix 2026" / "bahrein.mp4"
        source.parent.mkdir()
        source.write_bytes(b"video")
        self.source = source
        self.now = 0.0
        config = Config(frozenset({".mp4"}), (Rule("2026", Path("season 26")),))
        sync = LinkSync(self.data, self.torrent, config, clock=lambda: self.now)
        self.logs = RecentLogs()
        self.service = SyncService(sync, self.logs)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, INDEX_PATH.read_bytes()))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.service.posters.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request_json(self, path, method="GET", headers=None):
        request = Request(self.base + path, method=method, headers=headers or {})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_poster_endpoint_requires_action_header_and_known_folder(self):
        for headers, payload, expected in (
            ({}, {"folder": "Season 2601"}, 403),
            ({"X-FolderSync-Action": "poster"}, {"folder": "../outside"}, 400),
            ({"X-FolderSync-Action": "poster"}, {"folder": "Season 2601", "url": "file:///image"}, 400),
        ):
            request = Request(self.base + "/api/poster", data=json.dumps(payload).encode(), headers=headers, method="POST")
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=3)
            self.assertEqual(caught.exception.code, expected)

    def test_poster_endpoint_queues_manual_url_and_reports_outcome(self):
        import io
        import time
        from PIL import Image
        folder = self.data / "Season 2601"
        folder.mkdir()
        self.service.posters.enabled = False
        self.service.posters.observe(folder, 2026, "Australia")
        buffer = io.BytesIO()
        Image.new("RGB", (600, 900)).save(buffer, format="JPEG")
        payload = {"folder": "Season 2601", "url": "https://official.example/poster.jpg"}
        request = Request(self.base + "/api/poster", data=json.dumps(payload).encode(),
                          headers={"X-FolderSync-Action": "poster"}, method="POST")
        with patch("posters.fetch", return_value=(buffer.getvalue(), payload["url"])):
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 202)
            deadline = time.monotonic() + 2
            while self.service.posters.status()[0]["status"] in ("queued", "downloading") and time.monotonic() < deadline:
                time.sleep(.01)
        status = self.request_json("/api/status")
        self.assertEqual(status["posters"][0]["status"], "downloaded")
        self.assertTrue((folder / "Season2601.jpg").exists())

    def test_poster_endpoint_rejects_requests_during_configuration(self):
        self.service.scan_lock.acquire()
        try:
            request = Request(self.base + "/api/poster", data=b'{"folder":"Season 2601"}',
                              headers={"X-FolderSync-Action": "poster"}, method="POST")
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=3)
            self.assertEqual(caught.exception.code, 409)
        finally:
            self.service.scan_lock.release()

    def test_full_scan_prepares_saved_poster_without_source_videos(self):
        import io
        from PIL import Image
        folder = self.data / "Season 2615"
        folder.mkdir()
        buffer = io.BytesIO()
        Image.new("RGB", (600, 900)).save(buffer, format="JPEG")
        body = buffer.getvalue()
        (folder / "poster.jpg").write_bytes(body)
        (folder / "poster.json").write_text(json.dumps({"year": 2026, "grand_prix": "Azerbaijan", "file_path": "poster.jpg"}))
        with patch("posters.fetch") as network:
            self.service.scan_now()
            network.assert_not_called()
        self.assertEqual((folder / "Season2615.jpg").read_bytes(), body)
        self.assertEqual(self.service.status()["posters"][0]["file_path"], "Season2615.jpg")

    def test_file_views_logs_and_manual_scan(self):
        self.logs.emit(logging.LogRecord("test", logging.INFO, __file__, 1, "Ready", (), None))
        with urlopen(self.base + "/", timeout=3) as response:
            page = response.read().decode()
        self.assertIn("Sync now", page)
        self.assertIn("Recent logs", page)

        status = self.request_json("/api/status")
        self.assertEqual(status["torrent_path"], str(self.torrent))
        self.assertEqual(status["data_path"], str(self.data))
        self.assertEqual(status["torrent_files"][0]["path"], "F1 grand prix 2026/bahrein.mp4")
        self.assertEqual(status["data_files"], [])
        self.assertIn("Ready", status["logs"][-1])

        headers = {"X-FolderSync-Action": "sync"}
        self.assertEqual(self.request_json("/api/sync", "POST", headers)["result"]["pending"], 1)
        self.assertEqual(self.request_json("/api/sync", "POST", headers)["result"]["pending"], 1)
        self.now += 120
        self.assertEqual(self.request_json("/api/sync", "POST", headers)["result"]["linked"], 1)
        destination = self.data / "season 26" / "bahrein.mp4"
        self.assertEqual((self.source.stat().st_dev, self.source.stat().st_ino), (destination.stat().st_dev, destination.stat().st_ino))
        self.assertEqual(self.request_json("/api/status")["data_files"][0]["path"], "season 26/bahrein.mp4")

    def test_manual_sync_requests_full_scan(self):
        with patch.object(self.service.sync, "scan_once", wraps=self.service.sync.scan_once) as scan:
            self.request_json("/api/sync", "POST", {"X-FolderSync-Action": "sync"})
        scan.assert_called_once_with(pending_only=False)

    def test_metadata_button_regenerates_all_destination_folders(self):
        import xml.etree.ElementTree as ET
        for code, gp in (("2615", "Azerbaijan"), ("2516", "Bahrain")):
            folder = self.data / "Formula 1" / f"Season {code}"
            folder.mkdir(parents=True)
            (folder / f"S{code}E01 - {gp} Race.mp4").write_bytes(b"video")
        with patch.object(self.service.sync, "scan_once") as scan, patch("posters.fetch") as fetch:
            result = self.request_json("/api/metadata", "POST", {"X-FolderSync-Action": "metadata"})["result"]
            scan.assert_not_called()
            fetch.assert_not_called()
        self.assertEqual(result["created"], 5)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(ET.parse(self.data / "Formula 1" / "tvshow.nfo").findtext("year"), "2025")
        status = self.request_json("/api/status")
        self.assertEqual(status["last_metadata_result"], result)
        self.assertIsNotNone(status["last_metadata_at"])
        self.assertFalse(status["regenerating_metadata"])
        self.assertFalse(status["scanning"])
        with urlopen(self.base + "/", timeout=3) as response:
            self.assertIn('id="metadata-button"', response.read().decode())

    def test_metadata_endpoint_requires_header_setup_and_idle_service(self):
        with self.assertRaises(HTTPError) as caught:
            self.request_json("/api/metadata", "POST")
        self.assertEqual(caught.exception.code, 403)
        headers = {"X-FolderSync-Action": "metadata"}
        self.service.scan_lock.acquire()
        try:
            with self.assertRaises(HTTPError) as caught:
                self.request_json("/api/metadata", "POST", headers)
            self.assertEqual(caught.exception.code, 409)
        finally:
            self.service.scan_lock.release()
        self.service.sync = None
        with self.assertRaises(HTTPError) as caught:
            self.request_json("/api/metadata", "POST", headers)
        self.assertEqual(caught.exception.code, 409)

    def test_metadata_failure_releases_lock(self):
        with patch("app.regenerate", side_effect=OSError("write failure")):
            with self.assertRaises(HTTPError) as caught:
                self.request_json("/api/metadata", "POST", {"X-FolderSync-Action": "metadata"})
        self.assertEqual(caught.exception.code, 500)
        self.assertFalse(self.service.scan_lock.locked())
        self.assertFalse(self.service.regenerating_metadata)

    def test_scheduler_rechecks_pending_at_ten_seconds_and_scans_at_120(self):
        scans = []
        waits = []
        original = self.service.sync.scan_once

        def scan(pending_only=False):
            scans.append((self.now, pending_only))
            return original(pending_only=pending_only)

        def wait(seconds):
            waits.append(seconds)
            self.now += seconds
            if self.now > 120:
                raise KeyboardInterrupt

        with patch("app.time.monotonic", side_effect=lambda: self.now), \
             patch.object(self.service.wake_event, "wait", side_effect=wait), \
             patch.object(self.service.sync, "scan_once", side_effect=scan):
            with self.assertRaises(KeyboardInterrupt):
                run_scheduler(self.service, 120)
        self.assertEqual(scans, [(0, False), (10, True), (120, False)])
        self.assertEqual(waits, [10, 110, 120])
        self.assertTrue(self.source.samefile(self.data / "season 26" / self.source.name))

    def test_scheduler_waits_for_scan_interval_when_no_file_is_pending(self):
        self.source.unlink()
        scans = []
        waits = []
        original = self.service.sync.scan_once

        def scan(pending_only=False):
            scans.append(pending_only)
            return original(pending_only=pending_only)

        def wait(seconds):
            waits.append(seconds)
            raise KeyboardInterrupt

        with patch("app.time.monotonic", side_effect=lambda: self.now), \
             patch.object(self.service.wake_event, "wait", side_effect=wait), \
             patch.object(self.service.sync, "scan_once", side_effect=scan):
            with self.assertRaises(KeyboardInterrupt):
                run_scheduler(self.service, 120)
        self.assertEqual(scans, [False])
        self.assertEqual(waits, [120])

    def test_cross_device_failure_is_visible_and_clears_after_recovery(self):
        headers = {"X-FolderSync-Action": "sync"}
        self.request_json("/api/sync", "POST", headers)
        self.now += 120
        with patch("sync.os.link", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
            result = self.request_json("/api/sync", "POST", headers)["result"]
        self.assertEqual(result["failed"], 1)
        status = self.request_json("/api/status")
        self.assertIn("Mount the shared host parent once", status["last_error"])
        self.assertEqual(status["last_result"]["failed"], 1)
        self.assertEqual(status["data_files"], [])
        self.assertEqual(self.request_json("/api/sync", "POST", headers)["result"]["linked"], 1)
        self.assertIsNone(self.request_json("/api/status")["last_error"])

    def test_setup_api_validates_saves_and_enables_sync(self):
        self.service.settings = DirectorySettings(self.torrent.parent)
        self.service.sync = None
        self.assertFalse(self.request_json("/api/status")["configured"])
        with self.assertRaises(HTTPError) as error:
            self.request_json("/api/sync", "POST", {"X-FolderSync-Action": "sync"})
        self.assertEqual(error.exception.code, 409)
        payload = json.dumps({"source": "torrent", "destination": "data"}).encode()
        for body, headers, expected in (
            (payload, {}, 403),
            (b"{", {"X-FolderSync-Action": "configure"}, 400),
            (b"[]", {"X-FolderSync-Action": "configure"}, 400),
            (b"x" * 8193, {"X-FolderSync-Action": "configure"}, 400),
        ):
            with self.subTest(body=body[:20]):
                request = Request(self.base + "/api/config", data=body, headers=headers)
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=3)
                self.assertEqual(error.exception.code, expected)
        request = Request(self.base + "/api/config", data=payload,
                          headers={"X-FolderSync-Action": "configure"})
        with urlopen(request, timeout=3) as response:
            self.assertEqual(json.load(response)["directories"]["source"], "torrent")
        status = self.request_json("/api/status")
        self.assertTrue(status["configured"])
        self.assertEqual(status["directories"], {"source": "torrent", "destination": "data"})
        self.assertEqual(self.request_json("/api/sync", "POST", {"X-FolderSync-Action": "sync"})["result"]["pending"], 1)

    def test_sync_requires_action_header(self):
        with self.assertRaises(HTTPError) as error:
            self.request_json("/api/sync", "POST")
        self.assertEqual(error.exception.code, 403)
        self.assertFalse((self.data / "season 26" / "bahrein.mp4").exists())


if __name__ == "__main__":
    unittest.main()
