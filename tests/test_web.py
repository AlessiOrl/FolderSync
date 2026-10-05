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
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request_json(self, path, method="GET", headers=None):
        request = Request(self.base + path, method=method, headers=headers or {})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

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

    def test_manual_sync_requests_immediate_poster_retry(self):
        with patch.object(self.service.sync, "scan_once", wraps=self.service.sync.scan_once) as scan:
            self.request_json("/api/sync", "POST", {"X-FolderSync-Action": "sync"})
        scan.assert_called_once_with(pending_only=False, retry_posters=True)

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
