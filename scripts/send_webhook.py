"""Send a signed event to a trip's webhook, the way an external feed would.

    TRIP_WEBHOOK_SECRET=... uv run python scripts/send_webhook.py <server-url> <trip-id> delay flight 120 [--note TEXT]

Get <trip-id> from `trip list`. Kinds: delay, running_late, closure.
"""

import argparse
import json
import os
import sys
import time

import httpx

from trip_replanner.webhooks import sign


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("server")
    p.add_argument("trip_id")
    p.add_argument("kind", choices=["delay", "running_late", "closure"])
    p.add_argument("item")
    p.add_argument("minutes", type=int, nargs="?", default=0)
    p.add_argument("--note", default="")
    args = p.parse_args()

    secret = os.environ["TRIP_WEBHOOK_SECRET"]
    body = json.dumps({
        "trip_id": args.trip_id,
        "event": {"kind": args.kind, "item": args.item, "minutes": args.minutes, "note": args.note},
    }).encode()
    timestamp = str(int(time.time()))
    response = httpx.post(
        args.server.rstrip("/") + "/webhooks/events",
        content=body,
        headers={
            "content-type": "application/json",
            "x-trip-timestamp": timestamp,
            "x-trip-signature": sign(secret, timestamp, body),
        },
        timeout=30,
    )
    print(response.status_code, response.text)
    return 0 if response.status_code == 202 else 1


if __name__ == "__main__":
    sys.exit(main())
