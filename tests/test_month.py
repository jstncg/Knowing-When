"""The founders' one-pager: pipeline per role, spend against budget, and what's slipping, on invented people."""

import pytest

from app import contact, events, hiring, month, starts, today
from tests.test_api import client  # noqa: F401  (a fixture)


@pytest.fixture
def simulation():
    today._demo_store.cache_clear()
    hiring.reset()
    starts.reset()
    yield
    today._demo_store.cache_clear()
    hiring.reset()
    starts.reset()


def rows(m):
    return {r["role_id"]: r for r in m["pipeline"]}


def test_the_pipeline_per_role_counts_whos_in_sight_offers_plans_and_starts(simulation):
    m = month.month("simulation")
    assert m["month"] == "September 2026" and m["invented"] and m["year"] == 2027
    mts, backend = rows(m)["mts-research"], rows(m)["backend"]
    assert (mts["to_reach"], mts["planned"], mts["filled"]) == (3, 1, 0)  # Rin and Mara to reach, Lena to check
    assert mts["active"] == 1  # Tove, an event guest in conversation since August: counted for the night's role
    assert [r["role_id"] for r in m["pipeline"]] == ["mts-research", "backend", "product-designer"]  # config's order
    assert (backend["offers"], backend["starting"], backend["filled"]) == (1, 1, 0)  # Wren starts Oct 5, not in 2027
    assert "controller" not in rows(m)  # nothing to report: no row
    assert m["headline"] == "3 things slipping · people budget $271,208 left for 2027 · events $218 over for Q3 2026"


def test_reached_counts_hiring_s_own_notes_once_per_person_never_a_card_or_another_team(simulation):
    store = today._demo_store()[0]
    contact.record(store, "A900", "pinged", at="2026-09-02T00:00:00+00:00", role_id="mts-research")
    contact.record(store, "A900", "sent", at="2026-09-03T00:00:00+00:00", by="Dana Kest")
    contact.record(store, "A900", "follow_up", at="2026-09-10T00:00:00+00:00", by="Dana Kest")
    contact.record(store, "A900", "replied", at="2026-09-12T00:00:00+00:00")
    contact.record(store, "X900", "sent", at="2026-08-28T00:00:00+00:00", by="Dana Kest")  # last month
    contact.record(store, "D904", "pinged", at="2026-09-14T00:00:00+00:00", role_id="mts-research")  # a card only
    contact.record(store, "X902", "sent", at="2026-09-04T00:00:00+00:00", team="sales")  # not hiring's
    m = month.month("simulation")
    mts = rows(m)["mts-research"]
    assert (mts["reached"], mts["replied"], mts["active"]) == (1, 1, 3)  # Rin talking; Mara asked in August; Tove
    assert mts["to_reach"] == 1  # Lena: on a card, not yet written to
    assert rows(m)["product-designer"]["reached"] == 0


def test_a_posted_brief_alone_leaves_everyone_to_reach_and_nothing_thin(simulation):
    store = today._demo_store()[0]
    before = month.month("simulation")
    for sid, role in (("A900", "mts-research"), ("X900", "mts-research"), ("X902", "product-designer")):
        contact.record(store, sid, "pinged", at="2026-09-15T00:00:00+00:00", role_id=role)  # as route.py --inbox does
    after = month.month("simulation")
    assert [(r["role_id"], r["to_reach"], r["reached"]) for r in after["pipeline"]] == \
        [(r["role_id"], r["to_reach"], r["reached"]) for r in before["pipeline"]]
    assert after["slipping"] == before["slipping"]


def test_people_in_conversation_keep_a_pipeline_from_reading_thin(simulation):
    store = today._demo_store()[0]
    for sid in ("A900", "X900", "D904"):
        contact.record(store, sid, "sent", at="2026-09-03T00:00:00+00:00", by="Dana Kest", role_id="mts-research")
        contact.record(store, sid, "replied", at="2026-09-05T00:00:00+00:00")
    m = month.month("simulation")
    assert (rows(m)["mts-research"]["to_reach"], rows(m)["mts-research"]["active"]) == (0, 4)  # and Tove
    assert not any("Member of Technical Staff" in s["title"] for s in m["slipping"])
    contact.record(store, "A900", "never", at="2026-09-10T00:00:00+00:00", team="sales")  # a no from any team
    contact.record(store, "X900", "not_now", at="2026-09-10T00:00:00+00:00", until="2027-03-01")
    assert rows(month.month("simulation"))["mts-research"]["active"] == 2


def test_an_event_invite_counts_for_the_nights_role_even_without_a_call(simulation):
    events.mark("simulation", "gi-2026-09-games", "C801", "invited", "Dana Kest")  # Sam: no call, invited all the same
    mts = rows(month.month("simulation"))["mts-research"]
    assert (mts["reached"], mts["active"]) == (1, 2)


def test_another_teams_note_after_a_card_takes_them_off_to_reach(simulation):
    store = today._demo_store()[0]
    contact.record(store, "D904", "pinged", at="2026-09-10T00:00:00+00:00", role_id="mts-research")
    contact.record(store, "D904", "sent", at="2026-09-12T00:00:00+00:00", team="sales", by="Omar Example")
    m = month.month("simulation")
    assert (rows(m)["mts-research"]["to_reach"], rows(m)["mts-research"]["reached"]) == (2, 0)
    assert m["footnote"] in month.text(m) and "nobody has written to since" in m["footnote"]


def test_whats_slipping_is_the_briefs_overdue_items_budgets_over_and_thin_pipelines(simulation):
    m = month.month("simulation")
    titles = [s["title"] for s in m["slipping"]]
    assert titles == ["Get Wren Example ready to start Oct 5", "Event spend is $218 over for Q3 2026",
                      "Thin pipeline for Senior / Lead Backend Engineer"]
    assert m["slipping"][1]["why"] is None  # the spend part already has the detail
    text = month.text(m)
    assert text.startswith("GI hiring and ops, September 2026 (as of 2026-09-15): invented people and numbers")
    assert text.splitlines()[1] == m["headline"]
    assert ("- Senior / Lead Backend Engineer: 1 offer accepted this month, 0 of 2 planned 2027 hires filled, "
            "1 starting within 60 days") in text
    assert m["slipping"][2]["why"] == "2 planned 2027 hires still to make and nobody in sight."
    assert "- People 2027: $3,128,792 of $3,400,000 ($271,208 left), for payroll and 4 planned hires" in text


def test_the_one_pager_never_mentions_a_visa(simulation):
    starts.update("simulation", "wren", "visa", state="open", note="Said at the offer stage: needs sponsorship.")
    text = month.text(month.month("simulation"))
    assert "isa" not in text and "sponsor" not in text


def test_without_a_yearly_budget_it_says_so_rather_than_judge(simulation):
    plan, _ = hiring.load("simulation", today.DEMO_AS_OF)
    hiring.save("simulation", plan.model_copy(update={"assumptions": plan.assumptions.model_copy(
        update={"yearly_budget": 0})}))
    m = month.month("simulation")
    assert m["people_budget"] is None and "- People: no yearly budget set on the Hiring budget page" in month.text(m)


def test_an_unreadable_live_plan_says_so_rather_than_no_budget(monkeypatch, tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("{broken")
    monkeypatch.setitem(hiring.PLANS, "simulation-broken", path)
    monkeypatch.setattr(hiring, "load", lambda mode, as_of, _load=hiring.load: _load("simulation-broken", as_of))
    today._demo_store.cache_clear()
    try:
        m = month.month("simulation")
    finally:
        today._demo_store.cache_clear()
        hiring.reset()
        starts.reset()
    assert m["plan_unreadable"] and "- People: the saved hiring plan could not be read" in month.text(m)


def test_the_api_serves_the_page_and_its_text(client, simulation):  # noqa: F811
    m = client.get("/api/month?mode=simulation").json()
    assert m["pipeline"] and m["text"].startswith("GI hiring and ops, September 2026")
    assert client.get("/api/month?mode=nope").status_code == 422


def test_another_teams_note_never_ends_a_conversation_hiring_has_open(simulation):
    store = today._demo_store()[0]
    contact.record(store, "X902", "sent", at="2026-09-02T00:00:00+00:00", by="Justin", role_id="product-designer")
    contact.record(store, "X902", "replied", at="2026-09-05T00:00:00+00:00")
    contact.record(store, "X902", "sent", at="2026-09-12T00:00:00+00:00", team="marketing", by="Noor")  # a newsletter
    m = month.month("simulation")
    assert rows(m)["product-designer"]["active"] == 1
    assert not any("Product Designer" in s["title"] for s in m["slipping"])
    contact.record(store, "X902", "not_now", at="2026-09-14T00:00:00+00:00", team="sales", until="2027-01-04")
    assert rows(month.month("simulation"))["product-designer"]["active"] == 0  # a no from any team closes them


def test_someone_who_accepted_an_offer_counts_as_an_offer_never_as_active_or_to_reach(simulation):
    store = today._demo_store()[0]
    contact.record(store, "A900", "sent", at="2026-09-03T00:00:00+00:00", by="Dana Kest", role_id="mts-research")
    contact.record(store, "A900", "replied", at="2026-09-05T00:00:00+00:00")
    before = rows(month.month("simulation"))["mts-research"]
    rin = next(p for p in starts.page("simulation")["from_plan"] if p["name"] == "Rin Vale")
    starts.add("simulation", rin["name"], rin["role_id"], rin["office"], rin["start"], rin["subject_id"],
               offer_on="2026-09-10")
    after = rows(month.month("simulation"))["mts-research"]
    assert after["active"] == before["active"] - 1 and after["offers"] == before["offers"] + 1  # counted once
