"""Today's calls: the engine's call for each person as the brief's seven-part ping, on invented people."""

import copy
import gc
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import contact, detectors, golden, happened, main, readiness, routing, today
from app.sources.sec import OfficerEvent
from app.store import Store
from app.timeline import DETECTORS, TimelineEvent, add_event

INTERNAL = re.compile(r"\b[a-z]+_[a-z_]+\b")  # a detector, hold or channel id where a person reads words
HANDLE_OR_LINK = re.compile(r"@\w+|https?://\S+")


@pytest.fixture(scope="module")
def simulation():
    return today.calls("simulation")


def test_every_invented_person_appears_once_with_a_call_and_all_four_kinds_show(simulation):
    names = [c["name"] for c in simulation["calls"]]
    assert len(names) == len(set(names)) == 8
    assert {c["headline"] for c in simulation["calls"]} == {"Reach out now", "Check first", "Wait", "Stay quiet"}
    assert {c["role"]["id"] for c in simulation["calls"]} == {"mts-research", "backend", "product-designer"}
    assert [c["headline"] for c in simulation["calls"]][:4] == ["Reach out now"] * 4
    assert simulation["calls"][0]["track"] == today.PITCH  # pitches before notes about their work
    assert simulation["as_of"] == "2026-09-15" and simulation["team_loaded"]


def test_a_reach_carries_all_seven_parts(simulation):
    for c in (c for c in simulation["calls"] if c["action"] == "reach_now"):
        assert c["name"] and c["profile_url"].startswith("https://")  # 1 person, with a public profile
        assert c["role"]["title"] and c["role"]["jd_url"].startswith("https://")  # 2 role
        assert c["trigger"]["date"] and c["trigger"]["quote"] and c["trigger"]["source_url"].startswith("https://")  # 3
        assert c["why_now"] and c["next_step"].startswith("Reach out before")  # 4 why now
        assert c["confidence"] in ("medium", "high") and c["falsifiers"]  # 5 confidence and what would prove it wrong
        assert c["draft"]["body"].startswith(f"Hey {c['name'].split()[0]},")  # 6 draft
        assert c["route"]["channel"] and c["route"]["reason"]  # 7 route in


def test_a_reach_another_team_owns_is_held_and_listed_after_the_clear_ones(simulation):
    reaches = [c for c in simulation["calls"] if c["action"] == "reach_now"]
    theo = reaches[-1]
    assert theo["name"] == "Theo Marsh" and theo["gate"]["state"] == "hold"
    assert "per Omar Example (sales)" in theo["gate"]["reason"]
    assert all(c["gate"] is None for c in reaches[:-1])


def test_a_warm_path_is_found_from_the_team_file_and_named(simulation):
    rin = next(c for c in simulation["calls"] if c["name"] == "Rin Vale")
    # Dana coauthored with Rin, so Dana writes, and the card first asks Dana on Slack.
    assert rin["route"]["sender"] == "Dana Kest" and rin["route"]["ask"]["to"] == "Dana Kest"
    assert rin["route"]["channel"] == "email" and rin["route"]["target"] == "rin@rinvale.example"
    assert rin["route"]["open"] == "Open the email" and rin["route"]["url"] == ""  # it opens in Dana's account
    assert rin["route"]["ask"]["body"].startswith("Hey Dana, Rin")
    assert rin["draft"]["body"] not in rin["route"]["ask"]["body"]  # the page shows the draft once, in part 6
    assert rin["why_now"].startswith("Rin had a paper accepted on Aug 20") and "Open on" not in rin["why_now"]
    assert any("Dana Kest coauthored" in p for p in rin["route"]["paths"])


def test_calls_that_are_not_a_reach_say_what_to_do_instead_and_carry_no_draft(simulation):
    by_name = {c["name"]: c for c in simulation["calls"]}
    lena, priya, jonah = by_name["Lena Okafor"], by_name["Priya Vance"], by_name["Jonah Pike"]
    assert lena["action"] == "verify_first" and "Birchwood Lab" in lena["falsifiers"][0]
    assert lena["trigger"]["date"] == "2026-09-01"  # the day she said it, not the "from November" she named
    assert priya["action"] == "respect_follow_up" and priya["until"] == "2026-11-01"
    assert priya["next_step"] == "Look again on 2026-11-01."
    assert jonah["action"] == "quiet" and "not enough to reach out on its own" in jonah["why_now"]
    assert priya["why_now"] == "Priya posted work in progress on Sep 9. The person named a date; follow up " \
        "from Nov 1."
    for c in simulation["calls"]:  # plain sentences, never the engine's bookkeeping or an ISO date
        assert "Open on" not in c["why_now"] and not re.search(r"\d{4}-\d{2}-\d{2}", c["why_now"]), c["why_now"]
    assert all(c["draft"] is None and c["route"] is None for c in (lena, priya, jonah))


def test_no_internal_label_reaches_a_reader_and_no_two_people_share_their_reasoning(simulation):
    for c in simulation["calls"]:
        route = c["route"] or {}
        shown = [c["headline"], c["why_now"], c["next_step"], *(s["what"] for s in c["signals"]),
                 route.get("channel", ""), route.get("reason", ""), route.get("open", ""), *route.get("paths", [])]
        words = [HANDLE_OR_LINK.sub("", text) for text in shown]  # an @handle or a link may have underscores
        assert not [w for text in words for w in INTERNAL.findall(text)], c["name"]
    whys = [c["why_now"] for c in simulation["calls"]]
    assert len(set(whys)) == len(whys)


def test_every_reason_detector_and_hold_has_plain_words():
    assert all(d in readiness.LABELS for d in DETECTORS if not d.startswith(("hold_", "calendar_")))
    assert readiness.label("technical_ask") == "a public ask GI could answer"
    assert readiness.label("some_new_thing") == "some new thing"
    assert set(routing.CHANNELS) == set(today.OPEN)


def test_live_without_timelines_says_what_to_run(monkeypatch, tmp_path):
    monkeypatch.setattr(today, "TIMELINES", tmp_path / "missing.sqlite")
    out = today.calls("live")
    assert out["calls"] == [] and "Run the pull and the post reader" in out["missing"]


def test_live_reads_the_timelines_store_and_warns_when_no_team_list_is_loaded(monkeypatch, tmp_path):
    store, _ = today._demo_store()
    real = tmp_path / "timelines.sqlite"
    copy = Store(f"sqlite:///{real}")
    for kind in ("person_context", "timeline_event"):
        for row in store.all(kind):
            copy.put(row["id"], kind, row)
    monkeypatch.setattr(today, "TIMELINES", real)
    monkeypatch.setattr(today, "TEAM", tmp_path / "no-team.json")
    monkeypatch.setattr(today, "CONTACTS", tmp_path / "no-contacts.json")
    out = today.calls("live")
    assert len(out["calls"]) == 8 and not out["team_loaded"]
    rin = next(c for c in out["calls"] if c["name"] == "Rin Vale")
    assert rin["route"]["channel"] == "none found" and rin["route"]["paths"] == []


def test_the_newest_scorecard_is_shown_without_its_observations_and_with_plain_sign_names(tmp_path):
    assert today.latest_scorecard(tmp_path) is None
    sign = {"before_moment": 1, "ordinary": 0, "kept": False}
    for day in ("2026-09-20", "2026-09-23"):
        (tmp_path / f"{day}-scorecard.json").write_text(json.dumps({
            "noise": {"signs": {"technical_ask": dict(sign), "self_stated_availability": dict(sign)}},
            "scorecard": {}, "observations": [{"big": "list"}], "day": day}))
    report = today.latest_scorecard(tmp_path)
    assert report["file"] == "2026-09-23-scorecard.json" and "observations" not in report
    assert report["noise"]["signs"]["technical_ask"] == {**sign, "label": "a public ask GI could answer", "early": True}
    assert report["noise"]["signs"]["self_stated_availability"]["early"] is False


def test_the_api_serves_calls_and_keeps_the_scorecard_to_live():
    client = TestClient(main.app)
    calls = client.get("/api/calls?mode=simulation").json()
    assert len(calls["calls"]) == 8
    assert client.get("/api/calls?mode=nope").status_code == 422
    assert client.get("/api/scorecard?mode=simulation").json()["report"] is None


def test_a_role_missing_from_the_config_gets_its_row_and_no_draft_instead_of_failing_everyone(monkeypatch, tmp_path):
    real = tmp_path / "timelines.sqlite"
    store = Store(f"sqlite:///{real}")
    routing.seed(store, {"contexts": [{"subject_id": "Z1", "name": "Zed Example", "role": "designer"}], "events": [
        {"subject_id": "Z1", "event_type": "technical_ask", "event_date": "2026-09-20", "quote": "Anyone tried X?",
         "source_url": "https://x.com/zed_example/status/1"}]})
    monkeypatch.setattr(today, "TIMELINES", real)
    zed, = today.calls("live")["calls"]
    assert zed["action"] == "reach_now" and zed["draft"] is None and zed["route"] is None
    assert zed["kind"] == "early_sign"  # still says what the reach is about, without a route
    today.PROFILES.write_text("{not json")
    assert today.calls("live")["calls"][0]["name"] == "Zed Example"  # a bad profile read never takes the page down
    assert zed["role"]["title"] == "designer (not a role in config/roles.json)" and "config/roles.json" in zed["next_step"]


def test_a_flat_copy_per_detector_makes_the_same_calls_as_a_deep_copy(monkeypatch, simulation):
    # detectors.own_copy copies each event dict: a full copy only while every stored field is a scalar.
    scalar = {"string", "integer", "number", "boolean", "null"}
    models, seen = [TimelineEvent], []
    while models:  # TimelineEvent and its subclasses (sec.OfficerEvent), which add_event stores with their extra fields
        seen.append(model := models.pop())
        models += model.__subclasses__()
    assert OfficerEvent in seen
    for model in seen:
        for name, v in model.model_json_schema()["properties"].items():
            assert {t.get("type") for t in v.get("anyOf", [v])} <= scalar, (model.__name__, name)
    stored = Store("sqlite://")  # a fresh store: the simulation's is cached for the app
    for s in golden.load():
        for e in s["events"]:
            add_event(stored, golden.record(s, e))  # as golden.run stores them
    rows = [*today._demo_store()[0].all("timeline_event"), *stored.all("timeline_event")]
    assert len(rows) > 100 and all(v is None or isinstance(v, (str, int, float)) for row in rows for v in row.values())
    fast = [golden.run(s) for s in golden.load()]
    monkeypatch.setattr(detectors, "own_copy", copy.deepcopy)
    assert today.calls("simulation") == simulation
    assert [golden.run(s) for s in golden.load()] == fast


def test_each_reach_says_what_it_is_about_and_everyone_shows_what_just_happened_to_them(simulation):
    by_name = {c["name"]: c for c in simulation["calls"]}
    assert {n: c["kind"] for n, c in by_name.items() if c["action"] == "reach_now"} == {
        "Rin Vale": "just_happened", "Noa Brandt": "early_sign", "Mara Quill": "early_sign", "Theo Marsh": "early_sign"}
    rin, lena = by_name["Rin Vale"], by_name["Lena Okafor"]
    assert [m["line"] for m in rin["happened"]] == ["paper out · 2026-08-20 · day 26 of about 30"]
    assert rin["happened"][0]["source_url"].startswith("https://")
    # Not a reach, and still listed: GI should see a public moment whatever the call.
    assert lena["action"] == "verify_first" and lena["kind"] is None
    assert [m["label"] for m in lena["happened"]] == ["open to work"] and lena["happened"][0]["day"] == 14
    store, as_of, team, contacts, _ = today.source("simulation")
    r = routing.route(store, "A900", as_of, team, contacts.get("A900", []))
    assert [m["line"] for m in rin["happened"]] == [happened.line(h) for h in r.happened]  # the card's own moments
    assert r.card["blocks"][0]["text"]["text"] == "Rin Vale" and r.card["text"].startswith("Just happened: Rin Vale")
    assert rin["closes"] == routing.reach_by(r) == "2026-09-19"  # the card's reach-by day: a month after the paper


def test_a_slack_link_cannot_be_ended_early_by_its_url():
    assert routing._link("https://a.example/x|y>z<w") == "https://a.example/x%7Cy%3Ez%3Cw"


def test_a_never_holds_a_reach_and_an_undergraduate_gets_a_note_not_a_pitch(monkeypatch, tmp_path):
    store, _ = today._demo_store()
    real = tmp_path / "timelines.sqlite"
    live = Store(f"sqlite:///{real}")
    for kind in ("person_context", "timeline_event", "contact"):
        for row in store.all(kind):
            live.put(row["id"], kind, row)
    contact.record(live, "A900", "never", at="2026-09-10T00:00:00+00:00")
    contact.record(live, "X902", "undergraduate", at="2026-09-10T00:00:00+00:00")
    monkeypatch.setattr(today, "TIMELINES", real)
    monkeypatch.setattr(today, "iso", lambda: today.DEMO_AS_OF)
    by_name = {c["name"]: c for c in today.calls("live")["calls"]}
    assert by_name["Rin Vale"]["gate"]["state"] == "hold"  # they said never: held, whatever the timing says
    noa = by_name["Noa Brandt"]  # the gate's rapport-only turns the pitch into a note about their work only
    assert noa["track"] == today.STUDENT and "Product Designer" not in noa["draft"]["body"]


def test_someone_who_may_still_be_a_student_is_confirm_first_and_their_draft_names_no_role(monkeypatch, tmp_path):
    # Ways it could fail: they stay a Reach now, so the Monday post could send their card; the reason shown is not the
    # note the ledger has; the draft or the track pitches the role or hiring; the page says they're an undergraduate,
    # which nobody confirmed; the Monday brief lists them to reach; anyone else's call changes.
    from app import inbox

    store, _ = today._demo_store()
    real = tmp_path / "timelines.sqlite"
    live = Store(f"sqlite:///{real}")
    for kind in ("person_context", "timeline_event", "contact"):
        for row in store.all(kind):
            live.put(row["id"], kind, row)
    monkeypatch.setattr(today, "TIMELINES", real)
    monkeypatch.setattr(today, "iso", lambda: today.DEMO_AS_OF)
    before = {c["name"]: c for c in today.calls("live")["calls"]}
    said = "Their X bio says design at a school, so they may still be a student: confirm before pitching a full-time role."
    contact.record(live, "X902", "unconfirmed", at="2026-09-10T00:00:00+00:00", note=said)
    calls = today.calls("live")
    by_name = {c["name"]: c for c in calls["calls"]}
    noa = by_name["Noa Brandt"]
    assert before["Noa Brandt"]["gate"] is None and noa["action"] == "reach_now"  # the timing call itself stands
    assert noa["gate"]["state"] == "check_first" and noa["gate"]["reason"].startswith(said), noa["gate"]
    assert noa["track"] == today.UNCONFIRMED and "undergraduate" not in json.dumps(noa)
    assert not any(w in noa["draft"]["body"] + noa["draft"]["note"] for w in ("Product Designer", "hiring"))
    assert "undergraduate" not in noa["route"]["ask"]["body"] if noa["route"]["ask"] else True
    assert {n: c["gate"] for n, c in by_name.items() if n != "Noa Brandt"} == \
        {n: c["gate"] for n, c in before.items() if n != "Noa Brandt"}
    monkeypatch.setattr(inbox, "watchlist", lambda mode: (None, False, set()))  # no people file here: everyone
    week = {r["id"]: r for r in inbox.week("live", calls)["roles"]}["product-designer"]
    assert "Noa Brandt" in [p["name"] for p in week["check_first"]]
    assert "Noa Brandt" not in [p["name"] for p in week["reach"]]


def test_the_simulation_store_is_built_once_when_requests_race_and_a_reset_leaves_old_readers_working():
    from concurrent.futures import ThreadPoolExecutor

    today._demo_store.cache_clear()
    with ThreadPoolExecutor(4) as pool:  # a page load right after a reset asks for it several times at once
        stores = list(pool.map(lambda _: today._demo_store()[0], range(4)))
    assert all(s is stores[0] for s in stores) and stores[0].all("person_context")
    today._demo_store.cache_clear()  # a reset
    assert stores[0].all("person_context") and today._demo_store()[0] is not stores[0]
    old = Path(stores[0].engine.url.database).parent
    del stores
    gc.collect()
    assert not old.exists()  # once no request holds the old store, its folder goes
    today._demo_store.cache_clear()
