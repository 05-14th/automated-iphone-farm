"""Configuration, loaded from the environment (and `api/.env` if present).

Nothing here has a secret default. A missing SUPABASE_SERVICE_ROLE_KEY or
API_TOKEN is a hard startup failure, not a silently-insecure default.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Supabase (the system of record; the ONLY thing this API talks to) ---
    supabase_url: str = Field(..., description="https://<ref>.supabase.co")
    supabase_service_role_key: str = Field(
        ...,
        description="service_role key. BYPASSRLS. Server-side only, never logged, never returned.",
    )

    # --- HTTP ---------------------------------------------------------------
    api_token: str = Field(..., min_length=16, description="Bearer token for every /v1/* route.")
    api_host: str = "127.0.0.1"
    api_port: int = 8080

    # --- Routing ------------------------------------------------------------
    default_sender_slug: str = Field(
        default="sender01",
        description="Sender a BRAND NEW contact is pinned to when the caller names none. "
        "Existing contacts always keep their sticky sender.",
    )

    # --- Operational --------------------------------------------------------
    log_level: str = "info"
    docs_enabled: bool = True
    # CORS is OFF by default. Set a comma-separated list to enable it.
    cors_origins: str = ""
    db_timeout_seconds: float = 20.0
    # Advertised, not enforced here: the Mac paces sends at 8-12 s per sender.
    send_pace_seconds_min: int = 8
    send_pace_seconds_max: int = 12

    @field_validator("supabase_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
