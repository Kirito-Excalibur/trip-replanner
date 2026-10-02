import json
from types import SimpleNamespace

from trip_replanner import ranker

OPTIONS = [
    {"id": "A", "strategy": "compress", "title": "Keep everything", "changes": [],
     "cost": {"dropped_priority": 0, "minutes_cut": 30, "minutes_moved": 145}},
    {"id": "B", "strategy": "drop", "title": "Skip coffee", "changes": [],
     "cost": {"dropped_priority": 2, "minutes_cut": 0, "minutes_moved": 5}},
]
EVENTS = [{"kind": "delay", "item": "flight", "minutes": 120, "at": None, "note": "hungry"}]


def fake_claude(monkeypatch, stop_reason="end_turn", answer=None):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=stop_reason,
            model="claude-opus-5-5",
            content=[SimpleNamespace(type="text", text=json.dumps(answer or {}))],
        )

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))

    def construct(**kwargs):
        calls.append({"client": kwargs})
        return client

    monkeypatch.setattr(ranker.anthropic, "Anthropic", construct)
    return calls


def rank(plan):
    return ranker.claude_rank(OPTIONS, trip=plan["trip"], events=EVENTS, items=plan["items"])


def test_claude_choice_wins_over_heuristic(plan, monkeypatch):
    calls = fake_claude(monkeypatch, answer={"ranking": ["B"], "recommended": "B", "message": "Skip coffee."})
    rec = rank(plan)
    assert rec == {"ranking": ["B", "A"], "recommended": "B", "message": "Skip coffee.", "source": "claude-opus-5-5"}

    assert calls[0]["client"]["default_headers"] is None
    req = calls[1]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert req["output_config"]["format"]["schema"]["properties"]["recommended"]["enum"] == ["A", "B"]
    assert "hungry" in req["messages"][0]["content"]


def test_refusal_falls_back_to_heuristic(plan, monkeypatch):
    fake_claude(monkeypatch, stop_reason="refusal")
    rec = rank(plan)
    assert rec["recommended"] == "A"
    assert rec["source"] == "heuristic (Claude unavailable: stop_reason=refusal)"


def test_workspace_id_is_sent_for_unscoped_keys(plan, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    calls = fake_claude(monkeypatch, answer={"ranking": ["A"], "recommended": "A", "message": "ok"})
    rank(plan)
    assert calls[0]["client"]["default_headers"] == {"anthropic-workspace-id": "wrkspc_test"}
