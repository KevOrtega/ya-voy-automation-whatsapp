from __future__ import annotations

import os

import pytest

from ya_voy.config import Settings

# Snapshot taken at import, before `clean_env` strips settings variables for each test.
# Integration tests read DATABASE_URL / REDIS_URL / RUN_INTEGRATION from here.
ORIGINAL_ENV = dict(os.environ)

# Keep the developer's shell environment out of the tests: every Settings field maps to an
# upper-case env var with the same name.
_SETTINGS_ENV_VARS = {name.upper() for name in Settings.model_fields}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SETTINGS_ENV_VARS & set(os.environ):
        monkeypatch.delenv(name, raising=False)


def make_settings(**overrides: object) -> Settings:
    """Settings built only from defaults and ``overrides`` (never reads ``.env``)."""
    return Settings(_env_file=None, **overrides)
