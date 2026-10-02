from trip_replanner.cli import main


def test_full_session_with_time_travel(tmp_path, capsys):
    db = str(tmp_path / "trips.sqlite")
    run = lambda *args: main(["--db", db, *args])  # noqa: E731

    assert run("new") == 0
    assert run("delay", "flight", "30") == 1  # must approve first
    assert run("approve") == 0
    assert run("delay", "flight", "120", "--at", "07:40") == 0
    assert run("choose", "A") == 0
    capsys.readouterr()

    assert run("rechoose", "B") == 0
    out = capsys.readouterr().out
    assert "Applied B: Skip Coffee in Baixa." in out

    assert run("history") == 0
    history = capsys.readouterr().out
    # Only the current branch: B's lunch change is there, A's (13:20–14:30) is not.
    assert "12:50–14:05" in history and "13:20–14:30" not in history

    assert run("end") == 0
    assert "Trip finished." in capsys.readouterr().out
