"""Stub ``OrderExtractor``s for tests (no network, ever)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ya_voy.llm.extractor import LLMError
from ya_voy.llm.schemas import OrderExtraction

CANONICAL_ORDER_TEXT = (
    Path(__file__).parent / "fixtures" / "orders" / "canonical_order.txt"
).read_text(encoding="utf-8")

# docs/05 "Entrada (ejemplo canónico) y salida esperada".
CANONICAL_EXTRACTION = OrderExtraction(
    pickup_address="casa nogal almendros",
    delivery_address="el ceibo 2871",
    product="almacen",
    payment_method="transfer",
    payment_method_raw="transferencia",
    notes=None,
)


class StubExtractor:
    """Returns a scripted extraction per exact text; unknown texts give an empty extraction."""

    def __init__(
        self,
        answers: dict[str, OrderExtraction] | None = None,
        *,
        fail: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.answers = {CANONICAL_ORDER_TEXT: CANONICAL_EXTRACTION} | (answers or {})
        self.fail = fail
        self.delay = delay
        self.calls: list[tuple[str, OrderExtraction | None]] = []

    async def extract(self, text: str, current: OrderExtraction | None = None) -> OrderExtraction:
        self.calls.append((text, current))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise LLMError("stub outage")
        return self.answers.get(text, OrderExtraction())
