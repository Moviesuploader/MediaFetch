from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = ""
    download_dir: str = "/tmp/mediafetch"
    max_file_mb: int = 50
    max_concurrent_downloads: int = 2

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
