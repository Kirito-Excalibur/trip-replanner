# trip-replanner

A trip itinerary that replans itself when something goes wrong. Each trip is a
**LangGraph thread** that sleeps between disruptions. When a flight is delayed or
a place closes, the thread wakes up, works out what's affected, builds repair
options in parallel, and asks you to pick one.

## Quick start

```bash
uv sync
uv run trip new                         # load the bundled Lisbon day; pauses for approval
uv run trip approve                     # thread now sleeps at await_event
uv run trip delay flight 120 --at 07:40 # wake it: 2 h flight delay
uv run trip close maat                  # a second event while you're deciding stacks on the first
uv run trip choose B                    # apply an option; thread goes back to sleep
uv run trip rechoose A                  # time travel: fork from that decision, pick A instead
uv run trip history                     # checkpoints on the current branch
uv run trip graph                       # Mermaid diagram of the graph
```

Other events: `trip late <item> <minutes>` (you're running behind) and
`trip close <item>`. Add `--note "kids are tired"` to give the ranker context.
`--trip NAME` runs several trips side by side; each one is its own thread.

**Claude ranks the options** if credentials are available (`ANTHROPIC_API_KEY` or
an `ant auth login` profile). Without them, or with `TRIP_RANKER=heuristic`, it
falls back to a deterministic cost function, and the CLI says which one ranked.
Both settings can go in `.env` (git-ignored). The CLI loads it at startup, and
shell variables take precedence.

## Where LangGraph is doing real work

| Feature | Where | Why it's needed |
|---|---|---|
| `interrupt()` + checkpointer | `confirm_plan`, `await_event`, `choose` | A trip lasts days. Between events nothing runs; the thread is only a row in SQLite. |
| `Command(resume=...)` | `cli.py` | The CLI acts as the watcher that wakes a thread when something happens. |
| `Send` fan-out | `route_after_impact` → `propose` | Shift, compress and drop strategies run in parallel, and a reducer merges their options. |
| `Command(goto=...)` | `choose` | A new event while you're deciding goes back to `assess_impact` and stacks on the pending one. |
| `Overwrite` | `assess_impact`, `apply_and_notify` | Resets the reducer-backed `options` list between disruptions. |
| `update_state` fork | `trip rechoose` | Time travel: branch from an old decision and pick differently. |

## What's code vs. what's the LLM

`solver.py` handles everything that has to be right: time math, travel times,
opening windows, fixed bookings, and dependency propagation. Every option it
produces is already checked. `ranker.py` only decides which feasible trade-off
suits this traveler's preferences and writes the notification. It uses structured
output with the option letters as an enum, so it can't recommend an option that
doesn't exist.

## Layout

```
src/trip_replanner/
  models.py   state shapes (JSON-friendly, ISO times)
  solver.py   deterministic: conflicts, downstream impact, repair strategies
  ranker.py   Claude ranking + heuristic fallback
  graph.py    the StateGraph
  cli.py      the watcher / front end
  data/lisbon.json   sample trip (copy it to plan your own; `trip new --file my.json`)
tests/        solver, graph flow, CLI + time travel, ranker (fake client)
```

## Known limits / next steps

- Single-day sample; multi-day works in the model but there's no "move to tomorrow" strategy yet.
- Events come from the CLI. A real watcher would poll flight-status / weather APIs
  and call the same `Command(resume=...)`.
- Only one process should resume a given trip at a time; a real service needs a per-thread queue.
