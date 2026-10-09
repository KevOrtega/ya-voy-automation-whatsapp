"""Logging setup: one JSON object per line (RNF-08)."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

# Attributes every LogRecord has; anything else passed via `extra=` is emitted as a field.
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "color_message"}


# The Evolution webhook carries WEBHOOK_SECRET in its path (/webhooks/evolution/{secret}).
# Everything after the prefix (secret, extra segments, query string) is replaced, whether or not
# the secret is correct, up to whitespace or a quote (the end of the path in access log lines).
_WEBHOOK_PATH_RE = re.compile(r"(/webhooks/evolution/)[^\s\"'<>]*", re.IGNORECASE)
REDACTED = "***"


def redact(text: str) -> str:
    """Hide the webhook secret in any string that may contain a request path or URL."""
    return _WEBHOOK_PATH_RE.sub(rf"\g<1>{REDACTED}", text)


def _redact_value(value: Any) -> Any:
    return redact(value) if isinstance(value, str) else value


class RedactWebhookSecretFilter(logging.Filter):
    """Rewrite records in place so no handler or formatter ever sees the webhook secret.

    Covers uvicorn's access records (``msg`` template + ``args`` tuple with the path) and any
    other record that interpolates or embeds a request path.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_value(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_value(v) for k, v in record.args.items()}
        for key, value in list(vars(record).items()):
            if key not in _RESERVED and isinstance(value, str):
                setattr(record, key, redact(value))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Last line of defense: also covers tracebacks and non-string extras.
        return redact(json.dumps(payload, default=str, ensure_ascii=False))


# Loggers that uvicorn and arq configure with their own plain-text handlers.
_THIRD_PARTY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "arq")


def configure_logging(level: str = "INFO") -> None:
    """Route everything through a single JSON handler on the root logger."""
    redact_filter = RedactWebhookSecretFilter()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    # On the handler: applies to every record it emits, including ones propagated from children.
    handler.addFilter(redact_filter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in _THIRD_PARTY_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # Also at the source, in case another handler is attached to uvicorn's loggers later.
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactWebhookSecretFilter) for f in logger.filters):
            logger.addFilter(redact_filter)
