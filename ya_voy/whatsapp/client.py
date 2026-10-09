"""Evolution API v2.3.7 REST client: the four calls the outbox needs (docs/04 §9, docs/06).

Endpoints and bodies checked against the v2.3.7 source (``sendMessage.router.ts``,
``chat.router.ts`` and their DTOs). Authentication is the global ``apikey`` header.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ya_voy.config import Settings

DEFAULT_TIMEOUT_SECONDS = 15.0
_ERROR_BODY_LIMIT = 500


class EvolutionError(Exception):
    """A failed Evolution call. The message never includes the API key."""

    def __init__(self, operation: str, detail: str, status_code: int | None = None) -> None:
        self.operation = operation
        self.status_code = status_code
        self.detail = detail
        prefix = f"{operation}: HTTP {status_code}" if status_code else f"{operation}"
        super().__init__(f"{prefix}: {detail}")


@dataclass(frozen=True)
class SentMessage:
    wa_message_id: str | None
    raw: dict[str, Any]


class EvolutionClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        instance: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.instance = instance
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"apikey": api_key},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> EvolutionClient:
        return cls(
            base_url=settings.evolution_base_url,
            api_key=settings.evolution_api_key.get_secret_value(),
            instance=settings.evolution_instance,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, operation: str, method: str, path: str, body: dict[str, Any]) -> Any:
        url = f"{path}/{self.instance}"
        try:
            response = await self._http.request(method, url, json=body)
        except httpx.HTTPError as exc:
            raise EvolutionError(operation, f"{type(exc).__name__}: {exc}") from exc
        if response.is_error:
            raise EvolutionError(
                operation, response.text[:_ERROR_BODY_LIMIT], status_code=response.status_code
            )
        try:
            return response.json()
        except ValueError as exc:
            raise EvolutionError(operation, "response is not JSON", response.status_code) from exc

    @staticmethod
    def _sent(data: Any) -> SentMessage:
        raw = data if isinstance(data, dict) else {}
        key = raw.get("key") if isinstance(raw.get("key"), dict) else {}
        return SentMessage(wa_message_id=key.get("id"), raw=raw)

    async def send_text(
        self, number: str, text: str, *, quoted_wa_message_id: str | None = None
    ) -> SentMessage:
        """``POST /message/sendText/{instance}``. ``number`` may be a phone or a full JID.

        Quoting by id only works if Evolution stored the quoted message
        (``DATABASE_SAVE_DATA_NEW_MESSAGE=true``); it looks the message up by ``key.id``.
        """
        body: dict[str, Any] = {"number": number, "text": text}
        if quoted_wa_message_id:
            body["quoted"] = {"key": {"id": quoted_wa_message_id}}
        return self._sent(await self._call("sendText", "POST", "/message/sendText", body))

    async def send_document(
        self,
        number: str,
        *,
        media_base64: str,
        file_name: str,
        mimetype: str,
        caption: str = "",
    ) -> SentMessage:
        """``POST /message/sendMedia/{instance}`` with ``mediatype=document``."""
        body = {
            "number": number,
            "mediatype": "document",
            "mimetype": mimetype,
            "caption": caption,
            "fileName": file_name,
            "media": media_base64,
        }
        return self._sent(await self._call("sendMedia", "POST", "/message/sendMedia", body))

    async def delete_for_everyone(
        self, remote_jid: str, wa_message_id: str, *, participant: str | None = None
    ) -> SentMessage:
        """``DELETE /chat/deleteMessageForEveryone/{instance}`` for one of the bot's messages."""
        body: dict[str, Any] = {"id": wa_message_id, "remoteJid": remote_jid, "fromMe": True}
        if participant:
            body["participant"] = participant
        data = await self._call(
            "deleteMessageForEveryone", "DELETE", "/chat/deleteMessageForEveryone", body
        )
        return self._sent(data)

    async def update_message(self, remote_jid: str, wa_message_id: str, text: str) -> SentMessage:
        """``POST /chat/updateMessage/{instance}``: edit one of the bot's text messages."""
        body = {
            "number": remote_jid,
            "key": {"id": wa_message_id, "remoteJid": remote_jid, "fromMe": True},
            "text": text,
        }
        return self._sent(await self._call("updateMessage", "POST", "/chat/updateMessage", body))
