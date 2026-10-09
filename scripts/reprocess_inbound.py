"""Reprocess failed inbound events (docs/06 "Eventos entrantes fallidos").

    python -m scripts.reprocess_inbound 12 15     # these ids, if they are still failed
    python -m scripts.reprocess_inbound --all     # every failed event

Uses DATABASE_URL and REDIS_URL from the environment / .env. Run it inside the stack:
``make reprocess ids="12 15"`` or ``make reprocess ids=--all``.
"""

from __future__ import annotations

import argparse
import asyncio

from arq import create_pool

from ya_voy.config import Settings
from ya_voy.db.session import create_engine, create_session_factory
from ya_voy.queue import enqueue_process_inbound_event, redis_settings_from_url
from ya_voy.worker.reprocess import reset_failed_inbound_events


async def run(settings: Settings, ids: list[int] | None, all_failed: bool) -> list[int]:
    engine = create_engine(settings)
    redis = await create_pool(redis_settings_from_url(settings.redis_url))
    try:
        async with create_session_factory(engine)() as session, session.begin():
            reset = await reset_failed_inbound_events(session, ids=ids, all_failed=all_failed)
        for event_id in reset:
            await enqueue_process_inbound_event(redis, event_id)
        return reset
    finally:
        await redis.aclose()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ids", nargs="*", type=int, help="inbound_events ids")
    parser.add_argument("--all", action="store_true", help="every failed event")
    args = parser.parse_args(argv)
    if bool(args.ids) == args.all:
        parser.error("give event ids or --all (not both)")
    reset = asyncio.run(run(Settings(), args.ids or None, args.all))
    print(f"re-enqueued {len(reset)} event(s): {reset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
