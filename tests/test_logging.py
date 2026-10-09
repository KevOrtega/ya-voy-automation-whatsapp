from __future__ import annotations

import json
import logging

from ya_voy.logging import JsonFormatter


def test_json_formatter_includes_extra_fields() -> None:
    record = logging.makeLogRecord(
        {"name": "ya_voy.test", "levelname": "INFO", "msg": "order %s", "args": ("YV-1",)}
    )
    record.order_id = 42
    data = json.loads(JsonFormatter().format(record))
    assert data["message"] == "order YV-1"
    assert data["level"] == "INFO"
    assert data["logger"] == "ya_voy.test"
    assert data["order_id"] == 42
    assert "args" not in data
