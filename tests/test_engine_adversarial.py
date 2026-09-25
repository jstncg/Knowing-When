"""Adversarial contract tests for timing decisions; all data is fictional."""

from timing_helpers import INVITATION, assessment, purpose

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

# The repository is intentionally not packaged; make this standalone test runnable
# both as ``pytest`` and as an explicitly selected test file.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.contact import Entry
from app.engine import decide, review_hash
from app.models import Event, parse_time


NOW = datetime(2031, 6, 15, 12, tzinfo=timezone.utc)


def stamp(delta: timedelta = timedelta()) -> str:
    return (NOW + delta).isoformat().replace("+00:00", "Z")


def role(role_id="fictional-controller", brief_version=1):
    return {
        "id": role_id,
        "title": "Fictional Global Controller",
        "jd_url": "https://jobs.example.test/fictional-controller",
        "brief_version": brief_version,
        "criteria": ["close", "automation", "audit"],
        "required_criteria": ["close", "automation"],
    }


def candidate(mode="live"):
    record = {
        "name": "Avery Example",
        "profile_url": "https://profiles.example.test/avery",
        "role_id": "fictional-controller",
        "mode": mode,
        "summary": "Fictional person used only for engine tests.",
        "identity_status": "verified",
        "evidence": [
            {
                "id": "close-source",
                "url": "https://sources.example.test/close",
                "title": "Fictional close evidence",
                "excerpt": "Avery led a documented close automation program. " + INVITATION,
                "observed_at": stamp(timedelta(days=-1)),
                "event_date": "2031-06-14",
                "verified": True,
                "source_kind": "public_web",
            },
            {
                "id": "automation-source",
                "url": "https://sources.example.test/automation",
                "title": "Fictional automation evidence",
                "excerpt": "Avery owned the reporting automation work.",
                "observed_at": stamp(timedelta(days=-1)),
                "event_date": "2031-06-14",
                "verified": True,
                "source_kind": "public_web",
            },
        ],
        "fit": [
            {
                "criterion": "close",
                "status": "supported",
                "evidence_ids": ["close-source"],
            },
            {
                "criterion": "automation",
                "status": "supported",
                "evidence_ids": ["automation-source"],
            },
            {
                "criterion": "audit",
                "status": "supported",
                "evidence_ids": ["close-source"],
            },
        ],
        "events": [
            {
                "id": "fictional-event",
                "kind": "professional_update",
                "title": "Fictional, date-supported professional update",
                "date": "2031-06-14",
                "timing_action": "contact_now",
                "timing_assessment": assessment("close-source"),
                **purpose("close-source"),
                "recipient_value": "Compare this documented professional approach with the relevant role scope at GI.",
                "why_now": "The stated update creates a specific professional conversation this week.",
                "why_wait": "Waiting until a later public update would test whether this remains relevant.",
                "falsifier": "A corrected source saying the stated update did not occur would disprove this premise.",
                "evidence_ids": ["close-source"],
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
            "subject": "Fictional close automation conversation",
            "body": "This fictional test draft is deliberately long enough to meet the engine's content threshold while remaining unrelated to any real person or company.",
            "sender_function": "CEO / operations leader",
            "sender_name": "",
            "channel": "email",
            "source_ids": ["close-source"],
        },
        "reviewed_at": stamp(),
    }
    record["review_hash"] = review_hash(record, role())
    return record


def ready_candidate(mode="live"):
    return candidate(mode=mode)


def reasons(result):
    return " ".join(result["reasons"])


def test_missing_event_evidence_id_cannot_create_a_timing_opportunity():
    record = ready_candidate()
    record["events"][0]["evidence_ids"] = ["missing-source"]

    result = decide(record, role(), now=NOW)

    assert result["state"] == "research"
    assert "supported date and attributable evidence" in reasons(result)
    assert result["ping"] is None


def test_conflicting_required_fit_blocks_ready_even_with_supported_evidence():
    record = ready_candidate()
    record["fit"].append(
        {
            "criterion": "close",
            "status": "contradicted",
            "evidence_ids": ["close-source"],
        }
    )
    record["review_hash"] = review_hash(record, role())

    result = decide(record, role(), now=NOW)

    assert result["state"] == "research"
    assert "contradicts a required role criterion" in reasons(result)
    assert result["ping"] is None


def test_old_event_without_new_why_now_is_watched():
    record = ready_candidate()
    record["events"][0].update(timing_action="watch", date="2030-01-01")
    record["review_hash"] = review_hash(record, role())
    result = decide(record, role(), now=NOW)
    assert result["state"] == "watch"
    assert result["ping"] is None


def test_explicit_future_follow_up_is_honored_after_review():
    record = ready_candidate()
    quote = "Please contact me after 2031-06-19 to discuss this project."
    record["evidence"][0]["excerpt"] = quote
    record["events"][0].update(
        timing_action="follow_up", follow_up_on="2031-06-19", follow_up_quote=quote
    )
    record["review_hash"] = review_hash(record, role())
    result = decide(record, role(), now=NOW)
    assert result["state"] == "watch"
    assert result["event_id"] == "fictional-event"
    assert parse_time(result["next_check_at"]) == NOW + timedelta(days=3)
    assert result["ping"] is None


def test_role_brief_version_change_invalidates_prior_review():
    record = ready_candidate()

    result = decide(record, role(brief_version=2), now=NOW)

    assert result["state"] == "needs_review"
    assert "current role brief" in reasons(result)
    assert result["ping"] is None


@pytest.mark.parametrize(
    "other_role",
    [role_id for role_id in ("fictional-research", "fictional-controller")],
)
def test_opt_out_suppresses_the_same_person_for_every_role(other_role):
    record = ready_candidate()
    record["role_id"] = other_role
    record["review_hash"] = review_hash(record, role(other_role))

    never = Entry(person_id="p", kind="never", at="2031-06-01T00:00:00+00:00", role_id="fictional-backend")
    result = decide(record, role(other_role), [never.model_dump()], now=NOW)

    assert result["state"] == "suppressed"
    assert "every role" in reasons(result)
    assert result["ping"] is None


def test_model_normalizes_timezone_and_rejects_invalid_event_dates():
    assert parse_time("2031-06-15T08:00:00-04:00") == NOW


def test_model_rejects_invalid_event_date():
    with pytest.raises(ValidationError):
        Event(id="invalid-day", title="Fictional", date="2031-02-30")


@pytest.mark.parametrize(
    "legacy_start,legacy_end",
    [
        (stamp(timedelta(days=4)), stamp(timedelta(days=8))),
        (stamp(timedelta(days=-10)), stamp(timedelta(days=-5))),
        (stamp(timedelta(days=2)), stamp(timedelta(days=1))),
    ],
)
def test_legacy_windows_neither_block_nor_expire_a_reviewed_contact_now(
    legacy_start, legacy_end
):
    record = ready_candidate()
    record["events"][0].update(window_start=legacy_start, window_end=legacy_end)
    record["review_hash"] = review_hash(record, role())
    result = decide(record, role(), now=NOW)
    assert result["state"] == "ready"
    assert result["ping"]["expiry_basis"] == "review_revalidation_policy"
    assert result["ping"]["expires_at"] != legacy_end


def test_duplicate_source_urls_are_deduplicated_in_released_trigger():
    record = ready_candidate()
    duplicate = deepcopy(record["evidence"][0])
    duplicate["id"] = "duplicate-url-source"
    record["evidence"].append(duplicate)
    record["events"][0]["evidence_ids"].append("duplicate-url-source")
    record["review_hash"] = review_hash(record, role())

    result = decide(record, role(), now=NOW)

    assert result["state"] == "ready"
    assert result["ping"]["trigger"]["source_urls"] == [
        "https://sources.example.test/close"
    ]


def test_candidate_cannot_be_released_for_a_different_role_id():
    record = ready_candidate()
    unrelated = role("fictional-research")
    record["review_hash"] = review_hash(record, unrelated)

    result = decide(record, unrelated, now=NOW)

    assert result["state"] != "ready"
    assert result["ping"] is None


def test_live_and_simulation_pings_keep_separate_delivery_modes():
    live = decide(ready_candidate("live"), role(), now=NOW)
    simulation = decide(ready_candidate("simulation"), role(), now=NOW)

    assert live["state"] == simulation["state"] == "ready"
    assert live["ping"]["mode"] == "live"
    assert simulation["ping"]["mode"] == "synthetic-fixture"
    assert live["ping"]["person"]["id"] != simulation["ping"]["person"]["id"]


def test_unreviewed_follow_up_cannot_silently_defer_opportunity():
    record = ready_candidate()
    quote = "Please contact me on 2031-06-19 about this project."
    record["evidence"][0]["excerpt"] = quote
    record["events"][0].update(
        timing_action="follow_up", follow_up_on="2031-06-19", follow_up_quote=quote
    )
    record["review_hash"] = None
    result = decide(record, role(), now=NOW)
    assert result["state"] == "needs_review"
    assert "before deferring" in reasons(result)
    assert result["ping"] is None


def test_future_conference_can_justify_arranging_a_meeting_now():
    record = ready_candidate()
    record["events"][0].update(
        kind="conference",
        date="2031-06-25",
        recipient_value="Arrange a discussion of their relevant benchmark during a confirmed professional event.",
    )
    record["review_hash"] = review_hash(record, role())
    assert decide(record, role(), now=NOW)["state"] == "ready"


def test_refreshing_source_does_not_extend_old_contact_now_approval():
    record = ready_candidate()
    record["reviewed_at"] = stamp(timedelta(days=-8))
    for ev in record["evidence"]:
        ev["last_checked_at"] = stamp()
    result = decide(record, role(), now=NOW)
    assert result["state"] == "needs_review"
    assert "review validity" in reasons(result)


def test_contact_now_requires_recipient_benefit_not_just_recentness():
    record = ready_candidate()
    record["events"][0]["recipient_value"] = ""
    record["review_hash"] = review_hash(record, role())
    assert decide(record, role(), now=NOW)["state"] == "research"


def test_model_chosen_follow_up_date_without_source_request_fails_closed():
    record = ready_candidate()
    record["events"][0].update(
        timing_action="follow_up",
        follow_up_on="2031-06-19",
        follow_up_quote="Please contact me on 2031-06-19.",
    )
    record["review_hash"] = review_hash(record, role())
    assert decide(record, role(), now=NOW)["state"] == "research"


@pytest.mark.parametrize("quote", [
    "Please don't contact me before July 20, 2031.",
    "I'm on sabbatical until July 20, 2031; happy to talk after that.",
    "My deadline is July 20, 2031. Reach out then.",
])
def test_a_reviewed_request_to_wait_holds_every_hook_whatever_its_wording(quote):
    from app.sequence import internal_proposal

    record = ready_candidate()
    record.update(id="candidate:avery", revision=1)
    wait = deepcopy(record["events"][0])
    record["evidence"][0]["excerpt"] += " " + quote
    wait.update(id="wait", timing_action="follow_up", follow_up_on="2031-07-20", follow_up_quote=quote)
    record["events"].append(wait)
    record["review_hash"] = review_hash(record, role())
    result = decide(record, role(), now=NOW)
    assert (result["state"], result["event_id"]) == ("watch", "wait") and result["ping"] is None
    assert internal_proposal(record, role(), {"status": "active", "brief_version": 1, "fit_basis": "Fictional"}, now=NOW) is None


def test_a_hook_the_kill_pass_never_reached_carries_no_ping():
    record = ready_candidate()
    record["events"][0]["kill_pass"] = {"entailment": None, "superseded_by": [], "checked": False, "sources_checked": 0,
                                        "errors": ["Per-run provider call limit reached; increase it in Settings to expand research."]}
    record["review_hash"] = review_hash(record, role())
    result = decide(record, role(), now=NOW)
    assert result["state"] == "research" and "the kill pass has not finished checking this event" in reasons(result)
    record["events"][0]["kill_pass"]["checked"] = True
    record["review_hash"] = review_hash(record, role())
    assert decide(record, role(), now=NOW)["state"] == "ready"


def test_legacy_event_without_explicit_action_requires_reassessment():
    record = ready_candidate()
    record["events"][0].pop("timing_action")
    record["events"][0].update(
        window_start=stamp(), window_end=stamp(timedelta(days=3))
    )
    record["review_hash"] = review_hash(record, role())
    result = decide(record, role(), now=NOW)
    assert result["state"] == "research"
    assert "Legacy" in reasons(result)


@pytest.mark.parametrize(
    "kind",
    [
        "company_news",
        "layoffs",
        "volunteer_activity",
        "personal_activity",
        "passive_engagement",
    ],
)
def test_contextual_or_personal_activity_alone_never_releases_a_ping(kind):
    record = ready_candidate()
    record["events"][0]["kind"] = kind
    record["review_hash"] = review_hash(record, role())
    assert decide(record, role(), now=NOW)["ping"] is None


def test_explicit_request_to_wait_cannot_be_bypassed_by_another_hook():
    record = ready_candidate()
    future = deepcopy(record["events"][0])
    quote = "Please contact me after 2031-06-19 to discuss this project."
    record["evidence"][0]["excerpt"] += " " + quote
    future.update(
        id="future",
        timing_action="follow_up",
        follow_up_on="2031-06-19",
        follow_up_quote=quote,
    )
    record["events"].insert(0, future)
    record["review_hash"] = review_hash(record, role())
    assert decide(record, role(), now=NOW)["state"] == "watch"


def test_legacy_contact_now_cannot_be_ready_without_grounded_timing_assessment():
    record = candidate()
    record["events"][0].pop("timing_assessment")
    result = decide(record, role(), now=NOW)
    assert result["state"] == "watch"
    assert result["ping"] is None
    assert "person impact" in " ".join(result["reasons"])
