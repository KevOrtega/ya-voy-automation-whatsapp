"""``apply_data_retention`` cron (RNF-10, DC-11): see ``ya_voy.retention.service``."""

from __future__ import annotations

from typing import Any

from ya_voy.retention import service


async def apply_data_retention(ctx: dict[str, Any]) -> dict[str, int]:
    return await service.apply_data_retention(ctx["session_factory"], ctx["settings"])
