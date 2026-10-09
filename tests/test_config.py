from __future__ import annotations

from datetime import time

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings
from ya_voy.config import Settings, normalize_phone, to_async_database_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@db:5432/yavoy", "postgresql+asyncpg://u:p@db:5432/yavoy"),
        ("postgresql://u:p@db:5432/yavoy", "postgresql+asyncpg://u:p@db:5432/yavoy"),
        ("postgresql+psycopg2://u:p@db/yavoy", "postgresql+asyncpg://u:p@db/yavoy"),
        ("postgresql+asyncpg://u:p@db/yavoy", "postgresql+asyncpg://u:p@db/yavoy"),
    ],
)
def test_database_url_is_converted_to_asyncpg(raw: str, expected: str) -> None:
    assert to_async_database_url(raw) == expected
    assert make_settings(database_url=raw).database_url == expected


def test_database_url_from_env_dokku_style(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://yavoy:secret@dokku-postgres-yavoy-db:5432/yavoy")
    settings = Settings(_env_file=None)
    assert settings.database_url == (
        "postgresql+asyncpg://yavoy:secret@dokku-postgres-yavoy-db:5432/yavoy"
    )


def test_database_url_rejects_other_schemes() -> None:
    with pytest.raises(ValidationError):
        make_settings(database_url="mysql://u:p@db/yavoy")


def test_admin_phones_comma_separated_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PHONES", " 5491100000000, +54 9 11 2222-3333 ,,5491100000000 ")
    settings = Settings(_env_file=None)
    assert settings.admin_phones == ["5491100000000", "5491122223333"]


def test_admin_phones_single_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PHONES", "5491100000000")
    assert Settings(_env_file=None).admin_phones == ["5491100000000"]
    monkeypatch.setenv("ADMIN_PHONES", "")
    assert Settings(_env_file=None).admin_phones == []


def test_admin_phones_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PHONES", "5491100000000,not-a-phone")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("raw", ["abc", "123", "+1234567890123456"])
def test_normalize_phone_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_phone(raw)


def test_defaults_match_docs() -> None:
    s = make_settings()
    assert s.app_env == "development"
    assert s.tz == "America/Argentina/Buenos_Aires"
    assert s.business_hours_start == time(9, 0)
    assert s.business_hours_end == time(23, 0)
    assert s.business_day_rollover_time == time(5, 0)
    assert s.schedule_earliest_open_time == time(6, 0)
    assert s.schedule_latest_close_time == time(2, 0)
    assert s.draft_ttl_minutes == 30
    assert s.unclaimed_alert_minutes == 20
    assert s.report_pending_reminder_minutes == 120
    assert s.data_retention_days == 90
    assert s.order_code_prefix == "YV-"
    assert s.notify_loser_on_race is True
    assert s.llm_base_url is None


def test_env_values_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUSINESS_HOURS_END", "23:30")
    monkeypatch.setenv("NOTIFY_LOSER_ON_RACE", "false")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    s = Settings(_env_file=None)
    assert s.business_hours_end == time(23, 30)
    assert s.notify_loser_on_race is False
    assert s.llm_base_url is None
    assert s.log_level == "DEBUG"
    assert s.webhook_secret.get_secret_value() == "s3cret"
    assert "s3cret" not in repr(s)


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(tz="Mars/Olympus_Mons")


PRODUCTION = {
    "app_env": "production",
    "webhook_secret": "w",
    "evolution_api_key": "k",
    "llm_api_key": "sk-x",
    "llm_model": "m",
}


def test_production_with_every_secret_is_valid() -> None:
    assert not make_settings(**PRODUCTION).is_development


@pytest.mark.parametrize(
    ("missing", "env_var"),
    [
        ("webhook_secret", "WEBHOOK_SECRET"),
        ("evolution_api_key", "EVOLUTION_API_KEY"),
        ("llm_api_key", "LLM_API_KEY"),
        ("llm_model", "LLM_MODEL"),
    ],
)
def test_production_requires_secrets(missing: str, env_var: str) -> None:
    with pytest.raises(ValidationError, match=env_var):
        make_settings(**(PRODUCTION | {missing: ""}))


def test_production_rejects_blank_llm_model() -> None:
    with pytest.raises(ValidationError, match="LLM_MODEL"):
        make_settings(**(PRODUCTION | {"llm_model": "   "}))


def test_production_reports_every_missing_secret() -> None:
    with pytest.raises(
        ValidationError, match="WEBHOOK_SECRET, EVOLUTION_API_KEY, LLM_API_KEY, LLM_MODEL"
    ):
        make_settings(app_env="production")


def test_production_refuses_the_fake_llm() -> None:
    with pytest.raises(ValidationError, match="LLM_PROVIDER=fake"):
        make_settings(**(PRODUCTION | {"llm_provider": "fake"}))


def test_development_does_not_require_llm_settings() -> None:
    settings = make_settings(app_env="development")
    assert settings.llm_api_key.get_secret_value() == "" and settings.llm_model == ""
    assert make_settings(app_env="development", llm_provider="fake").llm_provider == "fake"


def test_capture_mode_defaults_off() -> None:
    settings = make_settings()
    assert settings.capture_inbound_payloads is False
    assert settings.capture_dir == "data/captures"


def test_capture_mode_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="CAPTURE_INBOUND_PAYLOADS"):
        make_settings(
            **PRODUCTION,
            capture_inbound_payloads=True,
        )


def test_outbound_recovery_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make_settings().outbound_recovery_seconds == 60
    monkeypatch.setenv("OUTBOUND_RECOVERY_SECONDS", "120")
    assert make_settings().outbound_recovery_seconds == 120
    with pytest.raises(ValidationError):
        make_settings(outbound_recovery_seconds=0)
