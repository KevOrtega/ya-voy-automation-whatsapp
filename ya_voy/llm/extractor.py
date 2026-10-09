"""``OrderExtractor`` interface (docs/06 §2 ``llm``, RNF-13).

``extract(text, current)`` returns what the LLM finds in ``text``; ``current`` is the draft so far,
given as context only. The code merges and validates (RF-13/RF-14, D-05).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ya_voy.llm.schemas import OrderExtraction


class LLMError(Exception):
    """The provider failed or returned something unusable after all retries (CU-01 6a)."""


@runtime_checkable
class OrderExtractor(Protocol):
    async def extract(
        self, text: str, current: OrderExtraction | None = None
    ) -> OrderExtraction: ...
