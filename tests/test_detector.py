from app.downloader.detector import detect_platform


def test_youtube_variants() -> None:
    assert detect_platform("https://www.youtube.com/watch?v=abc") == "YouTube"
    assert detect_platform("https://youtu.be/abc") == "YouTube"


def test_social_platforms() -> None:
    assert detect_platform("https://www.instagram.com/reel/abc") == "Instagram"
    assert detect_platform("https://x.com/example/status/1") == "X"
    assert detect_platform("https://www.tiktok.com/@user/video/1") == "TikTok"


def test_unknown_platform() -> None:
    assert detect_platform("https://example.com/video") == "Unknown"
