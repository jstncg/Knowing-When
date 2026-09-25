"""Internal timing proposals: notify the team, never authorize candidate contact."""

from collections import defaultdict
from datetime import timedelta

from . import contact
from .engine import decide, digest, person_of
from .models import iso, parse_time, utcnow
from .journey import assess


def occasion_key(candidate, event):
    # One occasion per person and event day, across roles: citation enrichment and
    # model retitling cannot create another. refresh_alerts keeps one alert per
    # person at a time and raises at most one a day, whatever the occasions.
    return digest(
        [person_of(candidate), event.get("date")]
    )


def internal_proposal(candidate, role, watch, contacts=(), now=None, readiness=None):
    """An alert for the team: engine.decide's verdict (``contacts`` and ``readiness`` as there) without its human review gates."""
    now = now or utcnow()
    if (
        candidate["role_id"] != role["id"]
        or watch.get("role_id", candidate["role_id"]) != candidate["role_id"]
        or watch.get("mode", candidate["mode"]) != candidate["mode"]
        or watch.get("candidate_id", candidate.get("id")) != candidate.get("id")
        or watch.get("status") != "active"
        or watch.get("brief_version") != role["brief_version"]
        or candidate.get("source_change_pending")
        or candidate.get("proposal_hold")
    ):
        return None
    decision = decide(candidate, role, contacts, now, readiness, reviewed=False)
    if not (ping := decision["ping"]):
        return None
    event = next(e for e in candidate["events"] if e["id"] == decision["event_id"])
    occasion = occasion_key(candidate, event)
    ping["id"] = "review-ping:" + occasion
    known_evidence_ids = {e["id"] for e in candidate["evidence"]}
    supported = {
        f["criterion"]
        for f in candidate["fit"]
        if f["status"] == "supported"
        and f["evidence_ids"]
        and set(f["evidence_ids"]).issubset(known_evidence_ids)
    }
    fit_gaps = sorted(set(role["criteria"]) - supported)
    ping["confidence"]["fit"] = (
        "medium" if not fit_gaps and role["criteria"] else "low"
    )
    ping["confidence"]["reasoning"] = (
        "Unreviewed internal opportunity proposal. Monitoring premise: "
        + watch["fit_basis"].rstrip(". ")
        + ". "
        + event["why_wait"]
    )
    return {
        "ping": ping,
        "event_id": event["id"],
        "occasion_key": occasion,
        "candidate_revision": candidate["revision"],
        "role_brief_version": role["brief_version"],
        "fit_gaps": fit_gaps,
    }


def refresh_alerts(store, mode, role_for, now=None):
    now = now or utcnow()
    # Every role that proposes the person; one alert per person is chosen below.
    options = defaultdict(list)
    for watch in sorted(store.all("watch"), key=lambda w: w["id"]):
        if watch["mode"] != mode:
            continue
        candidate = store.get(watch["candidate_id"])
        proposal = (
            internal_proposal(
                candidate,
                role_for(candidate["role_id"]),
                watch,
                contact.history(store, candidate["person_id"]),
                now,
                assess(store, candidate["person_id"], iso(now), candidate["role_id"]),
            )
            if candidate
            else None
        )
        if not proposal:
            continue
        criteria_count = len(role_for(candidate["role_id"])["criteria"])
        rank = (
            (criteria_count - len(proposal["fit_gaps"])) / max(criteria_count, 1),
            -len(proposal["fit_gaps"]),
            candidate["role_id"],
        )
        options[person_of(candidate)].append(
            {"proposal": proposal, "candidate": candidate, "rank": rank}
        )
    # One alert per person at a time, and at most one new one a day. The person's
    # latest alert stays, or comes back, while its occasion still proposes, even
    # when another role's occasion now ranks higher. Any other occasion gets a new
    # alert only once a day has passed since the latest was raised.
    latest = {}
    for a in store.all("review_alert"):
        # The person through the alert's candidate, so linking two people's records keeps their alerts together.
        if a["mode"] != mode or not (candidate := store.get(a["candidate_id"])):
            continue
        person = person_of(candidate)
        if a["created_at"] > latest.get(person, {}).get("created_at", ""):
            latest[person] = a
    choices = {}
    for person, found in options.items():
        last = latest.get(person, {})
        # Its occasion, also after the key moved: linked records change the person, a re-dated event the day.
        current = [o for o in found if o["proposal"]["occasion_key"] == last.get("occasion_key")
                   or (o["candidate"]["id"], o["proposal"]["event_id"]) == (last.get("candidate_id"), last.get("event_id"))]
        if not current and last and now - parse_time(last["created_at"]) < timedelta(days=1):
            continue
        best = max(current or found, key=lambda o: o["rank"])
        choices[last["id"] if current else "alert:" + best["proposal"]["occasion_key"]] = {
            **best,
            "roles": sorted({o["candidate"]["role_id"] for o in found}),
        }
    new = 0
    for alert in store.all("review_alert"):
        if (
            alert["mode"] == mode
            and alert["status"] != "withdrawn"
            and alert["id"] not in choices
        ):
            store.put(
                alert["id"],
                "review_alert",
                {
                    **alert,
                    "status": "withdrawn",
                    "withdrawn_at": iso(now),
                    "withdrawal_reason": "Evidence, contact state, watch status or hiring brief no longer supports this opportunity.",
                },
            )
    for key, choice in choices.items():
        proposal, candidate = choice["proposal"], choice["candidate"]
        existing = store.get(key)
        if (
            existing
            and existing.get("candidate_revision") == candidate["revision"]
            and existing["candidate_id"] == candidate["id"]
            and existing["status"] != "withdrawn"
            and existing.get("matching_role_ids") == choice["roles"]
        ):
            continue
        payload = {
            **(existing or {}),
            **proposal,
            "candidate_id": candidate["id"],
            "matching_role_ids": choice["roles"],
            "mode": mode,
            "status": "proposed",
            "created_at": existing["created_at"] if existing else iso(now),
            "seen_at": existing.get("seen_at") if existing else None,
            "channel": "local_timing_workspace",
            "delivery_count": 1,
        }
        if existing:
            payload["revised_at"] = iso(now)
        store.put(key, "review_alert", payload)
        if not existing:
            new += 1
    return new
