"""Fictional scenarios; never mixed into real people or live delivery."""

from copy import deepcopy
from datetime import timedelta

from .engine import review_hash
from .models import iso, utcnow


# Each invented person's own reasoning, so no two records share a why, a wait or a falsifier.
WORDING = {
    "mts-research": {
        "recipient_value": "Compare how ActionGarden scores action consistency with how GI evaluates its own world models; the author asked for methods to compare.",
        "why_now": "Alex released the suite yesterday and is collecting input this week, before the next iteration is fixed.",
        "why_wait": "Wait if the release turns into a rush of bug reports, or if Alex says the input window has closed.",
        "falsifier": "Alex finalizes the next iteration without outside input, or the suite turns out to be a team release Alex did not lead.",
        "confidence": "high",
    },
    "controller": {
        "recipient_value": "Compare Morgan's upstream reconciliation checks with GI/Medal's multi-entity close; Morgan asked peers for their methods.",
        "why_now": "Morgan published the close walkthrough yesterday and asked finance peers to compare approaches this week.",
        "why_wait": "Wait until after month-end close if the walkthrough lands during Morgan's close week.",
        "falsifier": "Morgan's company announces a new controller-level hire over the walkthrough's scope, or Morgan says the redesign was a consultant's.",
        "confidence": "medium",
    },
}


def scenarios(roles):
    now = utcnow()
    cases = []
    for rid, name, title, work in [
        (
            "mts-research",
            "Alex Rowan",
            "Released ActionGarden evaluation suite",
            "ActionGarden separates visual realism from action consistency across long simulated rollouts",
        ),
        (
            "controller",
            "Morgan Vale",
            "Published a multi-entity close walkthrough",
            "a five-entity close redesign that moved reconciliation checks upstream and documented cross-border ownership",
        ),
    ]:
        role = roles[rid]
        url = "https://example.org/fictional/" + name.lower().replace(" ", "-")
        invitation = (
            "I am gathering input on this approach this week before finalizing the next iteration; "
            "please contact me now to compare methods."
        )
        assessment = {
            "mechanism": "active_work_need",
            "person_impact": {
                "statement": "The author is actively collecting input before finalizing an iteration.",
                "evidence_id": "work", "quote": invitation,
            },
            "opportunity": {
                "statement": "The explicit invitation provides a current opening for a relevant professional exchange.",
                "evidence_id": "work", "quote": invitation,
            },
            "waiting_cost": {
                "statement": "Waiting until after the iteration is finalized misses the stated opportunity to influence it.",
                "evidence_id": "work", "quote": invitation,
            },
        }
        c = {
            "name": name,
            "profile_url": url,
            "role_id": rid,
            "mode": "simulation",
            "summary": "Fictional evaluation scenario. All people, work, dates and relationships in this record are invented to test the workflow.",
            "employer": "Fictional company",
            "location": "New York (fictional)",
            "identity_status": "verified",
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
                    "url": url,
                    "title": "Fictional professional work",
                    "excerpt": work
                    + "; " + invitation,
                    "observed_at": iso(now),
                    "event_date": (now - timedelta(days=1)).date().isoformat(),
                    "verified": True,
                    "source_kind": "synthetic_fixture",
                }
            ],
            "events": [
                {
                    "id": "release",
                    "kind": "relevant_release",
                    "title": title,
                    "date": (now - timedelta(days=1)).date().isoformat(),
                    "timing_action": "contact_now",
                    "timing_assessment": assessment,
                    "contact_purpose": "rapport",
                    "purpose_reason": assessment["opportunity"],
                    **WORDING[rid],
                    "evidence_ids": ["work"],
                    "conversation_score": 3,
                    "career_openness": "unknown",
                }
            ],
            "route": {
                "status": "none_found",
                "description": "none found",
                "introducer": "",
                "evidence_ids": [],
            },
            "draft": {
                "subject": "Action consistency beyond visual quality"
                if rid == "mts-research"
                else "Your five-entity close redesign",
                "body": (
                    "Alex — ActionGarden’s split between visual realism and action consistency is exactly the distinction I’d like to discuss. How did you choose the long-rollout failures that belong in the benchmark? At General Intuition, that connects directly to the action-model problems we’re exploring. Would a short technical exchange next week be useful?"
                    if rid == "mts-research"
                    else "Morgan — your five-entity close walkthrough caught my attention, particularly moving reconciliation checks upstream instead of adding another month-end review. At General Intuition/Medal, multi-entity finance operations are relevant to our controller scope. I’d be interested in comparing the ownership and reconciliation approach you described. Would you be open to a brief conversation?"
                ),
                "sender_function": "CTO / research leader"
                if rid == "mts-research"
                else "CEO / operations leader",
                "sender_name": "",
                "channel": "email",
                "source_ids": ["work"],
            },
        }
        cases.append(c)
    future = deepcopy(cases[0])
    future["name"] = "Casey North"
    future["profile_url"] += "-future"
    future["events"][0].update(
        kind="requested_reconnect",
        title="Requested a conversation after a milestone",
        timing_action="follow_up",
        timing_assessment=None,
        follow_up_on=(now + timedelta(days=8)).date().isoformat(),
        follow_up_quote="Please contact me after "
        + (now + timedelta(days=8)).date().isoformat()
        + " to discuss the project.",
    )
    future["events"][0].update(
        why_now="Casey asked to be contacted after a named date, so the only right move is to wait for it.",
        why_wait="Casey named the date; contacting sooner ignores what they asked.",
        falsifier="Casey posts that the project slipped, or asks to push the conversation later again.",
        confidence="medium",
    )
    future["draft"].update(
        subject="Following up after your milestone",
        body="Casey — you asked to talk after the milestone, so I waited. How did the long-rollout evaluation land? At General Intuition we run into the same questions, and I would like to compare notes.",
    )
    future["evidence"][0]["excerpt"] = future["events"][0]["follow_up_quote"]
    future["events"][0]["purpose_reason"] = {"statement": "The requested follow-up is a project discussion, not a hiring invitation.", "evidence_id": "work", "quote": future["events"][0]["follow_up_quote"]}
    cases.append(future)
    stale = deepcopy(cases[1])
    stale["name"] = "Jamie Reed"
    stale["profile_url"] += "-stale"
    stale["events"][0].update(
        date=(now - timedelta(days=90)).date().isoformat(),
        timing_action="watch",
        timing_assessment=None,
        why_now="The old walkthrough still supports fit, but no new professional development explains why this week.",
        why_wait="Nothing new in three months: a message now would have no reason behind it.",
        falsifier="Jamie publishes something new on the close, or their company announces a finance reorganisation.",
        confidence="low",
    )
    stale["draft"].update(
        subject="Your close walkthrough",
        body="Jamie — I came back to your five-entity close walkthrough. Has the upstream reconciliation held up since you wrote it? I would like to compare notes with General Intuition/Medal's own multi-entity close.",
    )
    cases.append(stale)
    ambiguous = deepcopy(cases[0])
    ambiguous["name"] = "Taylor Quinn"
    ambiguous["profile_url"] += "-ambiguous"
    ambiguous["identity_status"] = "conflict"
    ambiguous["events"][0].update(
        why_now="The release reads like a reason to write, but two profiles share this name: confirm who wrote it first.",
        why_wait="Writing to the wrong Taylor Quinn would cost more than a day's delay.",
        falsifier="The release's author page links a different Taylor Quinn than the profile on file.",
        confidence="low",
    )
    ambiguous["draft"].update(
        subject="Your long-rollout evaluation",
        body="Taylor — I read your evaluation of long simulated rollouts. How did you decide which failures count as action inconsistency? It is close to what we work on at General Intuition.",
    )
    cases.append(ambiguous)
    quiet = deepcopy(cases[1])
    quiet["name"] = "Riley Chen"
    quiet["profile_url"] += "-quiet"
    quiet["events"] = []
    quiet["draft"].update(subject="", body="", source_ids=[])  # nothing to write about yet
    cases.append(quiet)
    for c in cases:
        c["review_hash"] = review_hash(c, roles[c["role_id"]])
        c["reviewed_at"] = iso(now)
    return cases
