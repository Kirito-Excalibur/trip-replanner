import json

from trip_replanner.auth import hash_token
from trip_replanner.cli import main


def test_token_command_prints_a_matching_hash(capsys):
    assert main(["token", "alice"]) == 0
    out = capsys.readouterr().out
    token = out.split("TRIP_TOKEN=")[1].split()[0]
    assert f"alice:{hash_token(token)}" in out


def test_commands_need_a_token(capsys):
    assert main(["show"]) == 1
    assert "set TRIP_TOKEN" in capsys.readouterr().err


def test_invalid_plans_fail_before_reaching_the_server(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRIP_TOKEN", "anything")
    monkeypatch.setenv("TRIP_SERVER_URL", "http://127.0.0.1:9")  # nothing listens here
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"trip": {"name": "x"}, "items": [{"id": "a", "title": "A", "kind": "meal",
                                                                  "start": "2026-10-14T12:00", "end": "2026-10-14T13:00",
                                                                  "location": "Here"}]}))
    assert main(["new", "--file", str(bad)]) == 1
    assert "UTC offset" in capsys.readouterr().err
