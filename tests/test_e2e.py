"""End to end against a real local Agent Server (`langgraph dev`) with throwaway credentials.

Opt in, since it starts a server: RUN_E2E=1 uv run pytest tests/test_e2e.py
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from langgraph_sdk import get_sync_client

from trip_replanner import webhooks
from trip_replanner.auth import hash_token
from trip_replanner.cli import main

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "trip_replanner"
pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="set RUN_E2E=1 to run against a local server")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("server")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = {
        "dependencies": [str(ROOT)],
        "graphs": {"trip": f"{SRC}/graph.py:graph"},
        "auth": {"path": f"{SRC}/auth.py:auth"},
        "http": {"app": f"{SRC}/webhooks.py:app"},
        "env": {  # never the developer's .env: no Claude calls, no real tokens
            "TRIP_RANKER": "heuristic",
            "TRIP_WEBHOOK_SECRET": "e2e-secret",
            "TRIP_USER_TOKENS": f"alice:{hash_token('alice-token')},bob:{hash_token('bob-token')}",
        },
    }
    (tmp / "langgraph.json").write_text(json.dumps(config))
    log = open(tmp / "server.log", "w")
    proc = subprocess.Popen(
        [str(Path(sys.executable).parent / "langgraph"), "dev", "--config", str(tmp / "langgraph.json"),
         "--port", str(port), "--no-browser", "--no-reload"],
        cwd=tmp, stdout=log, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail("server exited:\n" + (tmp / "server.log").read_text()[-3000:])
        try:
            if httpx.get(f"{url}/ok", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        pytest.fail("server did not start in time")
    yield url
    proc.terminate()
    proc.wait(timeout=15)
    log.close()


def test_full_trip_over_http(server, monkeypatch, capsys):
    monkeypatch.setenv("TRIP_SERVER_URL", server)

    def trip(token, *args):
        monkeypatch.setenv("TRIP_TOKEN", token)
        code = main(["--trip", "e2e", *args])
        return code, capsys.readouterr()

    assert trip("alice-token", "new")[0] == 0
    assert trip("alice-token", "approve")[0] == 0
    code, out = trip("alice-token", "delay", "flight", "120", "--at", "07:40")
    assert code == 0 and "★" in out.out

    code, out = trip("bob-token", "show")  # another traveler can't see it
    assert code == 1 and "no trip 'e2e'" in out.err
    code, out = trip("nobody", "show")
    assert code == 1 and "rejected TRIP_TOKEN" in out.err

    thread_id = trip("alice-token", "list")[1].out.split()[1]
    body = json.dumps({"trip_id": thread_id, "event": {"kind": "closure", "item": "maat"}}).encode()
    ts = str(int(time.time()))
    response = httpx.post(f"{server}/webhooks/events", content=body, headers={
        "x-trip-timestamp": ts, "x-trip-signature": webhooks.sign("e2e-secret", ts, body)})
    assert response.status_code == 202
    alice = get_sync_client(url=server, api_key=None, headers={"Authorization": "Bearer alice-token"})
    alice.runs.join(thread_id, response.json()["run_id"])
    assert [e["item"] for e in alice.threads.get_state(thread_id)["values"]["pending"]] == ["flight", "maat"]

    assert trip("alice-token", "choose", "A")[0] == 0
    code, out = trip("alice-token", "rechoose", "B")
    assert code == 0 and "Applied B: Skip Coffee in Baixa." in out.out
    assert "Trip finished." in trip("alice-token", "end")[1].out
