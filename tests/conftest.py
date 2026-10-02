import pytest

from trip_replanner.cli import load_plan
from trip_replanner.graph import plan_trip


@pytest.fixture
def plan():
    """The bundled Lisbon day, normalized the same way the graph's first node does it."""
    data = load_plan(None)
    return {"trip": data["trip"], "items": plan_trip({**data, "log": []})["items"]}


@pytest.fixture(autouse=True)
def offline_ranker(monkeypatch):
    monkeypatch.setenv("TRIP_RANKER", "heuristic")
