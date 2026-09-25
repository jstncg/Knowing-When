"""Rules that must hold for any input, checked over many seeded random ones: readiness's holds, tracks and confidence,
the contact gate's one card per person and stale signs, a run's caps, and the scorecard's p. A failure names its seed:
rerun that seed to see the input."""

from datetime import datetime, timedelta, timezone
from itertools import product
import json
from math import comb
from pathlib import Path
import random

import pytest

from app import contact, readiness, routing
from app import scorecard as sc
from app.readiness import Readiness
from app.store import Store
from app.timeline import DETECTORS

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-09-15T00:00:00+00:00"
DAY = datetime(2026, 9, 15, tzinfo=timezone.utc)
SEEDS = range(400)
HOLD_NAMES = {"hold_recent_promotion": "recent_promotion", "hold_short_tenure": "short_tenure",
              "hold_equity_refresh": "equity_refresh", "hold_imminent_launch": "imminent_launch",
              "hold_not_looking": "not_looking"}
ROLE_CLAIMS = ("accepted_next_role", "announced_next_role", "conflicting_role_claims")  # hold_role_claim's holds


def stamp(days):
    return (DAY + timedelta(days=days)).isoformat()


def activation(rng, detector_id, n):
    """One detector's window, somewhere around AS_OF, at a strength the detectors emit (a hold's is 1.0)."""
    holds = [HOLD_NAMES[detector_id]] if detector_id in HOLD_NAMES else \
        [rng.choice(ROLE_CLAIMS)] if detector_id == "hold_role_claim" else []
    opens = rng.randint(-120, 40)
    return {"detector_id": detector_id, "family": DETECTORS[detector_id][0],
            "strength": 1.0 if holds else rng.choice((0.3, 0.4, 0.5, 0.6, 0.7, 0.9)),
            "window_open": stamp(opens), "window_close": stamp(opens + rng.randint(1, 200)),
            "evidence_event_ids": [f"e{rng.randint(0, 2 * n)}" for _ in range(rng.randint(1, 2))], "holds": holds,
            "falsifier": "Check it." if rng.random() < 0.1 else ""}


def activations(rng, pool=tuple(DETECTORS), own_evidence=False):
    """1 to 6 activations; some share evidence unless ``own_evidence``, as each real detector reads its own events.
    A detector gives one window per event, so it never cites an event twice."""
    n, acts, cited = rng.randint(1, 6), [], set()
    for _ in range(n):
        a = activation(rng, rng.choice(pool), n)
        if not cited & {(a["detector_id"], e) for e in a["evidence_event_ids"]}:
            acts.append(a)
            cited |= {(a["detector_id"], e) for e in a["evidence_event_ids"]}
    return [{**a, "evidence_event_ids": [f"own{k}"]} for k, a in enumerate(acts)] if own_evidence else acts


def holding(acts):
    return [a for a in acts if a["window_open"] <= AS_OF <= a["window_close"]
            and (a["family"] in readiness.SUPPRESSORS or a["holds"])]


def test_a_hold_never_gives_reach_now_and_every_reach_rests_on_an_open_window():
    for seed in SEEDS:
        acts = activations(random.Random(seed))
        call = readiness.score(acts, AS_OF)
        assert 0 <= call.score <= 1, seed
        if holding(acts):
            assert call.action != "reach_now" and call.score <= readiness.HOLD_FACTOR, seed
        if any(set(a["holds"]) & set(readiness.OPT_OUT_HOLDS) for a in holding(acts)):
            assert call.action == "quiet", seed  # "not looking" silences everything else
        if call.action == "reach_now":
            assert call.open_windows and call.reasons and call.track in ("pitch", "rapport"), seed
        else:
            assert call.track is None, seed
        if call.action in ("watch_until", "respect_follow_up"):
            assert call.until and call.until >= AS_OF, seed  # a hold's window includes its last day


def test_confidence_is_high_only_for_a_pitch_on_converging_reasons_and_low_for_anything_but_a_reach():
    for seed in SEEDS:
        call = readiness.score(activations(random.Random(seed)), AS_OF)
        if call.action != "reach_now":
            assert call.confidence == "low", seed
        elif call.track == "rapport":  # one reason to write, however strong, is never high confidence
            assert call.confidence == "medium", seed
        else:
            assert call.confidence == ("high" if call.score >= readiness.STRONG else "medium"), seed


NEWS = tuple(readiness.NEWS)
EARLY = tuple(d for d, (family, _) in DETECTORS.items() if family in readiness.OPENERS)
DRIFT = tuple(d for d, (family, _) in DETECTORS.items() if family in readiness.SUPPORT)


def test_a_public_moment_is_never_a_pitch_with_early_signs_other_moments_or_drift_and_drift_never_reaches():
    # An early sign with drift is a pitch (drift toward GI's field is a sign they'd listen); a moment never is.
    for seed in SEEDS:
        assert readiness.score(activations(random.Random(seed), NEWS + EARLY), AS_OF).track != "pitch", seed
        assert readiness.score(activations(random.Random(seed), NEWS + DRIFT), AS_OF).track != "pitch", seed
        assert readiness.score(activations(random.Random(seed), DRIFT), AS_OF).action != "reach_now", seed


def test_their_own_word_or_a_forced_move_is_a_pitch_alone_when_nothing_holds():
    alone = [d for d, (family, _) in DETECTORS.items()
             if (family in readiness.SELF_EVIDENT or d in readiness.FORCED) and d != "stated_follow_up"]
    for seed in SEEDS:
        rng = random.Random(seed)
        a = activation(rng, rng.choice(alone), 1) | {"window_open": stamp(-rng.randint(0, 20)),
                                                     "window_close": stamp(rng.randint(1, 60))}
        others = [x for x in activations(rng) if x["family"] not in readiness.SUPPRESSORS | readiness.SELF_EVIDENT
                  and not x["holds"] and x["detector_id"] != a["detector_id"]]  # nothing holding, no date of theirs
        call = readiness.score([a, *others], AS_OF)
        assert (call.action, call.track) == ("reach_now", "pitch"), seed


def test_the_call_does_not_depend_on_the_order_the_detectors_ran_in_when_each_reads_its_own_events():
    # Not yet when two reasons of equal strength cite one event: the first to run claims it (readiness._reasons).
    for seed in SEEDS:
        rng = random.Random(seed)
        acts = activations(rng, own_evidence=True)
        first, second = readiness.score(acts, AS_OF), readiness.score(rng.sample(acts, len(acts)), AS_OF)
        assert (first.action, first.track, first.until, first.score, sorted(first.holds)) == \
               (second.action, second.track, second.until, second.score, sorted(second.holds)), seed


def test_a_role_that_watches_nothing_still_hears_their_own_word_forced_moves_signs_moments_and_holds():
    always = ["self_stated_availability", "stated_follow_up", "private_note", "own_departure", "placement_end",
              "company_closure", "work_in_progress", "technical_ask", "just_submitted", "paper_v1", "launch",
              "topic_drift", "hold_short_tenure", "hold_imminent_launch", "hold_not_looking"]
    chosen = ["warn_notice", "tenure_milestone", "exec_departure", "gi_citation", "similar_path"]
    acts = [{"detector_id": d, "family": DETECTORS[d][0]} for d in always + chosen]
    assert [a["detector_id"] for a in readiness.for_role(acts, frozenset())] == always
    assert [a["detector_id"] for a in readiness.for_role(acts, frozenset({"warn_notice"}))] == always + ["warn_notice"]
    assert readiness.for_role(acts, None) == acts  # no spec: every detector


def test_every_role_spec_selects_only_detectors_the_engine_has():
    for path in (ROOT / "config" / "role-specs").glob("*.json"):
        assert {d["id"] for d in json.loads(path.read_text())["detectors"]} <= set(DETECTORS), path.name


# ------------------------------------------------------------------------------------------------ the contact gate

REACH = Readiness(as_of=AS_OF, score=0.6, action="reach_now", families={}, reasons=["r"], holds=[], open_windows=[],
                  earliest_close=None, explanation="", evidence={"r": ["e1"]}, track="pitch")
ROLES = ("mts-research", "backend", "product-designer")


def entry(kind, days, **fields):
    extra = {"links": ["https://a.example/", "https://x.com/a"]} if kind == "identity" else \
        {"until": (DAY + timedelta(days=days + 3)).date().isoformat()} if kind == "invited" else {}
    return contact.Entry(person_id="P", kind=kind, at=stamp(days), **extra, **fields).model_dump()


def test_after_a_card_no_role_no_team_and_no_new_reason_clears_them_again_without_their_answer():
    for seed in SEEDS:
        rng = random.Random(seed)
        card = entry("pinged", -rng.randint(1, 900), role_id=rng.choice(ROLES),
                     team=rng.choice(("recruiting", "events", "marketing")))
        history = [card] + [entry(rng.choice(("identity", "checked", "invited", "follow_up", "sent", "pinged")),
                                  -rng.randint(0, 900), team=rng.choice(("recruiting", "events", "marketing", "sales")))
                            for _ in range(rng.randint(0, 5))]
        assert contact.check(history, AS_OF, REACH, role_id=rng.choice(ROLES)).state == "hold", seed


def test_a_sign_nobody_has_seen_or_checked_for_three_weeks_is_checked_first():
    for seed in SEEDS:
        rng = random.Random(seed)
        signs = [{"observed_at": stamp(-rng.randint(0, 50))} for _ in range(rng.randint(1, 3))]
        checks = [entry("checked", -rng.randint(0, 50)) for _ in range(rng.randint(0, 2))]
        newest = max([s["observed_at"] for s in signs] + [c["at"] for c in checks])
        stale = DAY - datetime.fromisoformat(newest) > timedelta(days=contact.STALE_DAYS)
        state = contact.check([entry("identity", -60), *checks], AS_OF, REACH, signals=signs).state
        assert state == ("check_first" if stale else "clear"), seed


# ----------------------------------------------------------------------------------------------- a run's caps

@pytest.fixture(scope="module")
def one_route(tmp_path_factory):
    store = Store(f"sqlite:///{tmp_path_factory.mktemp('caps') / 'r.sqlite'}")
    routing.seed(store, json.loads((ROOT / "tests/fixtures/routing/timeline.json").read_text()))
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00",
                   links=["https://rinvale.example/", "https://github.com/rinvale-example"])
    route = routing.route(store, "A900", AS_OF, team=[])
    assert route and route.gate.state == "clear" and route.outreach["checks"]["passes"]
    return route


def test_a_run_never_cards_past_the_caps_or_anyone_twice_and_accounts_for_everyone(one_route, tmp_path):
    for seed in SEEDS[:150]:
        rng = random.Random(seed)
        store = Store(f"sqlite:///{tmp_path / f'caps{seed}.sqlite'}")
        for n in range(rng.randint(0, 6)):  # earlier cards this week, older ones that no longer count, GTM's lines
            contact.record(store, f"old{n}", "pinged", at=stamp(-rng.randint(0, 12)), role_id=rng.choice(ROLES),
                           team=rng.choice(("recruiting", "recruiting", "sales")))
        found = []
        for _ in range(rng.randint(1, 9)):
            role, state = rng.choice(ROLES), rng.choice(("clear", "clear", "clear", "hold", "check_first"))
            found.append(one_route.model_copy(update={
                "subject_id": f"{role}:p{rng.randint(0, 5)}", "role": {**one_route.role, "id": role},  # some in two roles
                "gate": contact.Clearance(state=state, reason="" if state == "clear" else "held"),
                "outreach": {**one_route.outreach, "checks": {**one_route.outreach["checks"],
                                                              "passes": rng.random() > 0.15}},
                "call": one_route.call.model_copy(update={"score": rng.choice((0.3, 0.5, 0.7, 0.9))})}))
        kept, quiet = routing.pick(store, found, AS_OF)
        assert sorted((r.subject_id, r.role["id"]) for r in kept + quiet) == \
               sorted((r.subject_id, r.role["id"]) for r in found), seed
        assert all(r.gate.state == "clear" and r.outreach["checks"]["passes"] for r in kept), seed
        assert len({contact.person_key(r.subject_id) for r in kept}) == len(kept), seed
        for role in ROLES:
            assert sum(r.role["id"] == role for r in kept) <= contact.room(store, role, AS_OF), seed
        assert len(kept) <= contact.room_all(store, AS_OF), seed
        # And no fewer: each person with a card to give gets one unless their best role or the week is full.
        first = {}
        for r in routing.rank([r for r in found if r.gate.state == "clear" and r.outreach["checks"]["passes"]]):
            first.setdefault(contact.person_key(r.subject_id), r)
        carded = {contact.person_key(r.subject_id) for r in kept}
        for key, r in first.items():
            assert key in carded or len(kept) == contact.room_all(store, AS_OF) or \
                sum(k.role["id"] == r.role["id"] for k in kept) == contact.room(store, r.role["id"], AS_OF), seed


# ------------------------------------------------------------------------------------------------- the scorecard

def test_edge_p_is_the_one_sided_fisher_p_the_scorecard_quotes():
    def tail(seen, moments, alarms, ordinary):  # the hypergeometric tail, counted directly
        hits = seen + alarms
        return sum(comb(moments, k) * comb(ordinary, hits - k) for k in range(seen, min(moments, hits) + 1)) / \
            comb(moments + ordinary, hits)

    quoted = {"seen_coming": 4, "moments": 10, "fired_with_nothing_after": 5, "ordinary_stretches": 19}
    assert round(sc.edge_p(quoted), 2) == 0.36  # 4 of 10 moments against 5 of 19 ordinary stretches
    for seen, alarms in product(range(0, 11, 2), range(0, 20, 4)):
        s = {**quoted, "seen_coming": seen, "fired_with_nothing_after": alarms}
        assert sc.edge_p(s) == pytest.approx(tail(seen, 10, alarms, 19))
    assert sc.edge_p({**quoted, "seen_coming": 5}) < sc.edge_p(quoted)  # more seen coming, less likely luck
    assert sc.edge_p({**quoted, "moments": 0}) is None and sc.edge_p({**quoted, "ordinary_stretches": 0}) is None
