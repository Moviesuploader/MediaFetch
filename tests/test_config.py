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


def test_plan_and_mtproto_defaults(monkeypatch) -> None:
    for key in (
        "FREE_MAX_FILE_MB", "BRONZE_MAX_FILE_MB", "PLATINUM_MAX_FILE_MB",
        "DIAMOND_MAX_FILE_MB", "ADMIN_MAX_FILE_MB", "API_ID",
        "API_HASH", "USER_SESSION_STRING",
    ):
        monkeypatch.delenv(key, raising=False)
    from app.core.config import Settings

    settings = Settings()
    assert settings.free_max_file_mb == 100
    assert settings.bronze_max_file_mb == 500
    assert settings.platinum_max_file_mb == 1024
    assert settings.diamond_max_file_mb == 2048
    assert settings.admin_max_file_mb == 0
    assert settings.mtproto_nonpremium_max_mb == 2000
    assert settings.mtproto_premium_max_mb == 4000
