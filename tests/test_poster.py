import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from poster import _fallback_rank, _matches, find_image, save_image, scan, season_gp
from sync import CONFIG_FILE, Config, LinkSync, Rule, load_config, normalized_f1_name


class Response:
    def __init__(self, body, url="https://i.redd.it/poster.jpg", headers=None):
        self.body = io.BytesIO(body)
        self.url = url
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.body.close()

    def read(self, size=-1):
        return self.body.read(size)

    def geturl(self):
        return self.url


class PosterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "Season 2616"
        self.folder.mkdir()
        (self.folder / "S2616E01 - Bahrain Qualify.mkv").write_bytes(b"video")
        self.gps = ("Bahrain", "Spain", "Spain Barcelona", "Azerbaijan")

    def test_media_kit_title_must_match_gp_and_year(self):
        base = {"subreddit": "Formula1posters"}
        examples = (
            ("Bahrain Grand Prix In Malaysia 2026 [Media Kit]", "Bahrain", True),
            ("F1 Australian Grand Prix 2026 [Media Kit]", "Australia", True),
            ("F1 Japanese Grand Prix 2026 [Media Kit]", "Japan", True),
            ("F1 Canadian Grand Prix 2026 [Media Kit]", "Canada", True),
            ("Barcelona-Catalunya Grand Prix 2026 [Media Kit]", "Barcelona-Catalunya", True),
            ("Monaco Grand Prix 2026 [F1 Media Kit]", "Monaco", True),
            ("Spanish Grand Prix 2026 [Media Kit]", "Spain Barcelona", True),
            ("Azerbaijan Grand Prix 2026 [Media Kit]", "Azerbaijan", True),
            ("Bahrain Grand Prix 2025 [Media Kit]", "Bahrain", False),
            ("Bahrain Grand Prix 2026 [Fan Art]", "Bahrain", False),
            ("Bahrain Grand Prix 2026 [Media Kit]", "Spain", False),
        )
        for title, gp, expected in examples:
            with self.subTest(title=title):
                self.assertEqual(_matches({**base, "title": title}, gp, 2026), expected)
        self.assertFalse(_matches({"subreddit": "other", "title": examples[0][0]}, "Bahrain", 2026))

    def test_gp_inference_handles_normalized_and_mixed_names(self):
        folder = self.root / "Season 2604"
        folder.mkdir()
        (folder / "S2604E01 - Emilia Romagna Race.mkv").write_bytes(b"video")
        self.assertEqual(season_gp(folder, "2604", ("Emilia-Romagna",)), "emilia romagna")
        (self.folder / "S2616E02 - Spain Race.mkv").write_bytes(b"video")
        self.assertIsNone(season_gp(self.folder, "2616", self.gps))
        barcelona = self.root / "Season 2607"
        barcelona.mkdir()
        (barcelona / "S2607E01 - Spain Barcelona Race.mkv").write_bytes(b"video")
        self.assertEqual(season_gp(barcelona, "2607", self.gps), "barcelona catalunya")

    def test_2026_rounds_through_bahrain_can_be_named_and_matched(self):
        config = load_config(CONFIG_FILE)
        rounds = (
            (1, "Australia", "F1 Australian Grand Prix 2026 [Media Kit]"),
            (2, "China", "Chinese Grand Prix 2026 [Media Kit]"),
            (3, "Japan", "F1 Japanese Grand Prix 2026 [Media Kit]"),
            (4, "Miami", "Miami Grand Prix 2026 [Media Kit]"),
            (5, "Canada", "F1 Canadian Grand Prix 2026 [Media Kit]"),
            (6, "Monaco", "Monaco Grand Prix 2026 [F1 Media Kit]"),
            (7, "Barcelona-Catalunya", "Barcelona-Catalunya Grand Prix 2026 [Media Kit]"),
            (8, "Austria", "Austrian Grand Prix 2026 [Media Kit]"),
            (9, "Great Britain", "British Grand Prix 2026 [Media Kit]"),
            (10, "Belgium", "Belgian Grand Prix 2026 [Media Kit]"),
            (11, "Hungary", "Hungarian Grand Prix 2026 [Media Kit]"),
            (12, "Netherlands", "Dutch Grand Prix 2026 [Media Kit]"),
            (13, "Italy", "Italian Grand Prix 2026 [Media Kit]"),
            (14, "Spain", "Spanish Grand Prix 2026 [Media Kit]"),
            (15, "Azerbaijan", "Azerbaijan Grand Prix 2026 [Media Kit]"),
            (16, "Bahrain", "Bahrain Grand Prix In Malaysia 2026 [Media Kit]"),
        )
        for number, gp, post_title in rounds:
            with self.subTest(round=number, gp=gp):
                source_name = f"Formula1.2026.Round{number:02d}.{gp.replace(' ', '.')}.Race.mkv"
                parsed, reason = normalized_f1_name(source_name, config)
                self.assertIsNone(reason)
                self.assertEqual(parsed.code, f"26{number:02d}")
                folder = self.root / f"Season 26{number:02d}"
                if not folder.exists():
                    folder.mkdir()
                video = folder / parsed.episode_name(1)
                video.write_bytes(b"video")
                inferred = season_gp(folder, parsed.code, config.grand_prix)
                self.assertTrue(_matches({"subreddit": "Formula1posters", "title": post_title},
                                         inferred, 2026))

    def test_search_selects_matching_image_post(self):
        posts = [
            {"title": "Bahrain Grand Prix 2025 [Media Kit]", "url": "https://i.redd.it/old.jpg"},
            {"title": "Bahrain Grand Prix In Malaysia 2026 [Media Kit]", "url": "https://example.com/wrong.jpg",
             "gallery_data": {"items": [{"media_id": "abc"}]},
             "media_metadata": {"abc": {"s": {"u": "https://i.redd.it/new.png?x=1&amp;y=2"}}}},
        ]
        body = json.dumps({"data": {"children": [{"data": {**post, "subreddit": "Formula1posters"}}
                                                 for post in posts]}}).encode()
        with patch("poster._reddit_auth", return_value=("token", "test user agent")), \
             patch("poster.urlopen", return_value=Response(body)) as opener:
            self.assertEqual(find_image("Bahrain", 2026), "https://i.redd.it/new.png?x=1&y=2")
        self.assertIn("restrict_sr=1", opener.call_args.args[0].full_url)
        self.assertIn("sort=new", opener.call_args.args[0].full_url)
        self.assertTrue(opener.call_args.args[0].full_url.startswith("https://oauth.reddit.com/"))
        self.assertEqual(opener.call_args.args[0].get_header("Authorization"), "bearer token")

    def test_missing_oauth_settings_report_a_clear_error(self):
        from poster import PosterAuthError, _reddit_auth
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(PosterAuthError, "REDDIT_CLIENT_ID"):
                _reddit_auth()

    def test_official_fallback_is_used_after_missing_media_kit(self):
        empty = json.dumps({"data": {"children": []}}).encode()
        posts = [
            {"title": "Ferrari Chinese Grand Prix 2026", "score": 100,
             "url": "https://i.redd.it/team.jpg"},
            {"title": "FIA Chinese Grand Prix 2026", "url": "https://i.redd.it/fia.jpg"},
            {"title": "Official 2026 Formula 1 Chinese Grand Prix Poster [1020x1275]",
             "url": "https://i.redd.it/official.jpg"},
        ]
        listing = json.dumps({"data": {"children": [{"data": {**post, "subreddit": "Formula1posters"}}
                                                    for post in posts]}}).encode()
        with patch("poster._reddit_auth", return_value=("token", "test user agent")), \
             patch("poster.urlopen", side_effect=[Response(empty), Response(listing)]) as opener:
            self.assertEqual(find_image("China", 2026), "https://i.redd.it/official.jpg")
        self.assertEqual(opener.call_count, 2)
        self.assertIn("sort=relevance", opener.call_args.args[0].full_url)
        self.assertIsNone(_fallback_rank({"subreddit": "Formula1posters",
                                          "title": "Official 2025 Formula 1 Chinese Grand Prix Poster"},
                                         "China", 2026))
        self.assertEqual(_fallback_rank({"subreddit": "Formula1posters",
                                           "title": "Ferrari Chinese Grand Prix 2026"},
                                          "China", 2026), 3)
        self.assertEqual(_fallback_rank({"subreddit": "Formula1posters",
                                         "title": "FIA Chinese Grand Prix 2026"}, "China", 2026), 1)
        self.assertEqual(_fallback_rank({"subreddit": "Formula1posters",
                                         "title": "Barcelona-Catalunya Grand Prix 2026 [FIA]"},
                                        "Barcelona-Catalunya", 2026), 1)

    def test_team_fallback_matches_miami_2026_and_rejects_other_posts(self):
        teams = (
            "McLaren", "Red Bull", "Racing Bulls", "Scuderia Ferrari",
            "Mercedes", "Williams", "Alpine", "Cadillac",
        )
        for team in teams:
            with self.subTest(team=team):
                post = {"subreddit": "Formula1posters",
                        "title": f"{team} Miami Grand Prix 2026"}
                self.assertEqual(_fallback_rank(post, "Miami", 2026), 3)
                self.assertIsNone(_fallback_rank(post, "Bahrain", 2026))
        for title in (
            "Aston Martin Miami Grand Prix 2025",
            "Racing Bulls Miami Grand Prix 2025 [May the 4th edition]",
            "Photographer Miami Grand Prix 2026",
        ):
            with self.subTest(title=title):
                self.assertIsNone(_fallback_rank(
                    {"subreddit": "Formula1posters", "title": title}, "Miami", 2026))
        self.assertEqual(_fallback_rank(
            {"subreddit": "Formula1posters",
             "title": "McLaren Bahrain Grand Prix In Malaysia 2026"},
            "Bahrain", 2026), 3)
        self.assertIsNone(_fallback_rank(
            {"subreddit": "other", "title": "McLaren Miami Grand Prix 2026"},
            "Miami", 2026))

    def test_team_fallback_uses_highest_score_when_no_official_image(self):
        empty = json.dumps({"data": {"children": []}}).encode()
        posts = [
            {"title": "Aston Martin Miami Grand Prix 2025", "score": 100,
             "url": "https://i.redd.it/old.jpg"},
            {"title": "McLaren Miami Grand Prix 2026", "score": 10,
             "url": "https://i.redd.it/mclaren.jpg"},
            {"title": "Williams Miami Grand Prix 2026", "score": 41,
             "url": "https://i.redd.it/williams.jpg"},
            {"title": "Mercedes Miami Grand Prix 2026", "score": 32,
             "url": "https://i.redd.it/mercedes.jpg"},
        ]
        listing = json.dumps({"data": {"children": [{"data": {**post, "subreddit": "Formula1posters"}}
                                                    for post in posts]}}).encode()
        with patch("poster._reddit_auth", return_value=("token", "test user agent")), \
             patch("poster.urlopen", side_effect=[Response(empty), Response(listing)]):
            self.assertEqual(find_image("Miami", 2026), "https://i.redd.it/williams.jpg")

    def test_oauth_token_is_requested_once_and_cached(self):
        from poster import _reddit_auth
        env = {"REDDIT_CLIENT_ID": "client", "REDDIT_CLIENT_SECRET": "secret",
               "REDDIT_USERNAME": "poster_user"}
        token_json = json.dumps({"access_token": "access", "expires_in": 3600}).encode()
        with patch.dict("os.environ", env), patch("poster._token", None), \
             patch("poster.time.monotonic", return_value=100), \
             patch("poster.urlopen", return_value=Response(token_json)) as opener:
            self.assertEqual(_reddit_auth(), ("access", "python:foldersync.seasonposters:v1.0 (by /u/poster_user)"))
            self.assertEqual(_reddit_auth()[0], "access")
        self.assertEqual(opener.call_count, 1)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.reddit.com/api/v1/access_token")
        self.assertEqual(request.data, b"grant_type=client_credentials")
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))

    def test_download_and_existing_art_are_safe(self):
        jpeg = b"\xff\xd8\xff" + b"image bytes"
        with patch("poster.urlopen", return_value=Response(jpeg)):
            poster = save_image("https://i.redd.it/poster.jpg", self.folder, "2616")
        self.assertEqual(poster.name, "Season2616.jpg")
        self.assertEqual(poster.read_bytes(), jpeg)
        with patch("poster.urlopen", return_value=Response(b"\x89PNG\r\n\x1a\nimage")):
            self.assertIsNone(save_image("https://i.redd.it/poster.png", self.folder, "2616"))
        self.assertFalse((self.folder / "Season2616.png").exists())
        with self.assertRaisesRegex(ValueError, "Reddit image hosting"):
            save_image("https://example.com/poster.jpg", self.folder, "2616")
        self.assertEqual(list(self.folder.glob(".foldersync-poster-*")), [])

    def test_scan_creates_one_poster_and_preserves_existing(self):
        messages = []
        with patch("poster.find_image", return_value="https://i.redd.it/poster.jpg"), \
             patch("poster.urlopen", return_value=Response(b"\xff\xd8\xffimage")):
            preview = scan(self.root, self.gps, report=messages.append)
            self.assertEqual(preview.planned, 1)
            self.assertFalse((self.folder / "Season2616.jpg").exists())
            written = scan(self.root, self.gps, write=True, report=messages.append)
        self.assertEqual((written.created, written.errors), (1, 0))
        self.assertIn("CREATED", messages[-1])
        with patch("poster.find_image", side_effect=AssertionError("must not search")):
            repeated = scan(self.root, self.gps, write=True)
        self.assertEqual((repeated.existing, repeated.created), (1, 0))

    def test_custom_existing_poster_is_left_untouched(self):
        art = self.folder / "poster.png"
        art.write_bytes(b"custom")
        with patch("poster.find_image", side_effect=AssertionError("must not search")):
            result = scan(self.root, self.gps, write=True)
        self.assertEqual((result.existing, result.created), (1, 0))
        self.assertEqual(art.read_bytes(), b"custom")

    def test_unrecognized_season_is_counted_and_reported(self):
        folder = self.root / "Season 2604"
        folder.mkdir()
        (folder / "S2604E01 - Unknown Race.mkv").write_bytes(b"video")
        messages = []
        result = scan(self.root, self.gps, write=True, report=messages.append)
        self.assertEqual((result.unparsed, result.created), (1, 0))
        self.assertTrue(any("UNPARSED poster season" in message for message in messages))

    def test_new_link_gets_poster_during_pending_scan(self):
        source_root = self.root / "source"
        destination = self.root / "destination"
        source_root.mkdir()
        destination.mkdir()
        source = source_root / "Formula1.2026.Round16.Bahrain.Race.mkv"
        source.write_bytes(b"untouched media")
        config = Config(frozenset({".mkv"}), (Rule("2026", Path(".")),), self.gps)
        now = [0.0]
        sync = LinkSync(destination, source_root, config, clock=lambda: now[0],
                        registry_path=self.root / "links.json")
        self.assertEqual(sync.scan_once().pending, 1)
        now[0] = 120.0
        with patch("poster.find_image", return_value="https://i.redd.it/poster.jpg"), \
             patch("poster.urlopen", return_value=Response(b"\xff\xd8\xffposter")):
            result = sync.scan_once(pending_only=True)
        self.assertEqual((result.linked, result.posters_created, result.posters_failed), (1, 1, 0))
        self.assertEqual((destination / "Season 2616" / "Season2616.jpg").read_bytes(),
                         b"\xff\xd8\xffposter")
        self.assertEqual(source.read_bytes(), b"untouched media")

    def test_sync_retries_unavailable_posters_after_six_hours(self):
        source = self.root / "source"
        source.mkdir()
        config = Config(frozenset({".mkv"}), (Rule("2026", Path(".")),), self.gps)
        now = [0.0]
        sync = LinkSync(self.root, source, config, clock=lambda: now[0],
                        registry_path=self.root / "links.json")
        with patch("poster.find_image", return_value=None) as lookup:
            self.assertEqual(sync.scan_once().posters_missing, 1)
            deferred = sync.scan_once()
            self.assertEqual((deferred.posters_missing, deferred.posters_deferred), (0, 1))
            sync.poster_retry_after = 6 * 60 * 60
            self.assertEqual(sync.scan_once(retry_posters=True).posters_missing, 1)
            self.assertEqual(sync.poster_retry_after, 0.0)
            now[0] = 6 * 60 * 60
            self.assertEqual(sync.scan_once().posters_missing, 1)
        self.assertEqual(lookup.call_count, 3)


if __name__ == "__main__":
    unittest.main()
