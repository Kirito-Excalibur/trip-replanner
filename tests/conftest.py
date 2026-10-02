import pytest

from trip_replanner.cli import load_plan
from trip_replanner.graph import plan_trip


@pytest.fixture
def plan():
    """The bundled Lisbon day, normalized the same way the graph's first node does it."""
    result = plan_trip({**load_plan(None), "log": []})
    return {"trip": result["trip"], "items": result["items"]}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Tests never read the developer's .env or call Claude."""
    monkeypatch.setenv("TRIP_RANKER", "heuristic")
    for var in ("ANTHROPIC_WORKSPACE_ID", "TRIP_USER_TOKENS", "TRIP_WEBHOOK_SECRET", "TRIP_TOKEN", "TRIP_SERVER_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("trip_replanner.cli.load_dotenv", lambda *a, **k: None)
