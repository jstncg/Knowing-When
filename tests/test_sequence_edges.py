"""Internal proposals must survive explicit follow-up and reject broken context.

All people, evidence, roles, and contact states here are fictional.
"""

from timing_helpers import INVITATION, assessment, purpose

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import contact
from app.models import Candidate, iso
from app.sequence import internal_proposal, occasion_key, refresh_alerts


NOW = datetime(2031, 6, 15, 12, tzinfo=timezone.utc)


def example():
    role = {
        "id": "research",
        "title": "Fictional Research Role",
        "jd_url": "https://example.test/job",
        "brief_version": 2,
        "criteria": ["Attributable technical work", "Relevant topic"],
        "required_criteria": ["Attributable technical work"],
    }
    candidate = Candidate.model_validate(
        {
            "name": "Example Researcher",
            "profile_url": "https://example.test/person",
            "role_id": role["id"],
            "mode": "live",
            "identity_status": "unknown",
            "fit": [
                {
                    "criterion": criterion,
                    "status": "supported",
                    "evidence_ids": ["work"],
                }
                for criterion in role["criteria"]
            ],
            "evidence": [
                {
                    "id": "work",
                    "url": "https://example.test/work",
                    "title": "Fictional milestone",
                    "excerpt": "I completed the benchmark on June 14, 2031. Please contact me on June 15, 2031 to discuss the evaluation protocol. " + INVITATION,
                    "observed_at": iso(NOW - timedelta(hours=1)),
                    "verified": False,
                }
            ],
            "events": [
                {
                    "id": "milestone",
                    "kind": "professional_update",
                    "title": "Released a benchmark",
                    "date": "2031-06-14",
                    "timing_action": "contact_now",
                    "timing_assessment": assessment("work"),
                    **purpose("work"),
                    "evidence_ids": ["work"],
                    "conversation_score": 3,
                    "confidence": "medium",
                    "recipient_value": "Compare independent action-consistency evaluations with a relevant technical peer.",
                    "why_now": "The newly released benchmark invites a concrete discussion of its evaluation protocol.",
                    "why_wait": "Their implementation may already answer the open question; verify that before contact.",
                    "falsifier": "The quoted invitation was not written by this person or does not concern this work.",
                }
            ],
            "draft": {
                "subject": "The action consistency benchmark",
                "body": "Your benchmark separates visual quality from action consistency. Would comparing the evaluation protocol with a technical peer be useful?",
                "sender_function": "Research leader",
                "source_ids": ["work"],
            },
        }
    ).model_dump()
    candidate.update(id="candidate:fictional", person_id="person:fictional", revision=1)
    watch = {
        "id": "watch:fictional",
        "candidate_id": candidate["id"],
        "role_id": role["id"],
        "mode": "live",
        "status": "active",
        "brief_version": 2,
        "fit_basis": "Fictional relevant work merits watching, with fit still requiring review.",
    }
    return candidate, role, watch


def follow_up(
    candidate,
    day="2031-06-15",
    quote="Please contact me on June 15, 2031 to discuss the evaluation protocol.",
):
    candidate["events"][0].update(
        timing_action="follow_up", follow_up_on=day, follow_up_quote=quote,
        **purpose("work", quote)
    )


@pytest.mark.parametrize(
    "day, wording", [("2031-06-15", "June 15, 2031"), ("2031-06-14", "June 14, 2031")]
)
def test_due_source_backed_follow_up_reaches_internal_inbox_without_faking_approval(
    day, wording
):
    candidate, role, watch = example()
    quote = f"Please contact me on {wording} to discuss the evaluation protocol."
    candidate["evidence"][0]["excerpt"] = quote
    follow_up(candidate, day, quote)
    before = deepcopy(candidate)
    proposal = internal_proposal(candidate, role, watch, now=NOW)
    assert proposal is not None
    assert proposal["ping"]["draft_outreach"]["review_status"] == "unreviewed"
    assert proposal["ping"]["trigger"]["last_verified_at"] is None
    assert (
        proposal["ping"]["draft_outreach"]["sender"]["identity_use_authorized"] is False
    )
    assert candidate == before


def test_explicit_future_request_blocks_competing_hook_but_ungrounded_request_does_not():
    candidate, role, watch = example()
    topical = deepcopy(candidate["events"][0])
    topical["id"] = "second-hook"
    quote = "Please contact me on June 20, 2031 to discuss the evaluation protocol."
    candidate["evidence"][0]["excerpt"] += " " + quote
    follow_up(candidate, "2031-06-20", quote)
    candidate["events"].append(topical)
    assert internal_proposal(candidate, role, watch, now=NOW) is None
    candidate["events"][0]["follow_up_quote"] = (
        "Invented defer date with no source support."
    )
    assert (
        internal_proposal(candidate, role, watch, now=NOW)["event_id"] == "second-hook"
    )


def test_due_follow_up_without_grounded_request_is_not_a_trigger():
    candidate, role, watch = example()
    follow_up(candidate, quote="Invented request unrelated to the cited evidence.")
    assert internal_proposal(candidate, role, watch, now=NOW) is None


@pytest.mark.parametrize("refs", [[], ["missing"], ["work", "missing"]])
def test_every_draft_reference_must_resolve(refs):
    candidate, role, watch = example()
    candidate["draft"]["source_ids"] = refs
    assert internal_proposal(candidate, role, watch, now=NOW) is None


@pytest.mark.parametrize("status", ["plausible_unconfirmed", "verified"])
def test_warm_route_cannot_be_reported_without_relationship_source(status):
    candidate, role, watch = example()
    candidate["route"].update(
        status=status, description="A claimed introducer", evidence_ids=[]
    )
    assert internal_proposal(candidate, role, watch, now=NOW) is None


@pytest.mark.parametrize(
    "where, changes",
    [
        ("role", {"id": "different-role"}),
        ("watch", {"role_id": "different-role"}),
        ("watch", {"mode": "simulation"}),
        ("watch", {"candidate_id": "candidate:different"}),
        ("watch", {"brief_version": 1}),
        ("watch", {"status": "paused"}),
        ("role", {"closed": True}),
    ],
)
def test_role_watch_and_mode_context_cannot_leak_into_another_proposal(where, changes):
    candidate, role, watch = example()
    {"role": role, "watch": watch}[where].update(changes)
    assert internal_proposal(candidate, role, watch, now=NOW) is None


@pytest.mark.parametrize("kind", ["never", "replied", "sent", "pinged"])
def test_person_contact_constraints_hold_before_internal_proposals(kind):
    candidate, role, watch = example()
    ledger = [contact.Entry(person_id="P", kind=kind, at=iso(NOW - timedelta(days=3))).model_dump()]
    assert internal_proposal(candidate, role, watch, ledger, now=NOW) is None


@pytest.mark.parametrize("field", ["source_change_pending", "proposal_hold"])
def test_pending_source_change_or_negative_feedback_prevents_old_note(field):
    candidate, role, watch = example()
    candidate[field] = {"reason": "Must resolve before reconsideration"}
    assert internal_proposal(candidate, role, watch, now=NOW) is None


@pytest.mark.parametrize("checked", [NOW + timedelta(days=1), NOW - timedelta(days=8)])
def test_future_or_stale_source_checks_do_not_support_proposals(checked):
    candidate, role, watch = example()
    candidate["evidence"][0]["last_checked_at"] = iso(checked)
    assert internal_proposal(candidate, role, watch, now=NOW) is None


def test_old_event_is_not_expired_by_an_invented_receptivity_window():
    candidate, role, watch = example()
    candidate["events"][0]["date"] = "2030-01-01"
    candidate["events"][0]["why_now"] = (
        "A newly confirmed request to discuss the older work establishes a current reason; the historical artifact date alone does not."
    )
    # Operational freshness remains enforced separately from event age.
    assert internal_proposal(candidate, role, watch, now=NOW) is not None


def test_missing_fit_rows_remain_visible_gaps_not_complete_evidence():
    candidate, role, watch = example()
    candidate["fit"] = candidate["fit"][:1]
    proposal = internal_proposal(candidate, role, watch, now=NOW)
    assert proposal["fit_gaps"] == ["Relevant topic"]
    assert proposal["ping"]["confidence"]["fit"] == "low"


def test_legacy_person_without_id_does_not_share_an_occasion_with_other_people():
    first, _, _ = example()
    second = deepcopy(first)
    first.pop("person_id")
    second.pop("person_id")
    second["profile_url"] = "https://example.test/different-person"
    assert occasion_key(first, first["events"][0]) != occasion_key(
        second, second["events"][0]
    )


class MemoryStore:
    def __init__(self):
        self.records = {}

    def put(self, key, kind, record):
        self.records[key] = (kind, deepcopy({**record, "id": key}))

    def get(self, key):
        return deepcopy(self.records[key][1]) if key in self.records else None

    def all(self, kind):
        return [
            deepcopy(record)
            for stored_kind, record in self.records.values()
            if stored_kind == kind
        ]


def test_cross_role_choice_counts_missing_criteria_instead_of_rewarding_short_fit_lists():
    candidate, role, watch = example()
    alternative, alternative_role, alternative_watch = deepcopy(
        (candidate, role, watch)
    )
    alternative_role.update(
        id="other-role",
        criteria=[
            "Attributable technical work",
            "Relevant topic",
            "Systems experience",
        ],
    )
    alternative.update(
        id="candidate:alternative", role_id="other-role", fit=alternative["fit"][:1]
    )
    alternative_watch.update(
        id="watch:alternative", candidate_id=alternative["id"], role_id="other-role"
    )
    roles = {item["id"]: item for item in (role, alternative_role)}
    store = MemoryStore()
    for record in (candidate, alternative):
        store.put(record["id"], "candidate", record)
    for record in (watch, alternative_watch):
        store.put(record["id"], "watch", record)
    assert refresh_alerts(store, "live", roles.__getitem__, NOW) == 1
    alert = store.all("review_alert")[0]
    assert alert["candidate_id"] == candidate["id"]
    assert alert["matching_role_ids"] == ["other-role", "research"]
    assert refresh_alerts(store, "live", roles.__getitem__, NOW) == 0


def test_one_person_has_one_alert_at_a_time_and_at_most_one_new_a_day():
    candidate, role, watch = example()
    other, other_role, other_watch = deepcopy((candidate, role, watch))
    other_role["id"] = "other-role"
    other.update(id="candidate:other", role_id="other-role")
    other["events"][0].update(  # another occasion: a different event day
        date="2030-01-01",
        why_now="A newly confirmed request to discuss the older work establishes a current reason; the historical artifact date alone does not.",
    )
    other_watch.update(id="watch:other", candidate_id=other["id"], role_id="other-role")
    roles = {item["id"]: item for item in (role, other_role)}

    def fresh():
        store = MemoryStore()
        for kind, record in (("candidate", candidate), ("candidate", other), ("watch", watch), ("watch", other_watch)):
            store.put(record["id"], kind, record)
        return store

    def run(store, hours):
        return refresh_alerts(store, "live", roles.__getitem__, NOW + timedelta(hours=hours))

    store = fresh()
    assert run(store, 0) == 1
    first = store.all("review_alert")[0]
    assert first["matching_role_ids"] == ["other-role", "research"]
    # The other role now ranks higher: the alert stays while its occasion still proposes, a day later too.
    store.put(candidate["id"], "candidate", {**candidate, "fit": candidate["fit"][:1]})
    assert run(store, 1) == 0
    assert run(store, 25) == 0
    assert store.all("review_alert") == [first]
    # Once it stops proposing, the other occasion gets an alert: a day has passed since the first.
    store.put(watch["id"], "watch", {**watch, "status": "paused"})
    assert run(store, 26) == 1
    assert sorted(a["status"] for a in store.all("review_alert")) == ["proposed", "withdrawn"]

    # Within a day of the last alert nothing replaces it, whatever the calendar day. The alert counts for its
    # candidate's person, so linking the records under one id keeps the cap.
    store = fresh()
    assert run(store, 0) == 1
    first = store.all("review_alert")[0]
    for record in (candidate, other):
        store.put(record["id"], "candidate", {**record, "person_id": "person:linked"})
    assert run(store, 1) == 0  # the same occasion under a new key keeps its alert
    assert [(a["id"], a["status"]) for a in store.all("review_alert")] == [(first["id"], "proposed")]
    store.put(watch["id"], "watch", {**watch, "status": "paused"})
    assert run(store, 2) == 0
    assert run(store, 23) == 0
    assert [a["status"] for a in store.all("review_alert")] == ["withdrawn"]
    assert run(store, 24) == 1

    # A re-dated event keeps its alert, and the alert then follows the occasion it moved onto.
    store = fresh()
    assert run(store, 0) == 1
    first = store.all("review_alert")[0]
    moved = {k: other["events"][0][k] for k in ("date", "why_now")}
    store.put(candidate["id"], "candidate",  # the real store bumps the revision on every write
              {**candidate, "revision": 2, "events": [{**candidate["events"][0], **moved}]})
    assert run(store, 1) == 0
    store.put(watch["id"], "watch", {**watch, "status": "paused"})
    assert run(store, 2) == 0
    assert [(a["id"], a["status"], a["candidate_id"]) for a in store.all("review_alert")] == [
        (first["id"], "proposed", other["id"])]


def test_internal_proposal_does_not_inherit_an_expired_outreach_review():
    candidate, role, watch = example()
    candidate["reviewed_at"] = iso(NOW - timedelta(days=20))
    proposal = internal_proposal(candidate, role, watch, now=NOW)
    assert proposal is not None
    assert proposal["ping"]["trigger"]["last_verified_at"] is None
    assert proposal["ping"]["draft_outreach"]["review_status"] == "unreviewed"
    assert proposal["ping"]["expires_at"] == iso(
        NOW - timedelta(hours=1) + timedelta(days=7)
    )


@pytest.mark.parametrize("case", ["legacy", "paper_only", "company_only", "invented_quote", "missing_waiting_cost", "unlinked_evidence"])
def test_contact_now_requires_inspectable_person_specific_timing_assessment(case):
    candidate, role, watch = example()
    event = candidate["events"][0]
    if case == "legacy":
        event.pop("timing_assessment")
    elif case == "paper_only":
        event["kind"] = "paper_release"
        candidate["evidence"][0]["excerpt"] = "The researcher published a relevant paper on June 14, 2031."
    elif case == "company_only":
        event["timing_assessment"]["mechanism"] = "context_only"
    elif case == "invented_quote":
        event["timing_assessment"]["waiting_cost"]["quote"] = "I will stop taking external input tomorrow."
    elif case == "missing_waiting_cost":
        del event["timing_assessment"]["waiting_cost"]
    else:
        event["timing_assessment"]["person_impact"]["evidence_id"] = "missing"
    assert internal_proposal(candidate, role, watch, now=NOW) is None


def test_explicit_current_need_is_internal_hypothesis_not_verified_reachability():
    candidate, role, watch = example()
    result = internal_proposal(candidate, role, watch, now=NOW)
    assert result is not None
    assert result["ping"]["trigger"]["last_verified_at"] is None
    assert result["ping"]["draft_outreach"]["review_status"] == "unreviewed"
    assert candidate["events"][0]["career_openness"] == "unknown"
