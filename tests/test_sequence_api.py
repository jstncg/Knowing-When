"""Real local API/storage workflow, isolated from providers, secrets and live seeds."""

from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from app import contact, main
from app.fixtures import scenarios
from app.store import Store


@pytest.fixture
def client(monkeypatch, tmp_path):
    isolated = Store(f"sqlite:///{tmp_path / 'sequence.sqlite'}")
    monkeypatch.setattr(main, "store", isolated)
    monkeypatch.setattr(
        main,
        "settings",
        lambda: {
            "monitoring_enabled": False,
            "model": "test-model",
            "max_calls_per_run": 1,
            "exa_key": "",
            "anthropic_key": "",
        },
    )
    for role in json.loads((main.ROOT / "config/roles.json").read_text())["roles"]:
        role["required_criteria"] = (
            role.get("required_criteria") or role["criteria"][:2]
        )
        role["manager_notes"] = ""
        isolated.put("role:" + role["id"], "role", role)
    # Deliberately do not enter lifespan: no private seeds, monitor or job worker.
    test_client = TestClient(main.app, raise_server_exceptions=False)
    yield test_client
    test_client.close()
    isolated.engine.dispose()


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def demo(client):
    return ok(client.post("/api/sequence/demo", json={}))


def sequence(client):
    return ok(client.get("/api/sequence?mode=simulation"))


def one_alert(client):
    demo(client)
    alert = sequence(client)["alerts"][0]
    ok(client.post("/api/sequence/receipts", json={"alert_ids": [alert["id"]]}))
    return main.store.get(alert["id"])


def test_three_role_replay_fires_only_after_meaningful_change(client):
    result = demo(client)
    steps = {step["stage"]: step for step in result["steps"]}
    assert steps["baseline"]["new_alerts"] == 0
    assert steps["unchanged"]["new_alerts"] == 0
    assert steps["meaningful_change"]["persisted"] == 3
    assert steps["repeat"]["new_alerts"] == 0
    assert steps["opt_out"]["suppressed"] is True
    state = sequence(client)
    assert len(state["alerts"]) == 3
    assert {main.store.get(a["candidate_id"])["role_id"] for a in state["alerts"]} == {
        "controller",
        "backend",
        "mts-research",
    }
    for alert in state["alerts"]:
        ping = alert["ping"]
        assert all(
            ping.get(k)
            for k in (
                "person",
                "role",
                "trigger",
                "why_now",
                "confidence",
                "draft_outreach",
                "route_in",
            )
        )
        assert alert["delivery_count"] == 1
        assert alert["seen_at"] is None
        assert alert["status"] == "proposed"
    assert ok(client.get("/api/sequence?mode=live"))["alerts"] == []
    assert main.store.all("job") == []


def test_only_browser_acknowledgment_marks_active_proposal_displayed(client):
    demo(client)
    alert = main.store.all("review_alert")[0]
    assert alert["seen_at"] is None
    watch_id = "watch:" + alert["candidate_id"]
    ok(client.patch("/api/watches/" + watch_id, json={"status": "paused"}))
    state = sequence(client)
    withdrawn = next(a for a in state["alerts"] if a["id"] == alert["id"])
    assert withdrawn["status"] == "withdrawn"
    assert withdrawn["seen_at"] is None
    assert main.store.get(alert["id"])["seen_at"] is None
    assert all(a["seen_at"] is None for a in state["alerts"])
    active = next(a for a in state["alerts"] if a["status"] == "proposed")
    ok(
        client.post(
            "/api/sequence/receipts",
            json={"alert_ids": [active["id"], withdrawn["id"]]},
        )
    )
    assert main.store.get(active["id"])["seen_at"]
    assert main.store.get(withdrawn["id"])["seen_at"] is None
    acknowledged_at = main.store.get(active["id"])["seen_at"]
    ok(client.post("/api/sequence/receipts", json={"alert_ids": [active["id"]]}))
    assert main.store.get(active["id"])["seen_at"] == acknowledged_at


def test_watch_enrollment_is_not_identity_fit_or_outreach_approval(client):
    roles = {
        key: main.role_for(key) for key in ("mts-research", "controller", "backend")
    }
    body = scenarios(roles)[0]
    candidate = ok(client.post("/api/candidates", json=body))
    before = deepcopy(main.store.get(candidate["id"]))
    watch = ok(
        client.post(
            "/api/watches",
            json={
                "candidate_id": candidate["id"],
                "fit_basis": "Attributable technical work merits observation, but actual hiring fit is unreviewed.",
            },
        )
    )
    assert watch["status"] == "active"
    after = main.store.get(candidate["id"])
    assert after == before
    assert after["identity_status"] == "unknown"
    assert not after.get("review_hash")
    assert not any(e["verified"] for e in after["evidence"])
    state = sequence(client)
    assert (
        len(state["alerts"]) == 1
    )  # Internal proposal can arrive before send approval.
    assert main.store.all("delivery") == []
    assert contact.history(main.store, candidate["person_id"]) == []


def test_role_revision_invalidates_existing_watch_and_withdraws_its_alert(client):
    alert = one_alert(client)
    candidate = main.store.get(alert["candidate_id"])
    role = main.role_for(candidate["role_id"])
    ok(
        client.put(
            "/api/roles/" + candidate["role_id"],
            json={
                "brief_version": role["brief_version"],
                "manager_notes": "Now requires independently verified production ownership.",
            },
        )
    )
    state = sequence(client)
    assert (
        next(a for a in state["alerts"] if a["id"] == alert["id"])["status"]
        == "withdrawn"
    )
    assert (
        main.store.get("watch:" + candidate["id"])["brief_version"]
        < main.role_for(candidate["role_id"])["brief_version"]
    )
    other_alerts = [a for a in state["alerts"] if a["candidate_id"] != candidate["id"]]
    assert all(a["status"] == "proposed" for a in other_alerts)


def test_retitle_and_url_tracking_change_do_not_create_second_interruption(client):
    alert = one_alert(client)
    candidate = main.store.get(alert["candidate_id"])
    candidate["events"][0]["title"] = (
        "Different wording for exactly the same professional milestone"
    )
    candidate["events"][0]["kind"] = "professional_update"
    candidate["evidence"][0]["url"] += "?utm_source=repost#fragment"
    ok(client.put("/api/candidates/" + candidate["id"], json=candidate))
    state = sequence(client)
    matching = [a for a in state["alerts"] if a["candidate_id"] == candidate["id"]]
    assert len(matching) == 1
    assert matching[0]["id"] == alert["id"]
    assert matching[0]["created_at"] == alert["created_at"]
    assert matching[0]["seen_at"] == alert["seen_at"]
    assert matching[0]["delivery_count"] == 1
    assert (
        matching[0]["ping"]["trigger"]["what_changed"]
        == candidate["events"][0]["title"]
    )


def test_source_change_pending_withdraws_stale_proposal_until_research_resolves(client):
    alert = one_alert(client)
    candidate = main.store.get(alert["candidate_id"])
    # Simulates the monitor's persisted observation, without mocking decisions.
    main.store.put(
        candidate["id"],
        "candidate",
        {
            **candidate,
            "source_change_pending": {
                "urls": [candidate["evidence"][0]["url"]],
                "detected_at": candidate["updated_at"],
            },
        },
    )
    main.evaluate(candidate["mode"])  # the monitor refreshes after recording a change
    assert (
        next(a for a in sequence(client)["alerts"] if a["id"] == alert["id"])["status"]
        == "withdrawn"
    )


def test_negative_feedback_holds_proposal_and_generic_edit_cannot_clear_it(client):
    alert = one_alert(client)
    ok(
        client.post(
            "/api/candidates/" + alert["candidate_id"] + "/feedback",
            json={
                "label": "wrong_time",
                "note": "This example does not establish a useful new conversation.",
            },
        )
    )
    assert (
        next(a for a in sequence(client)["alerts"] if a["id"] == alert["id"])["status"]
        == "withdrawn"
    )
    candidate = main.store.get(alert["candidate_id"])
    assert candidate["proposal_hold"]
    ok(
        client.put(
            "/api/candidates/" + candidate["id"],
            json={**candidate, "proposal_hold": None},
        )
    )
    assert main.store.get(candidate["id"])["proposal_hold"]
    assert (
        next(a for a in sequence(client)["alerts"] if a["id"] == alert["id"])["status"]
        == "withdrawn"
    )


def test_same_person_same_day_is_one_alert_across_roles_and_senders(client):
    roles = {
        key: main.role_for(key) for key in ("mts-research", "controller", "backend")
    }
    records = []
    for index, body in enumerate(scenarios(roles)[:2]):
        body["name"] = "Fictional Cross Role Person"
        body["profile_url"] = "https://example.org/fictional/shared-profile"
        body["evidence"][0]["url"] = "https://example.org/fictional/shared-work"
        if index == 0:
            body["fit"][0]["status"] = "unknown"
        candidate = ok(client.post("/api/candidates", json=body))
        records.append(candidate)
        ok(
            client.post(
                "/api/watches",
                json={
                    "candidate_id": candidate["id"],
                    "fit_basis": "Fictional role premise for testing cross-role notification deduplication.",
                },
            )
        )
    alerts = sequence(client)["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["matching_role_ids"] == ["controller", "mts-research"]
    assert main.store.get(alerts[0]["candidate_id"])["role_id"] == "controller"
    ok(
        client.post(
            "/api/candidates/" + records[0]["id"] + "/action",
            json={"action": "opt_out"},
        )
    )
    assert all(a["status"] == "withdrawn" for a in sequence(client)["alerts"])
