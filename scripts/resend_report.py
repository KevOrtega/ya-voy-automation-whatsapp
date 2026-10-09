"""Resend failed daily reports (docs/06 "Reenvío manual de reportes fallidos").

    python -m scripts.resend_report 2026-10-09 2026-10-10   # these business days
    python -m scripts.resend_report --all-failed            # every report in ``failed``

A ``failed`` report is terminal for the crons (e.g. WhatsApp was disconnected overnight and the
outbox ran out of attempts). This moves it back: the latest revision is queued again under new
outbox keys, or rebuilt first if its file is missing. Reports that are not ``failed`` are left
alone, so running it twice (or concurrently) resends once.

Uses DATABASE_URL, REDIS_URL and ADMIN_PHONES from the environment / .env. Run it inside the
stack: ``make resend-report date=2026-10-09`` or ``make resend-report date=--all-failed``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt

from arq import create_pool

from ya_voy.config import Settings
from ya_voy.db.session import create_engine, create_session_factory
from ya_voy.queue import enqueue_outbound_ids, redis_settings_from_url
from ya_voy.reports.service import resend_failed_reports


async def run(
    settings: Settings, days: list[dt.date] | None, *, all_failed: bool
) -> tuple[dict[str, str], list[int]]:
    if (days is None) == (not all_failed):
        raise ValueError("pass either days or all_failed=True")
    engine = create_engine(settings)
    redis = await create_pool(redis_settings_from_url(settings.redis_url))
    try:
        outcomes, ids = await resend_failed_reports(
            create_session_factory(engine), settings, None if all_failed else days
        )
        await enqueue_outbound_ids(redis, ids)
        return outcomes, ids
    finally:
        await redis.aclose()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "dates", nargs="*", type=dt.date.fromisoformat, help="business days (YYYY-MM-DD)"
    )
    parser.add_argument("--all-failed", action="store_true", help="every failed report")
    args = parser.parse_args(argv)
    if bool(args.dates) == args.all_failed:
        parser.error("give dates or --all-failed (not both)")
    outcomes, ids = asyncio.run(run(Settings(), args.dates or None, all_failed=args.all_failed))
    if not outcomes:
        print("nothing to resend: no failed reports")
    for day, outcome in outcomes.items():
        print(f"{day}: {outcome}")
    print(f"queued {len(ids)} message(s): {ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
