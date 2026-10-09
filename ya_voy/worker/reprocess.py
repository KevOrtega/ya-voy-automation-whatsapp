"""Manual reprocessing of failed inbound events (docs/06 "Eventos entrantes fallidos").

``failed`` events are not retried automatically. After fixing the cause, an operator moves them
back to ``received`` and they are enqueued again (``scripts/reprocess_inbound.py``). The
conditional ``UPDATE`` makes it idempotent: only rows still ``failed`` are reset. ``attempts``
is kept as history; ``error`` and ``processed_at`` are cleared.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ya_voy.db.models import InboundEvent
from ya_voy.domain.enums import InboundEventStatus


async def reset_failed_inbound_events(
    session: AsyncSession, *, ids: Sequence[int] | None = None, all_failed: bool = False
) -> list[int]:
    """Move ``failed`` events (the given ``ids``, or every one with ``all_failed``) to ``received``.

    Returns the ids actually reset; enqueue ``process_inbound_event`` for them after the commit.
    """
    if (ids is None) == (not all_failed):
        raise ValueError("pass either ids or all_failed=True")
    stmt = (
        update(InboundEvent)
        .where(InboundEvent.status == InboundEventStatus.FAILED)
        .values(status=InboundEventStatus.RECEIVED, error=None, processed_at=None)
        .returning(InboundEvent.id)
    )
    if ids is not None:
        stmt = stmt.where(InboundEvent.id.in_(list(ids)))
    return sorted((await session.scalars(stmt)).all())
