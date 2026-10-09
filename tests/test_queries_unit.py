from __future__ import annotations

import pytest

from ya_voy.db.queries import format_order_code


@pytest.mark.parametrize(
    ("prefix", "number", "expected"),
    [
        ("YV-", 1, "YV-0001"),
        ("YV-", 42, "YV-0042"),
        ("YV-", 9999, "YV-9999"),
        ("X", 10000, "X10000"),
    ],
)
def test_format_order_code(prefix: str, number: int, expected: str) -> None:
    assert format_order_code(prefix, number) == expected
