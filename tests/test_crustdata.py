import asyncio
from copy import deepcopy

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import crustdata
from app.store import Store

URL = "https://www.linkedin.com/in/example"


@pytest.fixture
def store(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'crust.sqlite'}")
    s.put("candidate:one", "candidate", {"name": "Fictional Example", "profile_url": URL,
        "identity_status": "unknown", "evidence": [{"url": "https://www.linkedin.com/in/someone-else"}]})
    s.put("watch:one", "watch", {"candidate_id": "candidate:one", "status": "active", "mode": "live"})
    yield s
    s.engine.dispose()


def subscription(dataset="social_post"):
    return {"id": "sub:one", "dataset": dataset, "profile_url": URL, "remote_id": 5}


def run(credit=2):
    return {"id": 10, "status": "SUCCESS", "new_records_count": 1, "credits_deducted": credit,
        "notifications": [{"payload": {"notifications": [{"record": {
            "actor": {"professional_network_url": URL}, "share_url": "https://www.linkedin.com/posts/example_1",
            "date_posted": "2026-09-20T09:00:00Z", "text": "Fictional professional announcement."}}]}}]}


def test_identity_uses_primary_not_colleague_citations(store):
    assert crustdata.subjects(store) == ({URL: ["candidate:one"]}, [])


def test_name_or_conflicting_links_do_not_resolve_person(store):
    c = store.get("candidate:one")
    c.update(profile_url="https://example.org/person", evidence=[{"url": URL}, {"url": "https://www.linkedin.com/in/other"}])
    store.put(c["id"], "candidate", c)
    assert not crustdata.subjects(store)[0]


def test_remote_free_baseline_never_becomes_research_or_alert(store):
    item = crustdata.ingest_run(store, subscription(), run(0))[0]
    assert item["baseline"] and item["status"] == "baseline"
    assert not store.all("review_alert")


def test_live_observation_is_only_research_lead_and_replay_is_idempotent(store):
    data = run()
    data["notifications"].append(deepcopy(data["notifications"][0]))
    items = crustdata.ingest_run(store, subscription(), data)
    assert len(items) == 1
    assert items[0]["status"] == "pending_research"
    assert items[0]["event_date_basis"] == "post_publication_only"
    assert not crustdata.ingest_run(store, subscription(), {**data, "id": 11})
    assert not store.all("review_alert")


def test_wrong_post_author_is_quarantined(store):
    data = run()
    data["notifications"][0]["payload"]["notifications"][0]["record"]["actor"]["professional_network_url"] = URL + "-namesake"
    item = crustdata.ingest_run(store, subscription(), data)[0]
    assert item["status"] == "needs_identity" and not item["candidate_ids"]


def test_textless_post_baseline_is_retained_without_crashing(store):
    data = run(0)
    data["notifications"][0]["payload"]["notifications"][0]["record"]["text"] = None
    item = crustdata.ingest_run(store, subscription(), data)[0]
    assert item["baseline"] and item["status"] == "baseline"
    assert item["summary"].startswith("Post has no text")


def test_person_diff_has_no_fabricated_event_date(store):
    data = run(150)
    data["notifications"][0]["payload"]["notifications"] = [{"changes": [{"field": "basic_profile.current_title", "from": "A", "to": "B"}], "record": {"crustdata_person_id": 42}}]
    item = crustdata.ingest_run(store, subscription("person"), data)[0]
    assert item["event_date"] is None
    assert item["detected_at"]
    assert item["status"] == "pending_research"


def test_stopped_local_watch_does_not_route_changes(store):
    w = store.get("watch:one")
    store.put(w["id"], "watch", {**w, "status": "paused"})
    assert not crustdata.ingest_run(store, subscription(), run())[0]["candidate_ids"]


def test_unreadable_or_failed_runs_are_not_silent_success():
    with pytest.raises(crustdata.CrustdataError):
        crustdata.records_from_run({"status": "SUCCESS", "new_records_count": 1})
    with pytest.raises(crustdata.CrustdataError):
        crustdata.records_from_run({"status": "SUCCESS", "payload_delivery": {"type": "link"}})


def test_reconnect_reuses_watches_after_uncertain_creation(store):
    class FakeClient:
        def __init__(self):
            self.remote = {"person": [], "social_post": []}
            self.creates = 0
        async def request(self, method, path, body=None, params=None):
            if path == "/account/credits": return {"account": {"credits": 200}}
            if path == "/account/endpoints": return {"endpoints": []}
            dataset = path.split("/")[2]
            if method == "GET": return self.remote[dataset]
            assert method == "POST"
            self.creates += 1
            record = {**body, "id": self.creates, "status": "active"}
            self.remote[dataset].append(record)
            if self.creates == 1:
                raise crustdata.CrustdataError("Connection failed after provider accepted creation")
            return record
    client = FakeClient()
    first = asyncio.run(crustdata.connect(store, client))
    assert first["errors"]
    second = asyncio.run(crustdata.connect(store, client))
    assert second["reused"] == 2
    assert client.creates == 2
    assert len(store.all("crustdata_subscription")) == 2


def test_cross_role_observation_shared(store):
    c = store.get("candidate:one")
    store.put("candidate:two", "candidate", c)
    store.put("watch:two", "watch", {"candidate_id": "candidate:two", "status": "active", "mode": "live"})
    item = crustdata.ingest_run(store, subscription(), run())[0]
    assert set(item["candidate_ids"]) == {"candidate:one", "candidate:two"}
    assert len(store.all("crustdata_observation")) == 1


def test_paused_local_subject_pauses_remote_collection(store):
    store.put("sub:one", "crustdata_subscription", subscription())
    w = store.get("watch:one")
    store.put(w["id"], "watch", {**w, "status": "paused"})
    calls = []
    class FakeClient:
        async def request(self, method, path, body=None, params=None):
            calls.append((method, path, body))
            if path == "/account/credits": return {"account": {"credits": 200}}
            if path == "/account/endpoints": return {"endpoints": []}
            if method == "PATCH": return {"status": "paused"}
            return {"status": "active"}
    asyncio.run(crustdata.sync(store, FakeClient()))
    assert ("PATCH", "/watch/social_post/search/5", {"status": "paused"}) in calls
    assert not any("/runs" in p for _, p, _ in calls)
    assert store.get("sub:one")["status"] == "paused"


def test_observations_become_timeline_events_with_honest_precision(store):
    c = store.get("candidate:one")
    store.put(c["id"], "candidate", {**c, "person_id": "person:live:one"})
    data = run()
    older = deepcopy(data["notifications"][0])
    record = older["payload"]["notifications"][0]["record"]
    record.update(share_url="https://www.linkedin.com/posts/example_2", date_posted="2023-09-20T09:00:00Z", text="Older post.")
    data["notifications"].append(older)
    crustdata.ingest_run(store, subscription(), data)
    from app.timeline import timeline
    events = {e["quote"]: e for e in timeline(store, "person:live:one", "2100-01-01T00:00:00Z")}
    # Same clock time within one run means relative labels: keep month/year only.
    assert (events["Fictional professional announcement."]["event_date"], events["Fictional professional announcement."]["date_precision"]) == ("2026-09", "month")
    assert (events["Older post."]["event_date"], events["Older post."]["date_precision"]) == ("2023", "year")
    assert all(e["tier"] == 1 and e["extractor"] == "crustdata_v1" for e in events.values())
    assert crustdata.backfill_timeline(store) == 2
    assert len(timeline(store, "person:live:one", "2100-01-01T00:00:00Z")) == 2
