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
    target_requests_per_second: float = 1.0
    target_max_concurrency_per_host: int = 2
    target_jitter_seconds: float = 0.25
    target_max_retries: int = 3
    target_backoff_base_seconds: float = 1.0
    target_backoff_cap_seconds: float = 30.0
    target_max_retry_after_seconds: float = 120.0
    target_block_threshold: int = 3
    target_circuit_break_seconds: float = 300.0
    robots_obey: bool = True
    robots_user_agent: str = "EcommerceDiscoveryBot"


@lru_cache
def get_settings() -> Settings:
    return Settings()
