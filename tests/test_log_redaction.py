"""The webhook secret travels in the URL path and must never reach the logs."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
from collections.abc import Iterator

import httpx
import pytest
import uvicorn

from tests.conftest import make_settings
from ya_voy.api.main import create_app
from ya_voy.logging import JsonFormatter, RedactWebhookSecretFilter, configure_logging, redact

SECRET = "s3cr3t-Webhook-Value_42"
WRONG = "wrong-secret-guess-99"

_MANAGED = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "arq")


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    saved = {
        name: (lg.handlers[:], lg.filters[:], lg.level, lg.propagate)
        for name in _MANAGED
        for lg in [logging.getLogger(name)]
    }
    yield
    for name, (handlers, filters, level, propagate) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers[:], lg.filters[:], lg.level, lg.propagate = handlers, filters, level, propagate


def _access_record(path: str) -> logging.LogRecord:
    """Same shape as uvicorn's access log call (msg template + args tuple)."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("172.18.0.4:51234", "POST", path, "1.1", 200),
        exc_info=None,
    )


@pytest.mark.parametrize(
    "path",
    [
        f"/webhooks/evolution/{SECRET}",
        f"/webhooks/evolution/{WRONG}",
        f"/webhooks/evolution/{SECRET}?apikey=x&secret={SECRET}",
        f"/webhooks/evolution/{SECRET}/messages-upsert",
        f"/WEBHOOKS/Evolution/{SECRET}",
    ],
)
def test_access_record_is_redacted(path: str) -> None:
    record = _access_record(path)
    assert RedactWebhookSecretFilter().filter(record) is True
    output = JsonFormatter().format(record)
    message = json.loads(output)["message"]
    assert "/evolution/***" in message.lower()
    assert '"POST ' in message and " HTTP/1.1" in message
    assert SECRET not in output and WRONG not in output


def test_message_and_extras_are_redacted() -> None:
    record = logging.makeLogRecord(
        {
            "name": "ya_voy.test",
            "msg": f"calling http://web:8000/webhooks/evolution/{SECRET}",
            "url": f"http://web:8000/webhooks/evolution/{SECRET}",
        }
    )
    RedactWebhookSecretFilter().filter(record)
    output = JsonFormatter().format(record)
    assert SECRET not in output
    assert json.loads(output)["url"] == "http://web:8000/webhooks/evolution/***"


def test_formatter_redacts_tracebacks() -> None:
    try:
        raise RuntimeError(f"boom on /webhooks/evolution/{SECRET}")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, True)
        record.exc_info = sys.exc_info()
    assert SECRET not in JsonFormatter().format(record)


def test_other_paths_untouched() -> None:
    assert redact('"GET /health HTTP/1.1" 200') == '"GET /health HTTP/1.1" 200'


def test_configured_logging_redacts_uvicorn_access(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")  # handler bound to the captured stderr
    logging.getLogger("uvicorn.access").handle(_access_record(f"/webhooks/evolution/{SECRET}"))
    err = capsys.readouterr().err
    assert "/webhooks/evolution/***" in err
    assert SECRET not in err


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_real_uvicorn_access_log_never_contains_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: real uvicorn server, real access logger, configured JSON handler."""
    app = create_app(make_settings(webhook_secret=SECRET, log_level="INFO"))
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:  # noqa: ASYNC110 - uvicorn exposes a flag, not an event
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            ok = await client.post(f"/webhooks/evolution/{SECRET}", json={"event": "x"})
            bad = await client.post(f"/webhooks/evolution/{WRONG}?q={WRONG}", json={})
        assert ok.status_code == 200
        assert bad.status_code == 404
    finally:
        server.should_exit = True
        await task

    err = capsys.readouterr().err
    access = [
        json.loads(line)
        for line in err.splitlines()
        if line.startswith("{") and json.loads(line)["logger"] == "uvicorn.access"
    ]
    assert len(access) == 2
    assert all("/webhooks/evolution/***" in a["message"] for a in access)
    assert SECRET not in err
    assert WRONG not in err
