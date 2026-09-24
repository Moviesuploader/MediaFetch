from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = ""
    download_dir: str = "/tmp/mediafetch"
    # Role-based source/download limits. Actual Telegram upload size depends
    # on whether a Local Bot API Server is configured.
    free_max_file_mb: int = 100
    premium_max_file_mb: int = 500
    admin_max_file_mb: int = 2000
    max_file_mb: int = 50
    max_concurrent_downloads: int = 1
    webhook_mode: bool = False
    webhook_secret: str = ""
    public_base_url: str = ""
    koyeb_public_domain: str = ""
    # Optional Local Bot API Server. Official Bot API is limited to 50 MB
    # uploads; Telegram's local server supports uploads up to 2000 MB.
    telegram_api_base_url: str = ""
    telegram_api_file_base_url: str = ""
    # Optional public HTTPS base URL for hosts such as Antideploy.
    antideploy_public_url: str = ""
    ytdlp_cookies_file: str = "/tmp/mediafetch-cookies.txt"
    # Base64-encoded Netscape/Mozilla cookies file for ephemeral hosts such as Koyeb.
    ytdlp_cookies_b64: str = ""
    # Optional separate Instagram cookie jar for sites that require login.
    ytdlp_instagram_cookies_file: str = "/tmp/mediafetch-instagram-cookies.txt"
    ytdlp_instagram_cookies_b64: str = ""
    # Comma-separated platforms allowed to use imported cookies. Keep social
    # platforms cookie-free by default; YouTube is the common cookie-dependent case.
    ytdlp_cookie_platforms: str = "youtube"
    ytdlp_max_retries: int = 2
    extraction_timeout_seconds: int = 60
    download_timeout_seconds: int = 900
    max_download_bytes: int = 52428800

    # Optional persistence and service controls.
    mongodb_uri: str = ""
    mongodb_db: str = "mediafetch"
    admin_ids: str = ""
    free_daily_limit: int = 10
    premium_daily_limit: int = 100
    premium_max_file_mb: int = 50
    cache_ttl_days: int = 7
    max_carousel_items: int = 10

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def admin_id_set(self) -> set[int]:
        result: set[int] = set()
        for value in self.admin_ids.split(","):
            value = value.strip()
            if value.isdigit():
                result.add(int(value))
        return result


settings = Settings()
