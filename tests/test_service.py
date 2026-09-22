from app.downloader.service import _quality_selector


def test_quality_selectors() -> None:
    assert _quality_selector("best") == "bv*+ba/b"
    assert "height<=?720" in _quality_selector("720p")
    assert _quality_selector("audio") == "bestaudio/best"
    assert _quality_selector("photo") == ""


def test_quality_rejects_unknown() -> None:
    try:
        _quality_selector("999x")
    except Exception as exc:
        assert "Unknown download mode" in str(exc)
    else:
        raise AssertionError("unknown mode should fail")
