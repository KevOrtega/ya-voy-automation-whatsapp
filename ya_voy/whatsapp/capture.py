"""Development-only capture of raw webhook payloads (CAPTURE_INBOUND_PAYLOADS).

Used for the real-number verification (docs/plans/real-number-verification.md): every payload
that reaches the webhook, including the ones the filters drop, is written as one JSON file.
The secret envelope fields are redacted first. Captures contain real phone numbers and message
texts: they stay in the gitignored ``data/`` folder; anonymize them with
``scripts/anonymize_captures.py`` before turning them into fixtures.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_UNSAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


def capture_file_name(payload: dict[str, Any], now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    event = _UNSAFE.sub("-", str(payload.get("event") or "unknown"))
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:10]
    return f"{now:%Y%m%dT%H%M%S%fZ}_{event}_{digest}.json"


def write_capture(directory: str | Path, payload: dict[str, Any]) -> Path:
    """Write ``payload`` (already sanitized) to ``directory``; returns the file path."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / capture_file_name(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
