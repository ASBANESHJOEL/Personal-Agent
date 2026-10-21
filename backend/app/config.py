from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite:///./personal_agent.db"
    agent_state_storage: Literal["sqlite", "supabase"] = "sqlite"
    supabase_url: str | None = None
    supabase_publishable_key: str | None = None
    supabase_jwt_secret: str | None = None
    supabase_jwks_url: str | None = None
    supabase_request_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    discovery_request_timeout_seconds: float = Field(default=8.0, gt=0, le=30)
    github_token: str | None = None
    # Only the standalone Render Cron process should receive this credential.
    supabase_service_role_key: SecretStr | None = None
    scheduled_discovery_hour_utc: int = Field(default=3, ge=0, le=23)
    scheduled_discovery_worker_deployed: bool = False
    scheduled_discovery_lease_seconds: int = Field(default=1800, ge=60, le=7200)
    cors_origins: str = "http://localhost:3000"
    model_config = SettingsConfigDict(env_file=_ENV_FILE, extra="ignore", case_sensitive=False)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def supabase_storage_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_publishable_key)

    @property
    def production_storage_error(self) -> str | None:
        if self.app_env.strip().lower() != "production":
            return None
        if self.agent_state_storage != "supabase":
            return "Production requires AGENT_STATE_STORAGE=supabase; local SQLite is not durable."
        if not self.supabase_storage_configured:
            return "Production Supabase storage requires SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY."
        if self.supabase_service_role_key and self.supabase_service_role_key.get_secret_value().strip():
            return "SUPABASE_SERVICE_ROLE_KEY must be configured only on the scheduled worker, not the API service."
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
