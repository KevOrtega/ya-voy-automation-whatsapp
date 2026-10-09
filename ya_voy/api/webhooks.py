"""Evolution API webhook receiver.

Stage 1 only validates the shared secret in the path and acknowledges the call.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ya_voy.api.dependencies import settings_dependency
from ya_voy.config import Settings

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


@router.post(
    "/evolution/{secret}",
    dependencies=[Depends(verify_webhook_secret)],
    status_code=status.HTTP_200_OK,
)
async def evolution_webhook(request: Request) -> dict[str, str]:
    # TODO(stage 3): parse the payload, drop irrelevant events (fromMe, other groups, status),
    # INSERT into inbound_events (unique wa_message_id) and enqueue process_inbound_event.
    # Must stay fast (RNF-01): no LLM or Evolution calls here.
    await request.body()
    return {"status": "accepted"}
