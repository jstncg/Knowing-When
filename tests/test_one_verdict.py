"""Every surface shows the verdict readiness gives, on every golden scenario.

The golden replay runs detectors.view, journey.detect for the scenario's role and
readiness.score (golden.run); the UI decision (engine.decide), the review inbox
(sequence.internal_proposal) and the Slack card (routing.route) read
journey.assess for the same role. Whatever the model said
about the hook, the UI is ready, needs review to verify, or watches exactly when
the replay says reach now, verify first, or wait / not now; the UI's timing
label reads the same readiness (tests/test_ui_refresh.cjs).
"""

from copy import deepcopy
from datetime import timedelta

import pytest

from app import golden, journey, routing
from app.engine import decide, review_hash
from app.models import iso, parse_time
from app.sequence import internal_proposal
from app.store import Store
from app.timeline import add_event
from tests.test_engine_adversarial import ready_candidate, role

SCENARIOS = golden.load()
UI_STATE = {"reach_now": "ready", "verify_first": "needs_review", "wait": "watch", "not_now": "watch"}
WATCH = {"status": "active", "brief_version": 1, "fit_basis": "Fictional golden scenario"}
CHECKED = {"entailment": None, "superseded_by": [], "sources_checked": 1, "errors": []}


def reviewed(subject, now, timing_action):
    """A reviewed, complete candidate for the scenario's person with one hook the model called ``timing_action``."""
    c = deepcopy(ready_candidate())
    day = now - timedelta(days=1)
    c.update(id="candidate:golden", person_id=subject, revision=1, reviewed_at=iso(now))
    for e in c["evidence"]:
        e.update(observed_at=iso(day), event_date=day.date().isoformat())
    c["events"][0].update(date=day.date().isoformat(), timing_action=timing_action, kill_pass=CHECKED)
    if timing_action == "watch":
        c["events"][0]["timing_assessment"] = None  # a model watch carries no why-now of its own
    c["review_hash"] = review_hash(c, role())
    return c


@pytest.mark.parametrize("s", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_the_ui_the_inbox_and_the_slack_card_show_the_replay_verdict(s):
    as_of = golden.stamp(s["as_of"])
    store = Store("sqlite://")
    for e in s["events"]:
        add_event(store, golden.record(s, e))
    journey.save_context(store, journey.PersonContext(
        subject_id=s["subject"], name="Golden Example", role=s["role"], related=s["related"]))
    with golden.sealed():
        replay = golden.run(s)
        live = journey.assess(store, s["subject"], as_of, s["role"])
        card = routing.route(store, s["subject"], as_of, team=[])
    assert (live.action, live.until and live.until[:10]) == (replay["action"], replay["until"])
    assert (card is not None) == (replay["call"] == "reach_now")
    now = parse_time(as_of)
    for timing_action in ("watch", "contact_now"):
        c = reviewed(s["subject"], now, timing_action)
        decision = decide(c, role(), now=now, readiness=live)
        assert decision["state"] == UI_STATE[replay["call"]], decision["reasons"]
        assert decision["readiness"]["action"] == replay["action"]
        proposal = internal_proposal(c, role(), WATCH, now=now, readiness=live)
        assert (proposal is not None) == (replay["call"] == "reach_now")
    # With no researched hook yet the UI still shows the verdict: reach now waits only on research.
    bare = decide({**reviewed(s["subject"], now, "watch"), "events": []}, role(), now=now, readiness=live)
    assert bare["state"] == {"reach_now": "research", "verify_first": "needs_review"}.get(replay["call"], "watch")
