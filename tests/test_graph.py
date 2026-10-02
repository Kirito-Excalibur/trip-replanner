import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from trip_replanner.cli import load_plan
from trip_replanner.graph import build_graph
from trip_replanner.models import hm


def event(kind, item, minutes=0, at=None, note=""):
    return {"type": "event", "event": {"kind": kind, "item": item, "minutes": minutes, "at": at, "note": note}}


@pytest.fixture
def trip():
    graph = build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "test"}}
    graph.invoke(load_plan(None), cfg)
    assert graph.get_state(cfg).next == ("confirm_plan",)
    graph.invoke(Command(resume={"type": "approve"}), cfg)
    assert graph.get_state(cfg).next == ("await_event",)

    def send(reply):
        graph.invoke(Command(resume=reply), cfg)
        return graph.get_state(cfg)

    return send


def test_absorbed_delay_updates_plan_without_asking(trip):
    snap = trip(event("delay", "flight", 5))
    assert snap.next == ("await_event",)
    assert hm(snap.values["items"]["flight"]["start"]) == "09:15"
    assert "No knock-on effects" in snap.values["log"][-1]


def test_disruption_fans_out_then_applies_choice(trip):
    snap = trip(event("delay", "flight", 120, at="2026-10-14T07:40+01:00"))
    assert snap.next == ("choose",)
    assert [o["strategy"] for o in snap.values["options"]] == ["compress", "drop"]
    assert snap.values["recommendation"]["recommended"] in {"A", "B"}

    snap = trip({"type": "choose", "option_id": "B"})
    assert snap.next == ("await_event",)
    assert snap.values["items"]["coffee"]["status"] == "cancelled"
    assert snap.values["options"] == [] and snap.values["disrupted"] is None


def test_event_while_choosing_stacks_on_pending_disruption(trip):
    trip(event("delay", "flight", 120, at="2026-10-14T07:40+01:00"))
    snap = trip(event("closure", "maat", note="we're tired"))
    assert snap.next == ("choose",)
    assert [e["item"] for e in snap.values["pending"]] == ["flight", "maat"]
    assert len(snap.values["options"]) == 2  # options were recomputed, not appended

    snap = trip({"type": "choose", "option_id": "A"})
    items = snap.values["items"]
    assert hm(items["flight"]["start"]) == "11:10"  # the first event was not lost
    assert items["maat"]["status"] == "cancelled"


def test_bad_replies_are_ignored(trip):
    trip(event("delay", "flight", 120))
    snap = trip({"type": "choose", "option_id": "Z"})
    assert snap.next == ("choose",)
    snap = trip(event("delay", "nonexistent", 10))
    assert snap.next == ("choose",) and len(snap.values["options"]) == 2
    assert snap.values["log"][-1].startswith("Ignored event")
