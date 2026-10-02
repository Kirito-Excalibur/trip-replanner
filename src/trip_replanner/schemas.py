"""Validation for input from outside the graph: uploaded plans and resume payloads.

Graph state stays plain JSON (see models.py). These pydantic models only check
untrusted input and normalize it before it reaches state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from .models import Event, Item, Trip, fmt

MAX_ITEMS = 200

Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")]
Text = Annotated[str, Field(min_length=1, max_length=120)]
Minutes = Annotated[int, Field(ge=0, le=24 * 60)]


def _aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("needs a UTC offset, e.g. 2026-10-14T09:10+01:00")
    return v


When = Annotated[datetime, AfterValidator(_aware)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemIn(Strict):
    id: Id
    title: Text
    kind: Literal["flight", "train", "activity", "meal", "hotel"]
    start: When
    end: When
    location: Text
    fixed: bool = False
    priority: int = Field(3, ge=1, le=5)
    min_minutes: Minutes | None = None
    depends_on: list[Id] = []
    earliest: When | None = None
    latest: When | None = None

    @model_validator(mode="after")
    def _check_times(self) -> ItemIn:
        if self.end <= self.start:
            raise ValueError(f"{self.id}: end must be after start")
        length = int((self.end - self.start).total_seconds() // 60)
        if self.min_minutes is not None and self.min_minutes > length:
            raise ValueError(f"{self.id}: min_minutes is longer than the plan itself")
        return self

    def to_state(self) -> Item:
        length = int((self.end - self.start).total_seconds() // 60)
        item: Item = {
            "id": self.id, "title": self.title, "kind": self.kind,
            "start": fmt(self.start), "end": fmt(self.end), "location": self.location,
            "fixed": self.fixed, "priority": self.priority,
            "min_minutes": length if self.min_minutes is None else self.min_minutes,
            "depends_on": list(dict.fromkeys(self.depends_on)), "status": "planned",
        }
        if self.earliest:
            item["earliest"] = fmt(self.earliest)
        if self.latest:
            item["latest"] = fmt(self.latest)
        return item


class TripIn(Strict):
    name: Text
    preferences: str = Field("", max_length=1000)
    travel_minutes: dict[Annotated[str, Field(pattern=r"^[^|]{1,120}\|[^|]{1,120}$")], Minutes] = {}
    default_travel_minutes: Minutes = 20

    def to_state(self) -> Trip:
        return self.model_dump()  # type: ignore[return-value]


class PlanIn(Strict):
    trip: TripIn
    items: list[ItemIn] = Field(min_length=1, max_length=MAX_ITEMS)

    @model_validator(mode="after")
    def _check_references(self) -> PlanIn:
        ids = [it.id for it in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("item ids must be unique")
        for it in self.items:
            for dep in it.depends_on:
                if dep == it.id or dep not in ids:
                    raise ValueError(f"{it.id}: depends_on has unknown item {dep!r}")
        return self


class EventIn(Strict):
    kind: Literal["delay", "running_late", "closure"]
    item: Id
    minutes: Minutes = 0
    at: When | None = None
    note: str = Field("", max_length=500)

    @model_validator(mode="after")
    def _check_minutes(self) -> EventIn:
        if self.kind != "closure" and self.minutes < 1:
            raise ValueError("minutes must be at least 1")
        return self

    def to_state(self) -> Event:
        return {"kind": self.kind, "item": self.item, "minutes": self.minutes,
                "at": fmt(self.at) if self.at else None, "note": self.note}


class EventReply(Strict):
    type: Literal["event"]
    event: EventIn


class ChooseReply(Strict):
    type: Literal["choose"]
    option_id: Annotated[str, Field(pattern=r"^[A-H]$")]


class SimpleReply(Strict):
    type: Literal["approve", "reject", "dismiss", "end"]


# Discriminated on `type`, so errors name the field that's wrong rather than every variant.
_reply = TypeAdapter(Annotated[Union[EventReply, ChooseReply, SimpleReply], Field(discriminator="type")])


def parse_plan(raw: dict) -> tuple[Trip, dict[str, Item]]:
    """Validate a plan; `items` may be a list or a dict keyed by id. Raises ValueError."""
    items = raw.get("items")
    if isinstance(items, dict):
        if any(not isinstance(v, dict) or v.get("id", k) != k for k, v in items.items()):
            raise ValueError("items dict keys must match item ids")
        items = [{"id": k, **v} for k, v in items.items()]
    try:
        plan = PlanIn.model_validate({"trip": raw.get("trip"), "items": items})
    except ValidationError as e:
        raise ValueError(summarize(e)) from None
    return plan.trip.to_state(), {it.id: it.to_state() for it in plan.items}


def parse_reply(raw: object, allowed: set[str]) -> tuple[dict | None, str | None]:
    """Validate a resume payload. Returns (reply, None) or (None, reason)."""
    try:
        reply = _reply.validate_python(raw)
    except ValidationError as e:
        return None, summarize(e)
    if reply.type not in allowed:
        return None, f"{reply.type!r} isn't possible right now (expected {', '.join(sorted(allowed))})"
    if isinstance(reply, EventReply):
        return {"type": "event", "event": reply.event.to_state()}, None
    return reply.model_dump(), None


def summarize(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:3]:
        where = ".".join(str(p) for p in err["loc"])
        parts.append(f"{where}: {err['msg']}" if where else err["msg"])
    return "; ".join(parts)
