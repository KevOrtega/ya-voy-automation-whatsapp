"""POST Evolution webhook fixtures to a running ``web`` (docs/06 §3: test without a phone).

    python -m scripts.replay_fixture tests/fixtures/evolution/private_text.json [more.json ...] \
        [--url http://localhost:8000] [--times 2]

Reads ``WEBHOOK_SECRET`` from the environment (or ``.env``) and never prints it. ``--times 2``
re-delivers each payload to watch the dedup answer (``duplicate``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from ya_voy.config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--times", type=int, default=1, help="deliveries per file")
    args = parser.parse_args(argv)

    secret = Settings().webhook_secret.get_secret_value()
    if not secret:
        parser.error("WEBHOOK_SECRET is not set (environment or .env)")
    url = f"{args.url.rstrip('/')}/webhooks/evolution/{secret}"
    failed = 0
    with httpx.Client(timeout=10) as client:
        for path in args.files:
            payload = json.loads(path.read_text(encoding="utf-8"))
            for _ in range(args.times):
                resp = client.post(url, json=payload)
                print(f"{path.name}: {resp.status_code} {resp.text}")
                failed += resp.status_code >= 500
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
