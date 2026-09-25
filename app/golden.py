"""Golden timing set: scenarios with known right calls, replayed through the decision path.

Each file in tests/golden/ is a dated timeline, an as_of date and the right call
(reach_now, wait, verify_first or not_now) with the reason, written from
recruiting judgment and the traps rather than from the engine. The runner loads
the events into a fresh in-memory store, runs detectors.view, journey.detect for
the scenario's role and readiness.score as of that date, and grades the call, the wait date and the
evidence it rests on. Sockets and model calls are blocked while it runs.
Judgment scenarios are graded and shown but kept out of the pass rate.
"""

import hashlib
import json
import socket
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from . import detectors, journey, providers, readiness
from .store import Store
from .timeline import add_event

DIR = Path(__file__).resolve().parents[1] / "tests" / "golden"
CALL = {"reach_now": "reach_now", "verify_first": "verify_first", "watch_until": "wait",
        "respect_follow_up": "wait", "quiet": "not_now"}
PRECISION = {4: "year", 7: "month", 10: "day"}


def load(directory=DIR):
    return [json.loads(p.read_text()) for p in sorted(Path(directory).glob("*.json"))]


def stamp(value):
    return f"{value}T00:00:00+00:00" if len(value) == 10 else value


def record(scenario, event):
    """The timeline event a source would have written for one scenario event."""
    subject = event.get("subject", scenario["subject"])
    quote = json.dumps(event["changes"], sort_keys=True) if "changes" in event else event["quote"]
    sourced = event["tier"] in (1, 2)
    return {
        "subject_type": "org" if subject.startswith("org:") else "person", "subject_id": subject,
        "event_type": event["type"], "event_date": event["date"],
        "date_precision": PRECISION.get(len(event["date"] or "")), "observed_at": stamp(event["observed"]),
        "quote": quote, "tier": event["tier"], "extractor": event["source"],
        # An item and what the reader read from it share the item's URL ("url"), as they do in the timing store.
        "source_url": (event.get("url") or f"https://golden.invalid/{scenario['id']}/{event['id']}") if sourced else "",
        "source_version_hash": hashlib.sha256(quote.encode()).hexdigest() if sourced else "",
        "author": "golden" if event["tier"] == 0 else "",
    }


@contextmanager
def sealed():
    """No network (name lookups included) and no model calls: either one fails the run loudly."""
    def refuse(*_, **__):
        raise RuntimeError("golden runs are offline: network or model call attempted")
    with mock.patch.object(socket, "getaddrinfo", refuse), mock.patch.object(socket.socket, "connect", refuse), \
            mock.patch.object(providers, "_post", refuse):
        yield


def run(scenario):
    store = Store("sqlite://")
    label = {add_event(store, record(scenario, e))["id"]: e["id"] for e in scenario["events"]}
    as_of = stamp(scenario["as_of"])
    events = detectors.view(store, scenario["subject"], as_of, scenario["related"])
    activations = journey.detect(store, scenario["subject"], as_of, events, scenario["role"])
    scored = readiness.score(activations, as_of, events)
    live = [a for a in activations if a["window_close"] >= scored.as_of]
    used = {label[i] for a in activations for i in a["evidence_event_ids"]}
    visible = {label[e["id"]] for e in events}
    return {
        "call": CALL[scored.action], "action": scored.action, "track": scored.track,
        "until": scored.until and scored.until[:10],
        "score": scored.score, "explanation": scored.explanation,
        "cited": sorted({label[i] for a in live for i in a["evidence_event_ids"]}),
        "uncited": sorted(visible - used),  # visible to the engine, cited by no detector
        "hidden": sorted(set(label.values()) - visible),  # observed after as_of
    }


def grade(scenario, result):
    """pass, or why not: wrong_call, wrong_track (a reach_now on the wrong track: a pitch where only a note about
    their work fits), wrong_date (wait until outside the range) or wrong_reason (right call, key evidence unused)."""
    expected = scenario["expected"]
    if result["call"] not in (expected["call"], *expected.get("also_ok", ())):
        return "wrong_call"
    if result["call"] == "reach_now" and expected.get("track") and result.get("track") != expected["track"]:
        return "wrong_track"
    if result["call"] == "wait" and (window := expected.get("until_between")):
        if not (result["until"] and window[0] <= result["until"] <= window[1]):
            return "wrong_date"
    if result["call"] == expected["call"] and set(expected.get("cites", ())) - set(result["cited"]):
        return "wrong_reason"
    return "pass"


def evaluate(scenarios=None):
    rows = []
    with sealed():
        for s in scenarios if scenarios is not None else load():
            result = run(s)
            rows.append({"id": s["id"], "role": s["role"], "trap": s["trap"], "judgment": s["judgment"],
                         "expected": s["expected"], **result, "outcome": grade(s, result)})
    return rows


def summary(rows):
    scored = [r for r in rows if not r["judgment"]]
    out = {"passed": sum(r["outcome"] == "pass" for r in scored), "scored": len(scored),
           "judgment_agreed": sum(r["outcome"] == "pass" for r in rows if r["judgment"]),
           "judgment": len(rows) - len(scored)}
    out["pass_rate"] = round(out["passed"] / out["scored"], 3) if scored else 0.0
    out["roles"] = {}
    for role in sorted({r["role"] for r in scored}):
        mine = [r for r in scored if r["role"] == role]
        out["roles"][role] = f"{sum(r['outcome'] == 'pass' for r in mine)}/{len(mine)}"
    return out
