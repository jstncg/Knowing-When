"""Durable discovery lifecycle using isolated storage and fictional remote runs."""

import asyncio
import copy
import json

import httpx
import pytest
from fastapi import HTTPException

from app import exa_agent, main
from app.models import iso
from app.providers import ProviderError
from app.store import Store

REMOTE_ID = "agent_run_fictional_jobs"
PUBLIC_URL = "https://sources.example.test/fictional-alice"


@pytest.fixture()
def jobs(monkeypatch, tmp_path):
    isolated = Store(f"sqlite:///{tmp_path / 'jobs.sqlite'}")
    monkeypatch.setattr(main, "store", isolated)
    # initialize must never inspect local private seeds or actual credentials.
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "roles.json").write_text(
        json.dumps(
            {
                "roles": [
                    {
                        "id": "test-role",
                        "title": "Fictional research role",
                        "criteria": [
                            "Attributable public research",
                            "Systems experience",
                        ],
                        "implementation_stage": "pilot",
                        "brief_version": 1,
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(main, "ROOT", tmp_path)
    isolated.put("simulation-initialized", "meta", {"created_at": iso()})
    monkeypatch.setattr(
        main,
        "settings",
        lambda: {
            "exa_key": "fake-test-only",
            "monitoring_enabled": False,
            "max_calls_per_run": 8,
        },
    )

    async def forbidden_request(*args, **kwargs):
        pytest.fail("Lifecycle tests must not make a real network request")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden_request)
    main.initialize()
    yield isolated
    isolated.engine.dispose()


def remote(status="completed"):
    return {
        "id": REMOTE_ID,
        "status": status,
        "stopReason": "schema_satisfied" if status == "completed" else None,
        "costDollars": {"total": 1.25},
        "warnings": [],
        "output": {
            "structured": {
                "candidates": [
                    {
                        "name": "Fictional Alice",
                        "profile_url": PUBLIC_URL,
                        "employer": "Unknown",
                        "location": "Unknown",
                        "why_fit": "Public work may demonstrate the supplied research criterion.",
                        "source_urls": [PUBLIC_URL],
                        "evidence_remarks": "Agent synthesis for a fictional fixture.",
                        "timing_hint": "Unknown",
                    }
                ]
            },
            "grounding": [
                {
                    "field": "structured.candidates[0].profile_url",
                    "citations": [{"url": PUBLIC_URL, "title": "Fictional source"}],
                }
            ],
        },
    }


def fake_provider(monkeypatch, create_status="queued", poll_status="completed"):
    calls = {"create": 0, "poll": 0}

    async def create(role, settings, exclusions):
        calls["create"] += 1
        assert role["id"] == "test-role"
        assert settings["exa_key"] == "fake-test-only"
        recorded = main.store.all("job")[0]
        assert recorded["creation_started_at"]
        assert recorded["role_snapshot"] == role
        assert not recorded.get("remote_run_id")
        return remote(create_status)

    async def poll(run_id, settings):
        calls["poll"] += 1
        assert run_id == REMOTE_ID
        return remote(poll_status)

    monkeypatch.setattr(exa_agent, "create_run", create)
    monkeypatch.setattr(exa_agent, "poll_run", poll)
    return calls


def worker_once(monkeypatch):
    async def stop_after_iteration(_):
        raise asyncio.CancelledError()

    with monkeypatch.context() as patch:
        patch.setattr(main.asyncio, "sleep", stop_after_iteration)
        asyncio.run(main.worker())


def make_due(job):
    return main.store.put(
        job["id"], "job", {**main.store.get(job["id"]), "next_poll_at": iso()}
    )


def test_creation_persists_id_before_yield_then_completion_is_idempotent(
    jobs, monkeypatch
):
    calls = fake_provider(monkeypatch)
    job = main.enqueue("discover", "live", role_id="test-role")
    worker_once(monkeypatch)
    pending = jobs.get(job["id"])
    assert pending["status"] == "queued"
    assert pending["remote_run_id"] == REMOTE_ID
    assert pending["creation_started_at"]
    assert pending["next_poll_at"]
    assert pending["role_snapshot"]["brief_version"] == 1
    assert calls == {"create": 1, "poll": 0}
    assert jobs.all("candidate") == []

    # The queued worker must yield until its durable next-poll time.
    worker_once(monkeypatch)
    assert calls == {"create": 1, "poll": 0}

    make_due(job)
    worker_once(monkeypatch)
    finished = jobs.get(job["id"])
    assert finished["status"] == "complete"
    assert finished["result"]["candidates_added"] == 1
    assert finished["result"]["costDollars"] == {"total": 1.25}
    assert (
        finished["remote_run"]["output"]["grounding"] == remote()["output"]["grounding"]
    )
    candidate = jobs.all("candidate")[0]
    assert candidate["identity_status"] == "unknown"
    assert all(f["status"] == "unknown" for f in candidate["fit"])
    assert candidate["draft"]["body"] == ""
    assert candidate["events"] == []
    assert not candidate.get("review_hash")
    assert jobs.all("delivery") == []
    assert jobs.all("source")[0]["grounding"]

    repeated = asyncio.run(main.execute_job(finished))
    assert repeated["candidates_added"] == 0
    assert repeated["duplicates_skipped"] == 1
    assert len(jobs.all("candidate")) == 1
    assert calls["create"] == 1


def test_initialize_resumes_interrupted_run_without_duplicate_creation(
    jobs, monkeypatch
):
    calls = fake_provider(monkeypatch)
    job = main.enqueue("discover", "live", role_id="test-role")
    worker_once(monkeypatch)
    jobs.put(
        job["id"],
        "job",
        {**jobs.get(job["id"]), "status": "running", "next_poll_at": None},
    )
    main.initialize()
    assert jobs.get(job["id"])["status"] == "queued"
    worker_once(monkeypatch)
    assert jobs.get(job["id"])["status"] == "complete"
    assert calls == {"create": 1, "poll": 1}


def test_failed_poll_retries_same_job_and_remote_run(jobs, monkeypatch):
    calls = fake_provider(monkeypatch)
    job = main.enqueue("discover", "live", role_id="test-role")
    worker_once(monkeypatch)

    async def failed_poll(*_):
        raise ProviderError("Fictional temporary Exa request failure")

    successful_poll = exa_agent.poll_run
    monkeypatch.setattr(exa_agent, "poll_run", failed_poll)
    make_due(job)
    worker_once(monkeypatch)
    failed = jobs.get(job["id"])
    assert failed["status"] == "failed"
    assert "temporary" in failed["error"]
    retried = main.enqueue("discover", "live", role_id="test-role")
    assert retried["id"] == job["id"]
    assert retried["remote_run_id"] == REMOTE_ID
    assert retried["status"] == "queued"
    monkeypatch.setattr(exa_agent, "poll_run", successful_poll)
    worker_once(monkeypatch)
    assert jobs.get(job["id"])["status"] == "complete"
    assert calls["create"] == 1


def test_ambiguous_creation_is_blocked_after_failure_and_restart(jobs, monkeypatch):
    calls = []

    async def uncertain_create(*_):
        calls.append(True)
        raise ProviderError("Creation may have succeeded remotely; check Exa runs.")

    monkeypatch.setattr(exa_agent, "create_run", uncertain_create)
    job = main.enqueue("discover", "live", role_id="test-role")
    worker_once(monkeypatch)
    failed = jobs.get(job["id"])
    assert failed["status"] == "failed"
    assert failed["creation_started_at"]
    assert not failed.get("remote_run_id")
    main.initialize()
    with pytest.raises(HTTPException) as error:
        main.enqueue("discover", "live", role_id="test-role")
    assert error.value.status_code == 409
    assert "uncertain" in error.value.detail
    with pytest.raises(ValueError, match="uncertain"):
        asyncio.run(main.execute_job(failed))
    assert len(calls) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_terminal_failure_is_visible_and_never_empty_success(jobs, monkeypatch, status):
    calls = fake_provider(monkeypatch, poll_status=status)
    job = main.enqueue("discover", "live", role_id="test-role")
    worker_once(monkeypatch)
    make_due(job)
    worker_once(monkeypatch)
    failed = jobs.get(job["id"])
    assert failed["status"] == "failed"
    assert status in failed["error"]
    assert failed["remote_status"] == status
    assert failed["remote_run"]["costDollars"] == {"total": 1.25}
    assert jobs.all("candidate") == []
    assert calls == {"create": 1, "poll": 1}


def test_role_version_change_is_visible_and_original_brief_is_preserved(
    jobs, monkeypatch
):
    fake_provider(monkeypatch)
    job = main.enqueue("discover", "live", role_id="test-role")
    worker_once(monkeypatch)
    old_snapshot = copy.deepcopy(jobs.get(job["id"])["role_snapshot"])
    role = main.role_for("test-role")
    jobs.put(
        "role:test-role",
        "role",
        {**role, "brief_version": 2, "criteria": ["Entirely different criterion"]},
    )
    make_due(job)
    worker_once(monkeypatch)
    finished = jobs.get(job["id"])
    assert finished["result"]["role_brief_changed"] is True
    assert finished["role_snapshot"] == old_snapshot
    assert (
        jobs.all("candidate")[0]["fit"][0]["criterion"] == old_snapshot["criteria"][0]
    )


def test_enqueue_deduplicates_active_discovery_jobs(jobs, monkeypatch):
    calls = fake_provider(monkeypatch)
    original = main.enqueue("discover", "live", role_id="test-role")
    duplicate = main.enqueue("discover", "live", role_id="test-role")
    assert duplicate["id"] == original["id"]
    worker_once(monkeypatch)
    duplicate = main.enqueue("discover", "live", role_id="test-role")
    assert duplicate["id"] == original["id"]
    assert len(jobs.all("job")) == 1
    assert calls["create"] == 1


def test_immediately_completed_creation_is_saved_without_polling(jobs, monkeypatch):
    calls = fake_provider(monkeypatch, create_status="completed")
    job = main.enqueue("discover", "live", role_id="test-role")
    worker_once(monkeypatch)
    completed_job = jobs.get(job["id"])
    assert completed_job["status"] == "complete"
    assert completed_job["remote_run_id"] == REMOTE_ID
    assert completed_job["result"]["candidates_added"] == 1
    assert calls == {"create": 1, "poll": 0}


def test_explicit_create_rejection_allows_retry_after_setup_fix(jobs, monkeypatch):
    job = main.enqueue("discover", "live", role_id="test-role")

    async def rejected(*args):
        raise exa_agent.RequestRejected("Exa Agent returned HTTP 401.")

    monkeypatch.setattr(exa_agent, "create_run", rejected)
    with pytest.raises(exa_agent.RequestRejected):
        asyncio.run(main.execute_exa_discovery(job, main.settings()))
    current = jobs.get(job["id"])
    assert not current.get("creation_started_at")
    jobs.put(job["id"], "job", {**current, "status": "failed"})
    assert main.enqueue("discover", "live", role_id="test-role")["id"] != job["id"]
