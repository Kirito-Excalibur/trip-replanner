"""Data shapes kept in graph state.

Everything is JSON-friendly (times are ISO-8601 strings with a UTC offset), so
the checkpointer can hold a trip for days without custom serializers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, NotRequired, TypedDict


class Item(TypedDict):
    id: str
    title: str
    kind: Literal["flight", "train", "activity", "meal", "hotel"]
    start: str
    end: str
    location: str
    fixed: bool  # booked / timed ticket: the replanner never moves it
    priority: int  # 1 = nice to have … 5 = must do
    min_minutes: int  # how short a flexible item may be squeezed
    depends_on: list[str]  # items that must finish (plus travel) before this starts
    status: Literal["planned", "cancelled"]
    earliest: NotRequired[str]  # opening hours / booking window
    latest: NotRequired[str]


class Trip(TypedDict):
    name: str
    preferences: str  # free text, read by the ranker
    travel_minutes: dict[str, int]  # "A|B" -> minutes, either direction
    default_travel_minutes: int


class Event(TypedDict):
    kind: Literal["delay", "running_late", "closure"]
    item: str
    minutes: int
    at: str | None  # when it was reported; plans before this are frozen
    note: str  # optional free text from the traveler ("kids are tired")


class Change(TypedDict):
    item: str
    kind: Literal["moved", "shortened", "dropped"]
    text: str


class Option(TypedDict):
    id: str
    strategy: str
    title: str
    items: dict[str, Item]
    changes: list[Change]
    cost: dict[str, int]


class Recommendation(TypedDict):
    ranking: list[str]
    recommended: str | None
    message: str
    source: str


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def fmt(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def hm(ts: str) -> str:
    return parse(ts).strftime("%H:%M")


def shift_ts(ts: str, minutes: int) -> str:
    return fmt(parse(ts) + timedelta(minutes=minutes))


def minutes_between(a: str, b: str) -> int:
    return int((parse(b) - parse(a)).total_seconds() // 60)


def duration(item: Item) -> int:
    return minutes_between(item["start"], item["end"])
