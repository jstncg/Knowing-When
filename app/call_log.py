"""The daily run's call log: each watched person's call, once a day, in a file that shows it came first.

The scheduled run (scripts/daily.py with --post) writes the first line per person and role per day to calls.jsonl
beside its own log (research/private/social/): the time, the commit that made the call, the person key, the role,
the action before the contact gate (journey.assess), its track, reasons and evidence ids, the activations' detector
ids, and whether it is an early reach, by the scorecard's own test. Each line carries the hash of the line before
it, so a line changed or taken out later breaks the chain from there on; head() is what to copy off the Mac.

score() is the forward test of early signs, which no replay can fake (pre-registered on 2026-09-24 in the
project's unpublished pre-registration log): the scorecard's windows, tiled from each person's first logged day,
with the logged early reaches in place of the replay's. moments.write_prediction is the web app's record of one plan's windows; this is the daily run's record of
every call, reach or not.
"""

import hashlib
import json
import os
from datetime import date, timedelta
from pathlib import Path

from . import contact, journey, readiness
from . import scorecard as sc
from .models import parse_time

KINDS = ("paper", "release", "launch")  # the moments a hit comes before: job news would test leave prediction
COUNTED = sc.STRETCH - sc.LAST_WEEK  # a window's first 49 days, as the replay leaves out the last week
TEST_AT = 30  # complete before-windows before a report is read as a test


def _hash(line):
    return hashlib.sha256(json.dumps({k: v for k, v in line.items() if k != "hash"}, sort_keys=True).encode()).hexdigest()


def line(store, subject_id, role, at, commit):
    """One person's call as of ``at`` as a log line. ``early``: the scorecard's test on the same activations, with
    readiness.score and no live list (the early signs alone make a reach and the rest alone does not)."""
    call = journey.assess(store, subject_id, at, role)
    activations = journey.detect(store, subject_id, at, role=role)
    return {"at": at, "commit": commit, "person": contact.person_key(subject_id), "subject_id": subject_id,
            "role": role, "action": call.action if call else None, "track": call.track if call else None,
            "reasons": call.reasons if call else [], "evidence": call.evidence if call else {},
            "window": call.window if call else None, "until": call.until if call else None,
            "detectors": sorted({a["detector_id"] for a in activations}),
            "early": sc.scored(readiness.score, activations, at)["early"]}


def read(path):
    """The lines, oldest first; one that is not a JSON object is kept as None, so intact() stops there."""
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for text in path.read_text().splitlines():
        try:
            found = json.loads(text)
        except ValueError:
            found = None
        out.append(found if isinstance(found, dict) else None)
    return out


def intact(lines):
    """How many lines, from the first, hold together: each names the hash of the one before, matches its own, and
    is no earlier than it (a morning filled in later would read posts pulled after it)."""
    prev, last = "", None
    for n, found in enumerate(lines):
        if found is None or found.get("prev") != prev or found.get("hash") != _hash(found) \
                or (last and parse_time(found["at"]) < last):
            return n
        prev, last = found["hash"], parse_time(found["at"])
    return len(lines)


def append(path, calls):
    """Add each call as a line chained to the one before, in one write; returns how many were written. Refuses a
    log that no longer holds together (a torn last line, a line changed) or a call earlier than its last line, so
    the run's note says so every day until someone looks."""
    path = Path(path)
    lines = read(path)
    if (n := intact(lines)) < len(lines):
        raise ValueError(f"the chain breaks at line {n + 1} of {len(lines)}: nothing more is written to it")
    if lines and not path.read_text().endswith("\n"):
        raise ValueError("the log's last line has no newline (a write cut off): nothing more is written to it")
    times = [parse_time(c["at"]) for c in lines[-1:] + calls]
    if times != sorted(times):
        raise ValueError("a call is earlier than the line before it: a morning is never filled in later")
    prev, out = lines[-1]["hash"] if lines else "", []
    for call in calls:
        chained = {**call, "prev": prev}
        prev = chained["hash"] = _hash(chained)
        out.append(json.dumps(chained, sort_keys=True) + "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write("".join(out))
        f.flush()
        os.fsync(f.fileno())
    return len(calls)


def log(path, store, people, at, commit):
    """Each person's call as of ``at`` (people: .subject_id and .role each), but for a person and role already
    logged that day: only the day's first line counts. Returns how many lines were written."""
    done = {(c["person"], c["role"]) for c in read(path) if c and c["at"][:10] == at[:10]}
    return append(path, [line(store, p.subject_id, p.role, at, commit) for p in people
                         if (contact.person_key(p.subject_id), p.role) not in done])


def head(path):
    """What to copy off the Mac: how many lines, how many hold together, and the hash and time of the last of those."""
    lines = read(path)
    last = lines[n - 1] if (n := intact(lines)) else {}
    return {"lines": len(lines), "intact": n, "head": last.get("hash", ""), "at": last.get("at", "")}


def score(lines, moments):
    """The logged early reaches against each person's public moments, in the scorecard's windows.

    ``moments``: {person (contact.person_key), date (the earliest day any of its sources was public), kind,
    source_url} each; only KINDS count. A broken chain is reported as broken and never scored. Each person's windows
    are sc.stretches tiled from their first logged day to the log's last: the 56 days before a moment, and the ordinary
    56-day stretches with no moment in them or the 56 days after. A window counts lines from its first 49 days
    only (8 to 56 days before a moment), and only when each of those mornings was logged for the person; the rest
    are listed with their missing mornings. A moment is seen coming when an early line was logged in its counted
    days; p is sc.edge_p's one-sided Fisher test against the ordinary stretches with one."""
    if (n := intact(lines)) < len(lines):
        return {"lines": len(lines), "intact": n, "broken": True}
    logged, early, roles = {}, {}, {}
    for c in lines:
        day = date.fromisoformat(c["at"][:10])
        logged.setdefault(c["person"], set()).add(day)
        roles[c["person"]] = c["role"]
        if c["early"]:
            early.setdefault(c["person"], set()).add(day)
    every = set().union(*logged.values())
    windows = []
    for person, days in logged.items():
        watched = sc.Person(subject_id=person, role=roles[person], since=min(days).isoformat(),
                            until=max(every).isoformat(),
                            moments=[sc.Moment(date=m["date"], kind=m["kind"], source_url=m["source_url"])
                                     for m in moments if m["person"] == person and m["kind"] in KINDS])
        for start, end, moment in sc.stretches(watched, warm_up=timedelta(0)):
            counted = [start + timedelta(days=d) for d in range(COUNTED.days)]
            called = min((d for d in counted if d in early.get(person, ())), default=None)
            windows.append({"person": person, "start": start.isoformat(), "end": end.isoformat(),
                            "moment": moment.model_dump() if moment else None,
                            "missing": [d.isoformat() for d in counted if d not in days],
                            "first_early": called.isoformat() if called else None,
                            "lead_days": (end - called).days if moment and called else None})
    complete = [w for w in windows if not w["missing"]]
    pre = [w for w in complete if w["moment"]]
    summary = {"moments": len(pre), "seen_coming": sum(bool(w["first_early"]) for w in pre),
               "ordinary_stretches": len(complete) - len(pre),
               "fired_with_nothing_after": sum(bool(w["first_early"]) for w in complete if not w["moment"])}
    return {"lines": len(lines), "intact": n, "broken": False, **summary, "p": sc.edge_p(summary),
            "a_test": len(pre) >= TEST_AT, "incomplete": len(windows) - len(complete),
            "missing_mornings": sum(len(w["missing"]) for w in windows), "windows": windows}
