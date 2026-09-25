"""Moment planner: for someone who is not ready today, when the window opens, what would prove that wrong, and when to
look again.

plan_moments is a pure function of the timeline as of a date. It makes one hypothesis per detector window that is open
or opens within HORIZON days, each with the falsifiers for its mechanism and a recheck date (14 days before the window
opens, at opening, then weekly while it is open). A sign the call only lists (readiness.listed_only: time in a seat, a
retention cliff, a push with nothing beside it) is never planned. A falsifier the as_of view already shows (their next
role before a contract's window closes, say) retires the hypothesis instead.

save_plan stores the hypotheses and logs each window opening within PREREGISTER days exactly once to PREDICTIONS,
timestamped and hashed, so it can later be shown to predate what happened. due_rechecks replans whatever is due; the
worker runs it.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from . import detectors, journey, readiness
from .engine import digest
from .models import iso, parse_time
from .timeline import MomentHypothesis, save_hypothesis

HORIZON = timedelta(days=180)
PREREGISTER = timedelta(days=120)
LEAD = timedelta(days=14)
WEEK = timedelta(days=7)
IDLE_RECHECK = timedelta(days=30)  # a plan with nothing in view still looks again
RELATED = ("gi",)


# Falsifier checks. Each reads one event of the as_of view and the plan's subject.

def _own(event, subject):
    return event["subject_id"] == subject


def moved(event, subject):
    return _own(event, subject) and (event["event_type"] in ("job_started", "role_announced", "affiliation_change")
                                     or detectors._new_employment(event))


def promoted(event, subject):
    return _own(event, subject) and (event["event_type"] == "officer_appointment" or detectors._title_change(event))


def retained(event, subject):
    return _own(event, subject) and event["event_type"] in ("equity_refresh", "retention_or_commitment")


def renewed(event, subject):
    return event["event_type"] == "grant_started"


def resumed(event, subject):
    return _own(event, subject) and event["event_type"] in detectors.CADENCE_TYPES


def restated(event, subject):
    """A later statement of a date supersedes the earlier one."""
    return _own(event, subject) and event["event_type"] in ("self_stated_availability", "contact_constraint", "placement_end_expected")


def filed(event, subject):
    return event["event_type"] == "annual_report_filed"


# (what a human reads, the check that spots it on the timeline or None when only a human can tell)
MOVED = ("They start a new role elsewhere before the window closes", moved)
PROMOTED = ("A promotion or new title before the window closes", promoted)
RETAINED = ("A new equity or retention grant", retained)
FALSIFIERS = {
    "tenure_milestone": [PROMOTED, RETAINED],
    "exec_departure": [("They are promoted into the departed officer's scope", promoted),
                       ("A successor is named and the team holds", None), RETAINED],
    "pi_departure": [("A new PI takes the group and they stay", None)],
    "team_exodus": [RETAINED, PROMOTED, ("No further departures from the team within 90 days", None)],
    "coauthor_departure": [("They publish with the remaining team at the same affiliation", None)],
    "placement_end": [("An extension or a new end date is announced", restated)],
    "own_departure": [("They announce a new role or say they are taking time off", None)],
    "company_closure": [("The closure is called off, or an acquirer keeps the team", None)],
    "retention_cliff": [("A new grant or retention bonus before the cliff date", retained),
                        ("A promotion before the cliff date", promoted)],
    "paper_v1": [("They announce a new role or promotion on the back of the work", promoted),
                 ("No engagement with the work within the window", None)],
    "paper_accepted": [PROMOTED],
    "paper_talk": [("The talk is cancelled or given by a coauthor", None)],
    "rhythm_change": [("Their cadence resumes: a new paper at the old rhythm", resumed)],
    "own_precedent": [PROMOTED, RETAINED],
    "acquisition_closed": [("Integration retention grants for the team", retained), PROMOTED],
    "warn_notice": [("The layoffs are called off or their unit is not affected", None)],
    "auditor_change": [("The next annual report is filed on time with a clean opinion", filed), PROMOTED],
    "late_filing": [("The company files and the restatement is clean", filed), PROMOTED],
    "grant_end": [("The grant is renewed or a new grant starts", renewed)],
    "gi_citation": [("No further engagement with GI's work within 60 days", None)],
    "gi_attention": [("The attention was incidental: no follow-up within 30 days", None)],
    "work_in_progress": [("It was a passing post: nothing more on the work within three weeks", None)],
    "technical_ask": [("They say the ask is answered or closed", None)],
    "just_submitted": [("It goes public before we write: then everyone can see it", None)],
    "similar_path": [("The precedent does not hold: their next signals diverge from the past hire's path", None)],
    "associate_joined": [("The associate leaves the new lab again, or they were never close", None)],
    "topic_drift": [("Their next posts return to their old topics", None)],
    "new_field_contact": [("The exchanges were one-offs: no further contact within a month", None)],
    "posting_burst": [("Their posting drops back to its usual rate with nothing to show", None)],
    "self_stated_availability": [("They restate a later date", restated)],
    "stated_follow_up": [("They restate a later date", restated)],
    "private_note": [("A newer typed note from a GI teammate replaces it", None)],
}

MECHANISMS = {
    "tenure_milestone": "A tenure milestone in the current role: 1, 2 or 4 years, or their own typical tenure.",
    "exec_departure": "An officer at their employer left; reporting lines and scope are in flux.",
    "pi_departure": "Their PI or a senior coauthor is leaving or has left their institution.",
    "team_exodus": "Two or more people left their org within 60 days.",
    "coauthor_departure": "A coauthor left their employer.",
    "placement_end": "A fixed-term placement is ending.",
    "own_departure": "They were laid off or left their job, or will; they are between jobs.",
    "company_closure": "Their employer is closing; everyone there has to move.",
    "retention_cliff": "12 or 24 months since their employer's acquisition closed, when retention commonly vests.",
    "paper_v1": "A new paper gives a concrete reason to reach out.",
    "paper_accepted": "An acceptance is a natural moment to talk about what is next.",
    "paper_talk": "A talk puts them in front of people and open to conversations.",
    "rhythm_change": "Their public cadence broke from their own baseline.",
    "own_precedent": "The events that preceded their earlier moves are recurring.",
    "acquisition_closed": "A deal to buy their employer, agreed or closed; integration unsettles roles.",
    "warn_notice": "Layoffs at their employer: a WARN notice, or a headline that reports them.",
    "auditor_change": "Their employer changed auditors, which reshapes finance work.",
    "late_filing": "Their employer filed late, a sign of finance-team strain.",
    "grant_end": "Their funding is ending.",
    "gi_citation": "Their work engages with GI's own.",
    "gi_attention": "They paid attention to GI publicly.",
    "work_in_progress": "They are working on something close to GI's work or an open role's, before it is public.",
    "technical_ask": "They asked in public for data, compute, feedback or help with work close to GI's: an open door to write.",
    "just_submitted": "They just submitted work close to GI's that is not public yet.",
    "similar_path": "Their signals match what a past hire at GI or a lab like it showed before joining.",
    "associate_joined": "Someone close to them just joined GI or a lab like it.",
    "topic_drift": "Their posts moved toward GI's topics, against their own past.",
    "new_field_contact": "They started talking with people in GI's field they had not talked to before.",
    "posting_burst": "They are posting far more than their own usual rate.",
    "self_stated_availability": "They said they are looking, or when they are available.",
    "stated_follow_up": "They named a date to be contacted after.",
    "private_note": "A GI teammate noted they are open to a move, or the day to reach them.",
}


def falsifiers_for(detector_id):
    return FALSIFIERS.get(detector_id, []) + [MOVED]


def _day(event):
    return detectors.day_of(event) or detectors.observed_day(event)


def falsified_by(activation, events, subject):
    """Events in the view that break the mechanism: dated after its evidence, before its window closes.

    An undated event cannot be placed after the evidence (an old internship post seen today is not news), so it never
    falsifies.
    """
    evidence = set(activation["evidence_event_ids"])
    anchor = max(_day(e) for e in events if e["id"] in evidence)
    closes = parse_time(activation["window_close"]).date()
    checks = [check for _, check in falsifiers_for(activation["detector_id"]) if check]
    return sorted(e["id"] for e in events if e["id"] not in evidence and (d := detectors.day_of(e)) and anchor < d <= closes
                  and any(check(e, subject) for check in checks))


def next_check_at(opens, closes, as_of):
    """14 days before the window opens, at opening, then weekly while open; always after as_of."""
    if as_of < opens - LEAD:
        return opens - LEAD
    if as_of < opens:
        return opens
    return min(opens + WEEK * ((as_of - opens) // WEEK + 1), closes)


def _date(value):
    d = parse_time(value)
    return f"{d:%B} {d.day}"


def _headline(as_of, action, moments):
    if not moments:
        return f"Nothing in view within {HORIZON.days} days."
    open_now = [m for m in moments if parse_time(m["window_open"]) <= as_of]
    if action == "reach_now":
        return "Reach out now: " + ", ".join(m["detector_id"] for m in open_now) + " open."
    if open_now:
        m = open_now[0]
        return (f"Window open until {_date(m['window_close'])} ({m['detector_id']}); "
                f"{action.replace('_', ' ')}; recheck {_date(m['next_check_at'])}.")
    m = moments[0]
    return f"Not now; the window opens {_date(m['window_open'])}; recheck {_date(m['next_check_at'])}."


def plan_moments(store, subject, role_config, as_of, related=RELATED):
    """Moment hypotheses for one person and role, from the timeline as of a date. Writes nothing."""
    as_of_t = parse_time(as_of)
    as_of = iso(as_of_t)
    events = detectors.view(store, subject, as_of, related)
    activations = journey.detect(store, subject, as_of, events, role_config["id"])
    scored = readiness.score(activations, as_of, events)
    holding = [a for a in activations if a["family"] in readiness.SUPPRESSORS or a["holds"]]
    listed = readiness.listed_only(activations, as_of, events)  # a call lists them, never counts them: no window to plan
    moments, falsified = [], []
    for a in activations:
        if a in holding or any(a is b for b in listed) or not (a["window_open"] and a["window_close"]):
            continue
        opens, closes = parse_time(a["window_open"]), parse_time(a["window_close"])
        if not (as_of_t < closes and opens <= as_of_t + HORIZON):
            continue
        window = f"{opens.date()} to {closes.date()}"
        moment = {"detector_id": a["detector_id"], "family": a["family"], "strength": a["strength"],
                  "window_open": iso(opens), "window_close": iso(closes),
                  "evidence_event_ids": a["evidence_event_ids"]}
        if broken := falsified_by(a, events, subject):
            falsified.append({**moment, "falsified_by": broken})
            continue
        holds = sorted({h for hw in holding if hw["window_open"] <= a["window_close"]
                        and a["window_open"] <= hw["window_close"] for h in (hw["holds"] or [hw["detector_id"]])})
        hypothesis = MomentHypothesis(
            subject_id=subject, role_id=role_config["id"],
            mechanism=f"{MECHANISMS.get(a['detector_id'], a['detector_id'] + ' window.')} Window {window}.",
            trigger_condition=(f"Between {window}, readiness reaches reach_now (two independent reasons, or one "
                               "enough alone: what the person said, a GI teammate's note that they are open, "
                               "or a forced move) with no hold active."),
            expected_window=window, required_tier=0 if a["family"] == "private" else 1, holds=holds,
            falsifiers=[text for text, _ in falsifiers_for(a["detector_id"])],
            next_check_at=iso(next_check_at(opens, closes, as_of_t)),
            detector_ids=[a["detector_id"]],
        ).model_dump()
        moments.append({**moment, "hypothesis": hypothesis, "next_check_at": hypothesis["next_check_at"]})
    moments.sort(key=lambda m: (m["window_open"], m["detector_id"]))
    next_at = min((m["next_check_at"] for m in moments), default=iso(as_of_t + IDLE_RECHECK))
    return {"subject_id": subject, "role_id": role_config["id"], "related": list(related), "as_of": as_of,
            "action": scored.action, "readiness": scored.model_dump(), "moments": moments,
            "falsified": falsified, "next_check_at": next_at,
            "headline": _headline(as_of_t, scored.action, moments)}


PREDICTIONS = Path("research/predictions.jsonl")


def write_prediction(subject_id, as_of, action, windows, readiness, path=PREDICTIONS):
    """Append a timestamped, hashed prediction so it can be shown to predate the outcome."""
    inputs = {"subject_id": subject_id, "as_of": iso(parse_time(as_of)), "action": action,
              "windows": windows, "readiness": readiness}
    record = {**inputs, "recorded_at": iso(), "input_hash": digest(inputs)}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def read_predictions(path=PREDICTIONS):
    path = Path(path)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def plan_key(subject, role_id):
    return "moment-plan:" + digest([subject, role_id])


def save_plan(store, plan, predictions=PREDICTIONS):
    """Store the plan and its hypotheses; pre-register windows opening within PREREGISTER, once each."""
    as_of = parse_time(plan["as_of"])
    for moment in plan["moments"]:
        save_hypothesis(store, moment["hypothesis"])
        if parse_time(moment["window_open"]) > as_of + PREREGISTER:
            continue
        key = "moment-prediction:" + digest([plan["subject_id"], plan["role_id"], moment["detector_id"],
                                             moment["window_open"], moment["window_close"]])
        if store.get(key):
            continue
        window = {"detector_id": moment["detector_id"], "family": moment["family"],
                  "open": moment["window_open"], "close": moment["window_close"],
                  "next_check_at": moment["next_check_at"], "falsifiers": moment["hypothesis"]["falsifiers"]}
        record = write_prediction(plan["subject_id"], plan["as_of"], plan["action"], [window],
                                  plan["readiness"], predictions)
        store.put(key, "moment_prediction", {"subject_id": plan["subject_id"], "role_id": plan["role_id"],
                                             "input_hash": record["input_hash"], "recorded_at": record["recorded_at"]},
                  only_new=True)
    key = plan_key(plan["subject_id"], plan["role_id"])
    old = store.get(key)
    return store.put(key, "moment_plan", plan, expected=old["revision"] if old else None)


def due_rechecks(store, now, targets, predictions=PREDICTIONS):
    """Plan or replan each watched (subject, role_config, related) target that is due, and nobody else.

    A target is due when it has no plan, at its plan's next_check_at, when its related subjects change, or as soon as a
    new event lands on its view. People who are no longer watched are never rechecked and add no predictions. Running
    twice at the same time does nothing the second time. Returns the saved plans and when the next watched target falls
    due.
    """
    now = now if isinstance(now, datetime) else parse_time(now)
    latest = {}
    for e in store.all("timeline_event"):
        if parse_time(e["observed_at"]) <= now:
            latest[e["subject_id"]] = max(latest.get(e["subject_id"], ""), e["observed_at"])
    plans = {(p["subject_id"], p["role_id"]): p for p in store.all("moment_plan")}
    saved, upcoming = [], []
    for (subject, role_id), (role_config, related) in {(s, r["id"]): (r, rel) for s, r, rel in targets}.items():
        plan = plans.get((subject, role_id))
        if (plan is None or plan["related"] != list(related) or parse_time(plan["next_check_at"]) <= now
                or any(s in latest and parse_time(latest[s]) > parse_time(plan["as_of"]) for s in (subject, *related))):
            plan = save_plan(store, plan_moments(store, subject, role_config, iso(now), related), predictions)
            saved.append(plan)
        upcoming.append(parse_time(plan["next_check_at"]))
    return saved, min(upcoming, default=None)
