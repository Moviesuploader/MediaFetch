def test_default_settings(monkeypatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    from app.core.config import Settings

    settings = Settings()
    assert settings.download_dir == "/tmp/mediafetch"
    assert settings.max_file_mb == 50
    assert settings.max_concurrent_downloads == 2
