"""Application settings, loaded from environment variables (see docs/06-arquitectura.md §6)."""

from __future__ import annotations

import re
from datetime import time
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ASYNC_DB_SCHEME = "postgresql+asyncpg"
_DB_SCHEME_RE = re.compile(r"^(postgres|postgresql|postgresql\+psycopg2?|postgresql\+asyncpg)://")
_PHONE_STRIP_RE = re.compile(r"[\s+\-().]")


def to_async_database_url(url: str) -> str:
    """Normalize a Postgres URL to the SQLAlchemy asyncpg dialect.

    Dokku injects ``postgres://user:pass@host:port/db``; SQLAlchemy needs ``postgresql+asyncpg://``.
    """
    match = _DB_SCHEME_RE.match(url)
    if not match:
        raise ValueError("DATABASE_URL must be a postgres:// or postgresql:// URL")
    return f"{ASYNC_DB_SCHEME}://{url[match.end() :]}"


def normalize_phone(raw: str) -> str:
    """E.164 without '+', e.g. '+54 9 11 5555-1234' -> '5491155551234'."""
    phone = _PHONE_STRIP_RE.sub("", raw)
    if not phone.isdigit() or not 8 <= len(phone) <= 15:
        raise ValueError(f"invalid phone number: {raw!r}")
    return phone


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # General
    app_env: Literal["development", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    tz: str = "America/Argentina/Buenos_Aires"

    # Infrastructure
    database_url: str = f"{ASYNC_DB_SCHEME}://yavoy:yavoy@localhost:5432/yavoy"
    redis_url: str = "redis://localhost:6379/0"

    # Evolution API
    evolution_base_url: str = "http://localhost:8080"
    evolution_api_key: SecretStr = SecretStr("")
    evolution_instance: str = "yavoy"
    webhook_secret: SecretStr = SecretStr("")
    couriers_group_jid: str = ""
    admin_phones: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # LLM
    llm_provider: str = "openai"
    llm_model: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str | None = None
    llm_timeout_seconds: float = Field(20, gt=0)
    llm_max_retries: int = Field(3, ge=0)

    # Orders and schedule
    order_code_prefix: str = "YV-"
    business_hours_start: time = time(9, 0)
    business_hours_end: time = time(23, 0)
    business_day_rollover_time: time = time(5, 0)
    schedule_earliest_open_time: time = time(6, 0)
    schedule_latest_close_time: time = time(2, 0)
    schedule_confirmation_ttl_minutes: int = Field(5, gt=0)
    out_of_hours_reply_cooldown_minutes: int = Field(30, ge=0)
    draft_ttl_minutes: int = Field(30, gt=0)
    draft_debounce_seconds: int = Field(8, ge=0)
    max_missing_fields_prompts: int = Field(2, ge=0)
    unclaimed_alert_minutes: int = Field(20, gt=0)
    notify_loser_on_race: bool = True
    notify_reaction_on_cancelled: bool = True

    # Reports
    report_pending_reminder_minutes: int = Field(120, ge=0)
    daily_report_max_attempts: int = Field(3, gt=0)

    # Identity / anti-abuse
    store_fallback_name: str = "Local"
    courier_fallback_name: str = "Motero"
    sender_rate_limit_per_minute: int = Field(20, gt=0)
    help_reply_cooldown_minutes: int = Field(30, ge=0)

    # Worker / scheduler / outbox
    scheduler_interval_seconds: int = Field(30, gt=0)
    inbound_recovery_seconds: int = Field(60, gt=0)
    outbound_min_interval_ms: int = Field(1000, ge=0)
    outbound_max_attempts: int = Field(5, gt=0)
    data_retention_days: int = Field(90, gt=0)

    # Development tooling (docs/plans/real-number-verification.md)
    capture_inbound_payloads: bool = False
    capture_dir: str = "data/captures"

    @field_validator("database_url", mode="after")
    @classmethod
    def _async_database_url(cls, value: str) -> str:
        return to_async_database_url(value)

    @field_validator("admin_phones", mode="before")
    @classmethod
    def _split_admin_phones(cls, value: object) -> object:
        if isinstance(value, str):
            return [part for part in (p.strip() for p in value.split(",")) if part]
        return value

    @field_validator("admin_phones", mode="after")
    @classmethod
    def _normalize_admin_phones(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_phone(p) for p in value))

    @field_validator("llm_base_url", mode="before")
    @classmethod
    def _empty_base_url_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _production_requirements(self) -> Settings:
        if self.app_env == "production":
            missing = [
                name
                for name, secret in (
                    ("WEBHOOK_SECRET", self.webhook_secret),
                    ("EVOLUTION_API_KEY", self.evolution_api_key),
                )
                if not secret.get_secret_value()
            ]
            if missing:
                raise ValueError(f"required in production: {', '.join(missing)}")
            if self.capture_inbound_payloads:
                raise ValueError("CAPTURE_INBOUND_PAYLOADS is for development only")
        return self

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
