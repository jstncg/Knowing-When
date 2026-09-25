"""Paid research output survives stale-input rejection without replaying it."""

import asyncio
from copy import deepcopy
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, providers
from app.store import Conflict, Store


@pytest.fixture()
def research_store(monkeypatch, tmp_path):
    isolated = Store(f"sqlite:///{tmp_path / 'research-durability.sqlite'}")
    monkeypatch.setattr(main, "store", isolated)
    monkeypatch.setattr(
        main,
        "settings",
        lambda: {"max_calls_per_run": 8, "monitoring_enabled": False},
    )
    # Seed only public role configuration, never workstation dossiers or secrets.
    import json

    roles = json.loads((main.ROOT / "config/roles.json").read_text())["roles"]
    for role in roles:
        isolated.put("role:" + role["id"], "role", role)
    candidate = main.save_candidate(
        {
            "name": "Fictional Research Retention Example",
            "profile_url": "https://example.org/fictional-research-retention",
            "role_id": "controller",
            "mode": "live",
            "summary": "Original fictional summary",
        }
    )
    return isolated, candidate


def returned_proposal():
    return {
        "summary": "New fictional research proposal",
        "_documents": [
            {
                "url": "https://example.org/fictional-source",
                "title": "Fictional source",
                "text": "Fictional professional content retained for review.",
                "extraction_method": "native",
            }
        ],
        "_usage": {"provider_calls": 2, "input_tokens": 17, "output_tokens": 19},
    }


def run_research(candidate):
    return asyncio.run(
        main.execute_job(
            {
                "id": "job:durability-test",
                "kind": "research",
                "mode": "live",
                "candidate_id": candidate["id"],
            }
        )
    )


@pytest.mark.parametrize("edit_kind", ["candidate", "role"])
def test_stale_research_retains_proposal_and_preserves_edit(
    research_store, monkeypatch, edit_kind
):
    isolated, candidate = research_store
    original_role = main.role_for("controller")
    proposal = returned_proposal()
    calls = []

    async def research(*_):
        calls.append(True)
        if edit_kind == "candidate":
            current = isolated.get(candidate["id"])
            isolated.put(
                candidate["id"],
                "candidate",
                {**current, "summary": "Human correction during research"},
                expected=current["revision"],
            )
        else:
            isolated.put(
                "role:controller",
                "role",
                {
                    **original_role,
                    "brief_version": original_role["brief_version"] + 1,
                    "manager_notes": "Human role clarification during research",
                },
                expected=original_role["revision"],
            )
        return deepcopy(proposal)

    monkeypatch.setattr(providers, "research", research)
    with pytest.raises(Conflict, match="retained for review"):
        run_research(candidate)

    artifact = isolated.get("research-artifact:job:durability-test")
    assert artifact["candidate_id"] == candidate["id"]
    assert artifact["input_revision"] == candidate["revision"]
    assert artifact["role_brief_version"] == original_role["brief_version"]
    assert artifact["generated_at"]
    assert artifact["proposal"] == proposal
    assert calls == [True]
    assert isolated.get("research-baseline:" + candidate["id"]) is None
    assert isolated.all("observation") == []
    current = isolated.get(candidate["id"])
    if edit_kind == "candidate":
        assert current["summary"] == "Human correction during research"
    else:
        assert current["revision"] == candidate["revision"]
        assert current["summary"] == "Original fictional summary"
        assert main.role_for("controller")["manager_notes"] == (
            "Human role clarification during research"
        )

    # No app lifespan: do not start the real worker or initialize local seed data.
    client = TestClient(main.app)
    response = client.get("/api/jobs/job:durability-test/research-proposal")
    assert response.status_code == 200
    assert response.json()["proposal"]["_usage"] == proposal["_usage"]
    assert client.get("/api/jobs/job:missing/research-proposal").status_code == 404
    assert (
        client.get(
            "http://untrusted.example/api/jobs/job:durability-test/research-proposal"
        ).status_code
        == 403
    )


def test_unchanged_research_retains_artifact_and_applies_result(
    research_store, monkeypatch
):
    isolated, candidate = research_store
    proposal = returned_proposal()

    async def research(*_):
        return deepcopy(proposal)

    monkeypatch.setattr(providers, "research", research)
    result = run_research(candidate)

    assert result["usage"] == proposal["_usage"]
    assert result["step_calls"] == {"extraction": 0, "kill_pass": 0}  # no pages to read, no hook to check
    assert isolated.get(candidate["id"])["summary"] == proposal["summary"]
    assert isolated.get("research-baseline:" + candidate["id"])
    assert len(isolated.all("observation")) == 1
    assert isolated.get("research-artifact:job:durability-test")["proposal"] == proposal


def test_paid_research_is_retained_when_a_step_after_it_fails(research_store, monkeypatch):
    isolated, candidate = research_store
    proposal = returned_proposal()

    async def research(*_):
        return deepcopy(proposal)

    async def locked(*_):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(providers, "research", research)
    monkeypatch.setattr(main, "check_research", locked)
    with pytest.raises(RuntimeError, match="database is locked"):
        run_research(candidate)
    assert isolated.get("research-artifact:job:durability-test")["proposal"] == proposal
