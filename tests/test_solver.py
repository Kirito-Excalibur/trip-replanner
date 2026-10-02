from trip_replanner.models import hm
from trip_replanner.solver import (
    apply_event,
    downstream,
    find_violations,
    make_option,
    strategy_compress,
    strategy_drop,
    strategy_shift,
)


def delay(item, minutes, at=None):
    return {"kind": "delay", "item": item, "minutes": minutes, "at": at, "note": ""}


def test_sample_plan_is_feasible(plan):
    assert find_violations(plan["items"], plan["trip"]) == []


def test_small_delay_only_needs_a_shift(plan):
    items, trip = plan["items"], plan["trip"]
    disrupted = apply_event(items, delay("flight", 30))
    movable = downstream(items, "flight") - {"jeronimos"}

    assert [v["item"] for v in find_violations(disrupted, trip)] == ["coffee"]
    shifted = strategy_shift(disrupted, trip, movable, None)
    assert (hm(shifted["coffee"]["start"]), hm(shifted["coffee"]["end"])) == ("11:20", "12:05")
    assert shifted["lunch"]["start"] == items["lunch"]["start"]  # slack absorbed the rest
    assert strategy_compress(disrupted, trip, movable, None) is None
    assert strategy_drop(disrupted, trip, movable, None) is None


def test_long_delay_cannot_shift_past_the_fixed_ticket(plan):
    items, trip = plan["items"], plan["trip"]
    disrupted = apply_event(items, delay("flight", 120))
    movable = downstream(items, "flight") - {"jeronimos"}

    assert strategy_shift(disrupted, trip, movable, None) is None

    compressed = strategy_compress(disrupted, trip, movable, None)
    assert (hm(compressed["coffee"]["start"]), hm(compressed["coffee"]["end"])) == ("12:50", "13:10")
    assert (hm(compressed["lunch"]["start"]), hm(compressed["lunch"]["end"])) == ("13:20", "14:30")
    assert compressed["jeronimos"]["start"] == items["jeronimos"]["start"]

    dropped = strategy_drop(disrupted, trip, movable, None)
    assert dropped["coffee"]["status"] == "cancelled"
    assert dropped["lunch"]["depends_on"] == ["flight"]
    assert (hm(dropped["lunch"]["start"]), hm(dropped["lunch"]["end"])) == ("12:50", "14:05")

    option = make_option("drop", disrupted, dropped)
    assert option["title"] == "Skip Coffee in Baixa"
    assert option["cost"]["dropped_priority"] == 2


def test_closure_rewires_dependents(plan):
    items, trip = plan["items"], plan["trip"]
    closed = apply_event(items, {"kind": "closure", "item": "maat", "minutes": 0, "at": None, "note": ""})
    assert closed["maat"]["status"] == "cancelled"
    assert closed["tram"]["depends_on"] == ["pasteis"]
    assert find_violations(closed, trip) == []


def test_conflicts_before_now_are_ignored(plan):
    items, trip = plan["items"], plan["trip"]
    disrupted = apply_event(items, delay("flight", 120))
    assert find_violations(disrupted, trip, since=disrupted["lunch"]["start"]) == []
