"""The trip as a long-lived LangGraph thread.

    START → plan_trip → confirm_plan ⏸
                             │
          ┌────────────▶ await_event ⏸   ◀── the CLI or a watcher resumes the thread
          │                  │
          │             assess_impact      (solver: apply event, find conflicts)
          │              │         │
          │       no conflict    conflict
          │◀─────────────┘         │
          │                 propose × N    (Send: one branch per strategy, in parallel)
          │                        │
          │               validate_and_rank (dedupe, then Claude ranks and explains)
          │                        │
          │                     choose ⏸   (you pick, or a new event arrives)
          │                        │
          └──────────────── apply_and_notify

⏸ marks interrupt(). While paused, nothing runs; the trip exists only as a
checkpoint until someone resumes it with Command(resume=...).
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Annotated, Literal, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Overwrite, Send, interrupt

from trip_replanner.models import Event, Item, Option, Recommendation, Trip, parse
from trip_replanner.ranker import rank_options
from trip_replanner.schemas import parse_plan, parse_reply
from trip_replanner.solver import (
    STRATEGIES,
    apply_event,
    describe_event,
    downstream,
    find_violations,
    make_option,
    same_plan,
    validate_event,
)


class TripState(TypedDict):
    trip: Trip
    items: dict[str, Item]  # the committed plan
    log: Annotated[list[str], operator.add]  # notifications, newest last
    # Set only while a disruption is unresolved:
    disrupted: NotRequired[dict[str, Item] | None]  # plan with the event(s) applied
    pending: NotRequired[list[Event]]  # the events behind it (they can stack)
    movable: NotRequired[list[str]]  # what the strategies are allowed to touch
    now: NotRequired[str | None]  # plans that start before this are frozen
    options: Annotated[list[Option], operator.add]  # appended to in parallel by propose
    recommendation: NotRequired[Recommendation | None]
    # Hand-offs between nodes:
    event: NotRequired[Event | None]
    chosen: NotRequired[str | None]


class ProposeTask(TypedDict):
    strategy: str
    trip: Trip
    disrupted: dict[str, Item]
    movable: list[str]
    now: str | None


def plan_trip(state: TripState) -> dict:
    # The plan arrives from outside, so validate the shape here and the logic below.
    trip, items = parse_plan({"trip": state.get("trip"), "items": state.get("items")})
    problems = find_violations(items, trip)  # also rejects dependency cycles
    if problems:
        raise ValueError("The starting plan doesn't work: " + "; ".join(p["text"] for p in problems))
    return {"trip": trip, "items": items, "log": [f"Loaded {len(items)} plans for {trip['name']}."]}


def _ignored(node: str, error: str) -> Command:
    """Invalid resume payloads never move the trip; the node just waits again."""
    return Command(goto=node, update={"log": [f"Ignored reply: {error}."]})


def confirm_plan(state: TripState) -> Command[Literal["await_event", "confirm_plan", "__end__"]]:
    reply, error = parse_reply(interrupt({"waiting_for": "approval"}), {"approve", "reject"})
    if error:
        return _ignored("confirm_plan", error)
    if reply["type"] == "approve":
        return Command(goto="await_event", update={"log": ["Plan approved. Watching for disruptions."]})
    return Command(goto=END, update={"log": ["Plan rejected."]})


def await_event(state: TripState) -> Command[Literal["assess_impact", "await_event", "__end__"]]:
    reply, error = parse_reply(interrupt({"waiting_for": "event"}), {"event", "end"})
    if error:
        return _ignored("await_event", error)
    if reply["type"] == "event":
        return Command(goto="assess_impact", update={"event": reply["event"]})
    return Command(goto=END, update={"log": ["Trip finished."]})


def assess_impact(state: TripState) -> dict:
    event, trip = state["event"], state["trip"]
    # If a disruption is still waiting on you, stack the new event on top of it.
    base = state.get("disrupted") or state["items"]
    reset = {"event": None, "options": Overwrite([]), "recommendation": None}
    try:
        validate_event(base, event)
    except ValueError as e:
        return {**reset, "log": [f"Ignored event: {e}."]}

    if event["kind"] == "running_late" and not event["at"]:
        event = {**event, "at": base[event["item"]]["start"]}  # you're at that plan right now
    pending = [*(state.get("pending") or []), event]
    reported = [e["at"] for e in pending if e["at"]]
    now = max(reported, key=parse) if reported else None

    disrupted = apply_event(base, event)
    what = describe_event(event, base)
    conflicts = find_violations(disrupted, trip, since=now)
    if not conflicts:
        return {
            **reset,
            "items": disrupted, "disrupted": None, "pending": [], "movable": [], "now": None,
            "log": [f"{what}. No knock-on effects; plan updated."],
        }

    hit = {e["item"] for e in pending}
    candidates = set(state.get("movable") or []) | downstream(base, event["item"])
    movable = sorted(
        k for k in candidates
        if k not in hit
        and not disrupted[k]["fixed"]
        and disrupted[k]["status"] == "planned"
        and (now is None or parse(disrupted[k]["start"]) >= parse(now))
    )
    return {
        **reset,
        "disrupted": disrupted, "pending": pending, "movable": movable, "now": now,
        "log": [f"{what}. Conflict: " + "; ".join(c["text"] for c in conflicts) + "."],
    }


def route_after_impact(state: TripState) -> str | list[Send]:
    if not state.get("disrupted"):
        return "await_event"
    return [
        Send("propose", {
            "strategy": name,
            "trip": state["trip"],
            "disrupted": state["disrupted"],
            "movable": state["movable"],
            "now": state["now"],
        })
        for name in STRATEGIES
    ]


def propose(task: ProposeTask) -> dict:
    repaired = STRATEGIES[task["strategy"]](task["disrupted"], task["trip"], set(task["movable"]), task["now"])
    if repaired is None:
        return {"options": []}
    return {"options": [make_option(task["strategy"], task["disrupted"], repaired)]}


def make_validate_and_rank(ranker: Callable[..., Recommendation]):
    def validate_and_rank(state: TripState) -> dict:
        trip, disrupted, pending = state["trip"], state["disrupted"], state["pending"]
        order = list(STRATEGIES)
        unique: list[Option] = []
        for o in sorted(state["options"], key=lambda o: order.index(o["strategy"])):
            if find_violations(o["items"], trip, since=state["now"]):
                continue  # belt and braces: never offer an option the solver rejects
            if not any(same_plan(o["items"], u["items"]) for u in unique):
                unique.append(o)
        unique = [{**o, "id": "ABCDEFGH"[i]} for i, o in enumerate(unique)]

        if unique:
            rec = ranker(unique, trip=trip, events=pending, items=disrupted)
        else:
            what = "; ".join(describe_event(e, disrupted) for e in pending)
            rec = {
                "ranking": [], "recommended": None, "source": "solver",
                "message": f"{what}. No automatic fix works because fixed bookings collide. "
                           "Dismiss to keep the plan as it is and sort it out by hand.",
            }
        return {"options": Overwrite(unique), "recommendation": rec}

    return validate_and_rank


def choose(state: TripState) -> Command[Literal["assess_impact", "apply_and_notify", "choose"]]:
    offered = [o["id"] for o in state["options"]]
    reply, error = parse_reply(interrupt({"waiting_for": "choice", "options": offered}),
                               {"event", "choose", "dismiss"})
    if error:
        return _ignored("choose", error)
    if reply["type"] == "event":  # another disruption arrived while you were deciding
        return Command(goto="assess_impact", update={"event": reply["event"]})
    if reply["type"] == "dismiss":
        return Command(goto="apply_and_notify", update={"chosen": None})
    if reply["option_id"] not in offered:
        return _ignored("choose", f"there is no option {reply['option_id']}")
    return Command(goto="apply_and_notify", update={"chosen": reply["option_id"]})


def apply_and_notify(state: TripState) -> dict:
    chosen = state.get("chosen")
    if chosen is None:
        items, lines = state["disrupted"], ["Kept the plan as it is, conflicts included."]
    else:
        option = next(o for o in state["options"] if o["id"] == chosen)
        items = option["items"]
        lines = [f"Applied {chosen}: {option['title']}."] + [f"  • {c['text']}" for c in option["changes"]]
    return {
        "items": items, "disrupted": None, "pending": [], "movable": [], "now": None,
        "options": Overwrite([]), "recommendation": None, "chosen": None, "log": lines,
    }


def build_graph(checkpointer=None, ranker: Callable[..., Recommendation] = rank_options):
    g = StateGraph(TripState)
    g.add_node("plan_trip", plan_trip)
    g.add_node("confirm_plan", confirm_plan)
    g.add_node("await_event", await_event)
    g.add_node("assess_impact", assess_impact)
    g.add_node("propose", propose)
    g.add_node("validate_and_rank", make_validate_and_rank(ranker))
    g.add_node("choose", choose)
    g.add_node("apply_and_notify", apply_and_notify)

    g.add_edge(START, "plan_trip")
    g.add_edge("plan_trip", "confirm_plan")
    g.add_conditional_edges("assess_impact", route_after_impact, ["propose", "await_event"])
    g.add_edge("propose", "validate_and_rank")
    g.add_edge("validate_and_rank", "choose")
    g.add_edge("apply_and_notify", "await_event")
    return g.compile(checkpointer=checkpointer)


# Entry point for the Agent Server (see langgraph.json). The server supplies the
# checkpointer (Postgres on LangSmith Deployment), so none is attached here.
graph = build_graph()
