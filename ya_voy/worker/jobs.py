"""Job functions executed by the arq worker."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def ping(ctx: dict[str, Any], message: str = "pong") -> str:
    """Placeholder job to verify the queue end to end.

    TODO(stage 3+): replace with process_inbound_event, evaluate_draft, send_outbound and the
    scheduled tasks described in docs/06-arquitectura.md.
    """
    logger.info("ping job executed", extra={"job_id": ctx.get("job_id")})
    return message
