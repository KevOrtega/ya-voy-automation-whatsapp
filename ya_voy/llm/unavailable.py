"""Stand-in used when the LLM is not configured: every call fails like a provider outage.

Keeps the worker running without ``LLM_API_KEY`` in development; stores then get
``store_processing_error`` and the admins ``admin_llm_failure`` (CU-01 6a); schedule
instructions get ``admin_schedule_parse_error`` (CU-14 1b).
"""

from __future__ import annotations

from ya_voy.llm.extractor import LLMError, ScheduleContext
from ya_voy.llm.schemas import OrderExtraction, ScheduleOverrideIntent


class UnavailableOrderExtractor:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def extract(self, text: str, current: OrderExtraction | None = None) -> OrderExtraction:
        raise LLMError(f"LLM not configured: {self.reason}")


class UnavailableScheduleParser:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def parse(self, text: str, context: ScheduleContext) -> ScheduleOverrideIntent:
        raise LLMError(f"LLM not configured: {self.reason}")
