import contextlib
import io
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from nfo import episode_xml, main, scan


class NfoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.season = self.root / "Season 2616"
        self.season.mkdir()

    def test_preview_write_and_repeat_preserve_media(self):
        video = self.season / "S2616E01 - Bahrain Qualify.mkv"
        video.write_bytes(b"video")
        nfo = video.with_suffix(".nfo")

        preview = scan(self.root)
        self.assertEqual((preview.scanned, preview.parsed, preview.planned, preview.created),
                         (1, 1, 1, 0))
        self.assertFalse(nfo.exists())

        written = scan(self.root, write=True)
        self.assertEqual(written.created, 1)
        self.assertEqual(video.read_bytes(), b"video")
        self.assertEqual(ET.parse(nfo).getroot().findtext("title"), "Bahrain Qualify")
        self.assertEqual(nfo.read_text(), (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
            '<episodedetails>\n'
            '    <title>Bahrain Qualify</title>\n'
            '    <season>2616</season>\n'
            '    <episode>1</episode>\n'
            '</episodedetails>\n'
        ))

        original_nfo = nfo.read_bytes()
        repeated = scan(self.root, write=True)
        self.assertEqual((repeated.created, repeated.existing), (0, 1))
        self.assertEqual(nfo.read_bytes(), original_nfo)

    def test_escaping_other_extensions_and_unparsed_names(self):
        title = 'Tom & "Jerry" <Race> \'Final\''
        # Exercise XML escaping directly, including characters Windows forbids in paths.
        xml = episode_xml(2616, 2, title)
        video = self.season / "S2616E02 - Tom & Jerry Race Final.MP4"
        video.write_bytes(b"video")
        (self.season / "unknown.mkv").write_bytes(b"other")
        summary = scan(self.root, write=True)

        self.assertEqual((summary.scanned, summary.parsed, summary.unparsed, summary.created),
                         (2, 1, 1, 1))
        self.assertIn("&amp;", xml)
        self.assertIn("&lt;Race&gt;", xml)
        self.assertIn("&quot;Jerry&quot;", xml)
        self.assertIn("&apos;Final&apos;", xml)
        self.assertEqual(ET.fromstring(xml).findtext("title"), title)
        self.assertEqual(ET.parse(video.with_suffix(".nfo")).getroot().findtext("title"), "Tom & Jerry Race Final")
        self.assertFalse((self.season / "unknown.nfo").exists())

    def test_existing_nfo_is_not_overwritten(self):
        video = self.season / "S2616E03 - Bahrain Race.avi"
        video.write_bytes(b"video")
        nfo = video.with_suffix(".nfo")
        nfo.write_text("custom metadata")
        result = scan(self.root, write=True)
        self.assertEqual((result.existing, result.created), (1, 0))
        self.assertEqual(nfo.read_text(), "custom metadata")

    def test_cli_defaults_to_preview_and_reports_summary(self):
        (self.season / "S2616E04 - Bahrain Sprint.mov").write_bytes(b"video")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("WOULD CREATE", output.getvalue())
        self.assertIn("scanned=1 parsed=1 created=0", output.getvalue())
        self.assertFalse((self.season / "S2616E04 - Bahrain Sprint.nfo").exists())


if __name__ == "__main__":
    unittest.main()
