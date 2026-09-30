from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    openai_api_key: str
    openai_model: str = "gpt-6-luna"
    database_url: str = "sqlite+aiosqlite:///./pohudai.db"
    app_timezone: str = "Europe/Moscow"
    evening_poll_hour: int = 20
    daily_summary_hour: int = 22

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
