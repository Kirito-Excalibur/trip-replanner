"""Command-line client for trip-replanner running on a LangGraph Agent Server.

Every command is an HTTP call through the LangGraph SDK. A trip is a thread owned
by your token's user, and each reply resumes it with Command(resume=...). Nothing
runs between commands; the server holds the trip.

    TRIP_SERVER_URL   deployment URL (default http://127.0.0.1:2024, i.e. `langgraph dev`)
    TRIP_TOKEN        your personal token (an admin creates it with `trip token <name>`)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from importlib import resources

from dotenv import find_dotenv, load_dotenv
from langgraph_sdk import get_sync_client
from langgraph_sdk.errors import APIConnectionError, APIStatusError, AuthenticationError
from pydantic import ValidationError

from .auth import hash_token
from .models import fmt, hm, parse
from .schemas import EventIn, parse_plan
from .solver import find_violations, validate_event

ASSISTANT = "trip"  # graph id in langgraph.json
DEFAULT_URL = "http://127.0.0.1:2024"
HINTS = {
    "confirm_plan": "Waiting for your approval. Run: trip approve",
    "await_event": "Watching for disruptions. Try: trip delay flight 120",
}


class CliError(Exception):
    pass


def load_plan(path: str | None) -> dict:
    if path:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.loads(resources.files("trip_replanner").joinpath("data/lisbon.json").read_text("utf-8"))
    return {"trip": data["trip"], "items": {it["id"]: it for it in data["items"]}}


def connect():
    token = os.environ.get("TRIP_TOKEN")
    if not token:
        raise CliError("set TRIP_TOKEN to your personal token (an admin creates one with: trip token <name>)")
    url = os.environ.get("TRIP_SERVER_URL", DEFAULT_URL)
    return get_sync_client(url=url, api_key=None, headers={"Authorization": f"Bearer {token}"})


def find_trip(client, name: str) -> dict | None:
    found = client.threads.search(metadata={"trip_name": name}, limit=1)
    return found[0] if found else None


def waiting_at(state: dict) -> str | None:
    return state["next"][0] if state.get("next") else None


def lineage(client, thread_id: str):
    """Checkpoints on the current branch, newest first. Forks leave other branches in the thread."""
    by_id = {s["checkpoint"]["checkpoint_id"]: s for s in client.threads.get_history(thread_id, limit=1000)}
    s = client.threads.get_state(thread_id)
    while s is not None:
        yield s
        parent = s.get("parent_checkpoint")
        s = by_id.get(parent["checkpoint_id"]) if parent else None


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


def render(state: dict) -> None:
    values, waiting = state["values"], waiting_at(state)
    print(f"\n{values['trip']['name']} (thread is paused at: {waiting or 'finished'})")
    render_plan(values.get("disrupted") or values["items"], values["trip"], values.get("now"))
    print()
    if waiting == "choose":
        rec = values["recommendation"]
        print(f"  {rec['message']}\n  (ranked by {rec['source']})\n")
        by_id = {o["id"]: o for o in values["options"]}
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


def at_time(items: dict, item_id: str, value: str | None) -> str | None:
    """Accept HH:MM (same day as the item) or a full ISO timestamp."""
    if value is None or "T" in value:
        return value
    if not re.fullmatch(r"\d{1,2}:\d{2}", value):
        raise CliError(f"--at must be HH:MM or an ISO timestamp, not {value!r}")
    h, m = map(int, value.split(":"))
    return fmt(parse(items[item_id]["start"]).replace(hour=h, minute=m))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trip", description="A trip that replans itself (LangGraph Agent Server client).")
    p.add_argument("--trip", default="demo", help="trip name (default: demo)")
    sub = p.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new", help="start a trip from a JSON plan (default: the bundled Lisbon day)")
    new.add_argument("--file")
    sub.add_parser("approve", help="approve the plan and start watching")
    sub.add_parser("show", help="print the plan and anything waiting on you")
    sub.add_parser("list", help="list your trips and their thread ids (needed for webhooks)")
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
    tk = sub.add_parser("token", help="admin: create a token for a traveler (no server needed)")
    tk.add_argument("name")
    sub.add_parser("graph", help="print the graph as a Mermaid diagram")
    return p


def run(args: argparse.Namespace) -> None:
    if args.cmd == "token":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", args.name):
            raise CliError("names may use letters, digits, - and _ (max 32)")
        token = secrets.token_urlsafe(32)
        print(f"Send this to {args.name} privately; it is shown only once:\n  TRIP_TOKEN={token}\n")
        print(f"Add this entry to TRIP_USER_TOKENS on the server (comma-separated):\n  {args.name}:{hash_token(token)}")
        return
    if args.cmd == "graph":
        from .graph import graph

        print(graph.get_graph().draw_mermaid())
        return

    client = connect()
    if args.cmd == "list":
        trips = client.threads.search(limit=100)
        for t in sorted(trips, key=lambda t: t["created_at"]):
            print(f"  {t['metadata'].get('trip_name', '?'):<20} {t['thread_id']}  {t['status']}")
        if not trips:
            print("  No trips yet. Run: trip new")
        return

    if args.cmd == "new":
        plan = load_plan(args.file)
        try:
            parse_plan(plan)  # fail fast here; the server validates again
        except ValueError as e:
            raise CliError(f"invalid plan: {e}") from None
        if find_trip(client, args.trip):
            raise CliError(f"trip {args.trip!r} already exists; pick another --trip name")
        thread = client.threads.create(metadata={"trip_name": args.trip})
        client.runs.wait(thread["thread_id"], ASSISTANT, input=plan)
        print_update(client, thread["thread_id"], seen=0)
        return

    trip = find_trip(client, args.trip)
    if trip is None:
        raise CliError(f"no trip {args.trip!r}. Run: trip new (or trip list)")
    tid = trip["thread_id"]
    state = client.threads.get_state(tid)
    values, waiting = state["values"], waiting_at(state)
    seen = len(values.get("log", []))

    def resume(reply: dict) -> None:
        client.runs.wait(tid, ASSISTANT, command={"resume": reply}, multitask_strategy="enqueue")

    match args.cmd:
        case "approve":
            if waiting != "confirm_plan":
                raise CliError("there is no plan waiting for approval")
            resume({"type": "approve"})
        case "delay" | "late" | "close":
            if waiting not in ("await_event", "choose"):
                raise CliError("the trip isn't watching for events (approve the plan first?)")
            items = values.get("disrupted") or values["items"]
            kind = {"delay": "delay", "late": "running_late", "close": "closure"}[args.cmd]
            fields = {"kind": kind, "item": args.item, "minutes": getattr(args, "minutes", 0), "note": args.note}
            try:
                validate_event(items, EventIn(**fields).to_state())
                event = EventIn(**fields, at=at_time(items, args.item, args.at))
            except (ValueError, ValidationError) as e:
                raise CliError(f"{e}. Known plans: {', '.join(items)}") from None
            resume({"type": "event", "event": event.model_dump(mode="json")})
        case "choose":
            if waiting != "choose":
                raise CliError("nothing to choose right now")
            if args.option.upper() not in {o["id"] for o in values["options"]}:
                raise CliError(f"no option {args.option!r}")
            resume({"type": "choose", "option_id": args.option.upper()})
        case "dismiss":
            if waiting != "choose":
                raise CliError("nothing to dismiss right now")
            resume({"type": "dismiss"})
        case "end":
            if waiting not in ("await_event", "confirm_plan"):
                raise CliError("resolve the pending choice first")
            resume({"type": "end"})
        case "history":
            for s in lineage(client, tid):
                last = (s["values"].get("log") or ["-"])[-1]
                nxt = ", ".join(f"{n}×{s['next'].count(n)}" if s["next"].count(n) > 1 else n
                                for n in dict.fromkeys(s["next"])) or "END"
                print(f"  step {s['metadata'].get('step'):>3}  {s['checkpoint']['checkpoint_id'][-8:]}"
                      f"  next={nxt:<18} {last[:58]}")
            return
        case "rechoose":
            option = args.option.upper()
            past = next((s for s in lineage(client, tid)
                         if s["next"] == ["choose"] and option in {o["id"] for o in s["values"]["options"]}), None)
            if past is None:
                raise CliError(f"no earlier decision offered option {option!r}")
            print(f"Rewinding to checkpoint {past['checkpoint']['checkpoint_id'][-8:]} "
                  f"and choosing {option}. Anything after it is replaced on this new branch.")
            seen = len(past["values"].get("log", []))
            # Resuming the old checkpoint directly would replay the answer stored there.
            # update_state forks a new branch from it, so choose runs again.
            fork = client.threads.update_state(tid, {"chosen": None}, as_node="validate_and_rank",
                                               checkpoint_id=past["checkpoint"]["checkpoint_id"])
            client.runs.wait(tid, ASSISTANT, input=None, checkpoint_id=fork["checkpoint"]["checkpoint_id"])
            resume({"type": "choose", "option_id": option})
        case "show":
            pass
    print_update(client, tid, seen)


def print_update(client, thread_id: str, seen: int) -> None:
    state = client.threads.get_state(thread_id)
    for line in state["values"].get("log", [])[seen:]:
        print(f"» {line}")
    render(state)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True))  # shell variables win over .env
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except AuthenticationError:
        print("error: the server rejected TRIP_TOKEN", file=sys.stderr)
        return 1
    except APIConnectionError:
        url = os.environ.get("TRIP_SERVER_URL", DEFAULT_URL)
        print(f"error: can't reach the server at {url} (is `langgraph dev` running?)", file=sys.stderr)
        return 1
    except APIStatusError as e:
        print(f"error: server returned {e.status_code}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
