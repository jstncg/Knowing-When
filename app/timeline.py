"""Dated event timeline, detector registry and moment hypotheses.

Records are append-only in the shared store. Detectors see only events observed by `as_of`, so live watching and
historical replay run the same code. For a historical source, `observed_at` is when it became public (filing or post
time), never when we fetched it.
"""

import hashlib
import re
from datetime import date
from typing import Callable, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .engine import digest
from .models import iso, parse_time, public_url, utcnow

# 0 private note, 1 primary dated source, 2 reputable secondary, 3 inferred.
Tier = Literal[0, 1, 2, 3]
PRECISION = {"year": r"\d{4}", "month": r"\d{4}-\d{2}", "day": r"\d{4}-\d{2}-\d{2}"}


class LeakageError(ValueError):
    pass


class TimelineEvent(BaseModel):
    subject_type: Literal["person", "org"]
    subject_id: str = Field(min_length=1, max_length=300)
    event_type: str = Field(min_length=2, max_length=80)
    # At the precision the source states; a month is never silently made a day.
    event_date: str | None = None
    date_precision: Literal["year", "month", "day"] | None = None
    observed_at: str
    source_url: str = ""
    source_version_hash: str = ""
    quote: str = Field(min_length=1, max_length=4000)
    tier: Tier
    extractor: str = Field(min_length=1, max_length=120)
    author: str = Field(default="", max_length=120)
    supersedes: str | None = None

    @field_validator("observed_at")
    @classmethod
    def _observed(cls, value):
        return iso(parse_time(value))

    @field_validator("source_url")
    @classmethod
    def _url(cls, value):
        return public_url(value) if value else value

    @model_validator(mode="after")
    def _consistent(self):
        if (self.event_date is None) != (self.date_precision is None):
            raise ValueError("event_date and date_precision go together")
        if self.event_date and not re.fullmatch(PRECISION[self.date_precision], self.event_date):
            raise ValueError("event_date does not match date_precision")
        if self.tier in (1, 2) and not (self.source_url and self.source_version_hash):
            raise ValueError("Sourced events need a source URL and version hash")
        return self


class Activation(BaseModel):
    detector_id: str
    family: str
    strength: float = Field(ge=0, le=1)
    window_open: str | None = None
    window_close: str | None = None
    evidence_event_ids: list[str] = Field(min_length=1)
    holds: list[str] = Field(default_factory=list)
    # A specific fact a GI human checks before acting on this activation (a role claim, a late filing).
    falsifier: str = ""
    # Set by detectors.detect when it rests only on GitHub items that are not releases (a push, a pull request, an
    # issue, a comment) or on what the post reader read from them. readiness counts such an early sign or drift only
    # beside a fresh post, release or paper, or something enough alone.
    code_only: bool = False


class MomentHypothesis(BaseModel):
    subject_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    mechanism: str = Field(min_length=10, max_length=2000)
    trigger_condition: str = Field(min_length=10, max_length=2000)
    expected_window: str = Field(min_length=3, max_length=500)
    required_tier: Tier = 1
    holds: list[str] = Field(default_factory=list)
    falsifiers: list[str] = Field(min_length=1)
    next_check_at: str
    detector_ids: list[str] = Field(default_factory=list)

    @field_validator("next_check_at")
    @classmethod
    def _next(cls, value):
        return iso(parse_time(value))


def source_version(store, url, text, observed_at):
    """One immutable record per exact content; the first observation time wins."""
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    return store.put("source-version:" + content_hash, "source_version", {
        "url": public_url(url), "content_hash": content_hash, "text": text,
        "observed_at": iso(parse_time(observed_at)),
    }, only_new=True)


async def extract_once(store, content_hash, extractor, extract):
    """Run a (paid) extractor at most once per source version and extractor version."""
    key = extraction_key(content_hash, extractor)
    if cached := store.get(key):
        return cached["events"]
    events = await extract()
    store.put(key, "extraction", {"content_hash": content_hash, "extractor": extractor,
                                  "events": events, "created_at": iso()}, only_new=True)
    return events


def extraction_key(content_hash, extractor):
    return "extraction:" + digest([content_hash, extractor])


def event_key(record):
    """A timeline event's store id: the same event added twice is stored once."""
    return "timeline-event:" + digest([record[k] for k in (
        "subject_type", "subject_id", "event_type", "event_date", "observed_at",
        "source_version_hash", "quote", "extractor", "author", "supersedes")])


def add_event(store, event):
    record = TimelineEvent.model_validate(event).model_dump()
    if parse_time(record["observed_at"]) > utcnow():
        raise ValueError("An event cannot be observed in the future")
    return store.put(event_key(record), "timeline_event", record, only_new=True)


def timeline(store, subject_id, as_of):
    """Events known by as_of, oldest first. Superseded notes drop out once replaced."""
    cutoff = parse_time(as_of)
    known = [e for e in store.all("timeline_event")
             if e["subject_id"] == subject_id and parse_time(e["observed_at"]) <= cutoff]
    replaced = {e["supersedes"] for e in known if e.get("supersedes")}
    return sorted((e for e in known if e["id"] not in replaced), key=lambda e: e["observed_at"])


DETECTORS: dict[str, tuple[str, Callable]] = {}


def detector(detector_id, family):
    def register(fn):
        DETECTORS[detector_id] = (family, fn)
        return fn
    return register


def save_hypothesis(store, hypothesis):
    record = MomentHypothesis.model_validate(hypothesis).model_dump()
    key = "moment-hypothesis:" + digest([record["subject_id"], record["role_id"], record["mechanism"]])
    old = store.get(key)
    return store.put(key, "moment_hypothesis", record, expected=old["revision"] if old else None)


# A note's author types it: open to a move, or wait until a later day (its event_date, as a contact_constraint's is
# the day the person named). An untyped note is context.
NOTE_TYPES = {None: "private_note", "open": "note_open", "wait": "note_wait"}


def add_private_note(store, person_id, text, author, supersedes=None, note_type=None, wait_until=None, now=None):
    """A new read as of ``now`` (a workspace's clock, such as the simulation's), or an edit of ``supersedes``.

    An edit that keeps the type and day only corrects the wording: it keeps the read's date and place, so it cannot
    restart a window or displace a newer note.
    """
    now = now or iso()
    day = date.fromisoformat(wait_until).isoformat() if wait_until else None
    if note_type not in NOTE_TYPES or (note_type == "wait") != bool(day):
        raise ValueError("A note is open to a move, waits until a day, or is context")
    old = store.get(supersedes) if supersedes else None
    fix = old if old and old["event_type"] == NOTE_TYPES[note_type] and (not day or day == old["event_date"]) else None
    if day and day < now[:10] and not fix:
        raise ValueError("A new wait note waits until today or later")
    return add_event(store, {
        "subject_type": "person", "subject_id": person_id, "event_type": NOTE_TYPES[note_type],
        "event_date": fix["event_date"] if fix else day or now[:10], "date_precision": "day",
        "observed_at": fix["observed_at"] if fix else now,
        "quote": text, "tier": 0, "extractor": "manual_note", "author": author,
        "supersedes": supersedes,
    })


def private_notes(store, person_id):
    return [e for e in reversed(timeline(store, person_id, iso())) if e["event_type"] in NOTE_TYPES.values()]
