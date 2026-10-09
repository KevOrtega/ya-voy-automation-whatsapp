"""LLM interfaces (docs/06 §2 ``llm``, RNF-13): ``OrderExtractor`` and ``ScheduleIntentParser``.

``extract(text, current)`` returns what the LLM finds in ``text``; ``current`` is the draft so far,
given as context only. The code merges and validates (RF-13/RF-14, D-05).

``parse(text, context)`` interprets an admin's schedule instruction (CU-14 step 1); the code
validates it (``validate_schedule_intent``) and the admin confirms it (RN-24).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ya_voy.llm.schemas import OrderExtraction, ScheduleOverrideIntent


class LLMError(Exception):
    """The provider failed or returned something unusable after all retries (CU-01 6a)."""


@runtime_checkable
class OrderExtractor(Protocol):
    async def extract(
        self, text: str, current: OrderExtraction | None = None
    ) -> OrderExtraction: ...


@dataclass(frozen=True)
class ScheduleContext:
    """What the schedule parser is told besides the text (CU-14 step 1)."""

    now_local: dt.datetime  # current date and time in TZ
    business_date: dt.date
    hours_start: dt.time
    hours_end: dt.time


@runtime_checkable
class ScheduleIntentParser(Protocol):
    async def parse(self, text: str, context: ScheduleContext) -> ScheduleOverrideIntent: ...
