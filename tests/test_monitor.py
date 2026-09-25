"""Monitoring tests use fake public documents and a per-test local store."""

from timing_helpers import INVITATION, assessment, purpose

import asyncio
from datetime import timedelta
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, providers
from app.models import iso, utcnow
from app.store import Store


URL = "https://sources.example.test/shared-fictitious-profile"


@pytest.fixture()
def monitored_store(monkeypatch, tmp_path):
    isolated = Store(f"sqlite:///{tmp_path / 'monitor.sqlite'}")
    monkeypatch.setattr(main, "store", isolated)
    main.initialize()
    return isolated


def watched_candidate(role_id="controller", profile_url=URL):
    role = main.role_for(role_id)
    now = utcnow()
    day = (now - timedelta(days=1)).date().isoformat()
    record = {
        "name": f"Fictional {role_id} Monitor",
        "profile_url": profile_url,
        "role_id": role_id,
        "mode": "live",
        "summary": "A fictional record used solely for monitoring tests.",
        "identity_status": "verified",
        "evidence": [
            {
                "id": "shared-source",
                "url": profile_url,
                "title": "Fictional monitored page",
                "excerpt": "Fictional professional evidence for watch-path tests. " + INVITATION,
                "observed_at": iso(now),
                "event_date": day,
                "verified": True,
                "source_kind": "synthetic_fixture",
            }
        ],
        "fit": [
            {
                "criterion": criterion,
                "status": "supported",
                "evidence_ids": ["shared-source"],
            }
            for criterion in role["criteria"]
        ],
        "events": [
            {
                "id": "fictional-monitor-event",
                "kind": "professional_update",
                "title": "Fictional monitored professional update",
                "date": day,
                "timing_action": "contact_now",
                "timing_assessment": assessment("shared-source"),
                **purpose("shared-source"),
                "recipient_value": "Compare this documented professional approach with the relevant role scope at GI.",
                "why_now": "This fictional update creates a dated professional topic for a monitored test record this week.",
                "why_wait": "A later source correction would warrant waiting before treating this fictional event as useful.",
                "falsifier": "A source correction attributing the fictional update to someone else would disprove the test premise.",
                "evidence_ids": ["shared-source"],
                "conversation_score": 3,
                "career_openness": "possible",
                "confidence": "high",
            }
        ],
        "route": {
            "status": "none_found",
            "description": "none found",
            "introducer": "",
            "evidence_ids": [],
        },
        "draft": {
            "subject": "Fictional monitor test discussion",
            "body": "This fictional test-only outreach body is long enough to exercise reviewed delivery logic without describing a real person or business opportunity.",
            "sender_function": "CTO / research leader"
            if role_id == "mts-research"
            else "CEO / operations leader",
            "sender_name": "",
            "channel": "email",
            "source_ids": ["shared-source"],
        },
    }
    record["reviewed_at"] = iso(now)
    record["review_hash"] = main.review_hash(record, role)
    return main.save_candidate(record, trusted=True)


def baseline(record, content="unchanged", researched_at=None, brief_version=None):
    role = main.role_for(record["role_id"])
    main.store.put(
        "research-baseline:" + record["id"],
        "baseline",
        {
            "candidate_id": record["id"],
            "researched_at": researched_at or iso(),
            "sources": {URL: main.digest(content)},
            "brief_version": brief_version or role["brief_version"],
        },
    )


def watch(record):
    return asyncio.run(
        main.execute_job(
            {"kind": "watch", "mode": "live", "candidate_id": record["id"]}
        )
    )


def test_unchanged_watch_preserves_review_and_records_a_fresh_check(
    monitored_store, monkeypatch
):
    record = watched_candidate()
    baseline(record)
    calls = []

    async def fetch(url):
        calls.append(url)
        return {"url": url, "title": "Fictional page", "text": "unchanged"}

    async def research(*_):
        pytest.fail("unchanged source must not invoke research")

    monkeypatch.setattr(providers, "fetch_document", fetch)
    monkeypatch.setattr(providers, "research", research)

    result = watch(record)
    after = main.store.get(record["id"])

    assert result["provider_model_calls"] == 0
    assert calls == [URL]
    assert after["identity_status"] == "verified"
    assert after["review_hash"] == record["review_hash"]
    assert after["evidence"][0]["last_checked_at"]


def test_shared_observation_cache_avoids_repeat_fetches_across_roles(
    monitored_store, monkeypatch
):
    controller = watched_candidate("controller")
    researcher = watched_candidate("mts-research")
    baseline(controller)
    baseline(researcher)
    calls = []

    async def fetch(url):
        calls.append(url)
        return {"url": url, "title": "Fictional page", "text": "unchanged"}

    monkeypatch.setattr(providers, "fetch_document", fetch)

    assert watch(controller)["provider_model_calls"] == 0
    assert watch(researcher)["provider_model_calls"] == 0
    assert calls == [URL]


def test_changed_observation_researches_and_removes_prior_readiness(
    monitored_store, monkeypatch
):
    record = watched_candidate()
    baseline(record, content="old text")

    async def fetch(url):
        return {"url": url, "title": "Changed fictional page", "text": "new text"}

    async def research(*_):
        pytest.fail(
            "watch should queue a bounded research job instead of interpreting a change inline"
        )

    monkeypatch.setattr(providers, "fetch_document", fetch)
    monkeypatch.setattr(providers, "research", research)

    result = watch(record)
    after = main.store.get(record["id"])

    assert result["changed_sources"] == [URL]
    queued = [
        job
        for job in main.store.all("job")
        if job["kind"] == "research" and job["candidate_id"] == record["id"]
    ]
    assert len(queued) == 1 and queued[0]["status"] == "queued"
    assert not after.get("review_hash")
    assert main.enriched(after)["decision"]["state"] == "needs_review"


def test_fetch_failure_is_reported_instead_of_silently_preserving_coverage(
    monitored_store, monkeypatch
):
    record = watched_candidate()
    baseline(record)

    async def fail_fetch(_):
        raise providers.ProviderError("fictional source fetch failed")

    monkeypatch.setattr(providers, "fetch_document", fail_fetch)

    with pytest.raises(
        providers.ProviderError, match="Coverage degraded: some watched sources failed"
    ):
        watch(record)


def test_weekly_watch_forces_broader_research_without_refetching_known_sources(
    monitored_store, monkeypatch
):
    record = watched_candidate()
    baseline(record, researched_at=iso(utcnow() - timedelta(days=8)))
    calls = {"fetch": 0, "research": 0}

    async def fetch(_):
        calls["fetch"] += 1
        pytest.fail(
            "weekly broader research should bypass the short-interval fetch-only path"
        )

    async def research(*_):
        calls["research"] += 1
        return {
            "summary": "Fictional weekly research",
            "_documents": [],
            "_usage": {"provider_calls": 1},
        }

    monkeypatch.setattr(providers, "fetch_document", fetch)
    monkeypatch.setattr(providers, "research", research)

    watch(record)

    assert calls == {"fetch": 0, "research": 1}


def test_role_version_change_forces_research(monitored_store, monkeypatch):
    record = watched_candidate()
    baseline(record)
    role = main.role_for("controller")
    main.store.put(
        "role:controller",
        "role",
        {**role, "brief_version": role["brief_version"] + 1},
        expected=role["revision"],
    )
    calls = {"fetch": 0, "research": 0}

    async def fetch(_):
        calls["fetch"] += 1
        pytest.fail("role change should skip hash-only watching")

    async def research(*_):
        calls["research"] += 1
        return {
            "summary": "Fictional new-brief research",
            "_documents": [],
            "_usage": {"provider_calls": 1},
        }

    monkeypatch.setattr(providers, "fetch_document", fetch)
    monkeypatch.setattr(providers, "research", research)

    watch(record)

    assert calls == {"fetch": 0, "research": 1}


def test_watch_never_calls_providers_for_simulation(monitored_store, monkeypatch):
    calls = []

    async def forbidden(*_):
        calls.append(True)
        pytest.fail("simulation must not call a provider")

    monkeypatch.setattr(providers, "fetch_document", forbidden)
    monkeypatch.setattr(providers, "research", forbidden)

    with pytest.raises(ValueError, match="Simulation never calls external providers"):
        asyncio.run(
            main.execute_job(
                {
                    "kind": "watch",
                    "mode": "simulation",
                    "candidate_id": "candidate:fictional",
                }
            )
        )
    assert calls == []


def test_exa_watch_uses_same_extractor_and_ignores_native_cache(
    monitored_store, monkeypatch
):
    record = watched_candidate()
    baseline(record, content="Exa full page")
    key = "research-baseline:" + record["id"]
    old = main.store.get(key)
    main.store.put(
        key, "baseline", {**old, "extraction_methods": {URL: "exa_contents"}}
    )
    main.store.put(
        "observation:" + main.digest(URL),
        "observation",
        {
            "url": URL,
            "checked_at": iso(),
            "content_hash": main.digest("native extraction"),
            "extraction_method": "native",
            "text": "native extraction",
            "title": "Fictional",
        },
    )
    monkeypatch.setattr(
        main, "settings", lambda: {"exa_key": "test-only", "max_calls_per_run": 8}
    )
    calls = []

    async def current(urls, settings, budget, role_id):
        calls.append((urls, role_id))
        return {
            "documents": [{"url": URL, "title": "Fictional", "text": "Exa full page"}],
            "statuses": [],
        }

    async def native(_):
        pytest.fail("Changing extractors would create a false change signal")

    monkeypatch.setattr(providers, "fetch_current_documents", current)
    monkeypatch.setattr(providers, "fetch_document", native)
    result = watch(record)
    assert result["provider_model_calls"] == 0
    assert calls == [([URL], "controller")]
    assert main.store.get(record["id"])["review_hash"] == record["review_hash"]


def test_first_full_observation_establishes_baseline_without_false_change(
    monitored_store, monkeypatch
):
    record = watched_candidate()
    baseline(record)
    key = "research-baseline:" + record["id"]
    previous = main.store.get(key)
    main.store.put(key, "baseline", {**previous, "sources": {}})
    monkeypatch.setattr(main, "settings", lambda: {"max_calls_per_run": 8})

    async def fetch(url):
        return {"url": url, "title": "First full page", "text": "newly observed text"}

    monkeypatch.setattr(providers, "fetch_document", fetch)
    result = watch(record)
    assert result["baselines_established"] == [URL]
    assert main.store.get(key)["sources"][URL] == main.digest("newly observed text")
    assert main.store.get(record["id"])["review_hash"] == record["review_hash"]
    assert not main.store.all("job")


def test_extractor_migration_rebaselines_without_inventing_change(
    monitored_store, monkeypatch
):
    record = watched_candidate()
    baseline(record, content="old full paper extraction")
    key = "research-baseline:" + record["id"]
    previous = main.store.get(key)
    main.store.put(
        key, "baseline", {**previous, "extraction_methods": {URL: "exa_contents"}}
    )
    monkeypatch.setattr(
        main, "settings", lambda: {"exa_key": "test-only", "max_calls_per_run": 8}
    )

    async def current(*args):
        return {
            "documents": [
                {
                    "url": URL,
                    "title": "Original abstract",
                    "text": "submission history",
                    "extraction_method": "native",
                }
            ],
            "statuses": [],
        }

    monkeypatch.setattr(providers, "fetch_current_documents", current)
    result = watch(record)
    assert result["baselines_established"] == [URL]
    assert main.store.get(key)["extraction_methods"][URL] == "native"
    assert main.store.get(record["id"])["review_hash"] == record["review_hash"]
    assert not main.store.all("job")


def test_scheduler_enqueues_only_due_watches_and_reports_next_wake(monitored_store, monkeypatch):
    from datetime import timedelta as td
    from app import company_context
    monkeypatch.setattr(company_context, "due", lambda store: False)
    c = watched_candidate()
    later = utcnow() + td(hours=2)
    monitored_store.put("watch:" + c["id"], "watch", {"candidate_id": c["id"], "mode": "live", "status": "active",
                                                     "role_id": c["role_id"], "next_check_at": iso(later)})
    now = utcnow()
    assert main.schedule_monitoring(now) == now + td(minutes=15)  # the cap wins over a 2 h watch
    assert monitored_store.all("job") == []
    assert [p["subject_id"] for p in monitored_store.all("moment_plan")] == [c["person_id"]]  # moment rechecks run
    watch = monitored_store.get("watch:" + c["id"])
    monitored_store.put(watch["id"], "watch", {**watch, "next_check_at": iso(now - td(minutes=1))})
    main.schedule_monitoring(utcnow())
    assert [j["kind"] for j in monitored_store.all("job")] == ["watch"]
