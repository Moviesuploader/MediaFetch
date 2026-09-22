from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    download_dir: str = "/tmp/mediafetch"
    max_file_mb: int = 50

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
