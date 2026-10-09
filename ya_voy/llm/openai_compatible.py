"""``OrderExtractor`` over any OpenAI-compatible Chat Completions API (``LLM_PROVIDER=openai``).

``LLM_BASE_URL`` points it at other compatible providers or self-hosted models. Structured output
via ``response_format = json_schema`` (strict); the answer is validated again with Pydantic.
Retries transport errors, 408/429/5xx and unusable answers up to ``LLM_MAX_RETRIES`` with
exponential backoff; then raises :class:`LLMError`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from pydantic import ValidationError

from ya_voy.config import Settings
from ya_voy.llm.extractor import LLMError
from ya_voy.llm.schemas import OrderExtraction

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
RETRYABLE_STATUS = frozenset({408, 429})

SYSTEM_PROMPT = """\
You extract delivery orders from WhatsApp messages written in Argentine Spanish by shops.
Return only the fields you find in the message; use null for anything not present.
- Copy addresses, product and notes exactly as written: do not fix, complete or translate them.
- pickup_address: where the courier picks up ("retiro"). Never infer it from the shop.
- delivery_address: where the courier delivers ("entrega").
- product: what is carried (e.g. "almacen").
- payment_method_raw: the payment wording as written (e.g. "transferencia").
- payment_method: "efectivo" -> cash; "transferencia", "transf", "mp", "mercado pago" -> transfer;
  "pagado", "ya abonó" -> prepaid; any other payment text -> other.
- notes: any other relevant remark for the courier, otherwise null.
Greetings, thanks or questions without order data give all nulls.
The current draft (if any) is context only; extract from the new message."""


def _response_format() -> dict[str, Any]:
    nullable_text = {"type": ["string", "null"]}
    properties: dict[str, Any] = {
        "pickup_address": nullable_text,
        "delivery_address": nullable_text,
        "product": nullable_text,
        "payment_method": {
            "type": ["string", "null"],
            "enum": ["cash", "transfer", "prepaid", "other", None],
        },
        "payment_method_raw": nullable_text,
        "notes": nullable_text,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "OrderExtraction",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


class _Retryable(Exception):
    pass


class OpenAICompatibleExtractor:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 20,
        max_retries: int = 3,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self._http = httpx.AsyncClient(
            base_url=(base_url or DEFAULT_BASE_URL).rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatibleExtractor:
        return cls(
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    @staticmethod
    def worst_case_seconds(
        timeout: float,
        max_retries: int,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> float:
        """Longest :meth:`extract` can take: every attempt times out, plus every backoff."""
        total_backoff = backoff_base_seconds * (2**max_retries - 1)
        return timeout * (max_retries + 1) + total_backoff

    async def aclose(self) -> None:
        await self._http.aclose()

    def _body(self, text: str, current: OrderExtraction | None) -> dict[str, Any]:
        user = text
        if current is not None:
            draft = current.model_dump(mode="json", exclude_none=True)
            if draft:
                context = json.dumps(draft, ensure_ascii=False)
                user = f"Current draft: {context}\n\nNew message:\n{text}"
        return {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "response_format": _response_format(),
        }

    async def _once(self, body: dict[str, Any]) -> OrderExtraction:
        try:
            response = await self._http.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise _Retryable(f"{type(exc).__name__}") from exc
        if response.status_code >= 500 or response.status_code in RETRYABLE_STATUS:
            raise _Retryable(f"HTTP {response.status_code}")
        if response.is_error:
            raise LLMError(f"HTTP {response.status_code}: {response.text[:300]}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
            return OrderExtraction.model_validate_json(content)
        except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
            raise _Retryable(f"unusable answer: {type(exc).__name__}") from exc

    async def extract(self, text: str, current: OrderExtraction | None = None) -> OrderExtraction:
        body = self._body(text, current)
        last = "no attempt"
        for attempt in range(self.max_retries + 1):
            if attempt:
                await asyncio.sleep(self.backoff_base_seconds * 2 ** (attempt - 1))
            try:
                return await self._once(body)
            except _Retryable as exc:
                last = str(exc)
        raise LLMError(f"LLM failed after {self.max_retries + 1} attempts: {last}")
