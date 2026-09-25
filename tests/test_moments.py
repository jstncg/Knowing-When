"""Moment planner: windows ahead, falsifiers, recheck dates, pre-registration, and no leakage."""

from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import detectors, moments, readiness
from app.engine import digest
from app.store import Store
from app.timeline import add_event
from tests.test_detectors import ME, ORG, ev

ROLE = {"id": "controller"}
RELATED = (ORG, "gi")


def at(day):
    return f"{day}T00:00:00+00:00"


def cliff_store(tmp_path):
    """Acquisition closed 2025-04-02: retention cliffs open 2026-03-03 (12 months) and 2027-03-03 (24 months)."""
    store = Store(f"sqlite:///{tmp_path / 'm.sqlite'}")
    add_event(store, ev(ME, "job_started", "2023-07-01", quote="Assistant Controller, Acme"))
    add_event(store, ev(ORG, "acquisition_closed", "2025-04-02", quote="Acme completed its acquisition by Globex"))
    return store


def contract_store(tmp_path):
    """A fixed-term controller, each end announced months ahead: the first contract ends 2026-06-01 (its window opens
    2026-03-03), the renewal 2027-06-01 (opens 2027-03-03)."""
    store = Store(f"sqlite:///{tmp_path / 'm.sqlite'}")
    add_event(store, ev(ME, "placement_end_expected", "2026-06-01", observed=at("2025-10-01"),
                        quote="Interim Controller, Acme: contract through May 2026"))
    add_event(store, ev(ME, "placement_end_expected", "2027-06-01", observed=at("2026-05-01"),
                        quote="Interim Controller, Acme: contract renewed through May 2027"))
    return store


def test_not_now_names_the_window_the_recheck_and_the_falsifiers(tmp_path):
    plan = moments.plan_moments(contract_store(tmp_path), ME, ROLE, at("2026-09-22"), RELATED)
    assert plan["headline"] == "Not now; the window opens March 3; recheck February 17."
    [end] = plan["moments"]  # the first contract's window closed in July; nothing else is within 180 days
    assert (end["detector_id"], end["window_open"][:10], end["next_check_at"][:10]) == (
        "placement_end", "2027-03-03", "2027-02-17")
    hypothesis = end["hypothesis"]
    assert hypothesis["expected_window"] == "2027-03-03 to 2027-07-01" and hypothesis["role_id"] == "controller"
    assert "An extension or a new end date is announced" in hypothesis["falsifiers"]
    # The call follows readiness: a contract ending is a date to wait for.
    assert (plan["action"], plan["readiness"]["until"][:10]) == ("watch_until", "2027-03-03")
    assert plan["next_check_at"] == end["next_check_at"]


def test_recheck_dates_lead_then_open_then_weekly():
    opens, closes = datetime(2027, 3, 3, tzinfo=timezone.utc), datetime(2027, 6, 1, tzinfo=timezone.utc)
    check = lambda day: moments.next_check_at(opens, closes, datetime.fromisoformat(at(day))).date().isoformat()
    assert check("2026-09-22") == "2027-02-17"
    assert check("2027-02-17") == "2027-03-03"
    assert check("2027-03-03") == "2027-03-10"
    assert check("2027-03-12") == "2027-03-17"
    assert check("2027-05-30") == "2027-06-01"  # never past the close


def test_their_next_role_before_the_window_closes_falsifies_it(tmp_path):
    store = contract_store(tmp_path)
    add_event(store, ev(ME, "job_started", "2026-06-15", observed=at("2026-01-20"), quote="Controller, Birchwood"))
    plan = moments.plan_moments(store, ME, ROLE, at("2026-02-01"), RELATED)
    assert [m["detector_id"] for m in plan["moments"]] == []  # the new role's own windows are holds
    [broken] = plan["falsified"]
    assert broken["detector_id"] == "placement_end" and broken["window_open"][:10] == "2026-03-03"
    assert len(broken["falsified_by"]) == 1


def test_undated_or_earlier_events_do_not_falsify_a_statement():
    said = ev(ME, "self_stated_availability", observed=at("2026-09-10"), quote="seeking full-time roles")
    old_post = ev(ME, "job_started", observed=at("2026-09-21"), quote="Research intern, Example Lab")
    earlier = ev(ME, "job_started", "2021-06-01", observed=at("2026-09-21"), quote="Intern, Other Lab")
    later = ev(ME, "job_started", "2026-10-01", observed=at("2026-10-02"), quote="Research Scientist, Y")
    events = [said, old_post, earlier, later]
    [window] = [a for a in detectors.detect(events, ME, at("2026-10-03")) if a["detector_id"] == "self_stated_availability"]
    assert moments.falsified_by(window, events, ME) == [later["id"]]


def test_nothing_observed_after_as_of_changes_the_plan(tmp_path):
    store = contract_store(tmp_path)
    before = moments.plan_moments(store, ME, ROLE, at("2026-01-01"), RELATED)
    assert before["headline"] == "Not now; the window opens March 3; recheck February 17."
    # A falsifier, a new signal and a stated date, all observed after as_of, some dated before it.
    add_event(store, ev(ME, "job_started", "2026-06-15", observed=at("2026-01-02"), quote="Controller, Birchwood"))
    add_event(store, ev(ORG, "auditor_change", "2026-01-05", quote="Acme dismissed its auditor"))
    add_event(store, ev(ME, "self_stated_availability", "2026-02-01", observed=at("2026-01-10"), quote="free from February"))
    assert moments.plan_moments(store, ME, ROLE, at("2026-01-01"), RELATED) == before
    assert moments.plan_moments(store, ME, ROLE, at("2026-01-03"), RELATED)["falsified"][0]["detector_id"] == "placement_end"
    assert moments.plan_moments(store, ME, ROLE, at("2026-01-11"), RELATED) != before


def test_windows_are_preregistered_once(tmp_path):
    store, log = contract_store(tmp_path), tmp_path / "predictions.jsonl"
    near = moments.plan_moments(store, ME, ROLE, at("2026-01-01"), RELATED)  # opens in 61 days
    moments.save_plan(store, near, log)
    moments.save_plan(store, moments.plan_moments(store, ME, ROLE, at("2026-01-08"), RELATED), log)
    [prediction] = moments.read_predictions(log)
    assert prediction["as_of"] == at("2026-01-01") and prediction["action"] == "watch_until"
    assert prediction["windows"][0]["open"][:10] == "2026-03-03"
    assert [h["expected_window"] for h in store.all("moment_hypothesis")] == ["2026-03-03 to 2026-07-01"]

    far = moments.plan_moments(store, ME, ROLE, at("2026-09-22"), RELATED)  # opens in 162 days: not yet
    moments.save_plan(store, far, log)
    assert len(moments.read_predictions(log)) == 1


def test_prediction_log_is_hashed_and_round_trips(tmp_path):
    log = tmp_path / "predictions.jsonl"
    assert moments.read_predictions(log) == []
    record = moments.write_prediction("person:a", "2026-01-01", "watch_until", [{"detector_id": "retention_cliff"}],
                                      {"score": 0.4}, log)
    inputs = {k: record[k] for k in ("subject_id", "as_of", "action", "windows", "readiness")}
    assert record["as_of"] == at("2026-01-01") and record["recorded_at"]
    assert record["input_hash"] == digest(inputs)
    assert moments.read_predictions(log) == [record]


def test_due_rechecks_are_idempotent_and_follow_new_evidence(tmp_path):
    store, log, watched = contract_store(tmp_path), tmp_path / "predictions.jsonl", [(ME, ROLE, RELATED)]
    saved, next_at = moments.due_rechecks(store, at("2026-01-01"), watched, log)
    assert len(saved) == 1 and next_at.date().isoformat() == "2026-02-17"
    assert moments.due_rechecks(store, at("2026-01-01"), watched, log)[0] == []
    assert moments.due_rechecks(store, at("2026-02-16"), watched, log)[0] == []

    [recheck], next_at = moments.due_rechecks(store, at("2026-02-17"), watched, log)
    assert recheck["as_of"] == at("2026-02-17") and next_at.date().isoformat() == "2026-03-03"
    # Their next role is announced before the next check date: the plan is replanned at once and the end falls.
    add_event(store, ev(ME, "job_started", "2026-06-15", observed=at("2026-02-20"), quote="Controller, Birchwood"))
    [replanned], _ = moments.due_rechecks(store, at("2026-02-21"), watched, log)
    assert replanned["moments"] == [] and replanned["falsified"][0]["detector_id"] == "placement_end"
    assert moments.due_rechecks(store, at("2026-02-21"), watched, log)[0] == []
    assert len(moments.read_predictions(log)) == 1


def test_people_no_longer_watched_are_not_rechecked(tmp_path):
    store, log, watched = contract_store(tmp_path), tmp_path / "predictions.jsonl", [(ME, ROLE, RELATED)]
    moments.due_rechecks(store, at("2025-11-01"), watched, log)  # the window opens in 122 days: not yet registered
    assert moments.read_predictions(log) == []
    # Unwatched on the recheck date: no replan, no prediction, nothing to wake for.
    assert moments.due_rechecks(store, at("2026-02-17"), [], log) == ([], None)
    assert moments.read_predictions(log) == [] and store.all("moment_plan")[0]["as_of"] == at("2025-11-01")
    # Watched again: it is due at once and the window, now 14 days out, is registered.
    [plan], _ = moments.due_rechecks(store, at("2026-02-17"), watched, log)
    assert plan["as_of"] == at("2026-02-17") and len(moments.read_predictions(log)) == 1


def test_a_sign_a_call_only_lists_never_heads_a_plan_or_goes_in_the_log(tmp_path):
    """Time in a seat, a retention cliff, the pattern before their past moves and a path like people GI hired are
    listed on a call, never a reason (readiness.CONTEXT_ONLY); an early sign on GitHub code alone is never one by
    itself. None is planned, rechecked, headlined or pre-registered as a window, whatever the day. Failure modes: the
    cliff, a work anniversary, the pattern or a matched path heads the plan or is logged; a push with nothing beside
    it is planned; a push beside a fresh post (which it backs up) is not."""
    store, log = cliff_store(tmp_path), tmp_path / "predictions.jsonl"  # cliffs from 2026-03-03, 4 years in 2027-07
    for day in ("2025-11-01", "2026-01-01", "2026-03-10", "2026-09-22", "2027-01-15", "2027-05-01"):  # the deal: closed
        plan = moments.plan_moments(store, ME, ROLE, at(day), RELATED)
        assert not {m["detector_id"] for m in plan["moments"] + plan["falsified"]} & readiness.CONTEXT_ONLY, day
        assert plan["headline"] == "Nothing in view within 180 days.", day
        moments.save_plan(store, plan, log)
    assert moments.read_predictions(log) == [] and store.all("moment_hypothesis") == []

    def planned(role, *events, day="2026-09-22"):
        store = Store("sqlite://")
        ids = {e["id"]: add_event(store, e)["id"] for e in events}
        plan = moments.plan_moments(store, ME, {"id": role}, at(day), RELATED)
        return {m["detector_id"] for m in plan["moments"]}, {i for m in plan["moments"] for i in m["evidence_event_ids"]}, ids

    past = [ev(ME, "paper_v1", "2020-09-01"), ev(ME, "job_started", "2021-01-01"),
            ev(ME, "paper_v1", "2023-08-01"), ev(ME, "job_started", "2024-01-01"), ev(ME, "paper_v1", "2026-08-01")]
    assert "own_precedent" not in planned("mts-research", *past)[0]
    matched = ev(ME, "similar_path", "2026-09-10", quote="Their path matches a past hire's before joining")
    assert "similar_path" not in planned("no-spec-role", matched)[0]  # a role without a spec watches every detector

    repo = "https://github.com/me/queuekit"
    seen = at("2026-09-15")
    push = [ev(ME, "github_activity", "2026-09-15", observed=seen, source_url=repo, quote="Pushed to me/queuekit: Ack path"),
            ev(ME, "work_in_progress", "2026-09-15", observed=seen, source_url=repo, quote="Pushed to me/queuekit: Ack path")]
    assert planned("backend", *push)[0] == set()
    url, seen = "https://x.com/me/status/1", at("2026-09-18")
    post = [ev(ME, "x_post", "2026-09-18", observed=seen, source_url=url, quote="Rebuilding our queue around acks"),
            ev(ME, "work_in_progress", "2026-09-18", observed=seen, source_url=url, quote="Rebuilding our queue around acks")]
    kinds, evidence, ids = planned("backend", *push, *post)
    assert kinds == {"work_in_progress"} and {ids[push[1]["id"]], ids[post[1]["id"]]} <= evidence  # backed up
