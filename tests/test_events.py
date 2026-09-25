"""The event loop over invented GI records (tests/fixtures/events)."""

from datetime import date, datetime, timezone
import json
import shutil
from pathlib import Path

import pytest

from app import contact, detectors, events, inbox, journey, readiness, routing, today
from app.readiness import Readiness
from app.store import Store
from tests.test_api import client  # noqa: F401  (a fixture)

FIX = Path(__file__).parent / "fixtures" / "events"


@pytest.fixture
def records():
    return events.load(FIX)


@pytest.fixture
def store():
    return Store("sqlite://")


def by_id(records):
    return {e.id: e for e in records["events"]}


def call(action, until=None, track=None, reasons=("pi_departure",), holds=()):
    return Readiness(as_of="2026-09-24T00:00:00+00:00", score=0.5, action=action, until=until, track=track,
                     families={}, reasons=list(reasons), holds=list(holds), open_windows=[], earliest_close=None,
                     explanation="invented")


def test_loads_every_record(records):
    assert len(records["events"]) == 4 and len(records["guests"]) == 92 and len(records["spend"]) == 7
    assert sum(g.checked_in for g in records["guests"] if g.event_id == "gi-2026-07-dinner") == 9


def test_results_count_spend_notes_and_pipeline_in_window(records, store):
    dinner = by_id(records)["gi-2026-07-dinner"]
    events.record_note(store, dinner, "P801", "open", "wants to talk after her project", "host")
    events.record_note(store, dinner, "P802", "context", "nice chat", "host")
    r = events.results(store, dinner, records["guests"], records["spend"], records["pipeline"])
    assert r["cost"] == 2225.5 and r["came"] == 9 and r["per_head"] == round(2225.5 / 9, 2)
    assert r["notes"] == 2 and r["qualified"] == 1 and r["cost_per_qualified"] == 2225.5
    assert r["entered_pipeline"] == ["P801"]  # Kofi entered months before the dinner


def test_notes_belong_to_their_event(records, store):
    ev = by_id(records)
    events.record_note(store, ev["gi-2026-09-salon"], "X901", "open", "keen", "host")
    assert events.notes_from(store, ev["gi-2026-08-builders"], ["X901"]) == {}
    assert list(events.notes_from(store, ev["gi-2026-09-salon"], ["X901"])) == ["X901"]


def test_estimate_uses_the_same_format_only(records, store):
    ev = by_id(records)
    past = [events.results(store, e, records["guests"], records["spend"], records["pipeline"])
            for e in records["events"] if e.day < date(2026, 9, 15)]
    est = events.estimate(ev["gi-2026-09-games"], past)
    assert est["based_on"] == ["gi-2026-08-builders", "gi-2026-09-salon"]
    assert est["low"] <= est["mid"] <= est["high"]
    assert events.estimate(ev["gi-2026-09-games"].model_copy(update={"format": "workshop"}), past) is None


def test_invites_rank_by_call_and_hold_what_must_wait(records):
    night = by_id(records)["gi-2026-09-games"]
    calls = {"X900": call("reach_now", track="pitch"), "D904": call("verify_first"),
             "X901": call("watch_until", until="2027-06-01T00:00:00+00:00"),
             "P801": call("respect_follow_up", until="2026-11-01T00:00:00+00:00")}
    picks, held = events.suggest_invites(night, records["people"], records["guests"], calls, records["programs"],
                                         gate=lambda sid: "sales owns an open ask" if sid == "C801" else None,
                                         max_extra=10)
    names = [p["name"] for p in picks]
    assert names == ["Mara Quill", "Lena Okafor", "Ivy Stroud"]  # researchers only: the night is for MTS
    assert "Rin Vale" not in names  # already said yes
    assert "Kofi Adair" not in names  # lives in Boston
    assert "Example Robot Learning Workshop" in picks[1]["why"][0]  # in town for a public program
    assert {h["name"]: h["reason"] for h in held} == {
        "Tove Lind": "asked to be contacted after 2026-11-01", "Sam Okoro": "sales owns an open ask"}


def test_invites_keep_to_the_event_roles(records):
    builders = by_id(records)["gi-2026-08-builders"]
    picks, _ = events.suggest_invites(builders, records["people"], records["guests"], {}, records["programs"],
                                      max_extra=10)
    assert {p["name"] for p in picks} == {"Theo Marsh", "Noa Brandt", "Priya Vance"}  # Jonah already came


def test_invites_never_exceed_open_seats(records):
    night = by_id(records)["gi-2026-09-games"]
    full = night.model_copy(update={"capacity": 32})  # 31 yes already
    picks, _ = events.suggest_invites(full, records["people"], records["guests"], {}, records["programs"])
    assert len(picks) == 1
    # Rin was invited and is already a yes on the guest list: her seat counts once.
    picks, _ = events.suggest_invites(full, records["people"], records["guests"], {}, records["programs"],
                                      pending={"A900"})
    assert len(picks) == 1
    picks, _ = events.suggest_invites(full, records["people"], records["guests"], {}, records["programs"],
                                      pending={"X900"})
    assert picks == []  # Mara's invite holds the last seat


def test_host_briefs_match_topics_and_never_pitch_a_wait(records):
    night = by_id(records)["gi-2026-09-games"]
    people = [p for p in records["people"] if p.subject_id in ("X900", "X901", "D904", "C801")]
    calls = {"X901": call("watch_until", until="2027-06-01T00:00:00+00:00"), "X900": call("reach_now", track="pitch")}
    briefs = {b["host"]: [p["name"] for p in b["people"]] for b in events.host_briefs(night, people, calls)}
    assert briefs == {"Nora Example": ["Mara Quill", "Ivy Stroud", "Lena Okafor"], "Dana Kest": ["Sam Okoro"]}
    rules = {p["name"]: p["rule"] for b in events.host_briefs(night, people, calls) for p in b["people"]}
    assert rules["Ivy Stroud"].startswith("No pitch: their window opens Jun 1, 2027")
    assert "Member of Technical Staff" in rules["Mara Quill"]
    assert rules["Sam Okoro"] == "Talk about their work. No pitch."
    held = events.host_briefs(night, people, calls, gate=lambda sid: "Works at a GI partner." if sid == "X900" else None)
    assert {p["name"]: p["rule"] for b in held for p in b["people"]}["Mara Quill"] == "No pitch: Works at a GI partner."
    known = events.host_briefs(night, people, calls, knows={"C801": "Nora Example", "X901": "Someone Else"})
    assert {b["host"]: [p["name"] for p in b["people"]] for b in known}["Nora Example"][-1] == "Sam Okoro"  # who knows them
    assert "Ivy Stroud" in {b["host"]: [p["name"] for p in b["people"]] for b in known}["Nora Example"]  # not a host: by topic


def test_an_open_door_note_is_what_the_engine_reads(records, store):
    salon = by_id(records)["gi-2026-09-salon"]
    events.record_note(store, salon, "person:new", "open", "up for a real conversation", "Nora Example")
    assert journey.assess(store, "person:new", datetime.now(timezone.utc).isoformat(), "mts").action == "reach_now"


def test_follow_ups_fall_due_48_hours_after_and_differ_per_person(records, store):
    night = by_id(records)["gi-2026-09-games"]
    events.record_note(store, night, "X900", "open", "up for a real conversation", "Nora Example")
    events.record_note(store, night, "D904", "context", "talked sim-to-real evals", "Dana Kest")
    early = events.follow_ups(store, night, records["people"], datetime(2026, 9, 25, tzinfo=timezone.utc))
    late = events.follow_ups(store, night, records["people"], datetime(2026, 9, 27, tzinfo=timezone.utc))
    assert [f["overdue"] for f in early] == [False, False] and all(f["overdue"] for f in late)
    assert early[0]["due"] == "2026-09-26T23:00:00+00:00"  # 7pm in New York, two days after
    drafts = {f["name"]: f["draft"]["body"] for f in early}
    assert drafts["Mara Quill"].startswith("Hey Mara, good to meet you") and drafts["Mara Quill"].endswith("\n\nNora")
    assert drafts["Lena Okafor"].endswith("\n\nDana") and "Dana Kest" not in drafts["Lena Okafor"]  # signed, not named
    assert drafts["Mara Quill"].replace("Mara", "Lena") != drafts["Lena Okafor"]
    # The note says only that they're open to a conversation: the topic is GI's suggestion, never put in their mouth.
    assert "You mentioned you'd be up for a proper conversation. Would next week work for a longer chat about" \
        in drafts["Mara Quill"]
    assert "If a longer chat about" in drafts["Lena Okafor"] and "keep talking" not in drafts["Lena Okafor"]  # a note


@pytest.fixture
def simulation():
    """The demo store Today's calls reads, fresh before and after, since invites and notes write to it."""
    today._demo_store.cache_clear()
    yield
    today._demo_store.cache_clear()


def test_the_ledger_holds_an_invite_when_another_team_owns_the_ask(simulation):
    nxt = events.page("simulation")["next"]
    held = {h["name"]: h["reason"] for h in nxt["held"]}
    assert held["Tove Lind"].startswith("In conversation since 2026-08-04 with Dana Kest (recruiting)")
    assert "Tove Lind" not in {p["name"] for p in nxt["invite"]}


def test_a_doubt_about_whether_they_are_a_student_keeps_the_invite_but_the_host_pitches_nothing(simulation):
    # Ways it could fail: the doubt holds them out of the invites (an invite pitches nothing; an undergraduate is
    # invited too); the host brief still says the role is fair to raise; the brief's reason talks about a draft.
    before = events.page("simulation")["next"]
    store = today._demo_store()[0]
    for sid in ("A900", "X901"):
        contact.record(store, sid, "unconfirmed", at="2026-09-10T00:00:00+00:00",
                       note="Their bio says CS at a school, so they may still be a student.")
    nxt = events.page("simulation")["next"]
    assert [p["name"] for p in nxt["invite"]] == [p["name"] for p in before["invite"]]
    assert {h["name"] for h in nxt["held"]} == {h["name"] for h in before["held"]}
    rin = next(p for b in nxt["briefs"] for p in b["people"] + b["if_invited"] if p["name"] == "Rin Vale")
    assert rin["rule"].startswith("No pitch: Their bio says CS at a school") and "draft" not in rin["rule"], rin["rule"]
    ivy = next(p for p in nxt["invite"] if p["name"] == "Ivy Stroud")  # invited, and nobody pitches her
    assert "nobody pitches them now" in ivy["why_them"] and "reach out now" not in ivy["why_them"], ivy["why_them"]


def test_a_reach_who_may_still_be_a_student_is_confirm_first_on_the_events_page_as_on_todays_calls(simulation):
    # Ways it could fail: the invite still says reach out now (its badge or its why line) while Today's calls says
    # confirm first; the ledger's note is not on it; it still ranks first, as a reach; a window closing before the
    # night holds them out with "reach out directly", which nobody may do yet; the doubt holds them out altogether.
    store = today._demo_store()[0]
    before = events.page("simulation")["next"]
    assert "reach out directly" in next(h["reason"] for h in before["held"] if h["name"] == "Mara Quill")
    contact.record(store, "X900", "unconfirmed", at="2026-09-10T00:00:00+00:00",
                   note="Their bio says CS at a school, so they may still be a student.")
    nxt = events.page("simulation")["next"]
    assert "Mara Quill" not in [h["name"] for h in nxt["held"]]
    mara = next(p for p in nxt["invite"] if p["name"] == "Mara Quill")
    gate = next(c for c in today.calls("simulation")["calls"] if c["name"] == "Mara Quill")["gate"]
    assert gate["state"] == "check_first" and mara["check"] and mara["check"] in gate["reason"]
    assert mara["check"].startswith("Their bio says CS at a school, so they may still be a student.")
    assert (mara["action"], mara["call"], mara["rank"]) == ("verify_first", "Confirm first", 1)
    assert "reach out" not in mara["why_them"].lower(), mara["why_them"]
    assert [p["name"] for p in nxt["invite"]].index("Mara Quill") > 0  # the reaches and checks come first


def test_a_marked_invite_holds_every_other_ask_until_the_evening(simulation):
    before = events.page("simulation")["next"]["briefs"]
    assert "Mara Quill" not in [p["name"] for b in before for p in b["people"]]  # a suggestion is not a guest
    events.mark("simulation", "gi-2026-09-games", "X900", "invited", "Nora Example")
    nxt = events.page("simulation")["next"]
    assert nxt["invited"] == [{"subject_id": "X900", "name": "Mara Quill", "by": "Nora Example", "at": "2026-09-15"}]
    assert "Mara Quill" not in [p["name"] for p in nxt["invite"]]
    rule = next(p["rule"] for b in nxt["briefs"] for p in b["people"] if p["name"] == "Mara Quill")
    assert not rule.startswith("No pitch: Invited")  # her own invite is not a hold for the evening's hosts
    mara = next(c for c in today.calls("simulation")["calls"] if c["name"] == "Mara Quill")
    assert mara["action"] == "reach_now" and mara["gate"]["state"] == "hold"
    assert "by Nora Example (events)" in mara["gate"]["reason"]
    with pytest.raises(ValueError, match="Theo Marsh is held"):
        events.mark("simulation", "gi-2026-09-games", "D901", "invited", "Nora Example")
    with pytest.raises(ValueError, match="Priya Vance is held: asked to be contacted after 2026-11-01"):
        events.mark("simulation", "gi-2026-09-games", "D903", "invited", "Sam Example")
    with pytest.raises(ValueError, match="already happened"):
        events.mark("simulation", "gi-2026-09-salon", "X902", "invited", "Nora Example")


def test_a_wait_at_the_door_goes_in_the_ledger_and_gets_no_follow_up(simulation):
    r = events.add_note("simulation", "gi-2026-09-salon", "X901", "wait", "deadline; after mid-November", "Nora Example",
                        "2026-11-16")
    assert r["call"] == "Wait until 2026-11-16"
    page = events.page("simulation")
    assert {h["name"]: h["reason"] for h in page["next"]["held"]}["Ivy Stroud"] == "asked to be contacted after 2026-11-16"
    assert contact.check(contact.history(today.source("simulation")[0], "X901"), "2026-09-15T23:59:59+00:00").reason \
        == "They asked us to wait until 2026-11-16."
    assert page["last"]["follow_ups"] == []
    assert page["last"]["people"][0]["note"] == "deadline; after mid-November"


def test_a_sent_follow_up_goes_in_the_ledger_once(simulation):
    events.add_note("simulation", "gi-2026-09-salon", "D902", "open", "up for a real conversation", "Luis Example")
    due = events.page("simulation")["last"]["follow_ups"]
    assert [(f["name"], f["host"], f["sent"], f["hold"]) for f in due] == [("Jonah Pike", "Luis Example", None, None)]
    events.mark("simulation", "gi-2026-09-salon", "D902", "sent", "Luis Example")
    done = events.page("simulation")["last"]["follow_ups"][0]
    assert done["sent"] == {"by": "Luis Example", "at": "2026-09-15"} and not done["overdue"] and done["hold"] is None
    with pytest.raises(ValueError, match="Already followed up by Luis Example"):
        events.mark("simulation", "gi-2026-09-salon", "D902", "sent", "Luis Example")
    with pytest.raises(ValueError, match="No follow-up to send Ivy Stroud"):
        events.mark("simulation", "gi-2026-09-salon", "X901", "sent", "Nora Example")


def test_door_notes_only_after_the_event_and_only_for_who_came(simulation):
    with pytest.raises(ValueError, match="hasn't happened yet"):
        events.add_note("simulation", "gi-2026-09-games", "D902", "open", "keen", "Luis Example")
    with pytest.raises(ValueError, match="Mara Quill isn't checked in"):
        events.add_note("simulation", "gi-2026-09-salon", "X900", "open", "keen", "Nora Example")


def test_past_door_notes_count_toward_their_event(simulation):
    dinner = next(r for r in events.page("simulation")["past"] if r["event_id"] == "gi-2026-07-dinner")
    assert (dinner["notes"], dinner["qualified"], dinner["entered_pipeline"]) == (2, 1, ["P801"])


def test_a_folder_with_only_events_loads(tmp_path):
    (tmp_path / "events.json").write_text((FIX / "events.json").read_text())
    loaded = events.load(tmp_path)
    assert loaded["events"] and loaded["guests"] == [] and loaded["spend"] == []


def test_a_follow_up_is_held_when_another_team_owns_the_ask(records, store):
    night = by_id(records)["gi-2026-09-games"]
    events.record_note(store, night, "X902", "open", "up for a real conversation", "Sam Example")
    contact.record(store, "X902", "sent", at="2026-09-25T09:00:00+00:00", team="sales", by="Omar Example")
    f = events.follow_ups(store, night, records["people"], datetime(2026, 9, 25, 12, tzinfo=timezone.utc))
    assert f[0]["hold"].startswith("Sent on 2026-09-25 by Omar Example (sales)")


def test_the_events_api_logs_notes_and_invites_and_refuses_what_it_cannot(simulation, client):  # noqa: F811
    assert client.get("/api/events", params={"mode": "simulation"}).json()["next"]["event"]["id"] == "gi-2026-09-games"
    note = client.post("/api/events/gi-2026-09-salon/notes", json={"mode": "simulation", "subject_id": "D902",
                                                                   "kind": "open", "text": "up for a real talk"})
    assert note.status_code == 200 and note.json()["call"] == "Reach out now"
    assert client.post("/api/events/gi-2026-09-salon/notes", json={"subject_id": "D902", "kind": "send",
                                                                   "text": "x"}).status_code == 422
    assert client.post("/api/events/gi-2026-09-salon/notes", json={"subject_id": "D902", "kind": "wait", "text": "x",
                                                                   "wait_until": 20261116}).status_code == 422
    assert client.post("/api/events/gi-2026-09-salon/notes", json={"subject_id": "D902", "kind": "wait",
                                                                   "text": "x"}).status_code == 422  # no day
    assert client.post("/api/events/nope/ledger", json={"subject_id": "X900", "kind": "invited"}).status_code == 404
    assert client.post("/api/events/gi-2026-09-games/ledger", json={"subject_id": "X900", "kind": "never"}).status_code == 422
    assert client.post("/api/events/gi-2026-09-games/ledger", json={"subject_id": "D901", "kind": "invited"}).status_code == 409


def test_live_door_notes_come_back_with_the_new_call(monkeypatch, tmp_path):
    db = tmp_path / "timelines.sqlite"
    routing.seed(Store(f"sqlite:///{db}"), json.loads((FIX.parent / "demo" / "timeline.json").read_text()))
    monkeypatch.setattr(today, "TIMELINES", db)
    monkeypatch.setitem(events.RECORDS, "live", FIX)
    r = events.add_note("live", "gi-2026-09-salon", "D902", "open", "up for a real conversation", "Luis Example")
    assert r["call"] == "Reach out now"


def test_resetting_the_simulation_clears_what_the_events_page_logged(simulation, client):  # noqa: F811
    events.mark("simulation", "gi-2026-09-games", "X900", "invited", "Nora Example")
    assert client.post("/api/simulation/reset", json={}).status_code == 200
    assert events.page("simulation")["next"]["invited"] == []


def test_a_declined_invite_is_not_in_the_host_briefs(records, store):
    night = by_id(records)["gi-2026-09-games"]
    contact.record(store, "X900", "invited", at="2026-09-15T12:00:00+00:00", team="events", by="Nora Example",
                   until="2026-09-24", event_id=night.id, note=night.name)

    def briefed(recs):
        briefs = events.overview(store, "2026-09-15T23:00:00+00:00", recs)["next"]["briefs"]
        return [p["name"] for b in briefs for p in b["people"]]

    assert "Mara Quill" in briefed(records)
    declined = events.Guest(event_id=night.id, name="Mara Quill", subject_id="X900", rsvp="no")
    assert "Mara Quill" not in briefed({**records, "guests": [*records["guests"], declined]})


def test_a_follow_up_waits_for_whoever_asked_to_be_contacted_later(simulation):
    store, as_of, *_ = today.source("simulation")
    records = events.load(FIX)
    builders = by_id(records)["gi-2026-08-builders"]
    events.record_note(store, builders, "D903", "context", "talked design tooling", "Sam Example", now=as_of)
    f = events.follow_ups(store, builders, records["people"], datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert f[0]["name"] == "Priya Vance" and f[0]["hold"] == "asked to be contacted after 2026-11-01"


def test_an_old_card_does_not_hold_the_events_loop_for_good(store):
    contact.record(store, "X902", "pinged", at="2025-01-01T00:00:00+00:00", role_id="product-designer")
    assert events.ledger_hold(store, "X902", "2026-09-15T00:00:00+00:00") is None
    contact.record(store, "X902", "pinged", at="2026-09-01T00:00:00+00:00", role_id="product-designer")
    assert "(Product Designer): one card per person" in events.ledger_hold(store, "X902", "2026-09-15T00:00:00+00:00")


def test_on_the_evening_its_own_invite_does_not_hold_the_follow_up(records, store):
    night = by_id(records)["gi-2026-09-games"]
    contact.record(store, "X902", "invited", at="2026-09-15T12:00:00+00:00", team="events", by="Sam Example",
                   until="2026-09-24", event_id=night.id, note=night.name)
    events.record_note(store, night, "X902", "open", "up for a real conversation", "Sam Example")
    assert events.follow_ups(store, night, records["people"], datetime(2026, 9, 24, 23, tzinfo=timezone.utc))[0]["hold"] is None


def test_a_second_note_from_the_same_evening_replaces_the_first(simulation):
    salon = ("simulation", "gi-2026-09-salon", "D902")
    assert events.add_note(*salon, "open", "up for a real conversation", "Luis Example")["call"] == "Reach out now"
    assert events.add_note(*salon, "context", "misheard: just being friendly", "Luis Example")["call"] == "Stay quiet"
    assert events.add_note(*salon, "wait", "after their launch", "Luis Example", "2026-10-20")["call"] == \
        "Wait until 2026-10-20"
    jonah = next(p for p in events.page("simulation")["last"]["people"] if p["name"] == "Jonah Pike")
    assert jonah["note"] == "after their launch"


def test_the_budget_counts_event_spend_in_the_period_and_the_rest_of_each_upcoming_estimate(records):
    past = [events.results(Store("sqlite://"), e, records["guests"], records["spend"], records["pipeline"])
            for e in records["events"] if e.day <= date(2026, 9, 15)]
    b = events.budget(records, date(2026, 9, 15), past)
    assert (b["period"], b["amount"], b["spent"]) == ("Q3 2026", 9000, 6100.5)  # the untagged $49 of tools is not an event
    assert [(p["event_id"], p["mid"], p["to_come"]) for p in b["planned"]] == [("gi-2026-09-games", 3117, 3117)]
    assert b["left"] == -217.5
    deposit = events.Spend(day=date(2026, 9, 14), merchant="Example Venue", amount=1000, memo="gi-2026-09-games deposit")
    paid = events.budget({**records, "spend": records["spend"] + [deposit]}, date(2026, 9, 15), past)
    assert paid["spent"] == 7100.5 and paid["planned"][0]["to_come"] == 2117 and paid["left"] == -217.5  # not twice
    early = deposit.model_copy(update={"day": date(2026, 6, 20)})  # paid from last quarter's budget
    paid = events.budget({**records, "spend": records["spend"] + [early]}, date(2026, 9, 15), past)
    assert paid["spent"] == 6100.5 and paid["planned"][0]["to_come"] == 2117 and paid["left"] == 782.5
    later = deposit.model_copy(update={"day": date(2026, 9, 20)})  # not paid yet as of the day
    assert events.budget({**records, "spend": records["spend"] + [later]}, date(2026, 9, 15), past)["spent"] == 6100.5
    assert events.budget(records, date(2027, 3, 1), past) is None  # no budget set for that quarter


def test_each_invite_says_why_them_in_plain_sentences_and_links_their_profiles(simulation):
    nxt = events.page("simulation")["next"]
    ivy = next(p for p in nxt["invite"] if p["name"] == "Ivy Stroud")
    assert ivy["why_them"].startswith("Work in progress they posted on Sep 10: “Trying diffusion world models")
    assert "waiting until Jun 1, 2027, so this is a no-pitch chance" in ivy["why_them"]
    assert "they live in New York" in ivy["why_them"]
    assert ivy["why_them"].count(". ") <= 2 and "_" not in ivy["why_them"]  # sentences, not detector ids
    assert ivy["links"] == [{"label": "X", "url": "https://x.com/ivystroud_example"},
                            {"label": "Site", "url": "https://ivystroud.example/"}]
    sam = next(p for p in nxt["invite"] if p["name"] == "Sam Okoro")  # not watched: a researcher GI could hire from
    assert sam["why_them"].startswith("A researcher at Example Robotics, where GI could hire.")
    assert sam["links"] == [{"label": "Site", "url": "https://example.com/sam-okoro"}]
    lena = next(p for p in nxt["invite"] if p["name"] == "Lena Okafor")
    assert lena["links"][-1]["label"] == "Example Robot Learning Workshop"  # the program that puts them in town
    mara = next(h for h in nxt["held"] if h["name"] == "Mara Quill")  # her window closes Sep 22, before the night
    assert mara["reason"] == "reach out directly: the engine's window closes 2026-09-22, before the night"


def test_quotes_and_dates_read_as_sentences():
    assert events._quote("Our paper is out...") == "“Our paper is out...”"
    assert events._quote("Shipped it.") == "“Shipped it”."
    assert events._quote("Who has it?") == "“Who has it?”"
    assert events._quote("...") is None and events._quote("") is None
    assert events._quote('I called it "world models"') == '“I called it "world models"”.'
    assert events._quote('They asked "who has it?"') == '“They asked "who has it?"”'
    assert events._on("2026-08-20", "2026") == "on Aug 20" and events._on("2026-08", "2026") == "in Aug 2026"
    assert events._on("2025", "2026") == "in 2025" and events._on("2027-06-01", "2026") == "on Jun 1, 2027"


def test_a_coming_guest_the_ledger_holds_gets_no_reach_out_in_their_brief(simulation):
    store = today._demo_store()[0]
    contact.record(store, "A900", "replied", at="2026-09-14T00:00:00+00:00", team="recruiting", by="Dana Kest")
    rin = next(p for b in events.page("simulation")["next"]["briefs"] for p in b["people"] if p["name"] == "Rin Vale")
    assert "reach out now" not in rin["why_them"] and "nobody pitches them" in rin["why_them"]
    assert rin["rule"].startswith("No pitch: In conversation since 2026-09-14")


def test_every_suggested_invite_is_in_a_host_brief_marked_as_not_invited_yet(simulation):
    nxt = events.page("simulation")["next"]
    briefs = {b["host"]: b for b in nxt["briefs"]}
    assert [p["name"] for p in briefs["Dana Kest"]["people"]] == ["Rin Vale"]  # coming, and her coauthor hosts
    assert sorted(p["name"] for b in nxt["briefs"] for p in b["if_invited"]) == sorted(p["name"] for p in nxt["invite"])
    assert [p["name"] for p in briefs["Dana Kest"]["if_invited"]] == ["Sam Okoro"]  # model evals: Dana's topic
    rin = briefs["Dana Kest"]["people"][0]
    assert rin["tie"].startswith("Dana Kest coauthored") and briefs["Dana Kest"]["if_invited"][0]["tie"] is None
    assert rin["why_them"].startswith("A close coauthor left their employer on Sep 1")
    assert rin["links"] == [{"label": "GitHub", "url": "https://github.com/rinvale-example"},
                            {"label": "Site", "url": "https://rinvale.example/"}]


def test_a_bad_people_file_is_said_on_the_page_not_a_server_error(monkeypatch, tmp_path):
    people = tmp_path / "people.json"
    people.write_text("{not json")
    monkeypatch.setattr(events, "WATCHLIST", people)
    monkeypatch.setattr(today, "TIMELINES", tmp_path / "t.sqlite")
    Store(f"sqlite:///{tmp_path / 't.sqlite'}")
    monkeypatch.setitem(events.RECORDS, "live", tmp_path / "no-events")
    w = events.page("live")["watchlist"]
    assert w["invite"] == [] and w["error"].startswith("The people file could not be read")


def test_live_without_event_records_suggests_from_the_watchlist_with_their_links(monkeypatch, tmp_path):
    store = today._demo_store()[0]
    real = tmp_path / "timelines.sqlite"
    copy = Store(f"sqlite:///{real}")
    for kind in ("person_context", "timeline_event"):
        for row in store.all(kind):
            copy.put(row["id"], kind, row)
    people = tmp_path / "people.json"
    people.write_text(json.dumps([
        {"person_id": "X900", "name": "Mara Quill", "role": "mts-research", "since": "2026-01-01", "x_handle": "@maraquill_example",
         "github": "maraquill-example"},
        {"person_id": "D903", "name": "Priya Vance", "role": "product-designer", "since": "2026-01-01"},
        {"person_id": "M1", "name": "Moment Person", "role": "backend", "since": "2026-01-01",
         "moments": [{"date": "2026-05-01", "kind": "job", "source_url": "https://example.org/news"}]}]))
    monkeypatch.setattr(today, "TIMELINES", real)
    monkeypatch.setattr(today, "iso", lambda: today.DEMO_AS_OF)  # live runs as of now: pin it to the invented week
    monkeypatch.setattr(events, "WATCHLIST", people)
    monkeypatch.setitem(events.RECORDS, "live", tmp_path / "no-events")
    page = events.page("live")
    assert page["missing"] and page["watchlist"]["people_file"] == str(people)
    mara = page["watchlist"]["invite"][0]
    assert mara["name"] == "Mara Quill" and mara["role"] == "Member of Technical Staff"
    assert mara["links"] == [{"label": "X", "url": "https://x.com/maraquill_example"},
                             {"label": "GitHub", "url": "https://github.com/maraquill-example"},
                             {"label": "Site", "url": "https://maraquill.example/"}]
    assert "The engine says reach out now" in mara["why_them"]
    assert [h["name"] for h in page["watchlist"]["held"]] == ["Priya Vance"]  # asked us to wait
    assert "Moment Person" not in json.dumps(page)  # a moment person is the scorecard's, never an invite
    assert events.page("simulation")["watchlist"] is None
    # Someone who may still be a student: still invited, with the role as context, but Confirm first, as on Today's
    # calls, with the ledger's note, and nothing saying reach out now.
    contact.record(copy, "X900", "unconfirmed", at="2026-09-10T00:00:00+00:00", note="Their bio says CS at a school.")
    mara = next(p for p in events.page("live")["watchlist"]["invite"] if p["name"] == "Mara Quill")
    assert (mara["action"], mara["call"], mara["rank"], mara["role"]) == (
        "verify_first", "Confirm first", 1, "Member of Technical Staff")
    assert mara["check"].startswith("Their bio says CS at a school.") and "reach out" not in mara["why_them"].lower()


@pytest.fixture
def live_records(monkeypatch, tmp_path):
    """Live mode on the invented event records, as of the invented week, with a people file to write."""
    db = tmp_path / "timelines.sqlite"
    routing.seed(Store(f"sqlite:///{db}"), json.loads((FIX.parent / "demo" / "timeline.json").read_text()))
    monkeypatch.setattr(today, "TIMELINES", db)
    monkeypatch.setattr(today, "iso", lambda: today.DEMO_AS_OF)
    monkeypatch.setitem(events.RECORDS, "live", FIX)
    people = tmp_path / "people.json"
    monkeypatch.setattr(events, "WATCHLIST", people)
    return people


def test_live_event_records_never_suggest_a_moment_person(live_records):
    assert "Ivy Stroud" in [p["name"] for p in events.page("live")["next"]["invite"]]
    moment = [{"date": "2026-05-01", "kind": "job", "source_url": "https://example.org/news"}]
    live_records.write_text(json.dumps([
        {"person_id": "X901", "name": "Ivy Stroud", "role": "mts-research", "since": "2026-01-01", "moments": moment},
        {"person_id": "A900", "name": "Rin Vale", "role": "mts-research", "since": "2026-01-01", "moments": moment}]))
    page = events.page("live")
    assert page["people_error"] is None and "Ivy Stroud" not in json.dumps(page["next"])  # nor in a host's brief
    rin = next(p for b in page["next"]["briefs"] for p in b["people"] if p["name"] == "Rin Vale")  # coming anyway
    assert rin["rule"] == f"No pitch: {events.MOMENT_GUEST}" and "nobody pitches them" in rin["why_them"]
    with pytest.raises(ValueError, match="moment people"):
        events.mark("live", "gi-2026-09-games", "X901", "invited", "Nora Example")


def test_a_moment_person_under_another_id_is_never_suggested_either(live_records):
    moment = [{"date": "2026-05-01", "kind": "job", "source_url": "https://example.org/news"}]
    live_records.write_text(json.dumps([  # the people file knows Ivy by a role-prefixed id; the records by X901
        {"person_id": "mts-research:X901", "name": "Ivy Stroud", "role": "mts-research", "since": "2026-01-01",
         "moments": moment},
        {"person_id": "X901", "name": "Ivy Stroud", "role": "mts-research", "since": "2026-01-01"}]))
    page = events.page("live")
    assert "Ivy Stroud" not in json.dumps(page["next"])
    assert "Ivy Stroud" not in [p["name"] for p in inbox.week("live")["event"]["invite"]]  # nor the brief's panel
    with pytest.raises(ValueError, match="moment people"):
        events.mark("live", "gi-2026-09-games", "X901", "invited", "Nora Example")


def test_event_records_under_a_prefixed_id_still_meet_the_moment_row(live_records, tmp_path, monkeypatch):
    records = tmp_path / "records"  # the other way round: the records say mts-research:X901, the people file X901
    shutil.copytree(FIX, records)
    for name in ("people.json", "guests.csv"):
        f = records / name
        f.write_text(f.read_text().replace('"X901"', '"mts-research:X901"').replace(",X901,", ",mts-research:X901,")
                     .replace('"A900"', '"mts-research:A900"').replace(",A900,", ",mts-research:A900,"))
    monkeypatch.setitem(events.RECORDS, "live", records)
    assert "Ivy Stroud" in [p["name"] for p in events.page("live")["next"]["invite"]]
    moment = [{"date": "2026-05-01", "kind": "job", "source_url": "https://example.org/news"}]
    live_records.write_text(json.dumps([
        {"person_id": "X901", "name": "Ivy Stroud", "role": "mts-research", "since": "2026-01-01", "moments": moment},
        {"person_id": "A900", "name": "Rin Vale", "role": "mts-research", "since": "2026-01-01", "moments": moment}]))
    page = events.page("live")
    assert "Ivy Stroud" not in json.dumps(page["next"])
    rin = next(p for b in page["next"]["briefs"] for p in b["people"] if p["name"] == "Rin Vale")
    assert rin["rule"] == f"No pitch: {events.MOMENT_GUEST}"
    with pytest.raises(ValueError, match="moment people"):
        events.mark("live", "gi-2026-09-games", "mts-research:X901", "invited", "Nora Example")


def test_a_follow_up_is_never_marked_sent_to_a_moment_person(live_records):
    events.add_note("live", "gi-2026-09-salon", "X901", "open", "up for a real conversation", "Nora Example")
    assert [f["name"] for f in events.page("live")["last"]["follow_ups"]] == ["Ivy Stroud"]
    moment = [{"date": "2026-05-01", "kind": "job", "source_url": "https://example.org/news"}]
    live_records.write_text(json.dumps([{"person_id": "mts-research:X901", "name": "Ivy Stroud", "role": "mts-research",
                                         "since": "2026-01-01", "moments": moment}]))  # the replay case, by another id
    assert events.page("live")["last"]["follow_ups"] == []  # the page never offers it
    with pytest.raises(ValueError, match="moment people: never followed up"):  # nor records one sent by hand
        events.mark("live", "gi-2026-09-salon", "X901", "sent", "Nora Example")
    assert not [e for e in contact.history(today.source("live")[0], "X901") if e["kind"] == "sent"]


def test_a_people_file_that_does_not_read_holds_every_follow_up_too(live_records):
    events.add_note("live", "gi-2026-09-salon", "X901", "open", "up for a real conversation", "Nora Example")
    live_records.write_text('[{"person_id": "X902"}]')  # one bad row: Ivy may be a replay case, or not
    [f] = events.page("live")["last"]["follow_ups"]
    assert f["name"] == "Ivy Stroud" and f["hold"] == events.UNREAD_HOLD and not f["sent"]
    with pytest.raises(ValueError, match="could not be read") as refused:
        events.mark("live", "gi-2026-09-salon", "X901", "sent", "Nora Example")
    assert str(refused.value) == events.UNREAD_MARK  # a press posts it to Slack: never the file's path or rows


def test_a_people_file_that_does_not_read_suggests_nobody(live_records):
    live_records.write_text('[{"person_id": "X902"}]')  # one bad row: who the moment people are is unknown
    page = events.page("live")
    assert page["people_error"].startswith("The people file could not be read")
    assert page["next"]["invite"] == [] and page["next"]["blocked"].startswith("Nobody is suggested")
    assert not [p for b in page["next"]["briefs"] for p in b["if_invited"]]
    rin = next(p for b in page["next"]["briefs"] for p in b["people"] if p["name"] == "Rin Vale")
    assert rin["rule"].startswith("No pitch: Nobody is suggested")  # who is a moment person is unknown: pitch nobody
    with pytest.raises(ValueError, match="could not be read"):
        events.mark("live", "gi-2026-09-games", "X901", "invited", "Nora Example")


def test_a_follow_up_comes_from_the_host_who_talked_to_them(simulation):
    events.add_note("simulation", "gi-2026-09-salon", "D902", "open", "Up for a real talk.", "Nora Example")
    f = next(f for f in events.page("simulation")["last"]["follow_ups"] if f["name"] == "Jonah Pike")
    assert f["host"] == "Nora Example" and f["draft"]["body"].endswith("\n\nNora")  # Luis fits the topic; Nora spoke


def test_an_invite_comes_from_the_teammate_who_knows_them_and_says_so(simulation):
    store, as_of, records, _ = events.workspace("simulation")
    night, rin = by_id(records)["gi-2026-09-games"], next(p for p in records["people"] if p.subject_id == "A900")
    past = [e for e in records["events"] if e.day <= night.day and e.id != night.id]
    sender = events.invite_from(store, as_of, rin, night, today.source("simulation")[2], past)
    assert sender["from"] == "Dana Kest" and sender["kind"] == "coauthor" and sender["tie"].startswith("Dana Kest coauthored")
    body = events.invite_draft(store, as_of, rin, night, None, sender)["body"]
    assert body.startswith(f"Hey Rin, hope all is well since we wrote '{sender['work']}' together. We're hosting "
                           "World model evals night at General Intuition in New York on Thursday, September 24,")
    assert body.endswith("\n\nDana")


def test_same_employer_at_the_same_time_is_a_hint_to_ask_never_a_tie(simulation, monkeypatch):
    store, as_of, records, _ = events.workspace("simulation")
    night, ivy = by_id(records)["gi-2026-09-games"], next(p for p in records["people"] if p.subject_id == "X901")
    team = today.source("simulation")[2]
    omar = routing.Teammate.model_validate(next(m for m in team if m["name"] == "Omar Lind"))
    overlap = routing.Path(kind="overlap", level="likely", teammate=omar,
                           detail="Omar Lind and they were both at Acme Research in 2021-2023")
    monkeypatch.setattr(routing, "paths", lambda events_, mates, as_of=None: [overlap])
    sender = events.invite_from(store, as_of, ivy, night, team, [])
    assert sender["tie"] is None and sender["from"] == "Nora Example"  # the host, claiming nothing
    assert sender["hint"] == "Omar Lind and they were both at Acme Research in 2021-2023: ask Omar whether they know them"
    body = events.invite_draft(store, as_of, ivy, night, None, sender)["body"]
    assert "Acme" not in body and "Omar" not in body


def test_code_is_named_by_its_project_never_read_back_as_a_commit():
    opener = {"source_url": "https://github.com/ivy/mc-diffusion/commit/abc", "quote": "Pushed to ivy/mc-diffusion: fix loss",
              "public_at": "2026-09-10T00:00:00+00:00", "event_date": "2026-09-10"}
    assert events._opened_with(opener) == "Saw your recent work on ivy/mc-diffusion."


def test_with_no_tie_on_file_the_note_opens_with_their_own_work_and_claims_none(simulation):
    invite = {p["name"]: p for p in events.page("simulation")["next"]["invite"]}
    ivy, lena, sam = invite["Ivy Stroud"], invite["Lena Okafor"], invite["Sam Okoro"]
    assert (ivy["from"], ivy["tie"]) == ("Nora Example", None)  # the host whose topics fit: no tie claimed
    assert ivy["draft"]["body"].startswith("Hey Ivy, saw what you wrote: “Trying diffusion world models")
    assert ivy["draft"]["subject"] == "World model evals night, September 24"
    assert lena["draft"]["body"].startswith("Hey Lena, we're hosting World model evals night")
    assert "Example Robotics" not in sam["draft"]["body"] and sam["from"] == "Dana Kest"  # no claim about their work
    for p in invite.values():  # never a tie that isn't on file
        assert not any(w in p["draft"]["body"] for w in ("since we", "our time at", "good to talk", "we met"))


def test_someone_a_host_talked_with_before_gets_the_invite_from_that_host(simulation):
    events.add_note("simulation", "gi-2026-09-salon", "X901", "context", "Talked Minecraft evals.", "Dana Kest")
    page = events.page("simulation")
    ivy = next(p for p in page["next"]["invite"] if p["name"] == "Ivy Stroud")
    assert ivy["from"] == "Dana Kest" and ivy["tie"] == "Dana Kest talked with them at Agents in games salon"
    assert ivy["follow_up"] == "Agents in games salon" and ivy["draft"] is None  # one note, not two
    f = next(f for f in page["last"]["follow_ups"] if f["name"] == "Ivy Stroud")
    assert f["host"] == "Dana Kest" and f["invites_to"] == "World model evals night"  # Dana is no salon host, but talked
    assert f["draft"]["body"].startswith("Hey Ivy, good to meet you at Agents in games salon")
    assert "We're also hosting World model evals night" in f["draft"]["body"] and f["draft"]["body"].endswith("\n\nDana")
    assert "Dana Kest" in page["next"]["senders"] and "Nora Example" in page["next"]["senders"]
    events.mark("simulation", "gi-2026-09-games", "X901", "invited", ivy["from"])
    assert events.page("simulation")["next"]["invited"][0]["by"] == "Dana Kest"


def test_a_follow_up_with_nobody_on_file_as_having_talked_claims_no_meeting(records, store):
    night = by_id(records)["gi-2026-09-games"]
    events.record_note(store, night, "X900", "open", "up for a real conversation", "")
    f = events.follow_ups(store, night, records["people"], datetime(2026, 9, 25, tzinfo=timezone.utc))[0]
    assert f["met"] is False and "good to meet you" not in f["draft"]["body"]
    assert f["draft"]["body"].startswith("Hey Mara, thanks for coming to World model evals night")


def test_one_note_is_marked_once_and_records_the_follow_up_and_the_invite(simulation):
    events.add_note("simulation", "gi-2026-09-salon", "X901", "context", "Talked Minecraft evals.", "Dana Kest")
    f = next(f for f in events.page("simulation")["last"]["follow_ups"] if f["name"] == "Ivy Stroud")
    assert f["invites_to_id"] == "gi-2026-09-games"
    events.mark("simulation", "gi-2026-09-salon", "X901", "sent", f["host"], also_invite=f["invites_to_id"])
    page = events.page("simulation")
    assert next(x for x in page["last"]["follow_ups"] if x["name"] == "Ivy Stroud")["sent"]["by"] == "Dana Kest"
    assert [(i["name"], i["by"]) for i in page["next"]["invited"]] == [("Ivy Stroud", "Dana Kest")]
    assert "Ivy Stroud" not in [p["name"] for p in page["next"]["invite"]]  # invited: a seat, not a suggestion
    ivy = next(p for b in page["next"]["briefs"] for p in b["people"] if p["name"] == "Ivy Stroud")  # coming
    assert not ivy["rule"].startswith("No pitch: Sent")  # the note that invited her is no other team's open ask
    events.add_note("simulation", "gi-2026-09-salon", "D902", "open", "Up for a real talk.", "Nora Example")
    with pytest.raises(ValueError, match="carries no invite"):  # Jonah is backend: the MTS night was never in his note
        events.mark("simulation", "gi-2026-09-salon", "D902", "sent", "Nora Example", also_invite="gi-2026-09-games")


def test_a_combined_note_records_neither_when_the_invite_is_not_allowed(simulation, monkeypatch):
    events.add_note("simulation", "gi-2026-09-salon", "X901", "context", "Talked Minecraft evals.", "Dana Kest")
    monkeypatch.setattr(events, "_moment_people", lambda mode: (frozenset({"X901"}), None))
    with pytest.raises(ValueError, match="moment people"):
        events.mark("simulation", "gi-2026-09-salon", "X901", "sent", "Dana Kest", also_invite="gi-2026-09-games")
    store = today._demo_store()[0]
    assert not [e for e in contact.history(store, "X901") if e["kind"] in ("sent", "invited")]  # nothing half-recorded


def test_a_combined_invite_needs_the_night_to_have_picked_them(simulation, monkeypatch):
    events.add_note("simulation", "gi-2026-09-salon", "X901", "context", "Talked Minecraft evals.", "Dana Kest")
    monkeypatch.setattr(events, "suggest_invites", lambda *a, **k: ([], []))  # say the seats ran out
    with pytest.raises(ValueError, match="carries no invite"):
        events.mark("simulation", "gi-2026-09-salon", "X901", "sent", "Dana Kest", also_invite="gi-2026-09-games")
    assert not [e for e in contact.history(today._demo_store()[0], "X901") if e["kind"] in ("sent", "invited")]


def test_a_teammate_who_said_they_know_them_sends_the_invite(simulation):
    store, as_of, records, _ = events.workspace("simulation")
    night, ivy = by_id(records)["gi-2026-09-games"], next(p for p in records["people"] if p.subject_id == "X901")
    contact.record(store, "X901", "knows", at="2026-09-14T00:00:00+00:00", by="Omar Lind", note="met at NeurIPS")
    sender = events.invite_from(store, as_of, ivy, night, today.source("simulation")[2], [])
    assert (sender["from"], sender["tie"], sender["kind"]) == ("Omar Lind", "Omar Lind knows them (met at NeurIPS)", "knows")
    body = events.invite_draft(store, as_of, ivy, night, None, sender)["body"]
    assert body.endswith("\n\nOmar") and "NeurIPS" not in body  # the note claims nothing it wasn't told to
    contact.record(store, "X901", "knows", at="2026-09-14T01:00:00+00:00", by="Omar Lind",
                   note="Friends. Told me in confidence they're interviewing")  # the form's how, then its free text
    tie = events.invite_from(store, as_of, ivy, night, today.source("simulation")[2], [])["tie"]
    assert tie == "Omar Lind knows them (Friends)"  # as the Slack card says it: never the free text


def test_a_knows_from_outside_the_roster_or_before_a_wrong_person_is_only_a_hint(simulation):
    store, as_of, records, _ = events.workspace("simulation")
    night, ivy = by_id(records)["gi-2026-09-games"], next(p for p in records["people"] if p.subject_id == "X901")
    team = today.source("simulation")[2]
    contact.record(store, "X901", "knows", at="2026-09-14T00:00:00+00:00", by="U07ABCDEF", note="Friends.")
    sender = events.invite_from(store, as_of, ivy, night, team, [])
    assert sender["tie"] is None and sender["from"] == "Nora Example" and sender["hint"].startswith("U07ABCDEF said")
    contact.record(store, "X901", "knows", at="2026-09-14T01:00:00+00:00", by="Omar Lind")
    contact.record(store, "X901", "wrong_person", at="2026-09-14T02:00:00+00:00", by="Dana Kest")
    assert events.invite_from(store, as_of, ivy, night, team, [])["tie"] is None  # it was about someone else


def test_a_wait_for_a_first_year_hold_ranks_with_the_quiet_and_never_reads_as_a_window(records):
    night = by_id(records)["gi-2026-09-games"]
    soon = "2026-10-01T00:00:00+00:00"
    held = {"X901": call("watch_until", until=soon, reasons=(), holds=("short_tenure",))}
    window = {"X901": call("watch_until", until=soon, reasons=())}
    rank = lambda calls, alone=frozenset(): next(p["rank"] for p in events.suggest_invites(  # noqa: E731
        night, records["people"], records["guests"], calls, records["programs"], max_extra=10, alone=alone)[0]
        if p["name"] == "Ivy Stroud")
    assert (rank(window), rank(held, {"X901"})) == (2, 3)  # a window about to open outranks the quiet; a first year doesn't
    # A: a hold that lifts off a reason (their own launch this week): it is a reach the day after, so it ranks 2.
    launch = {"X901": call("watch_until", until=soon, reasons=("launch",), holds=("imminent_launch",))}
    assert rank(launch) == 2
    # B: a hold that lifts onto a window: not a wait on holds alone, so events.page leaves it out of ``alone``.
    assert rank(held) == 2
    people = [p for p in records["people"] if p.subject_id == "X901"]
    [rule] = [p["rule"] for b in events.host_briefs(night, people, held) for p in b["people"]]
    assert rule == ("No pitch while they're held back (under a year in their current role); the engine looks again "
                    "on Oct 1. Talk about their work.")  # the day isn't claimed as the hold's end


def test_a_held_wait_for_a_window_names_no_false_end_to_the_hold(records):
    night = by_id(records)["gi-2026-09-games"]
    as_of = "2026-09-24T00:00:00+00:00"
    signals = [  # a first year that runs to March, and a window that opens in October
        {"detector_id": "short_tenure", "family": "tenure", "window_open": "2026-03-01T00:00:00+00:00",
         "window_close": "2027-03-01T00:00:00+00:00", "holds": ["short_tenure"], "evidence_event_ids": ["e1"],
         "strength": 1.0},
        {"detector_id": "pi_departure", "family": "lab", "window_open": "2026-10-01T00:00:00+00:00",
         "window_close": "2027-01-01T00:00:00+00:00", "holds": [], "evidence_event_ids": ["e2"], "strength": 1.0}]
    c = readiness.score(signals, as_of)
    assert (c.action, c.until[:10], c.holds) == ("watch_until", "2026-10-01", ["short_tenure"])
    people = [p for p in records["people"] if p.subject_id == "X901"]
    [rule] = [p["rule"] for b in events.host_briefs(night, people, {"X901": c}) for p in b["people"]]
    assert "until" not in rule and "looks again on Oct 1." in rule


@pytest.mark.parametrize("words", ["Rough week: our whole team was laid off last week.",
                                   "Praying for my family at church this weekend.",
                                   "Voting for the Democrats this fall, go vote."])
def test_an_invite_never_quotes_a_layoff_or_anything_personal(simulation, monkeypatch, words):
    store, as_of, records, _ = events.workspace("simulation")
    night, ivy = by_id(records)["gi-2026-09-games"], next(p for p in records["people"] if p.subject_id == "X901")
    post = {"id": "p1", "quote": words, "source_url": "https://x.com/ivy_example/status/1", "event_type": "post",
            "event_date": "2026-09-10", "observed_at": "2026-09-10T00:00:00+00:00"}
    monkeypatch.setattr(events.journey, "view", lambda *a, **k: [post])
    monkeypatch.setattr(events.readiness, "opener", lambda call: "p1")
    body = events.invite_draft(store, as_of, ivy, night, call("reach_now", track="pitch"),
                               {"kind": None, "from": "Nora Example"})["body"]
    assert words[:12] not in body and "saw what you wrote" not in body.lower()  # the cards' own check: never quoted
    assert body.startswith("Hey Ivy, we're hosting World model evals night")
    ok = {**post, "quote": "Trying diffusion world models on Minecraft trajectories."}
    monkeypatch.setattr(events.journey, "view", lambda *a, **k: [ok])
    assert "saw what you wrote: “" in events.invite_draft(store, as_of, ivy, night, call("reach_now", track="pitch"),
                                                                {"kind": None, "from": "Nora Example"})["body"]


def test_why_them_shows_a_withheld_post_without_quotation_marks(simulation, monkeypatch):
    store, as_of, *_ = events.workspace("simulation")
    trigger = {"id": "p1", "what": "posted", "date": "2026-09-10", "quote": routing.WITHHELD}
    monkeypatch.setattr(events.routing, "signals", lambda call, by_id: [trigger])
    text = events.why_them(store, as_of, "X901", call("reach_now", track="pitch"))
    assert text.startswith(f"Posted on Sep 10 {routing.WITHHELD}.") and "“(" not in text


def test_why_them_shows_a_post_nobody_read_as_its_own_note_never_as_their_words(simulation, monkeypatch):
    # A burst of posting counts posts the reader read nothing off (tests/test_posts.py lists how one can leak): the
    # line says so, without quotation marks, and never calls it personal.
    store, as_of, *_ = events.workspace("simulation")
    trigger = {"id": "p1", "what": "a burst of posting", "date": "2026-09-10", "quote": routing.UNREAD}
    monkeypatch.setattr(events.routing, "signals", lambda call, by_id: [trigger])
    text = events.why_them(store, as_of, "X901", call("reach_now", track="pitch"))
    assert text.startswith(f"A burst of posting on Sep 10 {routing.UNREAD}.") and "“(" not in text
    assert routing.WITHHELD not in text


def test_why_them_never_repeats_a_layoff_or_someone_elses_words(simulation, monkeypatch):
    store, as_of, *_ = events.workspace("simulation")
    trigger = {"id": "p1", "what": "posted", "date": "2026-09-10", "type": "post",
               "quote": "Rough week: our whole team was laid off last week."}
    monkeypatch.setattr(events.routing, "signals", lambda call, by_id: [trigger])
    text = events.why_them(store, as_of, "X901", call("reach_now", track="pitch"))
    assert "laid off" not in text and text.startswith(f"Posted on Sep 10 {routing.WITHHELD}.")
    trigger["quote"] = "Agreed, great thread" + detectors.REPLY_CONTEXT + "@bob: my startup is shutting down"
    text = events.why_them(store, as_of, "X901", call("reach_now", track="pitch"))
    assert ": “Agreed, great thread”." in text and "bob" not in text and "shutting" not in text  # theirs only


def test_an_invite_never_quotes_the_post_they_answered_as_theirs(simulation, monkeypatch):
    store, as_of, records, _ = events.workspace("simulation")
    night, ivy = by_id(records)["gi-2026-09-games"], next(p for p in records["people"] if p.subject_id == "X901")
    post = {"id": "p1", "quote": detectors.REPLY_CONTEXT.lstrip() + "@bob: diffusion world models on Minecraft",
            "source_url": "https://x.com/ivy_example/status/1", "event_type": "post",
            "event_date": "2026-09-10", "observed_at": "2026-09-10T00:00:00+00:00"}  # a quote post with no words of its own
    monkeypatch.setattr(events.journey, "view", lambda *a, **k: [post])
    monkeypatch.setattr(events.readiness, "opener", lambda call: "p1")
    body = events.invite_draft(store, as_of, ivy, night, call("reach_now", track="pitch"),
                               {"kind": None, "from": "Nora Example"})["body"]
    assert "bob" not in body and "saw what you wrote" not in body.lower()


def test_only_a_wait_on_holds_alone_ranks_with_the_quiet(monkeypatch):
    as_of = "2026-09-24T00:00:00+00:00"
    first_year = {"family": "tenure", "window_open": "2026-03-01T00:00:00+00:00"}
    window_later = {"family": "lab", "window_open": "2026-10-20T00:00:00+00:00"}  # opens after the hold lifts (B)
    held = call("watch_until", until="2026-10-08T00:00:00+00:00", reasons=(), holds=("imminent_launch",))
    monkeypatch.setattr(events.journey, "detect", lambda *a, **k: [first_year])
    assert events._holds_alone(None, "X901", as_of, held)  # nothing to decide when it ends
    monkeypatch.setattr(events.journey, "detect", lambda *a, **k: [first_year, window_later])
    assert not events._holds_alone(None, "X901", as_of, held)  # B: a reach once the window opens
    launch = call("watch_until", until="2026-10-08T00:00:00+00:00", reasons=("launch",), holds=("imminent_launch",))
    assert not events._holds_alone(None, "X901", as_of, launch)  # A: a reason is behind it


def test_the_events_page_ranks_a_wait_on_holds_alone_with_the_quiet_and_any_other_wait_above_it(simulation, monkeypatch):
    real = events.journey.assess
    soon = call("watch_until", until="2026-10-01T00:00:00+00:00", reasons=(), holds=("imminent_launch",))
    monkeypatch.setattr(events.journey, "assess", lambda store, sid, *a: soon if sid == "X901" else real(store, sid, *a))
    ivy = lambda: next(p for p in events.page("simulation")["next"]["invite"] if p["name"] == "Ivy Stroud")  # noqa: E731
    monkeypatch.setattr(events.today, "hold_only", lambda activations, as_of: True)  # nothing behind the hold
    assert (ivy()["action"], ivy()["rank"]) == ("watch_until", 3)
    monkeypatch.setattr(events.today, "hold_only", lambda activations, as_of: False)  # a window opens after it (B)
    assert ivy()["rank"] == 2
