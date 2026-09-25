"""Purpose separation and existing rails; no claim of human receptivity accuracy."""
import pytest
from pydantic import ValidationError

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import decide, review_hash
from app.sequence import internal_proposal
from app.providers import _sanitize
from test_engine_adversarial import candidate, role, NOW
from test_sequence_edges import example, follow_up
from test_providers import timing_proposal


@pytest.mark.parametrize("purpose", [None, "unassessed", "recruiting"])
def test_unassessed_or_invalid_purpose_never_becomes_ready(purpose):
    c = candidate()
    c["events"][0]["contact_purpose"] = purpose
    if purpose in (None, "recruiting"):
        with pytest.raises(ValidationError):
            review_hash(c, role())
        return
    c["review_hash"] = review_hash(c, role())
    assert decide(c, role(), now=NOW)["ping"] is None


def test_rapport_ping_retains_unknown_career_openness_and_its_purpose():
    c = candidate()
    c["events"][0]["career_openness"] = "unknown"
    c["review_hash"] = review_hash(c, role())
    result = decide(c, role(), now=NOW)
    assert result["state"] == "ready"
    assert result["ping"]["contact_purpose"] == "rapport"
    assert result["ping"]["purpose_reason"] == c["events"][0]["purpose_reason"]
    assert result["career_openness"] == "unknown"


def test_new_purpose_invalidates_prior_review():
    c = candidate()
    c["events"][0]["contact_purpose"] = "hiring"
    assert decide(c, role(), now=NOW)["state"] != "ready"


@pytest.mark.parametrize("due_followup", [False, True])
def test_missing_purpose_blocks_internal_ping_including_due_followup(due_followup):
    c, r, w = example()
    if due_followup:
        follow_up(c)
    c["events"][0].pop("purpose_reason")
    assert internal_proposal(c, r, w, now=NOW) is None


@pytest.mark.parametrize("change", ["missing", "invented_quote", "wrong_source", "unknown_purpose"])
def test_provider_cannot_normalize_unsupported_purpose_into_contact_now(change):
    raw, doc = timing_proposal()
    event = raw["events"][0]
    if change == "missing":
        event.pop("purpose_reason")
    elif change == "invented_quote":
        event["purpose_reason"]["quote"] = "I am considering a role at General Intuition right now."
    elif change == "wrong_source":
        event["purpose_reason"]["evidence_id"] = "different-person"
    else:
        event["contact_purpose"] = "unassessed"
    assert _sanitize(raw, [doc], {})["events"][0]["timing_action"] == "watch"


def test_statement_timestamp_dates_invitation_not_its_deadline():
    raw, doc = timing_proposal()
    doc["published_at"] = "2026-09-19T10:00:00Z"
    doc["observed_at"] = "2026-09-20T12:00:00Z"
    raw["events"][0].update(date="2026-09-19", date_basis="source_statement_date",
                            date_source_id=doc["id"], date_quote=doc["published_at"])
    result = _sanitize(raw, [doc], {})
    event = result["events"][0]
    assert event["timing_action"] == "contact_now"
    assert event["date"] == "2026-09-19"
    assert event["date_basis"] == "source_statement_date"
    assert "not the date of an underlying job change or deadline" in event["date_caveat"]
    assert result["evidence"][0]["published_at"] == doc["published_at"]


@pytest.mark.parametrize("mistake", ["future_publication", "mismatched_day", "missing_timestamp", "wrong_source", "invalid_timestamp"])
def test_unsupported_statement_dates_fail_closed(mistake):
    raw, doc = timing_proposal()
    doc["published_at"] = "2026-09-19T10:00:00Z"
    doc["observed_at"] = "2026-09-20T12:00:00Z"
    event = raw["events"][0]
    event.update(date="2026-09-19", date_basis="source_statement_date",
                 date_source_id=doc["id"], date_quote=doc["published_at"])
    if mistake == "future_publication":
        doc["observed_at"] = "2026-09-18T00:00:00Z"
    elif mistake == "mismatched_day":
        event["date"] = "2026-09-20"
    elif mistake == "missing_timestamp":
        doc.pop("published_at")
    elif mistake == "wrong_source":
        event["date_source_id"] = "not-cited"
    else:
        doc["published_at"] = event["date_quote"] = "2026-09-19invalid"
    accepted = _sanitize(raw, [doc], {})
    assert accepted["events"][0]["timing_action"] == "watch"
    assert accepted["draft"]["body"] == ""


def test_quiet_decision_removes_model_draft():
    raw, doc = timing_proposal(timing_action="watch")
    raw["draft"] = {"subject": "A tempting pitch", "body": "Can we discuss your work anyway?", "source_ids": [doc["id"]]}
    result = _sanitize(raw, [doc], {})
    assert result["draft"]["body"] == result["draft"]["subject"] == ""


def test_future_contact_constraint_survives_missing_purpose_and_blocks_other_event():
    raw, doc = timing_proposal()
    quote = "On September 20, 2026, please contact me after October 4, 2026 to discuss the protocol."
    doc["text"] += " " + quote
    raw["evidence"][0]["excerpt"] = doc["text"]
    from copy import deepcopy
    deferred = deepcopy(raw["events"][0])
    deferred.update(timing_action="follow_up", follow_up_on="2026-10-04", follow_up_quote=quote,
                    contact_purpose="unassessed", purpose_reason=None)
    raw["events"].append(deferred)
    accepted = _sanitize(raw, [doc], {})["events"]
    assert accepted[-1]["timing_action"] == "follow_up"
    assert accepted[-1]["follow_up_quote"] == quote
    c, r, w = example()
    c["events"].append({**c["events"][0], "id": "future-constraint", "timing_action": "follow_up",
        "follow_up_on": "2031-06-20", "follow_up_quote": "Please contact me after June 20, 2031 to discuss the protocol.",
        "contact_purpose": "unassessed", "purpose_reason": None})
    c["evidence"][0]["excerpt"] += " " + c["events"][-1]["follow_up_quote"]
    assert internal_proposal(c, r, w, now=NOW) is None
