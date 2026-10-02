"""Signed webhook for machine-reported disruptions (flight feeds, ops tools, ...).

POST /webhooks/events
    X-Trip-Timestamp: <unix seconds>
    X-Trip-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>" with TRIP_WEBHOOK_SECRET>
    {"trip_id": "<thread id>", "event": {"kind": "delay", "item": "flight", "minutes": 120}}

The signature is the authentication, so this route calls the server in-process,
which skips user auth. Events are queued per trip (multitask_strategy="enqueue"),
so two reports arriving together are applied one after the other.
"""

# No `from __future__ import annotations` here: the server loads this file by
# path, and pydantic then can't resolve postponed annotations like UUID.
import functools
import hashlib
import hmac
import os
import time
from uuid import UUID

from langgraph_sdk import get_client
from langgraph_sdk.errors import NotFoundError
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from trip_replanner.schemas import EventIn, summarize

MAX_BODY = 10_000
MAX_SKEW_SECONDS = 300
ASSISTANT = "trip"


class WebhookEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trip_id: UUID
    event: EventIn


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, timestamp: str | None, signature: str | None, body: bytes, now: float) -> bool:
    if not timestamp or not signature or not timestamp.isdigit():
        return False
    if abs(now - int(timestamp)) > MAX_SKEW_SECONDS:
        return False  # stale or replayed
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


@functools.cache
def _server():
    return get_client()  # in-process connection to this Agent Server


async def receive_event(request: Request) -> JSONResponse:
    secret = os.environ.get("TRIP_WEBHOOK_SECRET")
    if not secret:
        return JSONResponse({"error": "webhooks are not configured"}, status_code=503)
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"error": "body too large"}, status_code=413)
    headers = request.headers
    if not verify(secret, headers.get("x-trip-timestamp"), headers.get("x-trip-signature"), body, time.time()):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    try:
        payload = WebhookEvent.model_validate_json(body)
    except ValidationError as e:
        return JSONResponse({"error": f"invalid event: {summarize(e)}"}, status_code=422)

    server, trip_id = _server(), str(payload.trip_id)
    try:
        state = await server.threads.get_state(trip_id)
    except NotFoundError:
        return JSONResponse({"error": "unknown trip"}, status_code=404)
    if not set(state["next"]) & {"await_event", "choose"}:
        return JSONResponse({"error": "trip is not watching for events"}, status_code=409)

    run = await server.runs.create(
        trip_id,
        ASSISTANT,
        command={"resume": {"type": "event", "event": payload.event.model_dump(mode="json")}},
        multitask_strategy="enqueue",
    )
    return JSONResponse({"accepted": True, "run_id": run["run_id"]}, status_code=202)


app = Starlette(routes=[Route("/webhooks/events", receive_event, methods=["POST"])])
