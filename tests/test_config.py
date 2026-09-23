def test_default_settings(monkeypatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    from app.core.config import Settings

    settings = Settings()
    assert settings.download_dir == "/tmp/mediafetch"
    assert settings.max_file_mb == 50
    assert settings.max_concurrent_downloads == 1
    assert settings.free_daily_limit == 10


def test_antideploy_public_url_setting(monkeypatch) -> None:
    monkeypatch.setenv("ANTIDEPLOY_PUBLIC_URL", "https://example.antideploy.com/")
    from app.core.config import Settings

    settings = Settings()
    assert settings.antideploy_public_url == "https://example.antideploy.com/"
