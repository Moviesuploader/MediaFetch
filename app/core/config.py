from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = ""
    download_dir: str = "/tmp/mediafetch"
    max_file_mb: int = 50
    max_concurrent_downloads: int = 1
    webhook_mode: bool = False
    webhook_secret: str = ""
    public_base_url: str = ""
    koyeb_public_domain: str = ""
    ytdlp_cookies_file: str = "/tmp/mediafetch-cookies.txt"

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
