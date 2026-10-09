"""Evolution API webhook receiver (docs/04 §1, docs/06 §2 web, RF-59, CU-11).

Validate the secret, drop what is obviously irrelevant, store the event once
(``inbound_events.wa_message_id`` unique) and enqueue ``process_inbound_event`` only for new rows.
Must stay fast (RNF-01): no LLM or Evolution calls here.

Status codes, chosen for Evolution's retry policy (v2.3.7 retries any failure except
400/401/403/404/422, up to ``WEBHOOK_RETRY_MAX_ATTEMPTS``):

- 404: wrong secret (looks like an unknown route; not retried).
- 400: body is not JSON or not an Evolution event (not retried, so no retry loop).
- 200: stored, duplicate or deliberately ignored.
- 503: the database is unavailable; Evolution retries later, dedup makes that safe.

If enqueueing fails after the row is stored, the call still answers 200: the recovery sweep
(``recover_inbound_events``) picks the row up.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Annotated, Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.api.dependencies import get_redis, get_session_factory, settings_dependency
from ya_voy.config import Settings
from ya_voy.db.queries import insert_inbound_event
from ya_voy.queue import enqueue_process_inbound_event
from ya_voy.whatsapp.capture import write_capture
from ya_voy.whatsapp.dedup import compute_inbound_dedup_key
from ya_voy.whatsapp.filters import discard_reason
from ya_voy.whatsapp.payloads import InboundMessage, PayloadError, parse_webhook, sanitize_payload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def verify_webhook_secret(
    secret: str,
    settings: Annotated[Settings, Depends(settings_dependency)],
) -> None:
    """Reject the request unless ``secret`` matches ``WEBHOOK_SECRET``.

    A wrong (or unconfigured) secret returns 404 so the endpoint is indistinguishable from a
    non-existent route.
    """
    expected = settings.webhook_secret.get_secret_value()
    if not expected:
        logger.error("WEBHOOK_SECRET is not configured; rejecting webhook call")
    if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _bad_request(reason: str) -> JSONResponse:
    logger.warning("webhook payload rejected", extra={"reason": reason})
    return JSONResponse({"status": "rejected", "reason": reason}, status_code=400)


async def _capture(settings: Settings, payload: dict[str, Any]) -> None:
    if not (settings.capture_inbound_payloads and settings.is_development):
        return
    try:
        path = await asyncio.to_thread(write_capture, settings.capture_dir, payload)
    except OSError:
        logger.exception("could not write webhook capture")
    else:
        logger.info("webhook payload captured", extra={"capture_file": str(path)})


@router.post(
    "/evolution/{secret}",
    dependencies=[Depends(verify_webhook_secret)],
    status_code=status.HTTP_200_OK,
)
async def evolution_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(settings_dependency)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    redis: Annotated[ArqRedis, Depends(get_redis)],
) -> JSONResponse:
    try:
        raw = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return _bad_request("invalid_json")
    if not isinstance(raw, dict):
        return _bad_request("not_an_object")
    payload = sanitize_payload(raw)
    await _capture(settings, payload)

    try:
        event = parse_webhook(payload)
    except PayloadError as exc:
        return _bad_request(str(exc))

    reason = discard_reason(
        event, instance=settings.evolution_instance, couriers_group_jid=settings.couriers_group_jid
    )
    if reason is not None:
        logger.debug(
            "webhook event discarded", extra={"reason": str(reason), "event": event.event_type}
        )
        return JSONResponse({"status": "ignored", "reason": str(reason)})

    message = event if isinstance(event, InboundMessage) else None
    try:
        async with session_factory() as session, session.begin():
            event_id = await insert_inbound_event(
                session,
                wa_message_id=compute_inbound_dedup_key(event, payload),
                instance=event.instance or settings.evolution_instance,
                event_type=event.event_type,
                remote_jid=message.remote_jid if message else None,
                participant=message.participant if message else None,
                payload=payload,
            )
    except (SQLAlchemyError, OSError):
        logger.exception("could not store webhook event")
        return JSONResponse({"status": "unavailable"}, status_code=503)

    if event_id is None:
        logger.info("duplicate webhook event", extra={"event": event.event_type})
        return JSONResponse({"status": "duplicate"})

    try:
        await enqueue_process_inbound_event(redis, event_id)
    except Exception:
        # Stored but not queued: recover_inbound_events re-enqueues it.
        logger.exception("could not enqueue inbound event", extra={"inbound_event_id": event_id})
    logger.info(
        "webhook event stored", extra={"inbound_event_id": event_id, "event": event.event_type}
    )
    return JSONResponse({"status": "accepted", "inbound_event_id": event_id})
