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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
