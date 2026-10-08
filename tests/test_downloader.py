import unittest
from urllib.parse import urlsplit

from app.downloader.detector import detect_platform
from app.downloader.service import (
    _entry_video_format,
    _youtube_api_height,
    _youtube_video_id,
    _facebook_video_page_fallback,
    _extract_profiles,
    _platform_from_url,
    _quality_selector,
    _url_variants,
)


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

    def test_youtube_channel_live_url_is_not_video_id(self):
        url = "https://www.youtube.com/live/UCcYP83kTmE?si=test"
        self.assertIsNone(_youtube_video_id(url))
        variants = _url_variants(url)
        self.assertEqual(variants[0], url)
        self.assertIn(
            "https://www.youtube.com/channel/UCcYP83kTmE/live",
            variants,
        )

    def test_youtube_video_live_url_keeps_video_id_normalization(self):
        url = "https://www.youtube.com/live/dQw4w9WgXcQ?si=test"
        self.assertEqual(_youtube_video_id(url), "dQw4w9WgXcQ")

    def test_social_profiles_have_generic_fallback(self):
        profiles = _extract_profiles("https://www.facebook.com/reel/123")
        self.assertGreaterEqual(len(profiles), 2)
        self.assertEqual(profiles[-1]["allowed_extractors"], ["generic"])

    def test_instagram_has_impersonated_generic_fallback(self):
        profiles = _extract_profiles("https://www.instagram.com/reel/123/")
        self.assertGreaterEqual(len(profiles), 3)
        self.assertEqual(profiles[-1]["allowed_extractors"], ["generic"])
        self.assertEqual(
            profiles[-1]["extractor_args"],
            {"generic": {"impersonate": "chrome"}},
        )

    def test_instagram_clean_url_variant(self):
        url = "https://www.instagram.com/reel/ABC123/?igsh=share-token&img_index=1"
        variants = _url_variants(url)
        self.assertEqual(variants[0], url)
        self.assertTrue(any(item.endswith("/reel/ABC123/") for item in variants))

    def test_ejs_runtime_is_enabled(self):
        profiles = _extract_profiles("https://www.youtube.com/watch?v=test")
        self.assertEqual(profiles[0]["js_runtimes"], {"deno": {}})
        self.assertIn("ejs:github", profiles[0]["remote_components"])

    def test_youtube_platform_profiles(self):
        url = "https://www.youtube.com/watch?v=test"
        self.assertEqual(_platform_from_url(url), "youtube")
        profiles = _extract_profiles(url)
        self.assertGreaterEqual(len(profiles), 4)
        public_clients = [
            profile["extractor_args"]["youtube"]["player_client"]
            for profile in profiles[1:]
        ]
        self.assertEqual(
            public_clients,
            [["web_safari"], ["android_vr"], ["tv"]],
        )
        self.assertEqual(
            profiles[1]["extractor_args"]["youtube"]["fetch_pot"],
            ["auto"],
        )
        self.assertEqual(
            profiles[1]["extractor_args"]["youtube"]["pot_trace"],
            ["true"],
        )
        self.assertEqual(
            profiles[1]["extractor_args"]["youtubepot-bgutilscript"]["server_home"],
            "/opt/bgutil-ytdlp-pot-provider/server",
        )
        self.assertEqual(
            profiles[1]["socket_timeout"],
            12,
        )
        self.assertIn("plugin_dirs", profiles[0])
        self.assertIn("youtubepot-bgutilscript", profiles[1]["extractor_args"])


    def test_youtube_mweb_is_last_resort(self):
        profiles = _extract_profiles("https://www.youtube.com/watch?v=test")
        clients = [
            profile.get("extractor_args", {}).get("youtube", {}).get("player_client")
            for profile in profiles
        ]
        if ["mweb"] in clients:
            self.assertEqual(clients[-1], ["mweb"])
            self.assertGreaterEqual(clients.index(["mweb"]), clients.index(["tv"]))

    def test_facebook_uses_browser_impersonation(self):
        profiles = _extract_profiles("https://www.facebook.com/reel/123")
        self.assertTrue(all(profile.get("impersonate") == "chrome" for profile in profiles))

    def test_facebook_public_page_fallback_extracts_signed_progressive_urls(self):
        import app.downloader.service as service

        class FakeResponse:
            status_code = 200
            url = "https://www.facebook.com/example/videos/123456789/"
            text = (
                '{"video_id":"123456789",'
                '"playable_url_quality_hd":"https:\\/\\/video.xx.fbcdn.net\\/v\\/hd.mp4?token=abc",'
                '"playable_url":"https:\\/\\/video.xx.fbcdn.net\\/v\\/sd.mp4?token=def"}'
            )
            content = text.encode()

        class FakeCurl:
            @staticmethod
            def get(*args, **kwargs):
                return FakeResponse()

        original = service.curl_requests
        service.curl_requests = FakeCurl
        try:
            result = _facebook_video_page_fallback(
                "https://www.facebook.com/share/v/ABC123/"
            )
        finally:
            service.curl_requests = original

        self.assertIsNotNone(result)
        info, final_url, _ = result
        self.assertEqual(final_url, "https://www.facebook.com/example/videos/123456789/")
        self.assertEqual(info["id"], "123456789")
        self.assertEqual(
            [fmt["height"] for fmt in info["formats"]],
            [1080, 480],
        )
        self.assertTrue(info["formats"][0]["url"].startswith("https://video.xx.fbcdn.net/"))

    def test_carousel_child_video_format_prefers_progressive(self):
        entry = {
            "formats": [
                {"url": "https://cdn.example/video-720.mp4", "vcodec": "h264", "acodec": "aac", "height": 720},
                {"url": "https://cdn.example/video-1080.mp4", "vcodec": "h264", "acodec": "aac", "height": 1080},
                {"url": "https://cdn.example/video-1440.mp4", "vcodec": "h264", "acodec": "none", "height": 1440},
            ]
        }
        selected = _entry_video_format(entry, "1080p")
        self.assertEqual(selected["height"], 1080)
        self.assertEqual(selected["acodec"], "aac")


    def test_youtube_api_rejects_generic_json_url_without_media_signal(self):
        import app.downloader.service as service

        original_urlopen = service.urllib.request.urlopen

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, *args):
                return b'{"url":"https://resolver.example/api/123"}'

        service.urllib.request.urlopen = lambda *args, **kwargs: FakeResponse()
        try:
            result = service._youtube_api_fallback("https://youtu.be/dQw4w9WgXcQ")
        finally:
            service.urllib.request.urlopen = original_urlopen

        assert result is None

    def test_youtube_api_codec_metadata_preserves_av1_for_candidate_filtering(self):
        import app.downloader.service as service

        original_urlopen = service.urllib.request.urlopen

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, *args):
                return (
                    b'{"formats":['
                    b'{"quality":"1080p","url":"https://cdn.example/av1.mp4","height":1080,"vcodec":"av01.0.08M.08","acodec":"mp4a.40.2"},'
                    b'{"quality":"720p","url":"https://cdn.example/h264.mp4","height":720,"vcodec":"h264","acodec":"mp4a.40.2"}]}'
                )

        service.urllib.request.urlopen = lambda *args, **kwargs: FakeResponse()
        try:
            result = service._youtube_api_fallback("https://youtu.be/dQw4w9WgXcQ")
        finally:
            service.urllib.request.urlopen = original_urlopen

        assert result is not None
        info, _, _ = result
        codecs = {(item["height"], item["vcodec"]) for item in info["formats"]}
        assert (1080, "av01.0.08m.08") in codecs
        assert (720, "h264") in codecs

    def test_youtube_api_codec_metadata_defaults_to_unknown(self):
        import app.downloader.service as service

        original_base = service._base_opts
        original_urlopen = service.urllib.request.urlopen

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, *args):
                return (
                    b'{"formats":[{"quality":"1080p",'
                    b'"url":"https://cdn.example/video.mp4",'
                    b'"height":1080}]}'
                )

        service.urllib.request.urlopen = lambda *args, **kwargs: FakeResponse()
        try:
            result = service._youtube_api_fallback("https://youtu.be/dQw4w9WgXcQ")
        finally:
            service.urllib.request.urlopen = original_urlopen
            service._base_opts = original_base

        self.assertIsNotNone(result)
        info, _, _ = result
        self.assertEqual(info["formats"][0]["vcodec"], "unknown")
        self.assertEqual(info["formats"][0]["acodec"], "unknown")

    def test_youtube_api_height_parser(self):
        self.assertEqual(_youtube_api_height("1080p"), 1080)
        self.assertEqual(_youtube_api_height("720"), 720)
        self.assertEqual(_youtube_api_height("1440x2560"), 1440)
        self.assertIsNone(_youtube_api_height("best"))

    def test_youtube_channel_live_url_does_not_extract_video_id(self):
        from app.downloader.service import _youtube_video_id

        self.assertIsNone(
            _youtube_video_id("https://www.youtube.com/live/UCcYP83kTmE?si=test"),
        )

    def test_tiktok_platform_profile(self):
        url = "https://www.tiktok.com/@user/video/123"
        self.assertEqual(_platform_from_url(url), "tiktok")
        profiles = _extract_profiles(url)
        self.assertGreaterEqual(len(profiles), 2)
        self.assertEqual(profiles[-1]["allowed_extractors"], ["generic"])

    def test_instagram_embed_variant(self):
        variants = _url_variants("https://www.instagram.com/reel/ABC123/?igsh=share")
        self.assertTrue(any("/reel/ABC123/embed/" in item for item in variants))
    def test_quality_selector(self):
        self.assertEqual(_quality_selector("best"), "bv*+ba/b")
        self.assertEqual(_quality_selector("audio"), "bestaudio/best")
        selector = _quality_selector("720p")
        self.assertIn("height<=720", selector)
        self.assertIn("bv[height<=720]", selector)
        self.assertIn("b[height<=720]", selector)

    def test_url_variant_is_parseable(self):
        for url in _url_variants("https://www.threads.com/@u/post/123"):
            self.assertTrue(urlsplit(url).netloc)


if __name__ == "__main__":
    unittest.main()
