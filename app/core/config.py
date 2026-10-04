from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = ""
    download_dir: str = "/tmp/mediafetch"

    # Plan-based source/download limits.
    free_max_file_mb: int = 100
    bronze_max_file_mb: int = 500
    platinum_max_file_mb: int = 1024
    diamond_max_file_mb: int = 2048
    premium_max_file_mb: int = 500  # legacy alias for Bronze
    admin_max_file_mb: int = 0  # 0 = unlimited at application level
    max_file_mb: int = 50  # legacy fallback
    # MTProto user-session uploader. Never put the session string in Git.
    api_id: int = 0
    api_hash: str = ""
    user_session_string: str = ""
    mtproto_upload_enabled: bool = True
    mtproto_nonpremium_max_mb: int = 2000
    mtproto_premium_max_mb: int = 4000
    # MediaFetch intentionally splits anything above 2 GB into sequential parts,
    # even when the connected Telegram user account can upload a larger single file.
    large_upload_split_mb: int = 2000
    local_bot_api_max_upload_mb: int = 2000
    max_concurrent_downloads: int = 1

    webhook_mode: bool = False
    webhook_secret: str = ""
    public_base_url: str = ""
    koyeb_public_domain: str = ""

    # Optional Local Bot API Server.
    telegram_api_base_url: str = ""
    telegram_api_file_base_url: str = ""
    antideploy_public_url: str = ""

    # YouTube/general Netscape cookies.
    ytdlp_cookies_file: str = "/tmp/mediafetch-cookies.txt"
    ytdlp_cookies_b64: str = ""

    # Dedicated Instagram cookies.
    ytdlp_instagram_cookies_file: str = "/tmp/mediafetch-instagram-cookies.txt"
    ytdlp_instagram_cookies_b64: str = ""

    # Optional Threads proxy.
    threads_proxy_url: str = ""

    # Dedicated Facebook cookies.
    ytdlp_facebook_cookies_file: str = "/tmp/mediafetch-facebook-cookies.txt"
    ytdlp_facebook_cookies_b64: str = ""

    # Optional YouTube PO-token provider URL, e.g. bgutil HTTP provider.
    youtube_pot_provider_url: str = ""

    # Operational Telegram channels. Accept numeric chat IDs or @usernames.
    dump_channel_id: str = ""
    links_log_channel_id: str = ""

    # Comma-separated platforms allowed to use imported general cookies.
    ytdlp_cookie_platforms: str = "youtube"
    ytdlp_max_retries: int = 2
    extraction_timeout_seconds: int = 60
    download_timeout_seconds: int = 900
    max_download_bytes: int = 52428800

    # Persistence and service controls.
    mongodb_uri: str = ""
    mongodb_db: str = "mediafetch"
    admin_ids: str = ""
    # Owner-only admin panel. If empty, the first ADMIN_IDS entry is owner.
    owner_id: str = ""
    free_daily_limit: int = 10
    premium_daily_limit: int = 100
    bronze_daily_limit: int = 100
    platinum_daily_limit: int = 100
    diamond_daily_limit: int = 100

    # Gateway-less manual UPI payments. Prices are configurable via environment.
    payment_upi_id: str = ""
    payment_currency: str = "INR"
    bronze_price: int = 0
    platinum_price: int = 0
    diamond_price: int = 0
    payment_duration_days: int = 30
    payment_qr_url: str = ""
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
