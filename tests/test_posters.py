import io
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, NumberObject, StreamObject

from app import RecentLogs, SyncService
from posters import PosterWorker, cover_image, discover, discover_media_kit, drive_download, fetch, prepare_existing, public_url
from sync import Config, LinkSync, Rule, parse_config


def image_bytes(size=(600, 900), format="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=format)
    return buffer.getvalue()


def cover_pdf(size=(600, 900), encrypted=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=900)
    stream = StreamObject()
    stream._data = image_bytes(size, "JPEG")
    stream.update({NameObject("/Type"): NameObject("/XObject"),
                   NameObject("/Subtype"): NameObject("/Image"),
                   NameObject("/Width"): NumberObject(size[0]),
                   NameObject("/Height"): NumberObject(size[1]),
                   NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                   NameObject("/BitsPerComponent"): NumberObject(8),
                   NameObject("/Filter"): NameObject("/DCTDecode")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Poster"): writer._add_object(stream)})})
    if encrypted:
        writer.encrypt("private")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class PosterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "Season 2601"
        self.folder.mkdir()
        self.worker = PosterWorker({"enabled": False})
        self.worker.configure(self.root)
        self.worker.observe(self.folder, 2026, "Australia")
        self.addCleanup(self.worker.close)

    def wait(self, worker=None):
        worker = worker or self.worker
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with worker.lock:
                if not worker.active:
                    return
            time.sleep(.01)
        self.fail("Poster worker did not finish")

    def test_manual_download_preserves_bytes_and_provenance(self):
        body = image_bytes()
        with patch("posters.fetch", return_value=(body, "https://cdn.example/poster.png")):
            self.worker.submit("Season 2601", "https://official.example/poster.png", retry=True)
            self.wait()
        self.assertEqual((self.folder / "Season2601.png").read_bytes(), body)
        metadata = json.loads((self.folder / "poster.json").read_text())
        self.assertEqual(metadata["status"], "downloaded")
        self.assertEqual(metadata["file_path"], "Season2601.png")
        self.assertEqual(metadata["year"], 2026)
        self.assertEqual(metadata["source_url"], "https://official.example/poster.png")
        self.assertFalse(list(self.folder.glob(".poster-*")))

    def test_manual_media_kit_extracts_original_cover(self):
        with patch("posters.fetch", return_value=(cover_pdf(), "https://official.example/2026-kit.pdf")):
            self.worker.submit("Season 2601", "https://official.example/2026-kit.pdf", retry=True)
            self.wait()
        self.assertEqual((self.folder / "Season2601.jpg").read_bytes(), image_bytes(format="JPEG"))
        self.assertEqual(self.worker.status()[0]["asset_kind"], "media_kit_cover")

    def test_existing_artwork_is_never_overwritten(self):
        (self.folder / "poster.jpg").write_bytes(b"custom")
        with patch("posters.fetch") as network:
            self.worker.submit("Season 2601", "https://official.example/poster.png", retry=True)
            network.assert_not_called()
        self.assertEqual((self.folder / "poster.jpg").read_bytes(), b"custom")
        self.assertEqual(self.worker.status()[0]["status"], "existing")

    def test_legacy_poster_is_prepared_without_network_or_original_changes(self):
        body = image_bytes(format="JPEG")
        (self.folder / "poster.jpg").write_bytes(body)
        (self.folder / "poster.json").write_text(json.dumps({"year": 2026, "grand_prix": "Australia", "file_path": "poster.jpg", "source_url": "https://official.example/poster"}))
        with patch("posters.fetch") as network:
            self.worker.backfill()
            network.assert_not_called()
        self.assertEqual((self.folder / "Season2601.jpg").read_bytes(), body)
        self.assertEqual((self.folder / "poster.jpg").read_bytes(), body)
        record = json.loads((self.folder / "poster.json").read_text())
        self.assertEqual(record["file_path"], "Season2601.jpg")
        self.assertEqual(record["source_url"], "https://official.example/poster")
        before = (self.folder / "Season2601.jpg").stat().st_mtime_ns
        self.worker.backfill()
        self.assertEqual((self.folder / "Season2601.jpg").stat().st_mtime_ns, before)

    def test_existing_plex_artwork_takes_priority_and_is_not_replaced(self):
        (self.folder / "season2601.png").write_bytes(b"custom art")
        (self.folder / "poster.jpg").write_bytes(image_bytes(format="JPEG"))
        self.worker.submit("Season 2601", "https://official.example/poster", retry=True)
        self.assertEqual((self.folder / "season2601.png").read_bytes(), b"custom art")
        self.assertFalse((self.folder / "Season2601.jpg").exists())

    def test_webp_download_produces_png_for_plex_and_keeps_original(self):
        body = image_bytes(format="WEBP")
        with patch("posters.fetch", return_value=(body, "https://official.example/poster.webp")):
            self.worker.submit("Season 2601", "https://official.example/poster.webp", retry=True)
            self.wait()
        self.assertEqual((self.folder / "poster.webp").read_bytes(), body)
        with Image.open(self.folder / "Season2601.png") as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (600, 900))
        self.assertEqual(self.worker.status()[0]["original_file_path"], "poster.webp")

    def test_legacy_webp_and_nested_destination_are_supported(self):
        nested = self.root / "Formula 1" / "Season 2615"
        nested.mkdir(parents=True)
        body = image_bytes(format="WEBP")
        (nested / "poster.webp").write_bytes(body)
        target = prepare_existing(nested)
        self.assertEqual(target, nested / "Season2615.png")
        self.assertEqual((nested / "poster.webp").read_bytes(), body)
        self.assertFalse((nested.parent / "Season2615.png").exists())

    def test_legacy_symlink_is_not_read_or_copied(self):
        source = self.root / "other.jpg"
        source.write_bytes(image_bytes(format="JPEG"))
        try:
            (self.folder / "poster.jpg").symlink_to(source)
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(ValueError):
            prepare_existing(self.folder)
        self.assertFalse((self.folder / "Season2601.jpg").exists())

    def test_failed_publication_cleans_temporary_download(self):
        with patch("posters.fetch", return_value=(image_bytes(), "https://official.example/p.png")), patch("posters.os.link", side_effect=OSError("read-only")):
            self.worker.submit("Season 2601", "https://official.example/p.png", retry=True)
            self.wait()
        self.assertEqual(self.worker.status()[0]["status"], "failed")
        self.assertFalse((self.folder / "Season2601.png").exists())
        self.assertFalse(list(self.folder.glob(".poster-*")))

    def test_invalid_and_small_images_leave_no_poster(self):
        for body in (b"not an image", image_bytes((100, 150)), image_bytes((900, 600))):
            with self.subTest(body_length=len(body)), patch("posters.fetch", return_value=(body, "https://official.example/image")):
                self.worker.submit("Season 2601", "https://official.example/image", retry=True)
                self.wait()
                self.assertEqual(self.worker.status()[0]["status"], "failed")
                self.assertFalse((self.folder / "Season2601.png").exists())

    def test_failure_is_persistent_and_manual_retry_recovers(self):
        with patch("posters.fetch", side_effect=OSError("offline")):
            self.worker.submit("Season 2601", "https://official.example/image", retry=True)
            self.wait()
        replacement = PosterWorker({})
        self.addCleanup(replacement.close)
        replacement.configure(self.root)
        with patch("posters.fetch") as network:
            replacement.observe(self.folder, 2026, "Australia")
            network.assert_not_called()
        self.assertEqual(replacement.status()[0]["status"], "failed")
        with patch("posters.fetch", return_value=(image_bytes(), "https://official.example/image")):
            replacement.submit("Season 2601", "https://official.example/image", retry=True)
            self.wait(replacement)
        self.assertEqual(replacement.status()[0]["status"], "downloaded")

    def test_missing_saved_poster_allows_retry(self):
        (self.folder / "poster.json").write_text(json.dumps({"status": "downloaded"}))
        self.worker.submit("Season 2601")
        self.assertEqual(self.worker.status()[0]["status"], "failed")

    def test_series_and_parent_season_posters_do_not_count_as_gp_artwork(self):
        (self.root / "poster.jpg").write_bytes(image_bytes(format="JPEG"))
        (self.root / "Season2601.png").write_bytes(image_bytes())
        self.assertFalse(self.worker.existing(self.folder))
        # Reproduce the false in-memory status shown by the dashboard.
        self.worker.races["Season 2601"].update(status="existing", file_path="poster.jpg")
        record = self.worker.status()[0]
        self.assertEqual(record["status"], "failed")
        self.assertIn("missing", record["reason"])
        self.assertNotIn("file_path", record)
        with patch("posters.fetch", return_value=(image_bytes(), "https://official.example/p.png")):
            self.worker.submit("Season 2601", "https://official.example/p.png", retry=True)
            self.wait()
        self.assertEqual(self.worker.status()[0]["status"], "downloaded")
        self.assertTrue((self.folder / "Season2601.png").is_file())

    def test_removed_poster_invalidates_existing_and_downloaded_status(self):
        for status in ("existing", "downloaded"):
            with self.subTest(status=status):
                poster = self.folder / "Season2601.png"
                poster.write_bytes(image_bytes())
                self.worker.races["Season 2601"].update(status=status, file_path=poster.name)
                self.assertEqual(self.worker.status()[0]["status"], status)
                poster.unlink()
                self.assertEqual(self.worker.status()[0]["status"], "failed")

    def test_non_file_poster_names_do_not_count_as_existing(self):
        (self.folder / "Season2601.jpg").mkdir()
        (self.folder / "poster.png").mkdir()
        self.assertFalse(self.worker.existing(self.folder))

    def test_observation_rechecks_cached_existing_status(self):
        self.worker.enabled = True
        self.worker.races["Season 2601"]["status"] = "existing"
        with patch("posters.discover", return_value=(None, None, "not_found", "No poster")) as lookup:
            self.worker.observe(self.folder, 2026, "Australia")
            self.wait()
        lookup.assert_called_once()
        self.assertEqual(self.worker.status()[0]["status"], "not_found")

    def test_unknown_folders_and_symlink_escape_rejected(self):
        for key in ("../outside", "not-found", str(self.root)):
            with self.assertRaises(ValueError):
                self.worker.submit(key, retry=True)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        redirected = self.root / "redirected"
        try:
            redirected.symlink_to(outside.name, target_is_directory=True)
        except OSError:
            return
        self.worker.races["redirected"] = {"status": "pending"}
        with self.assertRaises(ValueError):
            self.worker.submit("redirected", retry=True)

    def test_reconfiguration_discards_inflight_download(self):
        entered, release = threading.Event(), threading.Event()
        def network(url):
            entered.set()
            release.wait(2)
            return image_bytes(), url
        with patch("posters.fetch", side_effect=network):
            self.worker.submit("Season 2601", "https://official.example/image", retry=True)
            self.assertTrue(entered.wait(1))
            self.worker.configure(self.root)
            release.set()
            self.worker.close()
        self.assertFalse((self.folder / "Season2601.png").exists())
        self.assertFalse((self.folder / "poster.json").exists())

    def test_multiple_sessions_wait_for_one_poster_lookup_before_linking(self):
        torrent = self.root / "source"
        data = self.root / "destination"
        torrent.mkdir(); data.mkdir()
        for session in ("Practice.1", "Race"):
            (torrent / f"Formula1.2026.Round01.Australia.{session}.mp4").write_bytes(b"video")
        config = Config(frozenset({".mp4"}), (Rule("2026", Path(".")),), posters={"sources": {"Australia": ["https://official.example/"]}})
        now = [0]
        sync = LinkSync(data, torrent, config, stable_seconds=0, clock=lambda: now[0])
        service = SyncService(sync, RecentLogs())
        self.addCleanup(service.posters.close)
        entered, release = threading.Event(), threading.Event()
        def network(url):
            entered.set(); release.wait(2)
            return b"<h1>No poster</h1>", url
        with patch("posters.fetch", side_effect=network) as calls:
            summary = service.scan_now()
            self.assertEqual((summary["linked"], summary["pending"]), (0, 2))
            self.assertFalse(list(data.rglob("*.mp4")))
            self.assertTrue(entered.wait(1))
            service.scan_now()
            self.assertEqual(calls.call_count, 1)
            release.set()
            self.wait(service.posters)
            self.assertEqual(service.scan_now()["pending"], 2)
            now[0] = 60
            service.scan_now()
            self.assertEqual(calls.call_count, 1)
        self.assertEqual(service.status()["posters"][0]["status"], "not_found")
        self.assertEqual(len(list((data / "Season 2601").glob("S[0-9]*.nfo"))), 2)
        self.assertEqual(len(list(data.rglob("*.mp4"))), 2)

    def test_poster_lookup_starts_before_stability_wait(self):
        torrent = self.root / "source"
        data = self.root / "destination"
        torrent.mkdir(); data.mkdir()
        (torrent / "Formula1.2026.Round01.Australia.Race.mp4").write_bytes(b"video")
        now = [0]
        sync = LinkSync(data, torrent, Config(frozenset({".mp4"}), (Rule("2026", Path(".")),)), clock=lambda: now[0])
        callback = Mock()
        sync.poster_callback = callback
        self.assertEqual(sync.scan_once().pending, 1)
        callback.assert_called_once_with(data / "Season 2601", 2026, "Australia")
        callback.reset_mock()
        now[0] = 10
        self.assertEqual(sync.scan_once(pending_only=True).linked, 1)
        callback.assert_called_once_with(data / "Season 2601", 2026, "Australia")

    def test_season_metadata_precedes_lookup_and_poster_precedes_video_link(self):
        source, data = self.root / "source", self.root / "destination"
        source.mkdir(); data.mkdir()
        video = source / "Formula1.2026.Round01.Australia.Race.mp4"
        video.write_bytes(b"video")
        now = [0]
        sync = LinkSync(data, source, Config(frozenset({".mp4"}), (Rule("2026", Path(".")),)),
                        clock=lambda: now[0])
        service = SyncService(sync, RecentLogs())
        service.posters.enabled = False
        service.posters.config_require_poster = True
        self.addCleanup(service.posters.close)
        folder = data / "Season 2601"
        def observe(directory, year, gp):
            self.assertTrue((folder / "season.nfo").is_file())
            self.assertTrue((data / "tvshow.nfo").is_file())
            service.posters.observe(directory, year, gp)
        sync.poster_callback = observe
        self.assertEqual(service.scan_now()["pending"], 1)
        self.assertFalse(list(folder.glob("*.mp4")))
        now[0] = 10
        self.assertEqual(service.scan_now()["pending"], 1)
        (folder / "Season2601.png").write_bytes(image_bytes())
        now[0] = 11
        self.assertTrue(sync.pending_ready())
        self.assertEqual(service.scan_now(pending_only=True)["linked"], 1)
        linked = next(folder.glob("*.mp4"))
        self.assertTrue(linked.samefile(video))
        self.assertTrue(linked.with_suffix(".nfo").is_file())

    def test_missing_poster_deadline_is_shared_and_does_not_reset_on_scans(self):
        source, data = self.root / "source", self.root / "destination"
        source.mkdir(); data.mkdir()
        (source / "Formula1.2026.Round01.Australia.Race.mp4").write_bytes(b"video")
        now = [0]
        sync = LinkSync(data, source, Config(frozenset({".mp4"}), (Rule("2026", Path(".")),)),
                        stable_seconds=0, clock=lambda: now[0])
        sync.poster_callback = Mock()
        sync.poster_ready = Mock(return_value=False)
        self.assertEqual(sync.scan_once().pending, 1)
        now[0] = 30
        (source / "Formula1.2026.Round01.Australia.Practice.1.mp4").write_bytes(b"video")
        self.assertEqual(sync.scan_once().pending, 2)
        now[0] = 59
        self.assertEqual(sync.scan_once(pending_only=True).pending, 2)
        self.assertEqual(sync.pending_due_at, 60)
        now[0] = 60
        self.assertTrue(sync.pending_ready())
        self.assertEqual(sync.scan_once(pending_only=True).linked, 2)


class DiscoveryTests(unittest.TestCase):
    def discover(self, html, year=2026, gp="Australia"):
        with patch("posters.fetch", return_value=(html.encode(), "https://official.example/")), patch("posters.time.sleep"):
            return discover(year, gp, {"Australia": ["https://official.example/"]})

    def test_explicit_official_poster_found(self):
        result = self.discover('<h1>Australian Grand Prix 2026 official poster</h1><img alt="Australia event poster" src="/poster.png">')
        self.assertEqual(result[:3], ("https://official.example/poster.png", "https://official.example/", "found"))

    def test_wrong_year_unrelated_generic_and_multiple_candidates(self):
        for html in (
            '<h1>Australia 2025 official poster</h1><img alt="Australia poster" src="/p.png">',
            '<h1>Australia 2026</h1><img alt="Australia hero" src="/p.png">',
            '<h1>Australia 2026 poster</h1><img alt="Australia 2025 poster" src="/p.png">',
            '<h1>Australia 2026 official poster</h1><img alt="Australia poster" src="/shop/product.png">',
        ):
            self.assertEqual(self.discover(html)[2], "not_found")
        html = '<h1>Australia 2026 poster</h1><img alt="Australia poster" src="/p.png"><img alt="Australia poster" src="/q.png">'
        self.assertEqual(self.discover(html)[0], "https://official.example/p.png")

    def test_team_artwork_is_accepted(self):
        result = self.discover('<h1>Australian Grand Prix 2026 team poster</h1><img alt="Australia driver poster" src="/p.png">')
        self.assertEqual(result[2], "found")

    def test_gallery_finds_race_and_skips_invalid_images(self):
        start = "https://gallery.example/story"
        article = start + "/2026-singapore-grand-prix-posters"
        pages = {
            start: b'<title>Gallery</title><a href="/story/2026-singapore-grand-prix-posters"></a>',
            article: b'<title>2026 Singapore Grand Prix Posters</title><img src="/logo.png" alt="Logo"><img src="/small.png" alt="Team poster"><img data-src="https://cdn.example/team.jpg" alt="Poster by Alpine F1 Team"><img src="/other.png" alt="Other poster">',
            "https://gallery.example/small.png": image_bytes((100, 150)),
            "https://cdn.example/team.jpg": image_bytes(format="JPEG"),
        }
        with patch("posters.fetch", side_effect=lambda url: (pages[url], url)) as network:
            result = discover(2026, "Singapore", {}, galleries={"*": [start]})
        self.assertEqual(result[:3], ("https://cdn.example/team.jpg", article, "found"))
        self.assertEqual(network.call_count, 4)

    def test_gallery_rejects_wrong_year_and_index_images(self):
        start = "https://gallery.example/"
        for html in (
            '<title>2025 Singapore Grand Prix Posters</title><img alt="Team poster" src="/p.jpg">',
            '<title>Gallery</title><h2>2026 Singapore Grand Prix Posters</h2><img alt="Team poster" src="/p.jpg">',
            '<title>Gallery</title><img alt="2026 Singapore Grand Prix Posters" src="/p.jpg">',
            '<title>2026 Singapore Grand Prix Posters</title><img alt="2025 team poster" src="/p.jpg">',
            '<title>2026 Bahrain GP Posters</title><img alt="Team poster" src="/p.jpg">',
        ):
            with self.subTest(html=html), patch("posters.fetch", return_value=(html.encode(), start)) as network:
                self.assertEqual(discover(2026, "Singapore", {}, galleries={"*": [start]})[2], "not_found")
                self.assertEqual(network.call_count, 1)

    def test_missing_gp_configuration_explains_problem(self):
        self.assertIn("No poster sources configured for Bahrain", discover(2026, "Bahrain", {})[3])

    def test_aliases_and_bounded_followup(self):
        start = 'https://official.example/'
        pages = {
            start: '<a href="/2026-poster">Monza 2026 poster</a>',
            start + '2026-poster': '<h1>Italian Grand Prix 2026 official poster</h1><a href="/p.jpg">Download poster</a>'
        }
        with patch("posters.fetch", side_effect=lambda url: (pages[url].encode(), url)), patch("posters.time.sleep"):
            result = discover(2026, "Monza", {"Italy": [start]})
        self.assertEqual(result[0], start + 'p.jpg')

    def test_failed_source_does_not_stop_next_source(self):
        def network(url):
            if url.endswith('failed'):
                raise OSError("offline")
            return b'<h1>Australia 2026 poster</h1><img alt="Australia poster" src="/p.png">', url
        with patch("posters.fetch", side_effect=network), patch("posters.time.sleep"):
            result = discover(2026, "Australia", {"Australia": ["https://official.example/failed", "https://official.example/ok"]})
        self.assertEqual(result[2], "found")

    def test_configuration_validation(self):
        for config in ({"enabled": "yes"}, {"sources": {"Australia": "https://example.com"}}, {"sources": {"Australia": ["file:///poster"]}}):
            with self.assertRaises(ValueError):
                parse_config({"rules": [{"contains": "2026", "destination": "."}], "posters": config})


class NetworkTests(unittest.TestCase):
    def test_private_mixed_dns_and_credentials_are_rejected(self):
        for url in ('file:///poster', 'http://user:secret@example.com/image'):
            with self.assertRaises(ValueError):
                public_url(url)
        for addresses in (["127.0.0.1"], ["::1"], ["10.0.0.1"], ["8.8.8.8", "192.168.1.1"]):
            entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 80)) for address in addresses]
            with patch("posters.socket.getaddrinfo", return_value=entries), self.assertRaises(ValueError):
                public_url("http://example.com/image")

    def test_redirect_is_revalidated_and_address_is_pinned(self):
        response = Mock(status=302)
        response.getheader.return_value = 'http://127.0.0.1/image'
        entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))]
        with patch("posters.socket.getaddrinfo", side_effect=[entries, private]), patch("posters.socket.create_connection") as connect, patch("posters.http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = response
            with self.assertRaises(ValueError):
                fetch('http://example.com/image')
            connect.assert_called_once_with(('8.8.8.8', 80), timeout=10)

    def test_response_size_limit_and_transient_retry(self):
        response = Mock(status=200)
        response.getheader.return_value = str(21 * 1024 * 1024)
        entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80))]
        with patch("posters.socket.getaddrinfo", return_value=entries), patch("posters.socket.create_connection"), patch("posters.http.client.HTTPConnection") as connection, patch("posters.time.sleep"):
            connection.return_value.getresponse.return_value = response
            with self.assertRaises(ValueError):
                fetch('http://example.com/image')
            response.read.assert_not_called()
            response.status = 503
            with self.assertRaises(OSError):
                fetch('http://example.com/image')
            self.assertEqual(connection.return_value.getresponse.call_count, 3)


class MediaKitTests(unittest.TestCase):
    def test_cover_keeps_original_bytes(self):
        self.assertEqual(cover_image(cover_pdf()), image_bytes(format="JPEG"))

    def test_unsuitable_and_encrypted_pdfs_rejected(self):
        for content in (b"%PDF-corrupt", cover_pdf((100, 150)), cover_pdf((900, 600)), cover_pdf(encrypted=True)):
            with self.subTest(length=len(content)), self.assertRaises(Exception):
                cover_image(content)

    def test_current_media_kit_drive_file_is_resolved(self):
        url = "https://www.bakucitycircuit.com/media-kit"
        drive = "https://drive.google.com/file/d/publicid/view"
        html = f'<title>Formula 1 Azerbaijan Grand Prix 2026</title><h2>Media Kit</h2><a href="{drive}">Download</a>'
        def network(target):
            return ((html if target == url else '<title>Media Kit - AZGP 2026.pdf</title>').encode(), target)
        with patch("posters.fetch", side_effect=network):
            result = discover_media_kit(2026, "Azerbaijan", {"Azerbaijan": [url]})
        self.assertEqual(result[:3], ("https://drive.google.com/uc?export=download&id=publicid", url, "found"))

    def test_old_drive_file_is_rejected_despite_current_site_title(self):
        url = "https://official.example/media-kit"
        html = '<title>Azerbaijan Grand Prix 2026</title><h2>Media Kit</h2><a href="https://drive.google.com/file/d/old/view">Download</a>'
        def network(target):
            return ((html if target == url else '<title>Media Kit - AZGP 2025.pdf</title>').encode(), target)
        with patch("posters.fetch", side_effect=network):
            result = discover_media_kit(2026, "Azerbaijan", {"Azerbaijan": [url]})
        self.assertEqual(result[2], "not_found")

    def test_direct_pdf_and_competing_media_kits(self):
        url = "https://official.example/media-kit"
        html = '<title>Azerbaijan Grand Prix 2026</title><h2>Media Kit</h2><a href="/2026-kit.pdf">Download</a>'
        with patch("posters.fetch", return_value=(html.encode(), url)):
            self.assertEqual(discover_media_kit(2026, "Azerbaijan", {"Azerbaijan": [url]})[0], "https://official.example/2026-kit.pdf")
        html += '<a href="/2026-kit-other.pdf">Download</a>'
        with patch("posters.fetch", return_value=(html.encode(), url)):
            self.assertEqual(discover_media_kit(2026, "Azerbaijan", {"Azerbaijan": [url]})[2], "ambiguous")

    def test_media_kit_is_only_used_when_no_standalone_match(self):
        html = '<h1>Australia 2026 poster</h1><img alt="Australia poster" src="/p.jpg">'
        with patch("posters.fetch", return_value=(html.encode(), "https://official.example/")), patch("posters.discover_media_kit") as fallback, patch("posters.time.sleep"):
            result = discover(2026, "Australia", {"Australia": ["https://official.example/"]}, {"Australia": ["https://official.example/media-kit"]})
        fallback.assert_not_called()
        self.assertEqual(result[2], "found")
        with patch("posters.discover_media_kit", return_value=("https://official.example/2026.pdf", "https://official.example/media-kit", "found", "")) as fallback:
            self.assertEqual(discover(2026, "Azerbaijan", {}, {"Azerbaijan": ["https://official.example/media-kit"]})[2], "found")
            fallback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
