# trip-replanner

A trip itinerary that replans itself when something goes wrong. Each trip is a
**LangGraph thread** on a LangGraph Agent Server. The thread sleeps between
disruptions. When a flight is delayed or a place closes, it wakes up, works out
what's affected, builds repair options in parallel, has Claude rank them, and
waits for the traveler to pick one.

```
 trip CLI ──Bearer token──▶ ┌──────────── Agent Server (LangSmith Deployment) ────────────┐
                            │ auth.py      token → traveler; trips filtered by owner      │
 flight feed ──HMAC──────▶  │ webhooks.py  POST /webhooks/events → resume the trip        │
                            │ graph.py     the trip graph (Postgres checkpoints, queue)   │
                            └─────────────────────────────────────────────────────────────┘
```

## Local development

```bash
uv sync --group dev
cp .env.example .env            # then fill it in (an existing .env is already set up)
uv run trip token yourname      # prints TRIP_TOKEN and a TRIP_USER_TOKENS entry for .env
uv run langgraph dev            # local Agent Server on http://127.0.0.1:2024 (+ Studio link)
```

In another terminal:

```bash
uv run trip new && uv run trip approve
uv run trip delay flight 120 --at 07:40 --note "we're starving"
uv run trip close maat          # a second event while you decide stacks on the first
uv run trip choose B
uv run trip rechoose A          # time travel: fork from that decision, pick A instead
uv run trip list                # your trips and their ids
uv run trip history
```

Other events are `trip late <item> <minutes>` and `trip close <item>`. `--trip NAME`
picks a trip, and each traveler sees only their own trips.

## Deploy to LangSmith

Requires a LangSmith account, Docker running locally, and a LangSmith API key
(`LANGSMITH_API_KEY` in your shell or `.env`).

```bash
uv run langgraph deploy --name trip-replanner --deployment-type prod
```

This builds the image from `uv.lock` and uploads the server entries of `.env` as
deployment secrets. `.dockerignore` keeps `.env`, databases and tests out of the
image. Persistence (Postgres), queuing and tracing come from the platform.

Then point the CLI at the deployment: set `TRIP_SERVER_URL` in `.env` to the
deployment URL shown by the command (or in the LangSmith UI).

**Adding a traveler:** run `uv run trip token alice`. Send alice her `TRIP_TOKEN`
privately, append her `alice:<hash>` entry to `TRIP_USER_TOKENS`, and redeploy (or
update the secret in LangSmith). She installs the CLI and sets `TRIP_SERVER_URL` and
`TRIP_TOKEN`. To revoke someone, remove their entry.

## Configuration

| Variable | Side | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | server | Claude ranker. Without it, ranking falls back to the heuristic. |
| `ANTHROPIC_WORKSPACE_ID` | server | Only for API keys not scoped to a workspace. |
| `TRIP_RANKER` | server | `claude` (default) or `heuristic` (offline, free). |
| `TRIP_USER_TOKENS` | server | `name:sha256(token)` entries, comma-separated. Tokens never live on the server. |
| `TRIP_WEBHOOK_SECRET` | server | HMAC secret for the webhook. Empty disables it. |
| `TRIP_SERVER_URL` | client | Server URL (default `http://127.0.0.1:2024`). |
| `TRIP_TOKEN` | client | The traveler's personal token. |

## Webhook for machine-reported events

```
POST /webhooks/events
X-Trip-Timestamp: <unix seconds>
X-Trip-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>" keyed with TRIP_WEBHOOK_SECRET>

{"trip_id": "<id from trip list>", "event": {"kind": "delay", "item": "flight", "minutes": 120, "note": ""}}
```

The response is `202` with a `run_id`. Bad or older-than-5-minutes signatures get `401`,
invalid events `422`, unknown trips `404`, and trips that aren't watching yet `409`.
Events for the same trip are queued and applied in order. To try it:

```bash
TRIP_WEBHOOK_SECRET=... uv run python scripts/send_webhook.py "$TRIP_SERVER_URL" <trip-id> delay flight 120
```

## Where LangGraph is doing real work

| Feature | Where | Why it's needed |
|---|---|---|
| `interrupt()` + platform checkpointer | `confirm_plan`, `await_event`, `choose` | A trip lasts days. Between events nothing runs. |
| `Command(resume=...)` runs | CLI, webhook | Waking a trip is a run on its thread. |
| `multitask_strategy="enqueue"` | CLI, webhook | Simultaneous events are applied one after the other. |
| `Send` fan-out | `route_after_impact` → `propose` | Shift, compress and drop strategies run in parallel. |
| `Command(goto=...)` | interrupt nodes | Events while deciding stack; invalid replies re-wait. |
| `update_state` fork | `trip rechoose` | Time travel: branch from an old decision. |
| Custom `Auth` | `auth.py` | Per-traveler identity and data isolation. |

## What's code vs. what's the LLM

`solver.py` handles everything that has to be right: time math, travel, opening
windows, fixed bookings and dependency propagation. Every option it offers is
already checked. `ranker.py` only picks the feasible trade-off that suits the
traveler and writes the notification. Its structured output is limited to the
offered option letters. Everything entering the graph (plans, replies, webhook
events) is validated in `schemas.py`, and an invalid reply never moves a trip.

## Tests

```bash
uv run pytest                                  # unit tests
RUN_E2E=1 uv run pytest tests/test_e2e.py      # starts a real local Agent Server
```

CI (`.github/workflows/ci.yml`) runs lint, `langgraph validate`, and both suites.

## Known limits

- No push notifications yet: webhook-driven changes show up on the next `trip show`.
- Trips are kept indefinitely. Add a thread TTL if storage matters.
- No "move to tomorrow" repair strategy yet.
