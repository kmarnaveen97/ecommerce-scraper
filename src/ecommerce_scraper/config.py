from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    http_timeout_seconds: float = 25.0
    http_max_redirects: int = 5
    http_max_response_bytes: int = 10_000_000
    http_user_agent: str = (
        "EcommerceDiscoveryBot/0.1 (+https://github.com/kmarnaveen97/ecommerce-scraper)"
    )
    max_concurrency: int = 6


@lru_cache
def get_settings() -> Settings:
    return Settings()

