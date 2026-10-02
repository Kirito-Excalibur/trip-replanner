import json
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from trip_replanner import webhooks

TRIP = "01a0fbf0-4f6a-7be0-9ee4-9882bb82ae6b"
EVENT = {"trip_id": TRIP, "event": {"kind": "delay", "item": "flight", "minutes": 120}}


class FakeServer:
    """Stands in for the in-process Agent Server client."""

    def __init__(self):
        self.next = ["await_event"]
        self.created = []
        self.threads = SimpleNamespace(get_state=self.get_state)
        self.runs = SimpleNamespace(create=self.create)

    async def get_state(self, thread_id):
        return {"next": self.next}

    async def create(self, thread_id, assistant_id, **kwargs):
        self.created.append((thread_id, assistant_id, kwargs))
        return {"run_id": "run-1"}


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("TRIP_WEBHOOK_SECRET", "s3cret")
    fake = FakeServer()
    monkeypatch.setattr(webhooks, "_server", lambda: fake)
    return fake


def post(body: dict, *, secret: str = "s3cret", timestamp: int | None = None):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()) if timestamp is None else timestamp)
    headers = {"x-trip-timestamp": ts, "x-trip-signature": webhooks.sign(secret, ts, raw)}
    return TestClient(webhooks.app).post("/webhooks/events", content=raw, headers=headers)


def test_signed_event_is_queued_on_the_trip(server):
    response = post(EVENT)
    assert response.status_code == 202
    thread_id, assistant_id, kwargs = server.created[0]
    assert (thread_id, assistant_id, kwargs["multitask_strategy"]) == (TRIP, "trip", "enqueue")
    assert kwargs["command"]["resume"]["event"]["minutes"] == 120


@pytest.mark.parametrize("tamper", [{"secret": "wrong"}, {"timestamp": int(time.time()) - 3600}])
def test_bad_or_replayed_signatures_are_rejected(server, tamper):
    assert post(EVENT, **tamper).status_code == 401
    assert server.created == []


def test_invalid_events_and_idle_trips_are_refused(server):
    response = post({**EVENT, "event": {"kind": "delay", "item": "flight"}})
    assert response.status_code == 422 and "minutes must be at least 1" in response.json()["error"]
    server.next = ["confirm_plan"]
    assert post(EVENT).status_code == 409
    assert server.created == []


def test_webhooks_are_off_without_a_secret(server, monkeypatch):
    monkeypatch.delenv("TRIP_WEBHOOK_SECRET")
    assert post(EVENT).status_code == 503
