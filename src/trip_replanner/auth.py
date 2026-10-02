"""Bearer-token auth for a small group, and per-traveler isolation of trips.

The server only stores SHA-256 hashes of tokens, in TRIP_USER_TOKENS:
    TRIP_USER_TOKENS="alice:<sha256 hex>,bob:<sha256 hex>"
Generate an entry with `trip token <name>`. Each trip (thread) is stamped with its
owner, and every read, update and run is filtered to that owner.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from langgraph_sdk import Auth
from langgraph_sdk.auth.types import StudioUser

auth = Auth()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def known_users() -> dict[str, str]:
    """sha256 digest -> user name, from TRIP_USER_TOKENS."""
    users = {}
    for entry in os.environ.get("TRIP_USER_TOKENS", "").split(","):
        name, _, digest = entry.strip().partition(":")
        if name and digest:
            users[digest.strip().lower()] = name.strip()
    return users


@auth.authenticate
async def authenticate(authorization: str | None) -> Auth.types.MinimalUserDict:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Auth.exceptions.HTTPException(status_code=401, detail="Missing bearer token")
    digest = hash_token(token.strip())
    for known, name in known_users().items():
        if hmac.compare_digest(digest, known):
            return {"identity": name}
    raise Auth.exceptions.HTTPException(status_code=401, detail="Invalid token")


@auth.on
async def deny_by_default(ctx: Auth.types.AuthContext, value: dict) -> bool:
    # Studio requests are authenticated by LangSmith as the deployment's owner.
    return isinstance(ctx.user, StudioUser)


@auth.on.threads
async def own_trips(ctx: Auth.types.AuthContext, value: dict):
    """Trips are threads: stamp new ones with the owner and filter everything else by it."""
    if isinstance(ctx.user, StudioUser):
        return None
    owner = {"owner": ctx.user.identity}
    metadata = value.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata.update(owner)
    return owner


@auth.on.assistants.read
async def read_assistants(ctx: Auth.types.AuthContext, value: dict) -> bool:
    return True


@auth.on.assistants.search
async def search_assistants(ctx: Auth.types.AuthContext, value: dict) -> bool:
    return True
