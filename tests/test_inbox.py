"""The Monday brief: the week's decisions for GI's ops team on the invented people, and its Slack card, which
contacts no one."""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app import contact, events, hiring, inbox, routing, starts, today
from app.store import Store
from tests.test_api import client  # noqa: F401  (a fixture)

PINNED = ("SOCIAL_DIR", "TIMELINES_DB", "DATABASE_URL", "GI_PAUSE_FILE")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def simulation():
    """The demo store Today's calls reads, fresh before and after, since notes and invites write to it."""
    today._demo_store.cache_clear()
    hiring.reset()
    starts.reset()
    yield
    today._demo_store.cache_clear()
    hiring.reset()
    starts.reset()


def test_the_week_lists_per_role_who_to_reach_check_first_and_who_is_held(simulation):
    w = inbox.week("simulation")
    assert (w["as_of"], w["week_of"]) == ("2026-09-15", "2026-09-14")  # the Monday of the simulation's week
    roles = {r["id"]: r for r in w["roles"]}
    assert [p["name"] for p in roles["mts-research"]["reach"]] == ["Rin Vale", "Mara Quill"]
    assert [p["name"] for p in roles["mts-research"]["check_first"]] == ["Lena Okafor"]
    assert [p["name"] for p in roles["product-designer"]["reach"]] == ["Noa Brandt"]
    backend = roles["backend"]
    assert backend["reach"] == [] and [h["name"] for h in backend["held"]] == ["Theo Marsh"]  # a reach the ledger holds
    assert backend["held"][0]["reason"].startswith("Works at a GI partner per Omar Example (sales)")
    assert all(r["pay"] == "No posted range on file" for r in w["roles"])  # no job post in the repo states a range


def test_the_monday_brief_names_the_role_of_the_card_that_holds_a_check_by_title(simulation):
    # tests/test_contact.py lists how a hold can show a role's id (1: a check the brief gates itself).
    contact.record(today._demo_store()[0], "D904", "pinged", at="2026-09-01T00:00:00+00:00", role_id="backend")
    mts = next(r for r in inbox.week("simulation")["roles"] if r["id"] == "mts-research")
    assert {"name": "Lena Okafor", "reason": "On a card from 2026-09-01 (Senior / Lead Backend Engineer): one card per "
            "person, ever."} in mts["held"]


def test_today_s_calls_names_the_role_of_the_card_that_holds_a_reach_by_title(simulation):  # 2, as above
    contact.record(today._demo_store()[0], "X900", "pinged", at="2026-09-01T00:00:00+00:00", role_id="product-designer")
    mara = next(c for c in today.calls("simulation")["calls"] if c["subject_id"] == "X900")
    assert mara["gate"]["reason"] == "On a card from 2026-09-01 (Product Designer): one card per person, ever."


def test_a_role_shows_no_more_than_the_cards_left_this_week(simulation):
    store = today._demo_store()[0]
    for n in range(2):  # two MTS cards already went out this week
        contact.record(store, f"P90{n}", "pinged", at="2026-09-14T12:00:00+00:00", role_id="mts-research", team="recruiting")
    mts = next(r for r in inbox.week("simulation")["roles"] if r["id"] == "mts-research")
    assert [p["name"] for p in mts["reach"]] == ["Rin Vale"] and mts["more"] == 1 and mts["cap_used"] == 2
    contact.record(store, "P902", "pinged", at="2026-09-14T12:00:00+00:00", role_id="mts-research", team="recruiting")
    week = inbox.week("simulation")
    mts = next(r for r in week["roles"] if r["id"] == "mts-research")
    assert mts["reach"] == [] and mts["more"] == 2
    assert "Choose which Member of Technical Staff reach-outs wait" in [d["title"] for d in week["decisions"]]


def test_the_week_shows_no_more_than_the_cards_left_across_all_roles(simulation):
    store = today._demo_store()[0]
    for n in range(3):  # three cards for other roles already went out this week: one left in all
        contact.record(store, f"P90{n}", "pinged", at="2026-09-14T12:00:00+00:00", role_id="backend", team="recruiting")
    week = inbox.week("simulation")
    roles = {r["id"]: r for r in week["roles"]}
    assert [p["name"] for p in roles["mts-research"]["reach"]] == ["Rin Vale"] and roles["mts-research"]["more"] == 1
    assert roles["product-designer"]["reach"] == [] and roles["product-designer"]["more"] == 1
    waits = [d["why"] for d in week["decisions"] if d["title"].startswith("Choose which")]
    assert waits == ["1 more are ready past this week's cap of 4 cards across all roles."] * 2
    assert [d["ping"]["subject_id"] for d in week["decisions"] if d["ping"]] == ["A900"]  # the one card left: the strongest


def test_the_ledger_sorts_checks_and_reaches_into_held_and_check_first(simulation):
    store = today._demo_store()[0]
    contact.record(store, "D904", "never", at="2026-09-14T00:00:00+00:00", team="recruiting", by="Dana Kest")
    contact.record(store, "A900", "wrong_person", at="2026-09-14T00:00:00+00:00", team="recruiting", by="Dana Kest")
    mts = next(r for r in inbox.week("simulation")["roles"] if r["id"] == "mts-research")
    assert [h["name"] for h in mts["held"]] == ["Lena Okafor"]  # said never: not even a check
    assert [p["name"] for p in mts["check_first"]] == ["Rin Vale"] and "Rin Vale" not in [p["name"] for p in mts["reach"]]


def test_a_reach_whose_draft_fails_a_check_is_a_check_first_not_a_card(simulation, monkeypatch):
    real = routing.outreach.check
    monkeypatch.setattr(routing.outreach, "check", lambda draft, items, role, *a, **k: {
        **real(draft, items, role, *a, **k), "passes": False, "problems": ["it names a layoff"]}
        if "Mara" in draft["body"] else real(draft, items, role, *a, **k))
    calls = today.calls("simulation")
    mara = next(c for c in calls["calls"] if c["name"] == "Mara Quill")
    assert mara["gate"] == {"state": "draft", "reason": "The draft fails a check (it names a layoff): fix it before "
                                                        "anyone sends it."}
    week = inbox.week("simulation", calls)
    mts = next(r for r in week["roles"] if r["id"] == "mts-research")
    assert [p["name"] for p in mts["reach"]] == ["Rin Vale"] and "Mara Quill" in [p["name"] for p in mts["check_first"]]
    item = next(d for d in week["decisions"] if "Mara Quill" in d["title"])
    assert item["title"].startswith("Check") and item["act"] is None and item["ping"] is None  # no one-tap draft
    assert item["why"] == "The draft fails a check (it names a layoff): fix it before anyone sends it."
    assert "X900" not in [d["ping"]["subject_id"] for d in week["decisions"] if d["ping"]]  # posting it pings no one


def test_the_next_event_and_the_budget_come_from_the_events_page(simulation):
    w = inbox.week("simulation")
    assert w["event"]["name"] == "World model evals night" and w["event"]["day"] == "2026-09-24"
    assert [p["name"] for p in w["event"]["invite"]] == ["Lena Okafor", "Sam Okoro", "Ivy Stroud"]  # researchers only
    assert w["event"]["held"] == 2  # Mara (her window closes first) and Tove (in conversation with recruiting)
    assert inbox.budget_line(w["budget"]) == ("Q3 2026: $6,101 of $9,000 spent, $3,117 still to come for "
                                              "World model evals night; $218 over.")
    assert w["event"]["seats_left"] == 9  # 31 of 40 said yes


def test_door_notes_become_a_wait_ending_this_week_and_a_follow_up_due(simulation):
    assert inbox.week("simulation")["ending"] == [] and inbox.week("simulation")["follow_ups"] == []
    events.add_note("simulation", "gi-2026-09-salon", "D902", "wait", "Ask me again after Friday.", "Nora Example",
                    "2026-09-19")
    events.add_note("simulation", "gi-2026-09-salon", "X901", "open", "Would like to see the demo.", "Nora Example")
    w = inbox.week("simulation")
    assert [(e["name"], e["until"]) for e in w["ending"]] == [("Jonah Pike", "2026-09-19")]
    assert [(f["name"], f["overdue"]) for f in w["follow_ups"]] == [("Ivy Stroud", True)]  # due 48 hours after the 10th
    events.mark("simulation", "gi-2026-09-salon", "X901", "sent", "Nora Example")
    assert inbox.week("simulation")["follow_ups"] == []  # a host sent it: no longer due


def test_the_brief_ranks_the_weeks_decisions_most_urgent_first(simulation):
    items = inbox.week("simulation")["decisions"]
    assert [d["title"] for d in items] == [
        "Get Wren Example ready to start Oct 5",  # overdue: relocation was due on the offer day
        "Approve 2 invites for World model evals night (Sep 24)",  # invites go out a week before the night
        "Reach Rin Vale for Member of Technical Staff",  # a month after her paper: Sep 19, as her card says
        "Reach Mara Quill for Member of Technical Staff",  # her window closes Sep 22
        "Reach Noa Brandt for Product Designer",
        "Check Lena Okafor before anyone writes (Member of Technical Staff)",  # undated: after every dated one
        "Event spend is $218 over for Q3 2026"]
    rin = items[2]
    assert rin["due"] == "2026-09-19" and rin["cost"] == (  # the role's, labelled as an estimate: never her pay
        "Role's first-year cost to GI: about $312,500 (invented salary estimate plus 25% overhead and hiring)")
    assert rin["way_in"] == ("Warm path: ask Dana Kest for an intro. Dana wrote 'Action-conditioned world models' with "
                             "Rin (Nov 2025). Dana can email Rin at the address on rinvale.example, or use LinkedIn if "
                             "they're connected.")  # the card's own words
    assert rin["act"] is None and rin["where"] == "Today's calls"  # the draft rides in the ask to Dana, not a link
    assert items[3]["act"]["label"] == "Open the X message"
    assert items[3]["way_in"].startswith("Warm path: not checked yet for Mara. Ask the team first. Omar")
    assert "Theo Marsh" not in json.dumps(items)  # the ledger holds him: no decision to make


def test_one_ask_per_person_so_nobody_to_reach_or_check_is_also_invited(simulation):
    w = inbox.week("simulation")
    assert "Lena Okafor" in {p["name"] for p in w["event"]["invite"]}  # the Events page suggests her
    invites = next(d for d in w["decisions"] if d["title"].startswith("Approve"))
    assert invites["why"].startswith("Sam Okoro and Ivy Stroud:")  # the brief already asks to check Lena
    asked = {p["name"]: p["asked"] for p in w["event"]["invite"]}  # the week in detail says why she has no invite
    assert asked == {"Lena Okafor": True, "Sam Okoro": False, "Ivy Stroud": False}


def test_a_first_year_hold_with_nothing_behind_it_is_no_decision(simulation):
    # A new job seen only on LinkedIn reads as a wait until its first year is out; when it ends the call is quiet,
    # so there is nothing to decide that week.
    routing.seed(today._demo_store()[0], {
        "contexts": [{"subject_id": "N900", "name": "Tess Newjob", "role": "mts", "affiliations": ["Acme Research"]}],
        "events": [{"subject_id": "N900", "event_type": "profile_job_started", "event_date": "2025-09-18",
                    "observed_at": "2026-09-01T00:00:00+00:00", "quote": "Research Scientist at Acme since 2025-09-18",
                    "source_url": "https://www.linkedin.com/in/tess-newjob-example"}]})
    tess = next(c for c in today.calls("simulation")["calls"] if c["name"] == "Tess Newjob")
    assert tess["action"] == "watch_until" and tess["until"] == "2026-09-18"  # Today's calls still shows the wait
    w = inbox.week("simulation")
    assert "Tess Newjob" not in [e["name"] for e in w["ending"]]
    assert not any("Tess Newjob" in d["title"] for d in w["decisions"])
    # A wait for a window that opens later (a fixed-term role ending) under a hold is still a decision when it ends.
    held = {"family": "holds", "window_open": "2025-09-18T00:00:00+00:00", "window_close": "2026-09-18T00:00:00+00:00"}
    ending = {"family": "career", "window_open": "2026-09-20T00:00:00+00:00", "window_close": "2026-11-20T00:00:00+00:00"}
    assert today.hold_only([held], "2026-09-15") and not today.hold_only([held, ending], "2026-09-15")
    # A work anniversary ahead is never a window to wait for (readiness.CONTEXT_ONLY): the wait is on the hold alone.
    anniversary = {**ending, "family": "career", "detector_id": "tenure_milestone"}
    assert today.hold_only([held, anniversary], "2026-09-15")


def test_a_wait_the_ledger_holds_past_its_end_is_no_decision(simulation):
    events.add_note("simulation", "gi-2026-09-salon", "D902", "wait", "Ask me again after Friday.", "Nora Example",
                    "2026-09-19")
    assert [e["name"] for e in inbox.week("simulation")["ending"]] == ["Jonah Pike"]
    contact.record(today._demo_store()[0], "D902", "never", at="2026-09-15T00:00:00+00:00", team="recruiting")
    w = inbox.week("simulation")
    assert w["ending"] == [] and not any("Jonah Pike" in d["title"] for d in w["decisions"])


def test_a_wait_ending_for_someone_once_on_a_card_is_no_decision_however_old_the_card(simulation, monkeypatch):
    real = today.calls

    def waiting(mode, *args, **kwargs):  # the engine's own wait on Jonah Pike, ending this week: not an answer of theirs
        found = real(mode, *args, **kwargs)
        return {**found, "calls": [{**c, "action": "watch_until", "until": "2026-09-19"} if c["subject_id"] == "D902"
                                   else c for c in found["calls"]]}
    monkeypatch.setattr(today, "calls", waiting)
    assert [e["name"] for e in inbox.week("simulation")["ending"]] == ["Jonah Pike"]
    contact.record(today._demo_store()[0], "D902", "pinged", at="2026-05-01T00:00:00+00:00", role_id="backend")
    w = inbox.week("simulation")  # a card holds for good, past the 90 quiet days: no second one when the wait ends
    assert w["ending"] == [] and not any("Jonah Pike" in d["title"] for d in w["decisions"])
    # Their own answer after the card reopens them: a wait they asked for is a decision when it ends.
    events.add_note("simulation", "gi-2026-09-salon", "D902", "wait", "Ask me again after Friday.", "Nora Example",
                    "2026-09-19")
    assert [e["name"] for e in inbox.week("simulation")["ending"]] == ["Jonah Pike"]


def test_the_plan_is_judged_only_once_it_has_a_budget_and_reads(simulation, monkeypatch, tmp_path):
    plan, _ = hiring.load("simulation", today.DEMO_AS_OF)
    hiring.save("simulation", plan.model_copy(update={"assumptions": plan.assumptions.model_copy(
        update={"yearly_budget": 0})}))
    titles = [d["title"] for d in inbox.week("simulation")["decisions"]]
    assert "Set the yearly people budget" in titles and not any("over budget" in t for t in titles if "plan" in t)
    monkeypatch.setattr(hiring, "load", lambda mode, as_of: (plan, "The saved plan at x could not be read."))
    titles = [d["title"] for d in inbox.week("simulation")["decisions"]]
    assert "Fix the saved hiring plan" in titles and "Set the yearly people budget" not in titles
    monkeypatch.undo()
    bad = tmp_path / "payroll.csv"
    bad.write_text("team,annual_salary\nResearch,lots\n")
    monkeypatch.setitem(hiring.PAYROLL, "simulation", bad)
    fix = next(d for d in inbox.week("simulation")["decisions"] if d["title"] == "Fix the payroll export")
    assert "line 2" in fix["why"] and "lots" not in fix["why"]  # names the line, never the value


def test_someone_with_a_follow_up_due_hears_about_the_night_in_it(simulation):
    events.add_note("simulation", "gi-2026-09-salon", "X901", "open", "Would like to see the demo.", "Nora Example")
    items = inbox.week("simulation")["decisions"]
    ivy = next(d for d in items if d["title"].startswith("Send Ivy Stroud"))
    assert ivy["why"].endswith("Its draft also invites them to World model evals night (Sep 24).")
    invites = next(d for d in items if d["title"].startswith("Approve"))
    assert invites["title"].startswith("Approve 1 invite for") and invites["why"].startswith("Sam Okoro:")


def test_the_card_links_only_a_safe_https_url_whoever_built_the_item():
    item = {"area": "Hiring", "title": "T", "why": "W", "where": "Today's calls", "due": None, "overdue": False, "way_in": None,
            "cost": None, "act": {"label": "Open", "url": "https://x.example/a\n<!channel>"}}
    text = json.dumps(inbox.card({"week_of": "2026-09-14", "as_of": "2026-09-15", "decisions": [item]}))
    assert "x.example" not in text and "On Today's calls" in text


def test_posting_the_brief_records_each_reach_it_names(simulation, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import route
    posted = []
    monkeypatch.setattr(routing, "_post", lambda card, url, *rest: posted.append(card))
    monkeypatch.setattr(route, "webhook_url", lambda: "https://hooks.slack.test/x")
    monkeypatch.setattr(route, "iso", lambda: "2026-09-15T12:00:00+00:00")
    route.send_inbox("simulation", send=True)
    store = today._demo_store()[0]
    assert len(posted) == 6  # the post, then in its thread a new start, the invites and the three cards
    assert {sid for sid in ("A900", "X900", "X902", "D901") if any(e["kind"] == "pinged" for e in contact.history(store, sid))} \
        == {"A900", "X900", "X902"}  # Rin, Mara and Noa, not the held Theo
    for sid in ("A900", "X900", "X902"):  # a card about them went out: route.py --send holds them until something new
        assert routing.route(store, sid, "2026-09-16T00:00:00+00:00", [], []).gate.state == "hold"


def test_overdue_follow_ups_lead_and_a_plan_over_budget_is_a_decision(simulation):
    events.add_note("simulation", "gi-2026-09-salon", "X901", "open", "Would like to see the demo.", "Nora Example")
    plan, _ = hiring.load("simulation", today.DEMO_AS_OF)
    hiring.save("simulation", plan.model_copy(update={"assumptions": plan.assumptions.model_copy(
        update={"yearly_budget": 3000000})}))
    try:
        items = inbox.week("simulation")["decisions"]
    finally:
        hiring.reset()
    assert items[1]["title"] == "Send Ivy Stroud their follow-up from Nora Example" and items[1]["overdue"]  # after Wren's
    assert "The 2027 hiring plan is $128,792 over budget" in [d["title"] for d in items]


def test_the_card_fits_slack_names_no_one_to_contact_and_offers_the_draft(simulation):
    card = inbox.card(inbox.week("simulation"))
    blocks = card["blocks"]
    assert blocks[0]["text"]["text"] == "Monday brief: week of 2026-09-14" and len(blocks) <= 50
    assert blocks[1]["text"]["text"] == "*7 things worth your attention, most urgent first.*"
    assert all(len(b["text"]["text"]) <= routing.SLACK_TEXT_MAX for b in blocks if b["type"] == "section")
    text = json.dumps(card, ensure_ascii=False)
    assert "*3. Reach Rin Vale for Member of Technical Staff* · by Sep 19" in text and "|Open the X message>" in text
    assert "This card contacts no one" in blocks[-1]["elements"][0]["text"]
    assert not any(b["type"] == "actions" for b in blocks)  # no buttons: nothing to press that sends


def test_the_card_escapes_clips_and_keeps_to_the_few_that_matter():
    item = {"area": "Hiring", "title": "Reach <!channel> Ada", "why": "a <b>paper</b> " + "x" * 400, "where": "Today's calls",
            "due": None, "overdue": False, "way_in": "Email to <ada@x.example>", "cost": None,
            "act": {"label": "Open the draft (email)", "url": "https://mail.example/compose"}}
    week = {"week_of": "2026-09-14", "as_of": "2026-09-15", "decisions": [item] * 11}
    blocks = inbox.card(week)["blocks"]
    text = json.dumps(blocks, ensure_ascii=False)
    assert "Reach &lt;!channel&gt; Ada" in text and "<!channel>" not in text and "&lt;ada@x.example&gt;" in text
    assert sum(b["type"] == "section" for b in blocks) == 1 + inbox.BRIEF_MAX
    assert "3 more on the Monday brief page." in blocks[-1]["elements"][0]["text"]
    assert all(b["text"]["text"].count("x" * 300) == 0 for b in blocks if b["type"] == "section")  # long lines clipped
    assert inbox.card({**week, "decisions": []}) is None  # nothing to decide: silence


def test_the_digest_posts_the_one_card_it_is_given():
    seen = []
    stub = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(json.loads(r.content)) or httpx.Response(200)))
    card = {"text": "GI this week", "blocks": []}
    routing.send_digest(card, "https://hooks.slack.test/x", client=stub)
    assert seen == [card]
    with pytest.raises(ValueError, match="https"):
        routing.send_digest(card, "http://hooks.slack.test/x")


def test_route_inbox_prints_the_card_and_posts_nothing():
    run = subprocess.run([sys.executable, "scripts/route.py", "--demo", "--inbox"], cwd=ROOT, capture_output=True,
                         text=True, env={"PATH": "", "SLACK_ROUTING_WEBHOOK": "not-a-webhook",  # conftest's scratch:
                                         **{k: os.environ[k] for k in PINNED}})  # never a real store
    assert run.returncode == 0, run.stderr
    assert '"Monday brief: week of 2026-09-14"' in run.stdout and "See it rendered (nothing is posted)" in run.stdout


def test_the_api_serves_the_week_with_a_preview_only_for_invented_people(client, simulation):  # noqa: F811
    week = client.get("/api/week?mode=simulation").json()
    assert week["week_of"] == "2026-09-14" and week["preview"].startswith("https://app.slack.com/block-kit-builder#")
    assert client.get("/api/week?mode=nope").status_code == 422


def test_the_web_brief_names_only_who_the_monday_post_would(client, simulation, monkeypatch):  # noqa: F811
    def reaches(week):
        return [i for i in week["decisions"] if (i["thread"] or {}).get("kind") == "reach"]
    assert reaches(inbox.week("simulation"))  # the invented week has reaches to leave out
    monkeypatch.setattr(routing, "sendable", lambda store, routes, now: [])  # say none would post now
    week = client.get("/api/week?mode=simulation").json()
    assert not reaches(week) and week["week_of"] == "2026-09-14"
    assert not any(r["reach"] for r in week["roles"])  # nor in the week in detail


def test_live_without_timelines_says_what_to_run(client, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(today, "TIMELINES", tmp_path / "missing.sqlite")
    week = client.get("/api/week?mode=live").json()
    assert week["missing"] and week["preview"] is None


def test_live_with_timelines_gets_no_preview_link(client, monkeypatch, tmp_path):  # noqa: F811
    store = today._demo_store()[0]
    real = tmp_path / "timelines.sqlite"
    copy = Store(f"sqlite:///{real}")
    for kind in ("person_context", "timeline_event"):
        for row in store.all(kind):
            copy.put(row["id"], kind, row)
    monkeypatch.setattr(today, "TIMELINES", real)
    monkeypatch.setattr(today, "iso", lambda: today.DEMO_AS_OF)
    monkeypatch.setitem(events.RECORDS, "live", tmp_path / "no-events")  # never the real research/private records
    people = tmp_path / "people.json"  # the watchlist: everyone here, none a replay case
    people.write_text(json.dumps([{"person_id": r["subject_id"], "role": "mts-research", "since": "2026-01-01"}
                                  for r in store.all("person_context")]))
    monkeypatch.setattr(events, "WATCHLIST", people)
    week = client.get("/api/week?mode=live").json()
    assert week["roles"] and week["preview"] is None  # the link would carry real names


def test_a_note_is_marked_on_the_brief(simulation):
    items = {d["title"]: d for d in inbox.week("simulation")["decisions"]}
    mara = items["Reach Mara Quill for Member of Technical Staff"]  # her call leads with her work
    assert mara["why"].endswith(" (a note that leads with their work).")
    assert not items["Reach Rin Vale for Member of Technical Staff"]["why"].endswith("their work).")  # a pitch


def test_the_way_in_follows_the_card_and_a_teammate_who_said_they_know_them(simulation):
    contact.record(today._demo_store()[0], "X902", "knows", at="2026-09-14T00:00:00+00:00", by="Dana Kest",
                   note="worked together")
    noa = next(d for d in inbox.week("simulation")["decisions"] if d["title"].startswith("Reach Noa"))
    assert noa["way_in"].startswith("Warm path: Dana Kest knows Noa (worked together, said Sep 14). Ask Dana for an "
                                    "intro first.")
    assert "Kest knows Noa" in json.dumps(inbox.card(inbox.week("simulation")))


def test_a_ledger_that_cant_be_read_is_the_first_thing_the_brief_says(simulation, monkeypatch):
    real = contact.history
    broken = {"person_id": "x", "kind": "unreadable", "at": "2026-01-01T00:00:00+00:00", "team": "recruiting",
              "ledger": "the web app", "note": "DatabaseError"}
    monkeypatch.setattr(contact, "history", lambda store, pid: [*real(store, pid), {**broken, "person_id": pid}])
    monkeypatch.setattr(contact, "unreadable", lambda store: contact._broken("the web app", "DatabaseError"))
    items = inbox.week("simulation")["decisions"]
    assert items[0]["title"] == "Fix the contact ledger" and items[0]["overdue"]
    assert "can't be read" in items[0]["why"] and not any(d["title"].startswith("Reach") for d in items)
    assert sum(d["title"] == "Fix the contact ledger" for d in items) == 1


def test_the_brief_says_the_ledger_is_broken_even_when_nobody_was_held(simulation, monkeypatch):
    monkeypatch.setattr(contact, "unreadable", lambda store: contact._broken("the web app", "DatabaseError"))
    items = inbox.week("simulation", calls={**today.calls("simulation"), "calls": []})["decisions"]  # nobody to hold
    assert items[0]["title"] == "Fix the contact ledger"
    monkeypatch.undo()
    assert not any(d["title"] == "Fix the contact ledger" for d in inbox.week("simulation")["decisions"])


def test_a_wait_ending_this_week_is_listed_whether_or_not_they_may_still_be_a_student(simulation):
    # Ways it could fail: a doubt about whether they're still a student (contact unconfirmed) drops their wait from
    # the week's list, though a wait pitches nothing and the list is the only place it shows.
    calls = today.calls("simulation")
    priya = next(c for c in calls["calls"] if c["subject_id"] == "D903")
    calls["calls"] = [{**c, "action": "watch_until", "until": "2026-09-18", "hold_only": False} if c is priya else c
                      for c in calls["calls"]]
    assert [e["name"] for e in inbox.week("simulation", calls)["ending"]] == ["Priya Vance"]
    contact.record(today._demo_store()[0], "D903", "unconfirmed", at="2026-09-10T00:00:00+00:00")
    assert [e["name"] for e in inbox.week("simulation", calls)["ending"]] == ["Priya Vance"]
