"""Readiness: decay, dedupe, the two-reason rule, holds, and the two role scenarios."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import detectors, journey, readiness
from app.models import iso
from app.store import Store
from app.timeline import add_event, add_private_note
from tests.test_detectors import CFO, ME, ORG, ev

AS_OF = "2026-09-22T00:00:00+00:00"


def assess(store, subject, as_of, related=()):
    """View, detect and score with an explicit related list (journey.assess reads it from the person's context)."""
    events = detectors.view(store, subject, as_of, related)
    return readiness.score(detectors.detect(events, subject, as_of), as_of, events)


def activation(detector_id, family, strength=0.5, opens="2026-09-01", closes="2026-10-01", evidence=("e1",), holds=(),
               falsifier=""):
    return {"detector_id": detector_id, "family": family, "strength": strength,
            "window_open": f"{opens}T00:00:00+00:00", "window_close": f"{closes}T00:00:00+00:00",
            "evidence_event_ids": list(evidence), "holds": list(holds), "falsifier": falsifier}


def test_strength_decays_from_window_close_by_family_half_life():
    a = activation("exec_departure", "career", strength=0.8, closes="2026-08-08")  # closed 45 days ago
    assert readiness.score([a], AS_OF).families == {"career": 0.4}
    b = activation("gi_citation", "gi", strength=0.8, closes="2026-09-01")  # closed 21 days ago
    assert readiness.score([b], AS_OF).families == {"gi": 0.4}
    assert readiness.score([activation("paper_v1", "work", opens="2026-10-01", closes="2026-11-01")], AS_OF).families == {}


def test_a_preprint_and_its_acceptance_are_one_new_work_reason_and_upcoming_windows_are_watched():
    two = [activation("paper_v1", "work", 0.4), activation("paper_accepted", "work", 0.5, evidence=("e2",))]
    scored = readiness.score(two, AS_OF)
    assert scored.families == {"work": 0.7} and scored.reasons == ["new_work"]
    assert (scored.action, scored.track, scored.score) == ("reach_now", "rapport", 0.5)  # one reason, capped: a note
    stale = readiness.score([activation("paper_v1", "work", 0.4, opens="2026-08-01", closes="2026-10-01")], AS_OF)
    assert stale.action == "quiet"  # a paper out opens a note for about a month (NEWS_DAYS)
    ahead = readiness.score([activation("grant_end", "employer", opens="2026-12-01", closes="2027-01-01")], AS_OF)
    assert ahead.action == "watch_until" and ahead.until[:10] == "2026-12-01" and "when the next window opens (their grant is ending)" in ahead.explanation


def test_old_closed_windows_of_one_reason_never_add_up_and_the_strongest_counts_as_it_would_alone():
    # Three posts months ago saying they are available: each has faded below the floor (0.07, 0.10, 0.17), and
    # together they would stack past it (0.30). Beside a launch that is a note, never a pitch.
    said = [activation("self_stated_availability", "self", 0.6, opens=f"2026-0{m}-01", closes=f"2026-0{m + 1}-01",
                       evidence=(f"said{m}",)) for m in (4, 5, 6)]
    launch = activation("launch", "work", 0.5, opens="2026-09-15", closes="2026-10-15", evidence=("launch",))
    assert readiness.REASON_FLOOR < readiness._stack(readiness._at(a, AS_OF) for a in said)
    scored = readiness.score([launch, *said], AS_OF)
    assert (scored.action, scored.track, scored.reasons, scored.evidence) == ("reach_now", "rapport", ["launch"],
                                                                              {"launch": ["launch"]})
    # One closed window strong enough alone still counts, as strong as it is alone, beside an older one.
    strong = activation("self_stated_availability", "self", 0.9, opens="2026-07-01", closes="2026-08-01",
                        evidence=("strong",))
    alone, beside = readiness.score([launch, strong], AS_OF), readiness.score([launch, strong, said[0]], AS_OF)
    assert (alone.track, sorted(alone.reasons)) == ("pitch", ["launch", "self_stated_availability"])
    assert (beside.score, beside.track, beside.reasons) == (alone.score, alone.track, alone.reasons)
    no_start = {**said[0], "window_open": None}  # a closed window with no start is closed all the same
    assert readiness.score([launch, strong, no_start], AS_OF).score == alone.score
    # Open windows all stack, as before.
    both = [activation("self_stated_availability", "self", 0.2, evidence=(f"open{n}",)) for n in (1, 2)]
    assert sorted(readiness.score([launch, *both], AS_OF).reasons) == ["launch", "self_stated_availability"]


def test_old_closed_windows_still_carry_their_evidence_so_no_other_reason_is_freed_by_them():
    # Four preprints and their acceptance, all closed: new work on the acceptance alone (0.40). The quiet the
    # preprints' cadence sets rests on the same preprints, so it is not a second reason: quiet, as before.
    preprints = [activation("paper_v1", "work", 0.5, opens=f"2026-0{m}-01", closes=f"2026-0{m}-10",
                            evidence=(f"p{m}",)) for m in (3, 4, 5, 6)]
    accepted = activation("paper_accepted", "work", 0.6, opens="2026-08-12", closes="2026-09-12", evidence=("acc",))
    quiet = activation("rhythm_change", "work", 0.3, evidence=tuple(f"p{m}" for m in (3, 4, 5, 6)))
    scored = readiness.score([*preprints, accepted, quiet], AS_OF)
    assert scored.action == "quiet" and scored.reasons == ["new_work"]
    assert scored.evidence["new_work"][-4:] == ["p6", "p5", "p4", "p3"]  # listed behind it, as before


def test_separate_windows_from_one_event_both_count():
    cliffs = [activation("retention_cliff", "career", 0.5, opens="2026-09-01", closes="2026-10-01"),
              activation("retention_cliff", "career", 0.6, opens="2027-09-01", closes="2027-10-01")]
    assert len(readiness.dedupe(cliffs)) == 2


def test_two_reports_of_one_event_count_once():
    first, second = ev(CFO, "officer_departure", "2026-09-01"), ev(CFO, "officer_departure", "2026-09-02")
    twice = [activation("exec_departure", "career", 0.5, evidence=(first["id"],)),
             activation("exec_departure", "career", 0.5, evidence=(second["id"],))]
    assert readiness.score(twice, AS_OF, [first, second]).families == {"career": 0.5}
    assert readiness.score(twice, AS_OF).families == {"career": 0.75}  # without the events, ids are all we know


def test_two_independent_reasons_reach_now_one_reason_is_not_now():
    one = readiness.score([activation("exec_departure", "career", 0.9)], AS_OF)
    assert one.action == "quiet" and one.score == 0.5 and one.reasons == ["exec_departure"]
    assert "not enough to reach out on its own" in one.explanation
    both = readiness.score([activation("exec_departure", "career", 0.9),
                            activation("paper_accepted", "work", 0.5, evidence=("e2",))], AS_OF)
    assert both.action == "reach_now" and both.score == 0.95 and len(both.reasons) == 2
    assert both.earliest_close[:10] == "2026-10-01" and "2 independent reasons" in both.explanation
    shared = readiness.score([activation("acquisition_closed", "employer", 0.5),
                              activation("retention_cliff", "career", 0.5)], AS_OF)
    assert shared.reasons == ["acquisition_closed"] and shared.action == "quiet"  # the cliff is never a reason
    tenure = readiness.score([activation("tenure_milestone", "career", 0.6),
                              activation("retention_cliff", "career", 0.6, evidence=("e2",)),
                              activation("paper_accepted", "work", 0.5, evidence=("e3",))], AS_OF)
    assert (tenure.action, tenure.track, tenure.reasons) == ("reach_now", "rapport", ["new_work"])  # never a pitch


def test_only_a_named_fact_to_check_verifies_first():
    check = "Find out why the filing was late and whether it lands on them: 'NT 10-Q' (no URL)."
    late = activation("late_filing", "employer", 0.6, falsifier=check)
    alone = readiness.score([late], AS_OF)
    assert alone.action == "verify_first" and alone.falsifiers[0] == check
    with_cfo = readiness.score([late, activation("exec_departure", "career", 0.5, evidence=("e2",))], AS_OF)
    assert with_cfo.action == "reach_now" and check in with_cfo.falsifiers


def test_private_note_self_statement_or_forced_move_is_enough_alone():
    assert readiness.score([activation("private_note", "private", 0.7)], AS_OF).action == "reach_now"
    for forced, family in (("placement_end", "career"), ("own_departure", "career"), ("company_closure", "employer")):
        assert readiness.score([activation(forced, family, 0.6)], AS_OF).action == "reach_now"
    accepted = activation("hold_role_claim", "holds", 1.0, opens="2026-09-01", closes="2027-12-01", evidence=("e9",),
                          holds=("accepted_next_role",))
    assert readiness.score([activation("own_departure", "career", 0.6), accepted], AS_OF).action == "watch_until"
    stated = readiness.score([activation("stated_follow_up", "self", 0.9, opens="2026-11-01", closes="2027-01-01"),
                              activation("exec_departure", "career", 0.9),
                              activation("paper_accepted", "work", 0.5, evidence=("e2",))], AS_OF)
    assert stated.action == "respect_follow_up" and stated.until[:10] == "2026-11-01"


def test_a_note_reaches_now_only_when_typed_open_and_a_wait_note_holds_until_its_day():
    reasons = (ev(ME, "paper_accepted", "2026-09-10", quote="Scaling laws II: Accept (NeurIPS 2026)"),
               ev(ME, "gi_citation", "2026-09-12", quote="GI's interpretability post cites Scaling laws II"))

    def call(event_type, *others, day="2026-09-15", as_of=AS_OF):
        store = Store("sqlite://")
        note = ev(ME, event_type, day, observed="2026-09-15T18:00:00+00:00", tier=0, quote="Met at the GI dinner.")
        for event in (note, *others):
            add_event(store, event)
        return assess(store, ME, as_of, ["gi"])

    assert call("note_open").action == "reach_now"
    assert call("private_note").action == "quiet"
    assert call("private_note", *reasons).action == "reach_now"  # context neither counts nor holds
    held = call("note_wait", *reasons, day="2027-03-01")
    assert held.action == "respect_follow_up" and held.until[:10] == "2027-03-01"
    assert "A GI teammate's note says not until 2027-03-01." in held.explanation
    assert call("note_wait", *reasons, day="2028-01-01").until[:10] == "2028-01-01"
    assert call("note_wait", day="2027-03-01", as_of="2027-03-02T00:00:00+00:00").action == "reach_now"


def test_a_wording_fix_keeps_a_note_in_its_place_and_a_new_note_replaces_it():
    store = Store("sqlite://")
    alice = add_event(store, ev(ME, "note_open", "2026-09-15", observed="2026-09-15T18:00:00+00:00", tier=0,
                                quote="Open to a mvoe."))
    add_event(store, ev(ME, "note_wait", "2027-03-01", observed="2026-09-20T18:00:00+00:00", tier=0,
                        quote="Not until her March vest."))
    fixed = add_private_note(store, ME, "Open to a move.", "Alice", supersedes=alice["id"], note_type="open")
    assert (fixed["observed_at"], fixed["event_date"]) == (alice["observed_at"], "2026-09-15")
    held = assess(store, ME, AS_OF, ["gi"])  # the newer wait still holds; the fix restarts no window
    assert held.action == "respect_follow_up" and held.until[:10] == "2027-03-01"
    add_private_note(store, ME, "Looking now.", "Alice", note_type="open")
    assert assess(store, ME, iso(), ["gi"]).action == "reach_now"
    # A fix may keep a day that has passed; moving it to another past day is a new read, and refused.
    lapsed = add_event(store, ev(ME, "note_wait", "2020-06-01", observed="2020-05-01T18:00:00+00:00", tier=0,
                                 quote="Not until Jnue."))
    kept = add_private_note(store, ME, "Not until June.", "Ops", supersedes=lapsed["id"], note_type="wait",
                            wait_until="2020-06-01")
    assert kept["observed_at"] == lapsed["observed_at"]
    with pytest.raises(ValueError):
        add_private_note(store, ME, "Not until July.", "Ops", supersedes=kept["id"], note_type="wait",
                         wait_until="2020-07-01")


def test_holds_beat_signals():
    hold = activation("hold_short_tenure", "holds", 1.0, opens="2026-03-01", closes="2027-03-01", holds=("short_tenure",))
    one = readiness.score([hold, activation("exec_departure", "career", 0.9)], AS_OF)
    # They just started a job: nothing reaches before their first year is out, so the call says when that is.
    assert (one.action, one.until[:10], one.holds) == ("watch_until", "2027-03-01", ["short_tenure"])
    assert one.score == 0.15  # single reason cap, then the hold factor
    reasons = [activation("exec_departure", "career", 0.9), activation("paper_accepted", "work", 0.5, evidence=("e2",))]
    two = readiness.score([hold, *reasons], AS_OF)
    assert two.action == "watch_until" and two.until[:10] == "2027-03-01" and "Holding back: under a year in their current role" in two.explanation
    promoted = activation("hold_recent_promotion", "holds", 1.0, opens="2026-08-01", closes="2027-02-01",
                          evidence=("e3",), holds=("recent_promotion",))
    assert readiness.score([hold, promoted, *reasons], AS_OF).until[:10] == "2027-03-01"  # the last hold to lift
    grant = activation("hold_equity_refresh", "holds", 1.0, opens="2026-08-01", closes="2027-05-01", holds=("retention_grant",))
    assert readiness.score([hold, grant], AS_OF).until[:10] == "2027-05-01"  # the last hold to lift, as the text says
    alone = readiness.score([hold], AS_OF)
    assert (alone.action, alone.until[:10]) == ("watch_until", "2027-03-01") and "when the last hold lifts" in alone.explanation
    optout = activation("hold_not_looking", "holds", 1.0, opens="2026-09-05", closes="2027-09-05", holds=("not_looking",))
    assert readiness.score([optout, *reasons], AS_OF).action == "quiet"  # an opt-out beats any number of reasons
    assert readiness.score([], AS_OF).explanation == "No window open on 2026-09-22."


def controller_store(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'c.sqlite'}")
    for event in (
        ev(ME, "job_started", "2023-03-01", quote="Assistant Controller, Acme", tier=3),
        ev(ORG, "acquisition_closed", "2025-08-15", quote="Acme completed its acquisition by Globex"),
        ev(CFO, "officer_departure", "2026-06-10", quote="Jane Doe resigned as Chief Financial Officer"),
        ev(ORG, "auditor_change", "2026-08-01", quote="Acme dismissed its auditor; Globex's auditor engaged"),
    ):
        add_event(store, event)
    return store


def test_controller_scenario(tmp_path):
    store = controller_store(tmp_path)
    related = [ORG, CFO]
    after_deal = assess(store, ME, "2025-09-01T00:00:00+00:00", related)
    # One reason each time: not enough alone. The 12-month retention cliff is never a date to wait for.
    assert after_deal.open_windows == ["acquisition_closed"] and after_deal.action == "quiet"
    cfo_gone = assess(store, ME, "2026-06-20T00:00:00+00:00", related)
    assert cfo_gone.open_windows == ["exec_departure"] and cfo_gone.action == "quiet"
    # At the cliff the CFO leaving is still one reason: the cliff is listed, never a reason.
    cliff = assess(store, ME, "2026-07-20T00:00:00+00:00", related)
    assert cliff.open_windows == ["exec_departure"] and cliff.reasons == ["exec_departure"] and cliff.action == "quiet"
    assert "employer" not in cliff.families  # the deal's window closed in February: never counted on its fading tail
    assert "Listed, never a reason: a year or two after their employer's acquisition." in cliff.explanation
    audit = assess(store, ME, "2026-08-20T00:00:00+00:00", related)
    assert audit.open_windows == ["exec_departure", "auditor_change"]
    assert len(audit.reasons) == 2 and audit.action == "reach_now"
    assert audit.earliest_close[:10] == "2026-09-08" and "Earliest window closes 2026-09-08" in audit.explanation
    # Quarter close: a finance role goes on hold for the first days of October.
    add_event(store, ev(ME, "officer_appointment", "2026-09-15", quote="appointed Controller"))
    october = assess(store, ME, "2026-10-03T00:00:00+00:00", related)
    assert october.action == "watch_until" and october.holds == ["quarter_close", "recent_promotion", "short_tenure"]


def test_sec_colleagues_are_in_a_controllers_view_without_being_named(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 's.sqlite'}")
    me, cfo, elsewhere = "ann-roe|acme-corp", "jo-poe|acme-corp", "jo-poe|acme-corp-holdings"
    for event in (ev(me, "officer_appointment", "2024-03-01", quote="Ann Roe was appointed Controller"),
                  ev(cfo, "officer_departure", "2026-06-10", quote="Jo Poe resigned as Chief Financial Officer"),
                  ev(elsewhere, "officer_departure", "2026-06-12", quote="Jo Poe resigned as Treasurer")):
        add_event(store, event)
    assert detectors.colleagues(store, me) == [cfo]  # a longer company name is another company
    cfo_gone = assess(store, me, "2026-06-20T00:00:00+00:00")
    assert "exec_departure" in cfo_gone.open_windows


def test_mts_scenario(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'm.sqlite'}")
    for event in (
        ev(ME, "coauthor_link", "2025-02-01", quote="Sam Peer (A123) on 'Scaling laws'"),
        ev(ME, "paper_accepted", "2026-08-20", quote="Scaling laws II: Accept (NeurIPS 2026)"),
        ev("A123", "job_ended", "2026-09-01", quote="Sam Peer left Acme Research"),
        ev(ME, "gi_citation", "2026-09-10", quote="GI's interpretability post cites Scaling laws II"),
    ):
        add_event(store, event)
    before = assess(store, ME, "2026-08-25T00:00:00+00:00", ["A123", "gi"])
    assert before.open_windows == ["paper_accepted"] and (before.action, before.track) == ("reach_now", "rapport")
    now = assess(store, ME, "2026-09-15T00:00:00+00:00", ["A123", "gi"])
    assert now.open_windows == ["paper_accepted", "gi_citation", "coauthor_departure"]
    assert len(now.reasons) == 3 and now.action == "reach_now" and now.score == 0.875
    faded = assess(store, ME, "2027-01-15T00:00:00+00:00", ["A123", "gi"])
    assert faded.open_windows == [] and faded.action == "quiet" and faded.score < now.score / 2


def test_the_pattern_before_their_past_moves_is_listed_and_changes_no_call():
    """The events that came before someone's earlier moves, recurring now, predict a move from their own past: listed on
    the call like time in a seat, never a reason (GI does not predict who will leave). Failure modes: alone it reads as
    a reason; beside one other reason it makes two, and a pitch; it takes the recurring paper's evidence from the
    paper's own reason. Whatever else is open, the call is the one the person would get without that past."""
    moves = [ev(ME, "paper_v1", "2020-09-01"), ev(ME, "job_started", "2021-01-01"),
             ev(ME, "paper_v1", "2023-08-01"), ev(ME, "job_started", "2024-01-01")]
    no_past = [e for e in moves if e["event_type"] != "paper_v1"]  # the same jobs, nothing before the earlier moves
    cfo = ev(CFO, "officer_departure", "2026-09-10", quote="Jane Doe resigned as Chief Financial Officer")

    def call(*events):
        store = Store("sqlite://")
        for e in events:
            add_event(store, e)
        scored = assess(store, ME, AS_OF, [CFO])
        return scored.action, scored.track, scored.reasons, scored.until

    listed = "Listed, never a reason: the pattern that came before their past moves."
    for now in ([ev(ME, "paper_v1", "2026-05-01")], [ev(ME, "paper_v1", "2026-05-01"), cfo],
                [ev(ME, "paper_v1", "2026-09-01")]):
        store = Store("sqlite://")
        for e in moves + now:
            add_event(store, e)
        scored = assess(store, ME, AS_OF, [CFO])
        assert listed in scored.explanation and "own_precedent" not in scored.reasons + scored.open_windows
        assert call(*moves, *now) == call(*no_past, *now)
    assert call(*moves, ev(ME, "paper_v1", "2026-05-01"), cfo) == ("quiet", None, ["exec_departure"], None)
    assert call(*moves, ev(ME, "paper_v1", "2026-09-01"))[2] == ["new_work"]  # the paper keeps its own reason


def test_a_talk_announced_ahead_does_not_make_an_old_paper_fresh_beside_an_employer_record():
    layoffs = activation("warn_notice", "employer", opens="2026-09-20", closes="2026-10-11", evidence=("w1",))
    paper = activation("paper_v1", "work", strength=0.4, opens="2026-08-20", closes="2026-10-04", evidence=("p1",))
    talk = activation("paper_talk", "work", strength=0.4, opens="2026-10-01", closes="2026-10-15", evidence=("t1",))
    call = readiness.score([layoffs, paper, talk], "2026-09-24T00:00:00+00:00")
    # The paper is 35 days old: beside the layoffs it is no pitch, whatever talk on it is still to come.
    assert "new_work" in call.reasons and call.track != "pitch" and call.action != "reach_now"


def test_a_push_backs_up_a_post_or_a_release_but_never_makes_the_call_alone():
    """Justin (2026-09-24): one push is not enough to reach out; a push can back up a post or a release but never
    makes the call by itself. An early sign or drift read only from GitHub items that are not releases (a push, a pull
    request, an issue, a comment) counts only beside a fresh post about their work, a release or paper out this month,
    or something enough on its own; then the note opens with that, not the push. Failure modes: one push alone makes a
    note; a pull request, an issue or a comment alone does; several pushes alone add up; their GI-topic drift read from
    pushes counts; a post just gone stale, an old paper, posts drifting to GI's topics or a colleague's news lets a
    push make the call; something enough alone (their own "I'm open") does not let a push count beside it; a push
    beside a fresh post is dropped instead of backing it up, or opens the note; the same event reported by a push and a
    post is kept, or quoted, as the push's; a stale post of the same kind outranks the open push and is quoted; a
    release, or an ask read from one, stops counting; the live call (journey.assess, which scores without the view)
    and the replay's differ."""
    repo, issue = "https://github.com/me/queuekit", "https://github.com/me/queuekit/issues/7"

    def gh(day, quote, sign="work_in_progress", url=repo, dated=True):
        seen = f"{day}T09:00:00+00:00"  # the item and what the reader read from it: one item
        return [ev(ME, "github_activity", day, observed=seen, source_url=url, quote=quote),
                ev(ME, sign, day if dated else None, observed=seen, source_url=url, quote=quote)]

    def post(day, text, sign="work_in_progress"):
        seen, url = f"{day}T09:00:00+00:00", f"https://x.com/me/status/{day.replace('-', '')}"
        return [ev(ME, "x_post", day, observed=seen, source_url=url, quote=text),
                ev(ME, sign, day, observed=seen, source_url=url, quote=text)]

    stored = {}

    def call(*groups):
        store = Store("sqlite://")
        for e in (e for group in groups for e in group):
            stored[e["id"]] = add_event(store, e)["id"]
        live, replay = journey.assess(store, ME, AS_OF), assess(store, ME, AS_OF)
        assert (live.action, live.reasons, live.evidence) == (replay.action, replay.reasons, replay.evidence)
        return live

    push = gh("2026-09-15", "Pushed to me/queuekit: Add exactly-once ack path; Retry poisoned jobs with backoff")
    on_topic = [[*gh(day, f"Pushed to me/queuekit: world model eval {day}"),
                 ev(ME, "gi_topic", day, observed=f"{day}T09:00:00+00:00", source_url=repo,
                    quote=f"Pushed to me/queuekit: world model eval {day}")] for day in ("2026-09-08", "2026-09-12")]
    alone = [
        [push],
        [gh("2026-09-15", "Opened pull request on me/queuekit: Exactly-once acks", url=f"{repo}/pull/8")],
        [gh("2026-09-15", "Opened issue on me/queuekit: Looking for a large Postgres cluster to test failover on",
            sign="technical_ask", url=issue)],
        [gh("2026-09-15", "Anyone have spare GPUs for the eval runs?\n\nIn reply to me/queuekit: Eval harness",
            sign="technical_ask", url=f"{issue}#issuecomment-1")],
        [gh("2026-09-08", "Pushed to me/queuekit: Journal table"), gh("2026-09-12", "Pushed to me/queuekit: Ack path"),
         push],
        [*on_topic, push],  # their drift to GI's topics, read from pushes too
        [post("2026-08-25", "Rebuilding our job queue around exactly-once acks, writeup soon"), push],  # just stale
        [[ev(ME, "paper_v1", "2026-08-15", quote="Exactly-once job queues on Postgres (arXiv)")], push],  # 38 days old
        [post("2026-09-01", "World models trained on game clips are wild", "gi_topic"),
         post("2026-09-10", "Game agents that plan with a world model", "gi_topic"), push],  # drift, no work to quote
    ]
    for n, groups in enumerate(alone):
        scored = call(*groups)
        assert scored.action == "quiet" and "never a reason by itself" in scored.explanation, n  # listed
        assert scored.reasons == ([], [], [], [], [], [], [], ["new_work"], ["topic_drift"])[n], n  # theirs stay

    fresh = post("2026-09-18", "Rebuilding our job queue around exactly-once acks, writeup soon")
    backed = call(fresh, push)
    assert (backed.action, backed.track, backed.reasons) == ("reach_now", "rapport", ["work_in_progress"])
    assert {stored[fresh[1]["id"]], stored[push[1]["id"]]} <= set(backed.evidence["work_in_progress"])  # backed up
    earlier = post("2026-09-12", "Rebuilding our job queue around exactly-once acks, writeup soon")
    newer_push = call(earlier, gh("2026-09-18", "Pushed to me/queuekit: Poison queue"))
    assert newer_push.action == "reach_now" and readiness.opener(newer_push) == stored[earlier[1]["id"]]  # the post
    stale = post("2026-08-25", "Rebuilding our job queue around exactly-once acks, writeup soon")
    submitted = post("2026-09-18", "Just submitted our exactly-once queue paper to the workshop", "just_submitted")
    three = call(stale, push, submitted)  # the stale post's window closed on 15 September
    assert three.action == "reach_now" and readiness.opener(three) == stored[submitted[1]["id"]]  # the fresh post
    shipped = gh("2026-09-18", "Released v2.0.0 of me/queuekit: Exactly-once delivery", sign="project_release",
                 url=f"{repo}/releases/tag/v2.0.0")
    beside_release = call(stale, push, shipped)
    assert "work_in_progress" in beside_release.reasons and readiness.opener(beside_release) != stored[stale[1]["id"]]
    same_day = [gh("2026-09-15", "Opened issue on me/queuekit: Anyone solved hot partitions for a job index?",
                   sign="technical_ask", url=issue),
                post("2026-09-15", "Anyone solved hot partitions for a job index?", "technical_ask")]
    told_twice = call(*same_day)  # one ask, on GitHub and in a post the same day
    assert told_twice.action == "reach_now" and readiness.opener(told_twice) == stored[same_day[1][1]["id"]]
    open_to_work = call(post("2026-09-16", "I'm open to new backend roles", "self_stated_availability"), push)
    assert open_to_work.action == "reach_now" and "work_in_progress" in open_to_work.reasons  # enough alone backs it
    bridged = [gh("2026-09-10", "Pushed to me/queuekit: Journal table"),
               gh("2026-09-12", "Pushed to me/queuekit: cut v0.2", sign="project_release", dated=False),
               post("2026-09-14", "Rebuilding our job queue around exactly-once acks, writeup soon")]
    one_event = call(*bridged)  # three reports the engine reads as one event, one of them a post
    assert one_event.action == "reach_now" and readiness.opener(one_event) == stored[bridged[2][1]["id"]]
    released = call(gh("2026-09-10", "Released v2.0.0 of me/queuekit: Exactly-once delivery", sign="project_release",
                       url=f"{repo}/releases/tag/v2.0.0"))
    assert (released.action, released.reasons) == ("reach_now", ["launch"])  # a release is a launch, as before
    asked = call(gh("2026-09-15", "Released v0.1.0 of me/queuekit: Exactly-once acks. Looking for testers",
                    sign="technical_ask", url=f"{repo}/releases/tag/v0.1.0"))
    assert (asked.action, asked.reasons) == ("reach_now", ["technical_ask"])  # read from a release: not code alone
    # A paper read off a push counts only when the push links it: then the paper is the public moment, not the push.
    unlinked = call(gh("2026-09-15", "Pushed to me/lab: camera-ready for our NeurIPS paper", sign="publication"))
    assert (unlinked.action, unlinked.reasons) == ("quiet", []) and "never a reason by itself" in unlinked.explanation
    linked = call(gh("2026-09-15", "Pushed to me/lab: README links the paper, arxiv.org/abs/2609.01234", sign="publication"))
    assert (linked.action, linked.reasons) == ("reach_now", ["new_work"])
    theirs = call(gh("2026-09-15", "Our camera-ready is done\n\nIn reply to me/lab: see arxiv.org/abs/2609.05678",
                     sign="publication", url=f"{issue}#issuecomment-2"))  # the link is in the thread they answered
    assert (theirs.action, theirs.reasons) == ("quiet", [])


def test_a_path_like_people_gi_hired_is_listed_and_never_makes_a_note_a_pitch():
    """A match to what past hires showed before joining predicts a move: listed, never a reason (GI does not predict
    who will leave). Failure mode: beside a work-in-progress post it made the note a pitch (it counted as a sign they
    would listen)."""
    post = activation("work_in_progress", "reason", opens="2026-09-18", closes="2026-10-09", evidence=("p1",))
    path = activation("similar_path", "pattern", opens="2026-09-10", closes="2026-11-09", evidence=("s1",))
    call = readiness.score([post, path], AS_OF)
    assert (call.action, call.track, call.reasons) == ("reach_now", "rapport", ["work_in_progress"])
    assert "Listed, never a reason: a path like people GI hired." in call.explanation
