"""Pure timing decisions. Models propose; this module enforces the contract."""

import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from . import contact
from .models import (
    Candidate, canonical_url, iso, parse_time, utcnow, timing_assessment_supported,
    contact_purpose_supported, follow_up_supported,
)

POLICY_VERSION = 7
REVIEW_VALID_DAYS = 7  # Operational revalidation policy, not a receptivity forecast.
PING_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schemas/ping.schema.json").read_text()
)
VALIDATOR = Draft202012Validator(PING_SCHEMA, format_checker=FormatChecker())


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]


def person_key(candidate):
    return (
        f"person:{candidate['mode']}:{digest(canonical_url(candidate['profile_url']))}"
    )


def person_of(candidate):
    """The person a candidate record belongs to: its linked person, else its own profile."""
    return candidate.get("person_id") or person_key(candidate)


def review_hash(candidate, role):
    content = Candidate.model_validate(candidate).model_dump()
    for e in content["evidence"]:
        e.pop("last_checked_at", None)
    return digest([content, role["brief_version"], POLICY_VERSION])


def event_key(candidate, event):
    # Roles and URLs of syndicated articles do not create new contact occasions.
    title = re.sub(r"\W+", " ", event["title"].lower()).strip()
    return digest(
        [
            person_of(candidate),
            event["date"],
            event["kind"],
            title,
        ]
    )


def evidence_for(candidate, ids):
    wanted = set(ids)
    return [e for e in candidate["evidence"] if e["id"] in wanted]


CONTEXT_ONLY = frozenset({
    "company_news", "layoffs", "passive_engagement", "tenure", "personal",
    "volunteer", "personal_activity", "volunteer_activity",
})


def event_basics_failure(event, evidence):
    if not event.get("date") or not evidence:
        return "A trigger needs a supported date and attributable evidence."
    if event["kind"] in CONTEXT_ONLY:
        return "This does not establish a person-specific professional reason for contact now."
    return None


def event_merit_failure(event, evidence):
    """Whether the event can carry a ping: what the conversation is for and what the person gains."""
    if not contact_purpose_supported(event, evidence):
        return "Reassess the contact purpose: cite why the evidence supports building rapport or discussing hiring. A technical opening is not hiring readiness."
    if len(event.get("recipient_value", "").strip()) < 15:
        return "Describe a specific professional benefit to this person; recency alone is insufficient."
    if event["conversation_score"] < 2 or event["confidence"] == "low":
        return "The evidence does not yet support a strong conversation opportunity."
    if min(len(event.get(k, "").strip()) for k in ("why_now", "why_wait", "falsifier")) < 15:
        return "Explain why now, the case for waiting, and what would disprove the opportunity."
    return None


def draft_route_failure(candidate):
    known = {e["id"] for e in candidate["evidence"]}
    draft, route = candidate["draft"], candidate["route"]
    if (
        len(draft["subject"].strip()) < 5
        or len(draft["body"].strip()) < 60
        or not draft["source_ids"]
        or not set(draft["source_ids"]) <= known
    ):
        return "A complete, evidence-linked subject and outreach body are required."
    if not draft["sender_function"].strip():
        return "Select a relevant senior sender function."
    if route["status"] != "none_found" and (
        not route["evidence_ids"] or not set(route["evidence_ids"]) <= known
    ):
        return "The proposed warm route has no relationship evidence."
    return None


def contradicts_required(candidate, role):
    required = set(role.get("required_criteria", role["criteria"][:2]))
    return any(f["status"] == "contradicted" and f["criterion"] in required for f in candidate["fit"])


def hook_order(events):
    """The order decide tries hooks in, and the kill pass spends its calls in: contact-now first, then the strongest conversation."""
    return sorted(events, key=lambda e: (e.get("timing_action") == "contact_now", e.get("conversation_score", 0)), reverse=True)


def next_check(candidate, now):
    # Source polling cadence only; never an estimate of someone's receptivity.
    active = any(e.get("timing_action") == "contact_now" for e in candidate["events"])
    next_at = now + timedelta(hours=6 if active else 72)
    for event in candidate["events"]:
        ev = evidence_for(candidate, event["evidence_ids"])
        if event.get("timing_action") == "follow_up" and follow_up_supported(event, ev):
            due = parse_time(event["follow_up_on"])
            if due > now:
                next_at = min(next_at, due)
    return iso(next_at)


def revalidate_at(candidate, evidence):
    # Stable between polls so reading the inbox cannot extend its approval.
    times = [parse_time(e.get("last_checked_at") or e["observed_at"]) for e in evidence]
    if candidate.get("reviewed_at"):
        times.append(parse_time(candidate["reviewed_at"]))
    return iso(min(times) + timedelta(days=REVIEW_VALID_DAYS))


def decide(candidate, role, contacts=(), now=None, readiness=None, reviewed=True):
    """The contact decision for one candidate as of ``now``.

    ``contacts`` is the person's contact ledger (contact.history); contact.check
    holds anyone it says to leave alone, across roles, before any timing is read.

    ``readiness`` (journey.assess as of ``now``) decides the timing whenever the person has a
    timeline, so this shows the verdict the Slack card gives; the model's events
    then only supply the hook a ping and its draft rest on. Without one, the reviewed model
    judgment on each event stands alone. ``reviewed=False`` is the internal proposal gate
    (sequence.internal_proposal): the same checks without the human review gates, and an
    unreviewed ping.
    """
    now = now or utcnow()
    reasons = []
    supported = [
        f
        for f in candidate["fit"]
        if f["status"] == "supported" and evidence_for(candidate, f["evidence_ids"])
    ]
    fit_score = round(
        100
        * len({f["criterion"] for f in supported} & set(role["criteria"]))
        / max(len(role["criteria"]), 1)
    )
    result = {
        "state": "research",
        "reasons": reasons,
        "fit_score": fit_score,  # Compatibility: criterion coverage, not suitability.
        "fit_supported": len(
            {f["criterion"] for f in supported} & set(role["criteria"])
        ),
        "fit_total": len(role["criteria"]),
        "conversation_score": 0,
        "career_openness": "unknown",
        "priority": "routine",
        "event_id": None,
        "next_check_at": next_check(candidate, now),
        "ping": None,
        **({"readiness": readiness.model_dump()} if readiness is not None else {}),
    }

    def stop(state, reason):
        result["state"] = state
        reasons.append(reason)
        return result

    if candidate["role_id"] != role["id"]:
        return stop("research", "Candidate and role brief do not match.")

    gate = contact.check(contacts, now, readiness, role_id=role["id"])
    if gate.state != "clear":
        return stop("suppressed" if gate.state == "hold" else "research", gate.reason)
    if candidate.get("dismissed"):
        return stop(
            "suppressed", "Dismissed by reviewer. Reopen explicitly to reconsider."
        )
    if role.get("closed"):
        return stop("suppressed", "Role is closed.")
    if candidate.get("snooze_until") and parse_time(candidate["snooze_until"]) > now:
        result["next_check_at"] = candidate["snooze_until"]
        return stop("snoozed", "Waiting until the reviewer’s requested date.")
    if candidate["identity_status"] == "conflict":
        return stop(
            "research",
            "Conflicting identity evidence. Resolve the person before evaluating timing.",
        )
    if readiness is not None:
        if readiness.action == "verify_first":
            return stop("needs_review", "Verify first: " + readiness.explanation
                        + " Check: " + " ".join(readiness.falsifiers))
        if readiness.action != "reach_now":
            return stop("watch", "Keep watching: " + readiness.explanation)
    if not candidate["events"]:
        if readiness is not None:
            return stop("research", "Reach now: " + readiness.explanation
                        + " Research has found nothing to open the conversation with yet.")
        return stop(
            "watch",
            "No verified timing event yet. Fit evidence alone does not establish why this week.",
        )

    # A source-backed request to wait must not be bypassed by another topical hook. Research
    # also puts the model's on the timeline (extraction.record_follow_ups), where readiness
    # holds it; this also holds one typed into the record, and a record researched before that.
    for event in candidate["events"]:
        ev = evidence_for(candidate, event["evidence_ids"])
        if event.get("timing_action") == "follow_up" and follow_up_supported(event, ev):
            if parse_time(event["follow_up_on"]).date() > now.date():
                if candidate.get("review_hash") != review_hash(candidate, role):
                    return stop(
                        "needs_review",
                        "Confirm the source's explicit contact instruction before deferring this person.",
                    )
                result.update(event_id=event["id"])
                return stop(
                    "watch", "Waiting for the source-backed requested follow-up date."
                )

    events = hook_order(candidate["events"])
    failures = []
    for event in events:
        ev = evidence_for(candidate, event["evidence_ids"])
        if failure := event_basics_failure(event, ev):
            failures.append(failure)
            continue
        action = event.get("timing_action", "watch")
        # Readiness decides timing; the model's watch event is the hook once it earns a ping
        # below and the kill pass has checked it.
        promoted = readiness is not None and event.get("timing_action") == "watch"
        if promoted:
            event, action = {**event, "timing_action": "contact_now"}, "contact_now"
        if action == "watch":
            failures.append(
                "Keep watching: a dated fact alone does not establish a useful conversation now."
                if "timing_action" in event
                else "Legacy timing proposal needs reassessment; invented windows are no longer used."
            )
            continue
        if action == "follow_up":  # A supported one still in the future returned above: this one is due.
            if not follow_up_supported(event, ev):
                failures.append(
                    "A follow-up date needs the explicit contact request or constraint quoted with its date in the cited evidence."
                )
                continue
            if reviewed and candidate.get("review_hash") != review_hash(candidate, role):
                return stop(
                    "needs_review",
                    "Confirm the source's explicit contact instruction before deferring this person.",
                )
        if action == "verify_first":
            failures.append(
                "Verify first: the kill pass found the trigger contradicted or superseded. See the event's kill-pass findings."
            )
            continue
        if action not in ("contact_now", "follow_up"):
            failures.append("Timing action is not recognized; reassess this event.")
            continue
        if readiness is None and action == "contact_now" and not timing_assessment_supported(event, ev):
            failures.append(
                "Keep watching: why now needs source-grounded person impact, a conversational opportunity, and a reason waiting changes that opportunity."
            )
            continue
        if failure := event_merit_failure(event, ev):
            failures.append(failure)
            continue
        # A note (readiness track "rapport") opens with their work; a hook whose purpose is hiring would pitch.
        if readiness is not None and readiness.track == "rapport" and event.get("contact_purpose") == "hiring":
            failures.append("A note about their work only: this hook's purpose is hiring. Research a hook about "
                            "their work; this one waits for a reason that makes the call a pitch.")
            continue
        # A hook the kill pass never reached (its call cap ran out first) is unchecked, not passed.
        kill = event.get("kill_pass")
        if (promoted and not kill) or (kill and kill.get("checked") is False):
            failures.append("Research again: the kill pass has not finished checking this event, so it cannot carry a ping yet.")
            continue
        result.update(
            event_id=event["id"],
            conversation_score=event["conversation_score"],
            career_openness=event["career_openness"],
        )
        if reviewed and candidate["identity_status"] != "verified":
            return stop(
                "needs_review",
                "Confirm identity and evidence before releasing a live ping.",
            )
        if reviewed and not all(e["verified"] for e in ev):
            return stop(
                "needs_review", "The trigger’s source evidence has not been reviewed."
            )
        seen = [(parse_time(e["observed_at"]), parse_time(e.get("last_checked_at") or e["observed_at"])) for e in ev]
        if any(max(times) > now + timedelta(minutes=5) for times in seen):
            return stop(
                "research",
                "Evidence has a future observation time. Correct the source record.",
            )
        if candidate["mode"] == "live" and any(now - checked > timedelta(days=REVIEW_VALID_DAYS) for _, checked in seen):
            return stop(
                "needs_review",
                "Recheck the trigger source; its last observation is over seven days old.",
            )
        required = set(role.get("required_criteria", role["criteria"][:2]))
        proven = {
            f["criterion"]
            for f in supported
            if all(e["verified"] for e in evidence_for(candidate, f["evidence_ids"]))
        }
        if reviewed and not required.issubset(proven):
            return stop(
                "research",
                "Missing reviewed evidence for: "
                + "; ".join(sorted(required - proven)),
            )
        if contradicts_required(candidate, role):
            return stop("research", "Evidence contradicts a required role criterion.")
        if failure := draft_route_failure(candidate):
            return stop("research", failure)
        if contact.undergraduate(contacts, now):
            from .outreach import ROLE_FREE_WHY, names_role  # outreach imports this module

            if names_role(candidate["draft"], role):
                return stop("research", f"The draft {ROLE_FREE_WHY}: rewrite it about their work, without the role "
                                        "or hiring.")
        if not reviewed:
            result["ping"] = make_ping(candidate, role, event, now, reviewed=False, readiness=readiness)
            return stop("needs_review", "An unreviewed internal proposal: the team checks the evidence, fit and draft before anyone acts.")
        if candidate.get("review_hash") != review_hash(candidate, role):
            return stop(
                "needs_review",
                "Review this evidence and draft against the current role brief.",
            )
        if not candidate.get("reviewed_at") or parse_time(
            candidate["reviewed_at"]
        ) > now + timedelta(minutes=5):
            return stop(
                "needs_review",
                "Record a valid review time for this contact-now judgment.",
            )
        if now >= parse_time(revalidate_at(candidate, ev)):
            return stop(
                "needs_review",
                "Reassess why now: the seven-day review validity period has elapsed. This is an operational check, not a predicted outreach window.",
            )
        result["state"] = "ready"
        result["priority"] = "high"
        reasons.append(
            "Reviewed role fit, a current conversational opening, and a complete outreach draft."
        )
        result["ping"] = make_ping(candidate, role, event, now, readiness=readiness)
        return result
    reasons.extend(list(dict.fromkeys(failures)))
    if failures and all(
        f.startswith(("Keep watching:", "Waiting for")) for f in failures
    ):
        result["state"] = "watch"
    return result


def make_ping(c, role, event, now, reviewed=True, readiness=None):
    """With ``readiness``, the window, timing confidence and falsifiers are the detectors', not the model's."""
    evidence = evidence_for(c, event["evidence_ids"])
    route_evidence = evidence_for(c, c["route"]["evidence_ids"])
    route_status = c["route"]["status"]
    ping = {
        "id": "ping:" + event_key(c, event),
        "mode": "synthetic-fixture" if c["mode"] == "simulation" else "live",
        "person": {
            "id": person_of(c),
            "name": c["name"],
            "public_profile_url": c["profile_url"],
        },
        "role": {
            "id": role["id"],
            "title": role["title"],
            "jd_url": role["jd_url"],
            "brief_version": role["brief_version"],
        },
        "trigger": {
            "event_key": event_key(c, event),
            "type": event["kind"],
            "what_changed": event["title"],
            "date": event["date"],
            "date_basis": event.get("date_basis", "published_event_date"),
            "date_source_id": event.get("date_source_id"),
            **({"window": readiness.window} if readiness and readiness.window else {}),
            "date_caveat": event.get("date_caveat")
            or "Date as stated in source; contact-now rationale remains a reviewed judgment.",
            "source_urls": list(dict.fromkeys(e["url"] for e in evidence)),
            "detected_at": min(e["observed_at"] for e in evidence),
            "last_verified_at": (c.get("reviewed_at") or iso(now)) if reviewed else None,
        },
        "why_now": event["why_now"],
        "contact_purpose": event.get("contact_purpose", "unassessed"),
        "purpose_reason": event.get("purpose_reason"),
        "confidence": {
            "fit": "high"
            if all(f["status"] == "supported" for f in c["fit"])
            else "medium",
            "timing_evidence": readiness.confidence if readiness else event["confidence"],
            "route": {
                "none_found": "low",
                "plausible_unconfirmed": "medium",
                "verified": "high",
            }[route_status],
            "reasoning": "Evidence reviewed by operator; confidence is a judgment, not a probability of replying. "
            + (readiness.explanation + " " if readiness else "") + event["why_wait"],
            "what_would_prove_wrong": (readiness.falsifiers if readiness else []) or [event["falsifier"]],
        },
        "draft_outreach": {
            "subject": c["draft"]["subject"],
            "body": c["draft"]["body"],
            "channel": c["draft"]["channel"],
            "sender_status": "proposed",
            "claim_source_urls": [
                e["url"] for e in evidence_for(c, c["draft"]["source_ids"])
            ],
            "review_status": "reviewed" if reviewed else "unreviewed",
            "sender": {
                "proposed_function": c["draft"]["sender_function"],
                "selection_rationale": "Relevant senior leader for the professional topic; account use requires their authorization.",
                "roster_id": None,
                "identity_use_authorized": False,
            },
        },
        "route_in": {
            "status": route_status,
            "description": "none found"
            if route_status == "none_found"
            else c["route"]["description"],
            "source_urls": [e["url"] for e in route_evidence],
        },
        "policy_version": POLICY_VERSION,
        "expires_at": revalidate_at(c if reviewed else {}, evidence),
        "expiry_basis": "review_revalidation_policy",
        "recipient_value": event["recipient_value"],
        **({"readiness": {"action": readiness.action, "score": readiness.score,
                          "families": readiness.families, "explanation": readiness.explanation}}
           if readiness else {}),
    }
    VALIDATOR.validate(ping)
    return ping
