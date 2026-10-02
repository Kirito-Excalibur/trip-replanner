"""Deterministic itinerary logic. No LLM and no LangGraph in this module.

The itinerary is a dependency graph: each item lists the items that must finish
(plus travel time) before it can start. This module handles everything that has
to be right: time math, travel, opening windows, and finding which items are
downstream of a disruption. It also builds repair options. The LLM only ever
chooses between options this module has already checked.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta

from .models import (
    Change,
    Event,
    Item,
    Option,
    Trip,
    duration,
    fmt,
    hm,
    minutes_between,
    parse,
    shift_ts,
)

Items = dict[str, Item]


def travel(trip: Trip, a: str, b: str) -> int:
    if a == b:
        return 0
    table = trip["travel_minutes"]
    return table.get(f"{a}|{b}", table.get(f"{b}|{a}", trip["default_travel_minutes"]))


def live(items: Items) -> Items:
    return {k: it for k, it in items.items() if it["status"] != "cancelled"}


def topo_order(items: Items) -> list[str]:
    """Active items in dependency order, earliest start first among ready ones."""
    active = live(items)
    indegree = {k: 0 for k in active}
    children: dict[str, list[str]] = defaultdict(list)
    for k, it in active.items():
        for p in it["depends_on"]:
            if p in active:
                indegree[k] += 1
                children[p].append(k)
    by_start = lambda k: parse(active[k]["start"])  # noqa: E731
    ready = sorted((k for k, d in indegree.items() if d == 0), key=by_start)
    order: list[str] = []
    while ready:
        k = ready.pop(0)
        order.append(k)
        for c in children[k]:
            indegree[c] -= 1
            if indegree[c] == 0:
                ready.append(c)
        ready.sort(key=by_start)
    if len(order) != len(active):
        raise ValueError("dependency cycle in itinerary")
    return order


def ready_time(items: Items, trip: Trip, k: str):
    """Earliest moment item k can start, given its dependencies (None if it has none)."""
    it = items[k]
    times = [
        parse(items[p]["end"]) + timedelta(minutes=travel(trip, items[p]["location"], it["location"]))
        for p in it["depends_on"]
        if items[p]["status"] != "cancelled"
    ]
    return max(times, default=None)


def find_violations(items: Items, trip: Trip, since: str | None = None) -> list[dict]:
    """Broken constraints, ignoring plans that started before `since` (already past)."""
    out = []
    for k in topo_order(items):
        it = items[k]
        if since and parse(it["start"]) < parse(since):
            continue
        ready = ready_time(items, trip, k)
        if ready and ready > parse(it["start"]):
            out.append({
                "item": k,
                "kind": "too_soon",
                "text": f"can't reach {it['title']} ({hm(it['start'])}) before {ready:%H:%M}",
            })
        if "latest" in it and parse(it["end"]) > parse(it["latest"]):
            out.append({
                "item": k,
                "kind": "too_late",
                "text": f"{it['title']} would run past {hm(it['latest'])}",
            })
        if "earliest" in it and parse(it["start"]) < parse(it["earliest"]):
            out.append({
                "item": k,
                "kind": "too_early",
                "text": f"{it['title']} would start before {hm(it['earliest'])}",
            })
    return out


def downstream(items: Items, k: str) -> set[str]:
    children: dict[str, list[str]] = defaultdict(list)
    for c, it in items.items():
        for p in it["depends_on"]:
            children[p].append(c)
    seen: set[str] = set()
    stack = list(children[k])
    while stack:
        c = stack.pop()
        if c not in seen:
            seen.add(c)
            stack.extend(children[c])
    return seen


def ancestors(items: Items, k: str) -> set[str]:
    seen: set[str] = set()
    stack = list(items[k]["depends_on"])
    while stack:
        p = stack.pop()
        if p not in seen and items[p]["status"] != "cancelled":
            seen.add(p)
            stack.extend(items[p]["depends_on"])
    return seen


def cancel(items: Items, k: str) -> None:
    """Cancel k in place; its dependents now wait on k's own dependencies."""
    items[k]["status"] = "cancelled"
    for it in items.values():
        if k in it["depends_on"]:
            rewired = [p for p in it["depends_on"] if p != k] + items[k]["depends_on"]
            it["depends_on"] = list(dict.fromkeys(rewired))


def validate_event(items: Items, event: Event) -> None:
    k = event["item"]
    if k not in items:
        raise ValueError(f"no plan called {k!r}")
    if items[k]["status"] == "cancelled":
        raise ValueError(f"{items[k]['title']} is already cancelled")
    if event["kind"] != "closure" and event["minutes"] <= 0:
        raise ValueError("minutes must be positive")


def apply_event(items: Items, event: Event) -> Items:
    validate_event(items, event)
    new = copy.deepcopy(items)
    it = new[event["item"]]
    if event["kind"] == "delay":
        it["start"] = shift_ts(it["start"], event["minutes"])
        it["end"] = shift_ts(it["end"], event["minutes"])
    elif event["kind"] == "running_late":
        it["end"] = shift_ts(it["end"], event["minutes"])
    elif event["kind"] == "closure":
        cancel(new, event["item"])
    return new


def describe_event(event: Event, items: Items) -> str:
    title = items[event["item"]]["title"]
    match event["kind"]:
        case "delay":
            return f"{title} is delayed {event['minutes']} min"
        case "running_late":
            return f"You're running {event['minutes']} min late at {title}"
        case "closure":
            return f"{title} is closed / cancelled"
    return title


# Repair strategies. Each returns a repaired itinerary or None if it can't
# produce a valid one. `movable` holds the only items a strategy may change;
# conflicts before `since` are in the past and ignored.

def shift(items: Items, trip: Trip, movable: set[str]) -> Items:
    """Push movable items later just enough to respect dependencies and travel."""
    new = copy.deepcopy(items)
    for k in topo_order(new):
        if k not in movable:
            continue
        ready = ready_time(new, trip, k)
        if ready and ready > parse(new[k]["start"]):
            length = duration(new[k])
            new[k]["start"] = fmt(ready)
            new[k]["end"] = fmt(ready + timedelta(minutes=length))
    return new


def _with_durations(items: Items, minutes: dict[str, int]) -> Items:
    new = copy.deepcopy(items)
    for k, m in minutes.items():
        new[k]["end"] = shift_ts(new[k]["start"], m)
    return new


def _pick(items: Items, candidates: set[str]) -> str:
    return min(candidates, key=lambda k: (items[k]["priority"], parse(items[k]["start"])))


def strategy_shift(items: Items, trip: Trip, movable: set[str], since: str | None) -> Items | None:
    new = shift(items, trip, movable)
    return None if find_violations(new, trip, since) else new


def strategy_compress(items: Items, trip: Trip, movable: set[str], since: str | None) -> Items | None:
    """Keep every plan but shorten flexible ones (lowest priority first)."""
    squeezed: dict[str, int] = {}
    while violations := find_violations(shift(_with_durations(items, squeezed), trip, movable), trip, since):
        v = violations[0]["item"]
        candidates = {
            k for k in ancestors(items, v) | {v}
            if k in movable and k not in squeezed and items[k]["min_minutes"] < duration(items[k])
        }
        if not candidates:
            return None
        k = _pick(items, candidates)
        squeezed[k] = items[k]["min_minutes"]
    if not squeezed:
        return None  # nothing had to shrink, so plain shifting already covers it
    # Give the last squeezed item back as much time as still fits.
    last = list(squeezed)[-1]
    for m in range(duration(items[last]), squeezed[last], -5):
        trial = shift(_with_durations(items, {**squeezed, last: m}), trip, movable)
        if not find_violations(trial, trip, since):
            squeezed[last] = m
            break
    return shift(_with_durations(items, squeezed), trip, movable)


def strategy_drop(items: Items, trip: Trip, movable: set[str], since: str | None) -> Items | None:
    """Skip the lowest-priority plans in the way until everything else fits."""
    new = copy.deepcopy(items)
    dropped: set[str] = set()
    while violations := find_violations(shift(new, trip, movable - dropped), trip, since):
        v = violations[0]["item"]
        candidates = {k for k in ancestors(new, v) | {v} if k in movable and k not in dropped}
        if not candidates:
            return None
        k = _pick(new, candidates)
        cancel(new, k)
        dropped.add(k)
    if not dropped:
        return None
    return shift(new, trip, movable - dropped)


STRATEGIES: dict[str, Callable[[Items, Trip, set[str], str | None], Items | None]] = {
    "shift": strategy_shift,
    "compress": strategy_compress,
    "drop": strategy_drop,
}


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def make_option(strategy: str, before: Items, after: Items) -> Option:
    changes: list[Change] = []
    cost = {"dropped_priority": 0, "minutes_cut": 0, "minutes_moved": 0}
    for k in sorted(before, key=lambda k: parse(before[k]["start"])):
        b, a = before[k], after[k]
        span = f"{hm(b['start'])}–{hm(b['end'])}"
        if b["status"] == "planned" and a["status"] == "cancelled":
            changes.append({"item": k, "kind": "dropped", "text": f"{b['title']}: skipped (was {span})"})
            cost["dropped_priority"] += b["priority"]
        elif (b["start"], b["end"]) != (a["start"], a["end"]):
            cut = duration(b) - duration(a)
            new_span = f"{hm(a['start'])}–{hm(a['end'])}"
            if cut > 0:
                text = f"{b['title']}: {span} → {new_span} ({cut} min shorter)"
                changes.append({"item": k, "kind": "shortened", "text": text})
                cost["minutes_cut"] += cut
            else:
                changes.append({"item": k, "kind": "moved", "text": f"{b['title']}: {span} → {new_span}"})
            cost["minutes_moved"] += minutes_between(b["start"], a["start"])

    def titles(kind: str) -> list[str]:
        return [before[c["item"]]["title"] for c in changes if c["kind"] == kind]

    if strategy == "drop":
        title = f"Skip {_join(titles('dropped'))}"
    elif strategy == "compress":
        title = f"Keep everything, shorten {_join(titles('shortened'))}"
    else:
        n = len(changes)
        title = f"Push {n} plan{'s' if n != 1 else ''} later"
    return {"id": "", "strategy": strategy, "title": title, "items": after, "changes": changes, "cost": cost}


def same_plan(a: Items, b: Items) -> bool:
    key = lambda it: (it["status"], it["start"], it["end"])  # noqa: E731
    return all(key(a[k]) == key(b[k]) for k in a)
