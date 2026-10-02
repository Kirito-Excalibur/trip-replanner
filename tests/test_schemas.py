import pytest

from trip_replanner.cli import load_plan
from trip_replanner.graph import plan_trip
from trip_replanner.schemas import parse_plan, parse_reply


def test_bundled_plan_is_valid_and_normalized():
    trip, items = parse_plan(load_plan(None))
    assert trip["default_travel_minutes"] == 25
    assert items["coffee"]["min_minutes"] == 20
    assert items["flight"]["min_minutes"] == 60  # defaults to the full length: not compressible
    assert items["flight"]["status"] == "planned"


@pytest.mark.parametrize("change, message", [
    ({"depends_on": ["nope"]}, "unknown item 'nope'"),
    ({"start": "2026-10-14T12:45"}, "UTC offset"),
    ({"end": "2026-10-14T12:00+01:00"}, "end must be after start"),
    ({"priority": 9}, "less than or equal to 5"),
    ({"surprise": True}, "Extra inputs are not permitted"),
])
def test_bad_plans_are_rejected(change, message):
    plan = load_plan(None)
    plan["items"]["lunch"].update(change)
    with pytest.raises(ValueError, match=message):
        parse_plan(plan)


def test_dependency_cycles_are_rejected():
    plan = load_plan(None)
    plan["items"]["flight"]["depends_on"] = ["dinner"]
    with pytest.raises(ValueError, match="cycle"):
        plan_trip({**plan, "log": []})


def test_replies_are_validated():
    assert parse_reply({"type": "approve"}, {"approve"}) == ({"type": "approve"}, None)
    assert parse_reply({"type": "end", "x": 1}, {"end"}) == (None, "end.x: Extra inputs are not permitted")
    assert "isn't possible right now" in parse_reply({"type": "end"}, {"approve"})[1]
    reply, error = parse_reply({"type": "event", "event": {"kind": "closure", "item": "maat"}}, {"event"})
    assert error is None
    assert reply["event"] == {"kind": "closure", "item": "maat", "minutes": 0, "at": None, "note": ""}
