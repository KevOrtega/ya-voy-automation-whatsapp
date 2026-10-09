"""Outbox helpers on top of :func:`ya_voy.db.queries.enqueue_outbound_message`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ya_voy.config import Settings
from ya_voy.db.models import Courier, Store
from ya_voy.db.queries import enqueue_outbound_message
from ya_voy.messaging import catalog
from ya_voy.whatsapp.jids import jid_for_phone, phone_from_jid


def admin_jids(settings: Settings) -> list[str]:
    return [jid_for_phone(phone) for phone in settings.admin_phones]


async def enqueue_admin_alert(
    session: AsyncSession,
    settings: Settings,
    *,
    template_key: str,
    idempotency_base: str,
    variables: Mapping[str, Any],
) -> list[int]:
    """One outbox row per admin, keyed ``{idempotency_base}:{admin_phone}``.

    Returns the ids of the new rows (to enqueue ``send_outbound`` after the commit).
    """
    body = catalog.render(template_key, **variables)
    ids: list[int] = []
    for phone in settings.admin_phones:
        new_id = await enqueue_outbound_message(
            session,
            idempotency_key=f"{idempotency_base}:{phone}",
            template_key=template_key,
            recipient_jid=jid_for_phone(phone),
            body=body,
        )
        if new_id is not None:
            ids.append(new_id)
    return ids


async def display_name_for_jid(session: AsyncSession, settings: Settings, jid: str) -> str | None:
    """Visible name of a store or courier by any of its JIDs; ``None`` if unknown.

    Never returns a phone number (RN-17).
    """
    name = await session.scalar(select(Store.display_name).where(Store.wa_jid == jid))
    if name is None:
        name = await session.scalar(
            select(Courier.display_name).where((Courier.phone_jid == jid) | (Courier.lid == jid))
        )
    return name


async def recipient_name(session: AsyncSession, settings: Settings, jid: str) -> str:
    """``{recipient_name}`` for ``admin_outbound_failed``."""
    if jid == settings.couriers_group_jid:
        return catalog.COURIERS_GROUP_NAME
    name = await display_name_for_jid(session, settings, jid)
    if name:
        return name
    if phone_from_jid(jid) in settings.admin_phones:
        return catalog.ADMIN_RECIPIENT_NAME
    return catalog.UNKNOWN_RECIPIENT_NAME
