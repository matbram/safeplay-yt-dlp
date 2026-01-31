from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Server
    PORT: int = 3002
    ENVIRONMENT: str = "production"

    # Supabase
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str

    # OxyLabs Residential Proxies
    OXYLABS_USERNAME: str
    OXYLABS_PASSWORD: str

    # Internal Authentication
    API_KEY: str

    # Temp storage
    TEMP_DIR: str = "/tmp/safeplay-downloads"

    # Storage bucket name
    STORAGE_BUCKET: str = "videos"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # Allow extra env vars (agent config, etc.)


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
