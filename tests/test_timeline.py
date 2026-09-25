"""Timeline, as-of detector contract and tier-0 notes, on an isolated store."""

import asyncio
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import detectors, main, timeline
from app.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'timeline.sqlite'}")


@pytest.fixture()
def probe():
    seen = []

    @timeline.detector("probe", "test")
    def detect(events, subject_id, as_of):
        seen.append({e["id"] for e in events})
        return [{"strength": 0.5, "evidence_event_ids": [e["id"] for e in events]}] if events else []

    yield seen
    timeline.DETECTORS.pop("probe")


def sourced(store, observed_at, event_date, quote):
    version = timeline.source_version(store, "https://sec.example.test/filing", quote, observed_at)
    return timeline.add_event(store, {
        "subject_type": "org", "subject_id": "org:acme", "event_type": "acquisition_closed",
        "event_date": event_date, "date_precision": "day", "observed_at": observed_at,
        "source_url": version["url"], "source_version_hash": version["content_hash"],
        "quote": quote, "tier": 1, "extractor": "test",
    })


def test_detectors_never_see_events_observed_after_as_of(store, probe):
    past = sourced(store, "2026-03-01T00:00:00Z", "2026-02-27", "Merger completed on February 27, 2026.")
    # Announced before as_of, happening after it: legitimately knowable.
    scheduled = sourced(store, "2026-03-02T00:00:00Z", "2026-06-30", "Expected to close June 30, 2026.")
    sourced(store, "2026-07-01T00:00:00Z", "2026-06-30", "Closed June 30, 2026.")

    as_of = "2026-04-01T00:00:00Z"
    activations = detectors.detect(timeline.timeline(store, "org:acme", as_of), "org:acme", as_of)

    assert probe == [{past["id"], scheduled["id"]}]
    assert [a["family"] for a in activations if a["detector_id"] == "probe"] == ["test"]


def test_detector_citing_unseen_event_is_rejected(store):
    sourced(store, "2026-03-01T00:00:00Z", "2026-02-27", "Merger completed on February 27, 2026.")

    @timeline.detector("cheat", "test")
    def cheat(events, subject_id, as_of):
        return [{"strength": 1, "evidence_event_ids": ["timeline-event:from-the-future"]}]

    try:
        with pytest.raises(timeline.LeakageError):
            as_of = "2026-04-01T00:00:00Z"
            detectors.detect(timeline.timeline(store, "org:acme", as_of), "org:acme", as_of)
    finally:
        timeline.DETECTORS.pop("cheat")


def test_event_rules(store):
    base = {"subject_type": "person", "subject_id": "person:x", "event_type": "post",
            "observed_at": "2026-05-30T00:00:00Z", "quote": "Open to roles from October 2026", "extractor": "test"}
    with pytest.raises(ValueError):  # month precision cannot pose as a day
        timeline.add_event(store, {**base, "tier": 3, "event_date": "2026-10", "date_precision": "day"})
    with pytest.raises(ValueError):  # a sourced tier needs the source version
        timeline.add_event(store, {**base, "tier": 1, "event_date": "2026-10", "date_precision": "month"})
    first = timeline.add_event(store, {**base, "tier": 3, "event_date": "2026-10", "date_precision": "month"})
    again = timeline.add_event(store, {**base, "tier": 3, "event_date": "2026-10", "date_precision": "month"})
    assert first["id"] == again["id"] and first["revision"] == again["revision"] == 1


def test_extraction_runs_once_per_source_version(store):
    calls = []

    async def extract():
        calls.append(1)
        return [{"event_type": "departure"}]

    for _ in range(2):
        assert asyncio.run(timeline.extract_once(store, "abc", "v1", extract)) == [{"event_type": "departure"}]
    asyncio.run(timeline.extract_once(store, "abc", "v2", extract))
    assert len(calls) == 2


def test_private_notes_api_appends_and_edits(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "store", Store(f"sqlite:///{tmp_path / 'api.sqlite'}"))
    with TestClient(main.app) as client:
        person = next(c["person_id"] for c in client.get("/api/state?mode=simulation").json()["candidates"])
        path = f"/api/people/{person}/notes"
        assert client.post(path, json={"text": "x", "author": ""}).status_code == 422
        first = client.post(path, json={"text": "Referred by a former colleague.", "author": "Justin"}).json()
        assert first["tier"] == 0 and first["event_type"] == "private_note"
        edited = client.post(path, json={"text": "Referred by a former manager.", "author": "Justin",
                                         "supersedes": first["id"]}).json()
        assert client.post(path, json={"text": "again", "author": "Justin", "supersedes": first["id"]}).status_code == 422
        notes = next(c for c in client.get("/api/state?mode=simulation").json()["candidates"]
                     if c["person_id"] == person)["private_notes"]
        assert [n["id"] for n in notes] == [edited["id"]]
        typed = client.post(path, json={"text": "Not before her March vest.", "author": "Justin",
                                        "note_type": "wait", "wait_until": "2099-03-01"}).json()
        assert (typed["event_type"], typed["event_date"]) == ("note_wait", "2099-03-01")
        opened = client.post(path, json={"text": "Open to a move.", "author": "Justin", "note_type": "open"}).json()
        assert (opened["event_type"], opened["event_date"]) == ("note_open", opened["observed_at"][:10])
        today = opened["observed_at"][:10]
        for bad in ({"note_type": "wait"}, {"note_type": "wait", "wait_until": "2020-01-01"},
                    {"note_type": "wait", "wait_until": "2099-02-30"}, {"note_type": "maybe"},
                    {"note_type": "open", "wait_until": "2099-03-01"}, {"wait_until": "2099-03-01"}):
            assert client.post(path, json={"text": "x", "author": "Justin", **bad}).status_code == 422, bad
        # Today is a day to wait for (the UTC day can run ahead of GI's). An edit to another day is a new read.
        due = client.post(path, json={"text": "Not before today.", "author": "Justin", "note_type": "wait",
                                      "wait_until": today}).json()
        assert due["event_type"] == "note_wait"
        assert client.post(path, json={"text": "Not before 2020.", "author": "Justin", "note_type": "wait",
                                        "wait_until": "2020-01-01", "supersedes": due["id"]}).status_code == 422
        assert client.post("/api/people/person:missing/notes", json={"text": "a", "author": "b"}).status_code == 404


def test_windows_come_only_from_detector_activations():
    import json
    from app import readiness
    from app.engine import make_ping
    from app.fixtures import scenarios
    from app.models import Candidate, Event, utcnow

    legacy = Event.model_validate({"id": "e", "title": "t", "window_start": "2026-09-20T00:00:00Z"})
    assert "window_start" not in legacy.model_dump()  # a model or legacy record cannot set a window
    roles = {r["id"]: {**r, "brief_version": 1} for r in json.loads(Path("config/roles.json").read_text())["roles"]}
    c = Candidate.model_validate(scenarios(roles)[0]).model_dump()
    scored = readiness.score([{
        "detector_id": "acquisition_closed", "family": "employer", "strength": 0.6, "holds": [],
        "window_open": "2026-10-01T00:00:00+00:00", "window_close": "2026-12-31T00:00:00+00:00",
        "evidence_event_ids": ["x"]}], "2026-10-15T00:00:00+00:00")
    ping = make_ping(c, roles[c["role_id"]], c["events"][0], utcnow(), readiness=scored)  # validates against the schema
    assert ping["trigger"]["window"]["detector_id"] == "acquisition_closed"
    assert ping["trigger"]["window"]["opens"].startswith("2026-10-01")
    assert ping["readiness"]["action"] == "quiet"  # one reason alone is not now
    assert ping["confidence"]["what_would_prove_wrong"][0] == "Integration retention grants for the team"
