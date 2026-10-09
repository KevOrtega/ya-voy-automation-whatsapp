"""Loader for the Evolution webhook fixtures in tests/fixtures/evolution/."""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "evolution"
INSTANCE = "yavoy"
COURIERS_GROUP_JID = "120363000000000001@g.us"


@cache
def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def load_fixture(name: str, **data_key_overrides: Any) -> dict[str, Any]:
    """A fresh copy of a fixture; ``data_key_overrides`` replace fields of ``data.key``."""
    payload = copy.deepcopy(_load(name))
    if data_key_overrides:
        payload["data"]["key"].update(data_key_overrides)
    return payload


def fixture_names() -> list[str]:
    return sorted(p.stem for p in FIXTURES_DIR.glob("*.json"))
