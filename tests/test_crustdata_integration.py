"""Provider recovery and research dispatch, using isolated data and fake HTTP only."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import json

import httpx
import pytest

from app import crustdata, main, providers
from app.models import iso, utcnow
from app.store import Store

URL = "https://www.linkedin.com/in/fictional-integration-person"
ORIGINAL_SETTINGS = main.settings


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'integration.sqlite'}")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "settings", lambda: {
        "crustdata_key": "fictional-secret", "exa_key": "", "anthropic_key": "",
        "monitoring_enabled": False, "max_calls_per_run": 8,
    })
    role = json.loads((main.ROOT / "config/roles.json").read_text())["roles"][0]
    store.put("role:" + role["id"], "role", role)
    person = main.save_candidate({"name": "Fictional Integration Person", "profile_url": URL,
                                  "role_id": role["id"], "mode": "live"})
    store.put("watch:" + person["id"], "watch", {
        "candidate_id": person["id"], "role_id": role["id"], "status": "active",
        "mode": "live", "brief_version": role["brief_version"],
        "fit_basis": "Fictional cohort for integration testing", "urls": [URL],
    })
    yield store, person
    store.engine.dispose()


def sub(store, **extra):
    return store.put("sub:integration", "crustdata_subscription", {
        "dataset": "social_post", "remote_id": 5, "profile_url": URL, **extra,
    })


def success(run_id):
    return {"id": run_id, "status": "SUCCESS", "credits_deducted": 2, "new_records_count": 1,
            "notifications": [{"payload": {"results": [{"record": {
                "actor": {"professional_network_url": URL},
                "share_url": f"https://www.linkedin.com/posts/fictional-{run_id}",
                "text": "A fictional professional development, not proof of timing.",
            }}]}}]}


def next_poll(store, client):
    """The next scheduled poll: every watch's back-off has passed."""
    for s in store.all("crustdata_subscription"):
        store.put(s["id"], "crustdata_subscription", {**s, "next_poll_at": iso(utcnow() - timedelta(seconds=1))})
    return asyncio.run(crustdata.sync(store, client))


class FakeHistory:
    def __init__(self, pages, details):
        self.pages, self.details, self.calls = pages, details, []

    async def request(self, method, path, body=None, params=None):
        assert method == "GET"
        self.calls.append((path, params))
        if path == "/account/credits":
            return {"account": {"credits": 1000}}
        if path == "/account/endpoints":
            return {"endpoints": []}
        if path == "/watch/social_post/search/5":
            return {"status": "active", "config": {}}
        if path == "/watch/social_post/5/runs":
            return deepcopy(self.pages[params.get("cursor")])
        if path.endswith("/summary"):
            return deepcopy(self.details[int(path.split("/")[-2])])
        raise AssertionError(path)


def test_terminal_failed_run_does_not_block_later_success_or_repeat(isolated):
    store, _ = isolated
    sub(store)
    client = FakeHistory({None: {"runs": [
        {"id": 11, "status": "SUCCESS"},
        {"id": 10, "status": "FAILED", "failure_reason": "Fictional outage"},
    ]}}, {11: success(11)})
    first = asyncio.run(crustdata.sync(store, client))
    assert first["observations_added"] == 1
    assert first["errors"]
    assert store.get("crustdata-run:social_post:5:10")["coverage_error"]
    second = next_poll(store, client)
    assert second["observations_added"] == 0
    assert len(store.all("crustdata_observation")) == 1
    assert sum(path.endswith("/11/summary") for path, _ in client.calls) == 1


def test_latest_runs_are_read_before_saved_backfill(isolated):
    store, _ = isolated
    sub(store, backfill_cursor="old-page")
    client = FakeHistory({
        None: {"runs": [{"id": 100, "status": "SUCCESS"}], "next_cursor": "old-page"},
        "old-page": {"runs": [{"id": 1, "status": "SUCCESS"}]},
    }, {100: success(100), 1: success(1)})
    result = asyncio.run(crustdata.sync(store, client))
    assert result["observations_added"] == 2
    pages = [params.get("cursor") for path, params in client.calls if path.endswith("/runs")]
    assert pages == [None, "old-page"]
    details = [path for path, _ in client.calls if path.endswith("/summary")]
    assert details[0].endswith("/100/summary")


def pending_observations(store, person, count):
    start = utcnow() - timedelta(hours=1)
    # Reverse insertion order to ensure dispatch uses timestamps, not store order.
    for index in reversed(range(count)):
        store.put(f"observation:test-{index:02}", "crustdata_observation", {
            "candidate_ids": [person["id"]], "kind": "social_post", "baseline": False,
            "status": "pending_research", "detected_at": iso(start + timedelta(seconds=index)),
            "source_url": f"https://example.test/post/{index}", "summary": "x" * 5000,
            "record": {"unused_large_payload": "y" * 10000},
        })


def fake_sync(monkeypatch):
    async def sync(*args):
        return {"observations_added": 0, "errors": []}
    monkeypatch.setattr(crustdata, "Client", lambda key: object())
    monkeypatch.setattr(crustdata, "sync", sync)


def sync_job():
    return {"kind": "crustdata_sync", "mode": "live"}


def test_more_than_twenty_observations_remain_pending_and_are_processed_next(isolated, monkeypatch):
    store, person = isolated
    fake_sync(monkeypatch)
    pending_observations(store, person, 25)
    contexts = []

    async def research(candidate, role, settings):
        contexts.append(deepcopy(candidate))
        return {"evidence": [], "events": [], "fit": [], "summary": "No supported timing found."}

    monkeypatch.setattr(providers, "research", research)
    asyncio.run(main.execute_job(sync_job()))
    first = store.all("job")[0]
    assert len([o for o in store.all("crustdata_observation") if o["status"] == "research_queued"]) == 20
    assert len([o for o in store.all("crustdata_observation") if o["status"] == "pending_research"]) == 5
    asyncio.run(main.execute_job(first))
    store.put(first["id"], "job", {**first, "status": "complete"})
    asyncio.run(main.execute_job(sync_job()))
    second = next(j for j in store.all("job") if j["id"] != first["id"])
    asyncio.run(main.execute_job(second))
    assert [len(c["provider_observations"]) for c in contexts] == [20, 5]
    assert contexts[0]["provider_observations"][0]["id"] == "observation:test-00"
    assert all("record" not in o and len(o["summary"]) <= 2400
               for c in contexts for o in c["provider_observations"])
    assert all(o["status"] == "researched" for o in store.all("crustdata_observation"))
    assert not store.all("review_alert")


def test_repeat_sync_does_not_duplicate_queued_research_or_edit_candidate(isolated, monkeypatch):
    store, person = isolated
    fake_sync(monkeypatch)
    pending_observations(store, person, 25)
    asyncio.run(main.execute_job(sync_job()))
    revision = store.get(person["id"])["revision"]
    asyncio.run(main.execute_job(sync_job()))
    assert len(store.all("job")) == 1
    assert store.get(person["id"])["revision"] == revision
    assert len([o for o in store.all("crustdata_observation") if o["status"] == "pending_research"]) == 5


def test_local_env_credential_is_not_exposed_in_settings_or_provider_errors(isolated, monkeypatch, tmp_path):
    sentinel = "fictional-env-key-do-not-expose"
    (tmp_path / ".env").write_text("CRUSTDATA_API_KEY=" + sentinel + "\n")
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.delenv("CRUSTDATA_API_KEY", raising=False)
    monkeypatch.setattr(main, "settings", ORIGINAL_SETTINGS)
    assert main.settings()["crustdata_key"] == sentinel
    settings = main.safe_settings()
    assert settings["crustdata_configured"] is True
    assert sentinel not in json.dumps(settings)
    original_client = httpx.AsyncClient

    def reject(request):
        assert request.headers["Authorization"] == "Bearer " + sentinel
        return httpx.Response(401, json={"error": sentinel})

    monkeypatch.setattr(crustdata.httpx, "AsyncClient", lambda **kwargs:
                        original_client(transport=httpx.MockTransport(reject), **kwargs))
    with pytest.raises(crustdata.CrustdataError) as caught:
        asyncio.run(crustdata.Client(sentinel).request("GET", "/account/credits"))
    assert sentinel not in str(caught.value)
    assert "credential rejected" in str(caught.value)


def test_new_pages_between_latest_and_saved_backlog_are_not_skipped(isolated):
    store, _ = isolated
    sub(store, backfill_cursor="old-page")
    client = FakeHistory({
        None: {"runs": [{"id": 100, "status": "SUCCESS"}], "next_cursor": "intermediate"},
        "intermediate": {"runs": [{"id": 99, "status": "SUCCESS"}], "next_cursor": "old-page"},
        "old-page": {"runs": [{"id": 1, "status": "SUCCESS"}]},
    }, {100: success(100), 99: success(99), 1: success(1)})
    asyncio.run(crustdata.sync(store, client))
    next_poll(store, client)
    assert store.get("crustdata-run:social_post:5:99") is not None
    assert len(store.all("crustdata_observation")) == 3


def test_saved_integer_frontier_and_latest_gap_both_make_progress(isolated):
    store, _ = isolated
    sub(store, backfill_cursor=1)
    client = FakeHistory({
        None: {"runs": [{"id": 100, "status": "SUCCESS"}], "next_cursor": 90},
        90: {"runs": [{"id": 90, "status": "SUCCESS"}], "next_cursor": 80},
        80: {"runs": [{"id": 80, "status": "SUCCESS"}]},
        1: {"runs": [{"id": 1, "status": "SUCCESS"}]},
    }, {n: success(n) for n in [100, 90, 80, 1]})
    asyncio.run(crustdata.sync(store, client))
    pages = [params.get("cursor") for path, params in client.calls if path.endswith("/runs")]
    assert pages == [None, 90, 1]
    assert store.get("sub:integration")["backfill_cursors"] == [80]
    next_poll(store, client)
    assert len(store.all("crustdata_observation")) == 4
    assert store.get("sub:integration")["backfill_cursors"] == []


def test_shared_observation_does_not_repeat_completed_candidate_while_other_snoozed(isolated, monkeypatch):
    store, person = isolated
    fake_sync(monkeypatch)
    other = store.put("candidate:second-role", "candidate", {
        **person, "snooze_until": iso(utcnow() + timedelta(days=1)),
    })
    pending_observations(store, person, 1)
    observation = store.all("crustdata_observation")[0]
    store.put(observation["id"], "crustdata_observation", {
        **observation, "candidate_ids": [person["id"], other["id"]],
    })

    async def research(candidate, role, settings):
        return {"evidence": [], "events": [], "fit": [], "summary": "No supported timing found."}

    monkeypatch.setattr(providers, "research", research)
    asyncio.run(main.execute_job(sync_job()))
    first = store.all("job")[0]
    asyncio.run(main.execute_job(first))
    store.put(first["id"], "job", {**first, "status": "complete"})
    asyncio.run(main.execute_job(sync_job()))
    assert len(store.all("job")) == 1
    assert store.get(observation["id"])["status"] == "pending_research"


def test_sync_waits_out_each_watch_back_off(isolated):
    store, _ = isolated
    sub(store)
    client = FakeHistory({None: {"runs": [{"id": 11, "status": "SUCCESS"}]}}, {11: success(11)})
    asyncio.run(crustdata.sync(store, client))
    polled = len(client.calls)
    assert crustdata.parse_time(store.get("sub:integration")["next_poll_at"]) > utcnow()
    assert asyncio.run(crustdata.sync(store, client)) == {"observations_added": 0, "errors": []}
    assert len(client.calls) == polled  # not even the credit check before the back-off ends
    next_poll(store, client)
    assert len(client.calls) > polled
