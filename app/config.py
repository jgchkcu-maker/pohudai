from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str

    ai_provider: str = "gemini"

    openai_api_key: str | None = None
    openai_model: str = "gpt-6-luna"

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash-lite"

    # Nutrition data providers. Open Food Facts works without credentials.
    fatsecret_client_id: str | None = None
    fatsecret_client_secret: str | None = None
    usda_api_key: str | None = None
    serper_api_key: str | None = None
    nutrition_region: str = "RU"
    nutrition_language: str = "ru"
    nutrition_cache_days: int = 30

    database_url: str = "sqlite+aiosqlite:///./pohudai.db"
    app_timezone: str = "Europe/Moscow"
    evening_poll_hour: int = 20
    daily_summary_hour: int = 22

    required_channel: str = "@ophudAI"
    required_channel_url: str = "https://t.me/ophudAI"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
