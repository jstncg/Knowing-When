"""End-to-end API guards using only fictional records and an isolated SQLite store."""

from timing_helpers import INVITATION, assessment, purpose

from datetime import timedelta
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main
from app.models import iso, utcnow
from app.store import Store


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(
        main, "store", Store(f"sqlite:///{tmp_path / 'api-test.sqlite'}")
    )
    with TestClient(main.app) as test_client:
        yield test_client


def candidate_body(
    role_id="controller",
    mode="simulation",
    profile="https://profiles.example.test/api-avery",
):
    now = utcnow()
    event_day = (now - timedelta(days=1)).date().isoformat()
    criteria = {
        "controller": [
            "Hands-on accounting and close",
            "US GAAP",
            "US and international tax",
            "Multi-entity work and audits",
            "Finance automation",
            "Substantial experience including public accounting",
        ],
        "mts-research": [
            "Exceptional relevant technical work with attributable evidence",
            "Action/world-model problem overlap (proposed specialization)",
        ],
    }[role_id]
    return {
        "name": "Avery API Example",
        "profile_url": profile,
        "role_id": role_id,
        "mode": mode,
        "summary": "Fictional record for API contract testing only.",
        "identity_status": "unknown",
        "evidence": [
            {
                "id": "fictional-source",
                "url": "https://sources.example.test/api-avery",
                "title": "Fictional source",
                "excerpt": "This fictional source supports a test-only professional update. " + INVITATION,
                "observed_at": iso(now),
                "event_date": event_day,
                "verified": False,
                "source_kind": "synthetic_fixture",
            }
        ],
        "fit": [
            {
                "criterion": criterion,
                "status": "supported",
                "evidence_ids": ["fictional-source"],
            }
            for criterion in criteria
        ],
        "events": [
            {
                "id": "fictional-event",
                "kind": "professional_update",
                "title": "Fictional published professional update",
                "date": event_day,
                "timing_action": "contact_now",
                "timing_assessment": assessment("fictional-source"),
                **purpose("fictional-source"),
                "recipient_value": "Compare this documented professional approach with the relevant role scope at GI.",
                "why_now": "This fictional update provides a specific and dated professional reason to consider a conversation this week.",
                "why_wait": "Waiting remains appropriate if later fictional evidence shows the event is no longer relevant.",
                "falsifier": "A correction showing this fictional update belongs to another person would disprove the proposed conversation.",
                "evidence_ids": ["fictional-source"],
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
            "subject": "Fictional API test conversation",
            "body": "Avery, this test-only note has enough content to validate the guarded release path without describing a real person, employer, or opportunity.",
            "sender_function": "CEO / operations leader",
            "sender_name": "",
            "channel": "email",
            "source_ids": ["fictional-source"],
        },
    }


def create(client, **overrides):
    body = candidate_body(**overrides)
    response = client.post("/api/candidates", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_web_refresh_preserves_offline_referral_and_named_sender():
    original = candidate_body()
    original["evidence"][0]["source_kind"] = "authorized_referral"
    original["route"] = {
        "status": "verified",
        "description": "A fictional colleague explicitly offered to introduce.",
        "introducer": "Test colleague",
        "evidence_ids": ["fictional-source"],
    }
    original["draft"]["sender_name"] = "Authorized test sender"
    proposal = {
        "evidence": [],
        "fit": [],
        "events": [],
        "route": {
            "status": "none_found",
            "description": "none found",
            "introducer": "",
            "evidence_ids": [],
        },
        "draft": {**original["draft"], "sender_name": "", "source_ids": []},
    }
    merged = main.merge_research(original, proposal)
    assert merged["evidence"] == original["evidence"]
    assert merged["events"] == original["events"]
    assert merged["route"] == original["route"]
    assert merged["draft"]["sender_name"] == "Authorized test sender"


def review(client, record):
    response = client.post(
        f"/api/candidates/{record['id']}/review",
        json={
            "revision": record["revision"],
            "identity_confirmed": True,
            "evidence_confirmed": True,
            "fit_confirmed": True,
            "note": "Fictional test review.",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def state_candidate(client, key, mode="simulation"):
    state = client.get(f"/api/state?mode={mode}").json()
    return next(
        candidate for candidate in state["candidates"] if candidate["id"] == key
    )


def test_human_review_releases_only_a_reviewed_fictional_candidate(client):
    created = create(client)
    assert state_candidate(client, created["id"])["decision"]["state"] == "needs_review"

    reviewed = review(client, created)

    assert reviewed["decision"]["state"] == "ready"


def test_negative_feedback_retracts_only_this_opportunity_without_rewriting_role(
    client,
):
    record = review(client, create(client))
    version = main.role_for("controller")["brief_version"]
    response = client.post(
        f"/api/candidates/{record['id']}/feedback",
        json={
            "label": "wrong_identity",
            "note": "This source identifies a different person.",
        },
    )
    assert response.status_code == 200
    assert state_candidate(client, record["id"])["identity_status"] == "conflict"
    assert state_candidate(client, record["id"])["decision"]["state"] != "ready"
    assert main.role_for("controller")["brief_version"] == version


def test_candidate_edit_invalidates_an_earlier_review(client):
    reviewed = review(client, create(client))
    response = client.put(
        f"/api/candidates/{reviewed['id']}",
        json={
            "revision": reviewed["revision"],
            "summary": "Edited fictional record; review must be repeated.",
        },
    )
    assert response.status_code == 200, response.text

    assert state_candidate(client, reviewed["id"])["decision"]["state"] == "needs_review"


def test_role_edit_invalidates_reviews_against_that_role(client):
    reviewed = review(client, create(client))
    role = next(
        role
        for role in client.get("/api/state?mode=simulation").json()["roles"]
        if role["id"] == "controller"
    )
    response = client.put(
        "/api/roles/controller",
        json={
            "brief_version": role["brief_version"],
            "title": "Fictional revised controller brief",
        },
    )
    assert response.status_code == 200, response.text

    assert (
        state_candidate(client, reviewed["id"])["decision"]["state"] == "needs_review"
    )


def test_opt_out_cannot_be_reversed_by_reopen(client):
    reviewed = review(client, create(client))
    opted_out = client.post(
        f"/api/candidates/{reviewed['id']}/action", json={"action": "opt_out"}
    )
    assert opted_out.status_code == 200, opted_out.text

    reopened = client.post(
        f"/api/candidates/{reviewed['id']}/action", json={"action": "reopen"}
    )

    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["decision"]["state"] == "suppressed"
    assert "asked not to be contacted" in " ".join(reopened.json()["decision"]["reasons"])


def test_contact_history_suppresses_the_same_person_across_roles(client):
    controller = review(
        client,
        create(
            client,
            role_id="controller",
            profile="https://profiles.example.test/cross-role",
        ),
    )
    researcher = review(
        client,
        create(
            client,
            role_id="mts-research",
            profile="https://profiles.example.test/cross-role",
        ),
    )
    sent = client.post(
        f"/api/candidates/{controller['id']}/action", json={"action": "mark_sent"}
    )
    assert sent.status_code == 200, sent.text

    decision = state_candidate(client, researcher["id"])["decision"]
    assert decision["state"] == "suppressed"
    assert "waiting for a reply" in " ".join(decision["reasons"])


def test_live_and_simulation_use_separate_person_records(client):
    profile = "https://profiles.example.test/mode-boundary"
    simulation = review(client, create(client, mode="simulation", profile=profile))
    live = review(client, create(client, mode="live", profile=profile))
    response = client.post(
        f"/api/candidates/{simulation['id']}/action", json={"action": "opt_out"}
    )
    assert response.status_code == 200, response.text

    assert simulation["person_id"] != live["person_id"]
    assert (
        state_candidate(client, simulation["id"])["decision"]["state"] == "suppressed"
    )
    assert (
        state_candidate(client, live["id"], mode="live")["decision"]["state"] == "ready"
    )


def test_stale_candidate_revision_is_rejected(client):
    created = create(client)

    response = client.put(
        f"/api/candidates/{created['id']}",
        json={"revision": created["revision"] - 1, "summary": "stale"},
    )

    assert response.status_code == 409
    assert "Candidate changed" in response.json()["detail"]


def test_secret_dotenv_is_not_exposed_and_cross_origin_writes_are_blocked(client):
    dot_env = client.get("/.env")
    cross_origin = client.post(
        "/api/run",
        json={"operation": "evaluate", "mode": "simulation"},
        headers={"Origin": "https://attacker.example.test"},
    )

    assert dot_env.status_code == 404
    assert cross_origin.status_code == 403
    assert cross_origin.json()["detail"] == "Cross-origin writes are disabled."


def test_call_limit_range_is_the_budget_cap(client):
    low, high = main.providers.MAX_CALLS_PER_RUN
    assert client.get("/api/settings").json()["max_calls_range"] == [low, high]
    assert client.post("/api/settings", json={"max_calls_per_run": high + 1}).status_code == 422
    assert main.providers.Budget({"max_calls_per_run": high + 10}).limit == high
