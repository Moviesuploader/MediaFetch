import unittest
from urllib.parse import urlsplit

from app.downloader.detector import detect_platform
from app.downloader.service import _extract_profiles, _quality_selector, _url_variants


class DownloaderRoutingTests(unittest.TestCase):
    def test_platform_detection_variants(self):
        cases = {
            "https://m.facebook.com/reel/123": "Facebook",
            "https://mobile.x.com/user/status/123": "X",
            "https://old.reddit.com/r/test/comments/abc/post": "Reddit",
            "https://www.threads.net/@user/post/123": "Threads",
            "https://pin.it/abc123": "Pinterest",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_platform(url), expected)

    def test_url_variants_keep_original(self):
        url = "https://www.facebook.com/share/r/abc/"
        variants = _url_variants(url)
        self.assertEqual(variants[0], url)
        self.assertTrue(any("m.facebook.com" in item for item in variants))

    def test_social_profiles_have_generic_fallback(self):
        profiles = _extract_profiles("https://www.facebook.com/reel/123")
        self.assertGreaterEqual(len(profiles), 2)
        self.assertEqual(profiles[-1]["allowed_extractors"], ["generic"])

    def test_quality_selector(self):
        self.assertEqual(_quality_selector("best"), "bv*+ba/b")
        self.assertEqual(_quality_selector("audio"), "bestaudio/best")
        self.assertIn("height<=?720", _quality_selector("720p"))

    def test_url_variant_is_parseable(self):
        for url in _url_variants("https://www.threads.com/@u/post/123"):
            self.assertTrue(urlsplit(url).netloc)


if __name__ == "__main__":
    unittest.main()
