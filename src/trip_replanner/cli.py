"""Command-line front end. It plays the role of the watcher.

Each command opens the SQLite checkpointer, checks where the trip's thread is
paused, and resumes it with Command(resume=...). Nothing runs between commands.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources

from dotenv import find_dotenv, load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .graph import build_graph
from .models import Event, fmt, hm, parse
from .solver import find_violations, validate_event

HINTS = {
    "confirm_plan": "Waiting for your approval. Run: trip approve",
    "await_event": "Watching for disruptions. Try: trip delay flight 120",
}


def load_plan(path: str | None) -> dict:
    if path:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.loads(resources.files("trip_replanner").joinpath("data/lisbon.json").read_text("utf-8"))
    return {"trip": data["trip"], "items": {it["id"]: it for it in data["items"]}}


def render_plan(items: dict, trip: dict, since: str | None = None) -> None:
    flagged = {v["item"] for v in find_violations(items, trip, since)}
    day = None
    for it in sorted(items.values(), key=lambda it: parse(it["start"])):
        if parse(it["start"]).date() != day:
            day = parse(it["start"]).date()
            print(f"\n  {day:%A %d %B}")
        if it["status"] == "cancelled":
            print(f"    {'skipped':^11}  {it['title']}")
            continue
        flags = " ".join(f for f, on in [("[fixed]", it["fixed"]), ("⚠ conflict", it["id"] in flagged)] if on)
        print(f"    {hm(it['start'])}–{hm(it['end'])}  {it['title']:<36} {it['location']:<11} {flags}")


def render(snapshot) -> None:
    state, waiting = snapshot.values, (snapshot.next or (None,))[0]
    print(f"\n{state['trip']['name']} (thread is paused at: {waiting or 'finished'})")
    render_plan(state.get("disrupted") or state["items"], state["trip"], state.get("now"))
    print()
    if waiting == "choose":
        rec = state["recommendation"]
        print(f"  {rec['message']}\n  (ranked by {rec['source']})\n")
        by_id = {o["id"]: o for o in state["options"]}
        for oid in rec["ranking"]:
            o = by_id[oid]
            star = "★" if oid == rec["recommended"] else " "
            print(f"  {star} {oid}  {o['title']}")
            for c in o["changes"]:
                print(f"         - {c['text']}")
        ids = "|".join(rec["ranking"]) or "-"
        print(f"\n  Reply: trip choose {ids}, or trip dismiss. A new event will stack on this one.")
    elif waiting in HINTS:
        print(f"  {HINTS[waiting]}")


def lineage(graph, cfg):
    """Checkpoints on the current branch, newest first. Forks leave other branches in the thread."""
    by_id = {s.config["configurable"]["checkpoint_id"]: s for s in graph.get_state_history(cfg)}
    s = graph.get_state(cfg)
    while s is not None:
        yield s
        s = by_id.get(s.parent_config["configurable"]["checkpoint_id"]) if s.parent_config else None


def at_time(items: dict, item_id: str, value: str | None) -> str | None:
    """Accept HH:MM (same day as the item) or a full ISO timestamp."""
    if value is None or "T" in value:
        return value
    h, m = map(int, value.split(":"))
    return fmt(parse(items[item_id]["start"]).replace(hour=h, minute=m))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trip", description="A trip that replans itself (LangGraph demo).")
    p.add_argument("--db", default="trips.sqlite", help="checkpoint database (default: trips.sqlite)")
    p.add_argument("--trip", default="demo", help="trip id = LangGraph thread_id (default: demo)")
    sub = p.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new", help="start a trip from a JSON plan (default: the bundled Lisbon day)")
    new.add_argument("--file")
    sub.add_parser("approve", help="approve the plan and start watching")
    sub.add_parser("show", help="print the plan and anything waiting on you")
    for name, help_ in [("delay", "a booking (flight, train...) is delayed"),
                        ("late", "you're running behind on a plan")]:
        e = sub.add_parser(name, help=help_)
        e.add_argument("item")
        e.add_argument("minutes", type=int)
    close = sub.add_parser("close", help="a place is closed or a booking is cancelled")
    close.add_argument("item")
    for name in ("delay", "late", "close"):
        sub.choices[name].add_argument("--at", help="when this was reported (HH:MM); earlier plans are frozen")
        sub.choices[name].add_argument("--note", default="", help='context for the ranker, e.g. "kids are tired"')
    ch = sub.add_parser("choose", help="pick a repair option")
    ch.add_argument("option")
    sub.add_parser("dismiss", help="keep the plan as it is, conflicts included")
    sub.add_parser("end", help="finish the trip")
    sub.add_parser("history", help="list this trip's checkpoints")
    rc = sub.add_parser("rechoose", help="time travel: go back to the last decision and pick differently")
    rc.add_argument("option")
    sub.add_parser("graph", help="print the graph as a Mermaid diagram")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True))  # shell variables win over .env
    args = build_parser().parse_args(argv)
    if args.cmd == "graph":
        print(build_graph().get_graph().draw_mermaid())
        return 0

    with SqliteSaver.from_conn_string(args.db) as saver:
        graph = build_graph(saver)
        cfg = {"configurable": {"thread_id": args.trip}}
        snap = graph.get_state(cfg)
        waiting = (snap.next or (None,))[0]
        seen = len(snap.values.get("log", []))

        def fail(msg: str) -> int:
            print(f"error: {msg}", file=sys.stderr)
            return 1

        if args.cmd != "new" and not snap.values:
            return fail(f"no trip {args.trip!r} yet. Run: trip new")

        match args.cmd:
            case "new":
                if snap.values:
                    return fail(f"trip {args.trip!r} already exists; use --trip NAME or another --db")
                graph.invoke(load_plan(args.file), cfg)
            case "approve":
                if waiting != "confirm_plan":
                    return fail("there is no plan waiting for approval")
                graph.invoke(Command(resume={"type": "approve"}), cfg)
            case "delay" | "late" | "close":
                if waiting not in ("await_event", "choose"):
                    return fail("the trip isn't watching for events (approve the plan first?)")
                items = snap.values.get("disrupted") or snap.values["items"]
                kind = {"delay": "delay", "late": "running_late", "close": "closure"}[args.cmd]
                event: Event = {"kind": kind, "item": args.item, "minutes": getattr(args, "minutes", 0),
                                "at": None, "note": args.note}
                try:
                    validate_event(items, event)
                    event["at"] = at_time(items, args.item, args.at)
                except ValueError as e:
                    return fail(f"{e}. Known plans: {', '.join(items)}")
                graph.invoke(Command(resume={"type": "event", "event": event}), cfg)
            case "choose":
                if waiting != "choose":
                    return fail("nothing to choose right now")
                if args.option.upper() not in {o["id"] for o in snap.values["options"]}:
                    return fail(f"no option {args.option!r}")
                graph.invoke(Command(resume={"type": "choose", "option_id": args.option.upper()}), cfg)
            case "dismiss":
                if waiting != "choose":
                    return fail("nothing to dismiss right now")
                graph.invoke(Command(resume={"type": "dismiss"}), cfg)
            case "end":
                if waiting not in ("await_event", "confirm_plan"):
                    return fail("resolve the pending choice first")
                graph.invoke(Command(resume={"type": "end"}), cfg)
            case "history":
                for s in lineage(graph, cfg):
                    last = (s.values.get("log") or ["-"])[-1]
                    nxt = ", ".join(f"{n}×{s.next.count(n)}" if s.next.count(n) > 1 else n
                                    for n in dict.fromkeys(s.next)) or "END"
                    print(f"  step {s.metadata.get('step'):>3}  {s.config['configurable']['checkpoint_id'][-8:]}"
                          f"  next={nxt:<18} {last[:58]}")
                return 0
            case "rechoose":
                option = args.option.upper()
                past = next((s for s in lineage(graph, cfg)
                             if s.next == ("choose",) and option in {o["id"] for o in s.values["options"]}), None)
                if past is None:
                    return fail(f"no earlier decision offered option {option!r}")
                print(f"Rewinding to checkpoint {past.config['configurable']['checkpoint_id'][-8:]} "
                      f"and choosing {option}. Anything after it is replaced on this new branch.")
                seen = len(past.values.get("log", []))
                # Resuming the old checkpoint directly would replay the answer stored there.
                # update_state forks a new branch from it, so choose runs again.
                fork = graph.update_state(past.config, {"chosen": None}, as_node="validate_and_rank")
                graph.invoke(None, fork)  # runs choose up to its interrupt on the new branch
                graph.invoke(Command(resume={"type": "choose", "option_id": option}), cfg)
            case "show":
                pass

        snap = graph.get_state(cfg)
        for line in snap.values.get("log", [])[seen:]:
            print(f"» {line}")
        render(snap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
