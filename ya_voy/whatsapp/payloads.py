"""Typed view of Evolution API v2.3.7 webhook payloads.

Only the fields Ya Voy uses are typed; everything else is kept in the raw payload stored in
``inbound_events.payload``. Unknown fields are ignored, missing optional fields are ``None``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "ConnectionUpdate",
    "EvolutionEvent",
    "InboundMessage",
    "MessageKey",
    "PayloadError",
    "WebhookEnvelope",
    "parse_webhook",
    "sanitize_payload",
]


class EvolutionEvent(StrEnum):
    MESSAGES_UPSERT = "messages.upsert"
    CONNECTION_UPDATE = "connection.update"


class PayloadError(ValueError):
    """The body is not a usable Evolution webhook payload."""


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class MessageKey(_Lenient):
    id: str | None = None
    remote_jid: str | None = Field(None, alias="remoteJid")
    from_me: bool = Field(False, alias="fromMe")
    participant: str | None = None
    # TODO(verify-evolution): which alternative ids arrive and when (RF-22, R-03, R-06).
    remote_jid_alt: str | None = Field(None, alias="remoteJidAlt")
    participant_alt: str | None = Field(None, alias="participantAlt")
    sender_pn: str | None = Field(None, alias="senderPn")
    addressing_mode: str | None = Field(None, alias="addressingMode")


class _ReactionMessage(_Lenient):
    key: MessageKey = Field(default_factory=MessageKey)
    text: str | None = None


class _ContextInfo(_Lenient):
    stanza_id: str | None = Field(None, alias="stanzaId")


class _MessageData(_Lenient):
    key: MessageKey
    push_name: str | None = Field(None, alias="pushName")
    message: dict[str, Any] | None = None
    message_type: str | None = Field(None, alias="messageType")
    context_info: _ContextInfo | None = Field(None, alias="contextInfo")
    message_timestamp: int | None = Field(None, alias="messageTimestamp")


class WebhookEnvelope(_Lenient):
    event: str
    instance: str | None = None
    data: Any = None


class InboundMessage(BaseModel):
    """A ``messages.upsert`` event, normalized."""

    model_config = ConfigDict(frozen=True)

    instance: str | None
    wa_message_id: str | None
    remote_jid: str | None
    from_me: bool
    participant: str | None
    participant_alt: str | None
    remote_jid_alt: str | None
    sender_pn: str | None
    push_name: str | None
    message_type: str | None
    text: str | None
    quoted_wa_message_id: str | None
    timestamp: int | None
    # Reactions (message.reactionMessage). Empty text = removal (docs/06 §2, to be verified).
    is_reaction: bool
    reaction_target_id: str | None
    reaction_text: str | None

    @property
    def is_reaction_removal(self) -> bool:
        # TODO(verify-evolution): removal shape (empty text) is from the docs, not yet observed.
        return self.is_reaction and not self.reaction_text

    @property
    def event_type(self) -> str:
        return EvolutionEvent.MESSAGES_UPSERT


class ConnectionUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    instance: str | None
    state: str | None
    status_reason: int | str | None

    @property
    def event_type(self) -> str:
        return EvolutionEvent.CONNECTION_UPDATE


class OtherEvent(BaseModel):
    """Any event type Ya Voy does not subscribe to."""

    model_config = ConfigDict(frozen=True)

    event_type: str
    instance: str | None


ParsedEvent = InboundMessage | ConnectionUpdate | OtherEvent


def _text_of(message: dict[str, Any] | None) -> str | None:
    if not message:
        return None
    if isinstance(message.get("conversation"), str):
        return message["conversation"]
    extended = message.get("extendedTextMessage")
    if isinstance(extended, dict) and isinstance(extended.get("text"), str):
        return extended["text"]
    return None


def _parse_message(instance: str | None, data: Any) -> InboundMessage:
    try:
        raw = _MessageData.model_validate(data)
    except ValidationError as exc:
        raise PayloadError(f"invalid messages.upsert data: {exc.error_count()} errors") from exc
    message = raw.message or {}
    reaction_raw = message.get("reactionMessage")
    reaction = (
        _ReactionMessage.model_validate(reaction_raw) if isinstance(reaction_raw, dict) else None
    )
    return InboundMessage(
        instance=instance,
        wa_message_id=raw.key.id,
        remote_jid=raw.key.remote_jid,
        from_me=raw.key.from_me,
        participant=raw.key.participant,
        participant_alt=raw.key.participant_alt,
        remote_jid_alt=raw.key.remote_jid_alt,
        sender_pn=raw.key.sender_pn,
        push_name=raw.push_name,
        message_type=raw.message_type,
        text=_text_of(message),
        quoted_wa_message_id=raw.context_info.stanza_id if raw.context_info else None,
        timestamp=raw.message_timestamp,
        is_reaction=reaction is not None,
        reaction_target_id=reaction.key.id if reaction else None,
        reaction_text=reaction.text if reaction else None,
    )


def parse_webhook(payload: Any) -> ParsedEvent:
    """Parse a decoded JSON body. Raises :class:`PayloadError` if it is not an Evolution event."""
    if not isinstance(payload, dict):
        raise PayloadError("payload must be a JSON object")
    try:
        envelope = WebhookEnvelope.model_validate(payload)
    except ValidationError as exc:
        raise PayloadError("missing or invalid 'event'") from exc
    event = envelope.event.lower()
    if event == EvolutionEvent.MESSAGES_UPSERT:
        return _parse_message(envelope.instance, envelope.data)
    if event == EvolutionEvent.CONNECTION_UPDATE:
        data = envelope.data if isinstance(envelope.data, dict) else {}
        return ConnectionUpdate(
            instance=envelope.instance,
            state=data.get("state"),
            status_reason=data.get("statusReason"),
        )
    return OtherEvent(event_type=event, instance=envelope.instance)


# Envelope fields that carry secrets: ``destination`` is the webhook URL (with WEBHOOK_SECRET in
# the path) and ``apikey`` the instance API key. Never stored, captured or logged (R-17).
SECRET_ENVELOPE_FIELDS = ("destination", "apikey")
REDACTED = "***"


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``payload`` with the secret envelope fields replaced by ``***``."""
    clean = dict(payload)
    for field in SECRET_ENVELOPE_FIELDS:
        if clean.get(field) is not None:
            clean[field] = REDACTED
    return clean
