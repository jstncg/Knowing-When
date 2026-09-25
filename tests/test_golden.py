"""The golden set itself: well-formed, balanced, covers every trap, and runs offline and deterministically.

The engine's pass rate is reported by scripts/golden.py, not asserted here.
"""

import socket

import pytest

from app import golden
from app.models import parse_time
from app.timeline import TimelineEvent

SCENARIOS = golden.load()
TRAPS = {"later_job_announced", "contact_me_after", "recent_promotion", "conflicting_claims",
         "weak_signal_alone", "stale_signal", "announced_future_date", "observed_after_as_of"}
# What real cards for backend and designers have hit: a release or work in progress (positive), their own "I'm open",
# a first-year hold, a layoff post.
LIVE_TRAPS = {"positive", "self_stated_window", "recent_move", "forced_move"}


def test_size_balance_and_trap_coverage():
    assert {s["role"] for s in SCENARIOS} == {"mts", "controller", "backend", "product-designer"}
    for role, traps in (("mts", TRAPS), ("controller", TRAPS), ("backend", LIVE_TRAPS), ("product-designer", LIVE_TRAPS)):
        assert traps <= {s["trap"] for s in SCENARIOS if s["role"] == role and not s["judgment"]}
    assert len({s["id"] for s in SCENARIOS}) == len(SCENARIOS)


@pytest.mark.parametrize("s", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario_is_well_formed(s):
    expected, ids = s["expected"], [e["id"] for e in s["events"]]
    calls = set(golden.CALL.values())
    assert expected["call"] in calls and set(expected.get("also_ok", ())) <= calls
    assert len(expected["reason"]) > 20 and len(ids) == len(set(ids))
    for e in s["events"]:
        TimelineEvent.model_validate(golden.record(s, e))
    as_of = parse_time(golden.stamp(s["as_of"]))
    visible = {e["id"] for e in s["events"] if parse_time(golden.stamp(e["observed"])) <= as_of}
    assert set(expected.get("cites", ())) <= visible, "a call can only rest on what was observed by as_of"
    if window := expected.get("until_between"):
        assert "wait" in (expected["call"], *expected.get("also_ok", ())) and window[0] <= window[1]
    assert expected["call"] != "wait" or "until_between" in expected


def test_reference_cases():
    by_id = {s["id"]: s for s in SCENARIOS}
    assert by_id["mts-01-researcher-k-in-window"]["expected"]["call"] == "reach_now"
    assert by_id["mts-03-researcher-y-conflicting-deepmind"]["expected"]["call"] == "verify_first"


def test_leak_scenarios_hide_the_later_event():
    rows = {r["id"]: r for r in golden.evaluate([s for s in SCENARIOS if s["trap"] == "observed_after_as_of"])}
    assert len(rows) >= 2 and all(r["hidden"] and not set(r["hidden"]) & set(r["cited"]) for r in rows.values())


def test_runs_offline_and_deterministically():
    first = golden.evaluate()
    assert first == golden.evaluate() and len(first) == len(SCENARIOS)
    assert {r["outcome"] for r in first} <= {"pass", "wrong_call", "wrong_date", "wrong_reason"}
    stats = golden.summary(first)
    assert stats["scored"] + stats["judgment"] == len(SCENARIOS) and 0 <= stats["pass_rate"] <= 1
    assert set(stats["roles"]) == {s["role"] for s in SCENARIOS if not s["judgment"]}


def test_sealed_blocks_network():
    with golden.sealed(), pytest.raises(RuntimeError, match="offline"):
        socket.create_connection(("example.com", 443), timeout=1)


def test_grade_checks_call_date_and_evidence():
    s = {"expected": {"call": "wait", "until_between": ["2026-12-01", "2026-12-31"], "cites": ["ask"], "reason": "r"}}
    base = {"call": "wait", "until": "2026-12-05", "cited": ["ask"]}
    assert golden.grade(s, base) == "pass"
    assert golden.grade(s, {**base, "call": "reach_now"}) == "wrong_call"
    assert golden.grade(s, {**base, "until": "2027-02-01"}) == "wrong_date"
    assert golden.grade(s, {**base, "cited": []}) == "wrong_reason"
