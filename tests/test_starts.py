"""New starts: per accepted offer, the five things to have ready by the first day, each with an owner and a due
day. Visa and relocation are only ever what the candidate said at the offer stage, typed in by ops."""

import json

import pytest

from app import contact, events, hiring, inbox, starts, today
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


def items(hire):
    return {i["key"]: i for i in hire["items"]}


def test_each_item_has_an_owner_and_a_due_day_before_the_start(simulation):
    page = starts.page("simulation")
    wren = page["hires"][0]
    assert (wren["name"], wren["start"], wren["open"]) == ("Wren Example", "2026-10-05", 3)
    it = items(wren)
    assert (it["laptop"]["due"], it["desk"]["due"]) == ("2026-09-28", "2026-09-21")  # a week and two before
    assert it["relocation"]["due"] == "2026-09-08" and it["relocation"]["overdue"]  # never before the offer day
    assert it["visa"]["state"] == "not_needed" and it["visa"]["note"].startswith("Said at the offer stage")
    assert [p["name"] for p in page["from_plan"]] == ["Rin Vale", "Noa Brandt"]  # named people in the plan


def test_an_accepted_offer_starts_visa_and_relocation_as_questions_for_the_offer_stage(simulation):
    plan = starts.page("simulation")["from_plan"][0]
    hire = starts.add("simulation", plan["name"], plan["role_id"], plan["office"], plan["start"], plan["subject_id"])
    it = items(starts.view(hire, starts._clock("simulation")))
    assert it["visa"]["state"] == it["relocation"]["state"] == "ask"  # never inferred: asked at the offer
    assert it["visa"]["note"] == "" and it["desk"]["state"] == "open"
    assert it["desk"]["owner"] == "Rae Example" and it["visa"]["owner"] == "Kai Example"  # the team's defaults
    assert "Rin Vale" not in [p["name"] for p in starts.page("simulation")["from_plan"]]
    with pytest.raises(ValueError, match="already on New starts"):
        starts.add("simulation", "Rin Vale", "mts-research", "New York City", "2027-01-04", "A900")


def test_what_the_candidate_said_is_saved_as_typed_and_only_asked_items_can_be_skipped(simulation):
    starts.update("simulation", "wren", "relocation", state="done", note="Moved; receipts in.")
    assert items(starts.page("simulation")["hires"][0])["relocation"]["note"] == "Moved; receipts in."
    with pytest.raises(ValueError, match="always needed"):
        starts.update("simulation", "wren", "laptop", state="not_needed")
    with pytest.raises(ValueError, match="always needed"):
        starts.update("simulation", "wren", "desk", state="ask")
    with pytest.raises(ValueError, match="ops owners"):
        starts.update("simulation", "wren", "desk", owner="Someone Else")
    with pytest.raises(ValueError, match="Not an office"):
        starts.add("simulation", "Ada Example", "backend", "London", "2026-11-02")
    with pytest.raises(ValueError, match="before the offer"):
        starts.add("simulation", "Ada Example", "backend", "New York City", "2026-09-01")
    with pytest.raises(LookupError):
        starts.update("simulation", "nobody", "desk", state="done")
    with pytest.raises(ValueError, match="Only visa and relocation take a note"):
        starts.update("simulation", "wren", "laptop", note="anything")
    with pytest.raises(ValueError, match="offer date is in the future"):
        starts.add("simulation", "Ada Example", "backend", "New York City", "2026-11-02", offer_on="2026-09-20")
    ada = starts.add("simulation", "Ada Example", "backend", "New York City", "2026-11-02", offer_on="2026-09-01")
    assert starts.due(ada, "visa").isoformat() == "2026-09-03"  # sixty days before the start, from the offer's day


def test_the_brief_lists_what_falls_due_and_never_names_the_visa(simulation):
    items_ = inbox.week("simulation")["decisions"]
    wren = items_[0]  # overdue: first
    assert wren["title"] == "Get Wren Example ready to start Oct 5" and wren["overdue"]
    assert wren["why"] == ("Open: relocation (Kai Example, overdue); office and desk (Rae Example, due Sep 21).")
    before = inbox.card(inbox.week("simulation"))
    for state in ("open", "ask"):  # whatever the visa's state, the brief reads the same: nothing to tell it by
        starts.update("simulation", "wren", "visa", state=state, note="Said at the offer stage: needs sponsorship.")
        assert inbox.card(inbox.week("simulation")) == before
    for key in ("relocation", "desk"):
        starts.update("simulation", "wren", key, state="done")
    assert not any(d["title"].startswith("Get Wren") for d in inbox.week("simulation")["decisions"])  # visa only: no item


def test_live_keeps_real_hires_in_the_private_file_and_never_overwrites_one_it_cannot_read(monkeypatch, tmp_path):
    path = tmp_path / "starts.json"
    monkeypatch.setitem(starts.FILES, "live", path)
    assert starts.page("live")["hires"] == []
    hire = starts.add("live", "Ada Example", "backend", "New York City", "2027-03-01")
    assert json.loads(path.read_text())["hires"][0]["id"] == hire.id
    assert items(starts.page("live")["hires"][0])["visa"]["state"] == "ask"
    path.write_text("{broken")
    assert starts.page("live")["problem"].startswith("The new starts at")
    with pytest.raises(ValueError, match="could not be read"):
        starts.add("live", "Bo Example", "backend", "New York City", "2027-03-01")
    assert path.read_text() == "{broken"  # left for a person to fix
    assert [f.name for f in tmp_path.iterdir()] == ["starts.json"]


def test_the_api_adds_changes_and_removes_a_start(client, simulation):  # noqa: F811
    added = client.post("/api/starts", json={"mode": "simulation", "name": "Ada Example", "role_id": "backend",
                                             "office": "New York City", "start": "2026-11-02"})
    assert added.status_code == 200
    hid = added.json()["id"]
    assert client.patch(f"/api/starts/{hid}", json={"mode": "simulation", "key": "visa", "state": "not_needed",
                                                    "note": "Said at the offer stage: no visa needed."}).status_code == 200
    ada = next(h for h in client.get("/api/starts?mode=simulation").json()["hires"] if h["id"] == hid)
    assert items(ada)["visa"]["state"] == "not_needed"
    assert client.patch(f"/api/starts/{hid}", json={"mode": "simulation", "key": "laptop", "state": "nope"}).status_code == 422
    assert client.post("/api/starts", json={"mode": "simulation", "name": "Bo", "role_id": "backend",
                                            "office": "New York City", "start": ""}).status_code == 422
    assert client.request("DELETE", f"/api/starts/{hid}", json={"mode": "simulation"}).status_code == 200
    assert client.request("DELETE", f"/api/starts/{hid}", json={"mode": "simulation"}).status_code == 404


def test_an_accepted_offer_takes_them_off_every_outreach_path_until_it_falls_through(simulation):
    rin = next(p for p in starts.page("simulation")["from_plan"] if p["name"] == "Rin Vale")
    assert any(d["title"].startswith("Reach Rin Vale") for d in inbox.week("simulation")["decisions"])
    hire = starts.add("simulation", rin["name"], rin["role_id"], rin["office"], rin["start"], rin["subject_id"])
    store, as_of = today.source("simulation")[:2]
    held = contact.check(contact.history(store, rin["subject_id"]), as_of)
    assert held.state == "hold" and held.reason.startswith("Accepted GI's offer")
    assert not any("Rin Vale" in d["title"] for d in inbox.week("simulation")["decisions"])  # nor a check or an invite
    assert not any(p["subject_id"] == rin["subject_id"] for p in hiring.page("simulation")["people"])
    starts.remove("simulation", hire.id)  # the offer fell through before the start: a candidate again
    assert contact.check(contact.history(store, rin["subject_id"]), as_of).state != "hold"


def test_a_hire_typed_in_by_hand_holds_no_one_and_a_started_hire_stays_held(simulation, monkeypatch):
    before = len(today.source("simulation")[0].all("contact"))
    hire = starts.add("simulation", "Pat Example", "backend", "New York City", "2026-10-01")  # no subject id: no one to hold
    assert len(today.source("simulation")[0].all("contact")) == before
    starts.remove("simulation", hire.id)
    rin = next(p for p in starts.page("simulation")["from_plan"] if p["name"] == "Rin Vale")
    hire = starts.add("simulation", rin["name"], rin["role_id"], rin["office"], "2026-09-15", rin["subject_id"],
                      offer_on="2026-09-01")
    starts.remove("simulation", hire.id)  # already started: tidied off the page, still not a candidate
    store, as_of = today.source("simulation")[:2]
    assert contact.check(contact.history(store, rin["subject_id"]), as_of).state == "hold"


def test_a_hire_saved_before_the_ledger_knew_them_is_held_once_the_calls_run(simulation):
    rin = next(p for p in starts.page("simulation")["from_plan"] if p["name"] == "Rin Vale")
    saved = starts.load("simulation")[0]
    old = starts.Hire(id="old1", name=rin["name"], role_id=rin["role_id"], office=rin["office"], start=rin["start"],
                      offer_on="2026-09-01", subject_id=rin["subject_id"],
                      items={k: starts.Item(state="ask" if k in starts.ASKED else "open") for k in starts.ITEMS})
    starts.save("simulation", saved.model_copy(update={"hires": [*saved.hires, old]}))  # no ledger entry
    store = today.source("simulation")[0]
    assert not any(e["kind"] == "hired" for e in contact.history(store, rin["subject_id"]))
    assert not any(c["name"] == "Rin Vale" and c["action"] == "reach_now" and not c["gate"]
                   for c in today.calls("simulation")["calls"])
    assert [e["kind"] for e in contact.history(store, rin["subject_id"])].count("hired") == 1
    today.calls("simulation")
    assert [e["kind"] for e in contact.history(store, rin["subject_id"])].count("hired") == 1  # once


def test_someone_back_on_new_starts_after_an_offer_fell_through_is_held_again(simulation):
    rin = next(p for p in starts.page("simulation")["from_plan"] if p["name"] == "Rin Vale")
    store, as_of = today.source("simulation")[:2]
    contact.record(store, rin["subject_id"], "hired", at="2026-09-01T00:00:00+00:00", by="New starts")
    contact.record(store, rin["subject_id"], "hired", at="2026-09-05T00:00:00+00:00", until="2026-09-05",
                   by="New starts")  # taken off before their start: a candidate again
    saved = starts.load("simulation")[0]
    back = starts.Hire(id="back1", name=rin["name"], role_id=rin["role_id"], office=rin["office"], start=rin["start"],
                       offer_on="2026-09-12", subject_id=rin["subject_id"],
                       items={k: starts.Item(state="ask" if k in starts.ASKED else "open") for k in starts.ITEMS})
    starts.save("simulation", saved.model_copy(update={"hires": [*saved.hires, back]}))  # no new ledger entry
    assert contact.check(contact.history(store, rin["subject_id"]), as_of).state != "hold"
    starts.hold_all("simulation")
    assert contact.check(contact.history(store, rin["subject_id"]), as_of).state == "hold"
    starts.hold_all("simulation")
    assert [e["kind"] for e in contact.history(store, rin["subject_id"])].count("hired") == 3  # once more, not twice


def test_a_hire_taken_off_while_the_catch_up_runs_is_never_held_again(simulation, monkeypatch):
    import threading
    import time

    rin = next(p for p in starts.page("simulation")["from_plan"] if p["name"] == "Rin Vale")
    hire = starts.add("simulation", rin["name"], rin["role_id"], rin["office"], rin["start"], rin["subject_id"])
    real, reading = starts.load, threading.Event()

    def slow(mode):  # hold_all has read the list: a remove arrives now
        out = real(mode)
        reading.set()
        time.sleep(0.3)
        return out
    monkeypatch.setattr(starts, "load", slow)
    other = threading.Thread(target=lambda: reading.wait(10) and starts.remove("simulation", hire.id))
    other.start()
    starts.hold_all("simulation")
    other.join()
    store, as_of = today.source("simulation")[:2]
    assert all(h.id != hire.id for h in starts.load("simulation")[0].hires)  # off New starts
    assert contact.check(contact.history(store, rin["subject_id"]), as_of).state != "hold"  # a candidate again


def test_a_new_hire_in_a_host_brief_shows_no_call_to_act_on(simulation):
    rin = next(p for p in starts.page("simulation")["from_plan"] if p["name"] == "Rin Vale")
    starts.add("simulation", rin["name"], rin["role_id"], rin["office"], rin["start"], rin["subject_id"])
    briefs = events.page("simulation")["next"]["briefs"]
    mine = [p for b in briefs for p in b["people"] + b["if_invited"] if p["subject_id"] == rin["subject_id"]]
    assert mine, "Rin is coming to the night in the simulation"
    assert all(p["action"] is None and p["call"] == "No pitch" and "Accepted GI's offer" in p["rule"] for p in mine)
    assert all("open ask" not in p["why_them"] for p in mine)
