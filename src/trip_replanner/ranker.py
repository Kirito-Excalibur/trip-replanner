"""Rank repair options. Claude handles judgment and wording; a heuristic is the fallback.

Every option has already been checked by the solver, so Claude never does time
math here. It decides which trade-off suits this traveler and explains it in
one short notification.
"""

from __future__ import annotations

import json
import os

import anthropic

from .models import Event, Item, Option, Recommendation, Trip
from .solver import describe_event

MODEL = "claude-opus-5-5"

SYSTEM = """\
You help a traveler decide how to repair their day after a disruption.

Every option you receive has already been checked by a scheduling solver: all \
of them fit (travel times, opening hours and bookings are respected). Do not \
re-check times and do not invent new options.

Rank the options by how well they fit the traveler's stated preferences and \
any note they added. Then write the phone notification they will see: at most \
two sentences and under 280 characters. Say what happened, recommend one option \
by its letter, and name the main trade-off."""


def heuristic_rank(options: list[Option], *, trip: Trip, events: list[Event], items: dict[str, Item]) -> Recommendation:
    def cost(o: Option) -> int:
        c = o["cost"]
        return 40 * c["dropped_priority"] + c["minutes_cut"] + c["minutes_moved"] // 10

    ranked = sorted(options, key=cost)
    what = "; ".join(describe_event(e, items) for e in events)
    return {
        "ranking": [o["id"] for o in ranked],
        "recommended": ranked[0]["id"],
        "message": f"{what}. Suggested fix: {ranked[0]['id']}, {ranked[0]['title']}.",
        "source": "heuristic",
    }


def claude_rank(options: list[Option], *, trip: Trip, events: list[Event], items: dict[str, Item]) -> Recommendation:
    fallback = heuristic_rank(options, trip=trip, events=events, items=items)
    ids = [o["id"] for o in options]
    payload = {
        "traveler_preferences": trip["preferences"],
        "what_happened": [describe_event(e, items) for e in events],
        "traveler_notes": [e["note"] for e in events if e["note"]],
        "options": [
            {"id": o["id"], "summary": o["title"], "changes": [c["text"] for c in o["changes"]]}
            for o in options
        ],
    }
    schema = {
        "type": "object",
        "properties": {
            "ranking": {"type": "array", "items": {"type": "string", "enum": ids}},
            "recommended": {"type": "string", "enum": ids},
            "message": {"type": "string"},
        },
        "required": ["ranking", "recommended", "message"],
        "additionalProperties": False,
    }

    def unavailable(reason: str) -> Recommendation:
        return {**fallback, "source": f"heuristic (Claude unavailable: {reason})"}

    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    client = anthropic.Anthropic(
        timeout=60.0,
        # Only needed for API keys that aren't scoped to a workspace.
        default_headers={"anthropic-workspace-id": workspace} if workspace else None,
    )
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            # A simple choice between a few options doesn't need deep thinking.
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            # If a safety classifier declines, retry server-side on Anthropic's recommended model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}],
        )
    except TypeError as e:  # raised by the SDK when no credentials can be resolved
        if "authentication" not in str(e):
            raise
        return unavailable("no API credentials")
    except anthropic.AuthenticationError:
        return unavailable("invalid API key")
    except anthropic.RateLimitError:
        return unavailable("rate limited")
    except anthropic.APIStatusError as e:
        error = e.body.get("error") if isinstance(e.body, dict) else None
        detail = error.get("message") if isinstance(error, dict) else None
        return unavailable(f"API error {e.status_code}" + (f": {detail}" if detail else ""))
    except anthropic.APIConnectionError:
        return unavailable("can't reach the API")

    if response.stop_reason != "end_turn":  # refusal or max_tokens
        return unavailable(f"stop_reason={response.stop_reason}")
    data = json.loads(next(b.text for b in response.content if b.type == "text"))

    # Recommended first, then Claude's order, then anything it left out.
    ranking = list(dict.fromkeys([data["recommended"], *data["ranking"], *fallback["ranking"]]))
    return {
        "ranking": ranking,
        "recommended": data["recommended"],
        "message": data["message"],
        "source": response.model,
    }


def rank_options(options: list[Option], *, trip: Trip, events: list[Event], items: dict[str, Item]) -> Recommendation:
    """Default ranker: Claude, unless TRIP_RANKER=heuristic (offline / free / deterministic)."""
    if os.environ.get("TRIP_RANKER", "claude") == "heuristic":
        return heuristic_rank(options, trip=trip, events=events, items=items)
    return claude_rank(options, trip=trip, events=events, items=items)
