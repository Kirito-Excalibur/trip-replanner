import asyncio
from types import SimpleNamespace

import pytest
from langgraph_sdk import Auth
from langgraph_sdk.auth.types import StudioUser

from trip_replanner import auth


@pytest.fixture(autouse=True)
def users(monkeypatch):
    monkeypatch.setenv("TRIP_USER_TOKENS", f"alice:{auth.hash_token('a-secret')}, bob:{auth.hash_token('b-secret')}")


def ctx(user):
    return SimpleNamespace(user=user)


def test_valid_token_identifies_the_user():
    assert asyncio.run(auth.authenticate("Bearer b-secret")) == {"identity": "bob"}


@pytest.mark.parametrize("header", [None, "", "Bearer", "Basic a-secret", "Bearer wrong"])
def test_bad_tokens_are_rejected(header):
    with pytest.raises(Auth.exceptions.HTTPException) as e:
        asyncio.run(auth.authenticate(header))
    assert e.value.status_code == 401


def test_trips_are_stamped_and_filtered_by_owner():
    value = {"metadata": {"trip_name": "demo"}}
    assert asyncio.run(auth.own_trips(ctx(SimpleNamespace(identity="alice")), value)) == {"owner": "alice"}
    assert value["metadata"] == {"trip_name": "demo", "owner": "alice"}


def test_everything_else_is_denied_except_for_studio():
    assert asyncio.run(auth.deny_by_default(ctx(SimpleNamespace(identity="alice")), {})) is False
    assert asyncio.run(auth.deny_by_default(ctx(StudioUser("owner")), {})) is True
