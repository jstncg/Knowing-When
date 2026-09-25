"""The hiring budget on the invented plan (tests/fixtures/hiring): role costs, never a person's pay."""

import json

import pytest

from app import contact, events, hiring, pay, today
from tests.test_api import client  # noqa: F401  (a fixture)


@pytest.fixture(autouse=True)
def no_posted_ranges(monkeypatch, tmp_path):
    """GI's posts state no range until scripts/pay_bands.py reads one, and live has no payroll export: these
    tests don't depend on either file."""
    monkeypatch.setattr(pay, "bands", lambda: {})
    monkeypatch.setitem(hiring.PAYROLL, "live", tmp_path / "no-payroll.csv")


@pytest.fixture
def simulation():
    today._demo_store.cache_clear()
    hiring.reset()
    yield
    today._demo_store.cache_clear()
    hiring.reset()


def plan(**changes):
    base = hiring.Plan.model_validate({"assumptions": {"year": 2027, "yearly_budget": 500000, "overhead_pct": 25,
                                                       "agency_fee_pct": 20, "conversations_per_hire": 3,
                                                       "salary_estimates": {"backend": 200000}}, "lines": []})
    return base.model_copy(update=changes)


def test_the_demo_plan_is_costed_by_role_against_the_year(simulation):
    page = hiring.page("simulation")
    roles = {r["id"]: r for r in page["roles"]}
    assert roles["mts-research"]["salary"] == 250000
    assert roles["mts-research"]["basis"] == "an estimate invented for the demo: the demo shows no posted range"
    assert roles["mts-research"]["first_year"] == 312500  # salary plus 25% overhead, found by GI's own outreach
    pr = page["projection"]
    assert [(line["name"], line["months"], line["cost"]) for line in pr["lines"]] == [
        ("Rin Vale", 12, 312500), ("Noa Brandt", 11, 194792), (None, 10, 521500)]  # two backend seats via an agency
    assert (pr["hires_cost"], pr["payroll"]) == (1028792, 2100000)  # nine invented salaries, 1,680,000, plus 25%
    assert (pr["total"], pr["budget"], pr["left"], pr["over"], pr["hires"]) == (3128792, 3400000, 271208, False, 4)
    assert pr["run_rate"] == 1050000
    assert pr["offices"] == [{"office": "New York City", "now": 8, "planned": 4, "desks": 12},
                             {"office": "London", "now": 1, "planned": 0, "desks": 1}]
    assert page["invented"] is True


def test_surfaced_people_are_the_engines_reaches_and_checks_with_their_roles_cost(simulation):
    page = hiring.page("simulation")
    people = {p["name"]: p for p in page["people"]}
    assert set(people) == {"Rin Vale", "Noa Brandt", "Mara Quill", "Lena Okafor"}  # Theo is held by the ledger
    roles = {r["id"]: r for r in page["roles"]}
    assert all(p["first_year"] == roles[p["role_id"]]["first_year"] for p in page["people"])  # the role's, never theirs
    assert people["Rin Vale"]["planned"] and not people["Mara Quill"]["planned"]


def test_a_posted_range_beats_an_estimate_and_each_source_has_its_cost(monkeypatch):
    monkeypatch.setattr(pay, "bands", lambda: {"backend": {"min": 180000, "max": 240000, "currency": "USD",
                                                           "interval": "year", "source_url": "x", "read_on": "d"}})
    a = plan().assumptions
    assert hiring.salary("backend", a, mode="live") == (210000, "midpoint of GI's posted range, USD 180,000 to 240,000")
    assert hiring.hiring_cost("agency", 210000, a, 6000) == 42000
    assert hiring.hiring_cost("event", 210000, a, 6000) == 18000  # three qualified conversations at $6,000
    assert hiring.hiring_cost("event", 210000, a, None) is None  # no past event to cost it from
    assert hiring.salary("product-designer", a, mode="live") == (None, "GI's post states no range: add your estimate")


def test_only_a_yearly_usd_range_counts_as_posted(monkeypatch):
    monkeypatch.setattr(pay, "bands", lambda: {
        "backend": {"min": 90, "max": 120, "currency": "USD", "interval": "hour", "source_url": "x", "read_on": "d"},
        "product-designer": {"min": 90000, "max": 120000, "currency": "EUR", "interval": "year", "source_url": "x",
                             "read_on": "d"}})
    a = plan().assumptions  # an estimate for backend only
    assert hiring.salary("backend", a, mode="live") == (200000, "your estimate: GI's post gives a range per hour in USD, not dollars a year")
    assert hiring.salary("product-designer", a, mode="live") == (
        None, "GI's post gives a range per year in EUR, not dollars a year: add your estimate")
    roles, _ = hiring.costs("live", a)
    assert not roles["backend"]["posted"] and not roles["product-designer"]["posted"]  # so the page asks for an estimate


def test_a_plan_over_budget_says_so_and_a_line_without_a_salary_is_not_counted():
    roles = {"backend": {"salary": 200000, "title": "Backend"}, "product-designer": {"salary": None, "title": "Designer"}}
    p = plan(lines=[hiring.Line(role_id="backend", count=2, start_month=7, source="agency"),
                    hiring.Line(role_id="product-designer", start_month=1)])
    pr = hiring.project(p, roles, None)
    assert pr["lines"][0]["cost"] == 2 * (200000 * 6 / 12 * 1.25 + 40000)  # half a year, plus the agency's fee
    assert pr["lines"][1]["cost"] is None and pr["missing"] == [1] and pr["lines"][1]["needs"] == "a salary"
    assert (pr["total"], pr["left"], pr["over"], pr["hires"], pr["uncosted_hires"]) == (330000, 170000, False, 3, 1)
    over = hiring.project(plan(lines=p.lines * 2), roles, None)
    assert over["over"] and over["left"] == -160000
    event = hiring.project(plan(lines=[hiring.Line(role_id="backend", start_month=1, source="event")]), roles, None)
    assert event["lines"][0]["needs"] == "an event cost"  # a salary, but no past event to cost the finding from
    exact = hiring.project(plan(lines=[hiring.Line(role_id="backend", start_month=1)]).model_copy(
        update={"assumptions": plan().assumptions.model_copy(update={"yearly_budget": 249999.6})}), roles, None)
    assert (exact["total"], exact["left"], exact["over"]) == (250000, 0, False)  # never "over by $0"


def test_the_api_saves_a_plan_and_refuses_a_bad_one(client, simulation):  # noqa: F811
    page = client.get("/api/hiring?mode=simulation").json()
    body = {"mode": "simulation", "assumptions": {**page["assumptions"], "yearly_budget": 900000},
            "lines": [{"role_id": "mts-research", "subject_id": "X900", "name": "Mara Quill", "start_month": 4,
                       "source": "event"}]}
    saved = client.put("/api/hiring", json=body).json()
    assert saved["projection"]["lines"][0]["name"] == "Mara Quill" and saved["projection"]["budget"] == 900000
    assert client.get("/api/hiring?mode=simulation").json()["projection"]["budget"] == 900000
    for bad in ({"role_id": "chef", "start_month": 1}, {"role_id": "backend", "start_month": 13},
                {"role_id": "backend", "start_month": 1, "count": 0}, {"role_id": "backend", "start_month": 1, "source": "x"}):
        assert client.put("/api/hiring", json={**body, "lines": [bad]}).status_code == 422
    assert client.post("/api/simulation/reset", json={}).status_code == 200
    assert client.get("/api/hiring?mode=simulation").json()["projection"]["budget"] == 3400000  # the reset clears it


def test_bad_numbers_are_refused_before_anything_is_saved(client, simulation):  # noqa: F811
    page = client.get("/api/hiring?mode=simulation").json()
    good = {"mode": "simulation", "assumptions": page["assumptions"], "lines": []}
    for change in ({"yearly_budget": 1e12}, {"yearly_budget": -1}, {"salary_estimates": {"backend": 1e308}},
                   {"salary_estimates": {"backend": -5}}, {"salary_estimates": {"backend": 0}},
                   {"salary_estimates": {"chef": 100000}}, {"overhead_pct": "25"}, {"yearly_budget": True},
                   {"year": 2025}, {"surprise": 1}):
        bad = {**good, "assumptions": {**page["assumptions"], **change}}
        assert client.put("/api/hiring", json=bad).status_code == 422, change
    raw = json.dumps({**good, "assumptions": {**page["assumptions"], "yearly_budget": 1}}).replace(
        '"yearly_budget": 1', '"yearly_budget": NaN')
    assert client.put("/api/hiring", content=raw, headers={"content-type": "application/json"}).status_code == 422
    twice = [{"role_id": "backend", "subject_id": "X900", "start_month": 1}] * 2
    assert client.put("/api/hiring", json={**good, "lines": twice}).status_code == 422
    assert client.get("/api/hiring?mode=simulation").json()["projection"]["total"] == page["projection"]["total"]


def test_a_person_the_ledger_holds_is_not_surfaced_whatever_their_call(simulation):
    def names():
        return [p["name"] for p in hiring.page("simulation")["people"]]

    assert "Lena Okafor" in names()  # the engine says check first
    store = today._demo_store()[0]
    contact.record(store, "D904", "never", at=today.DEMO_AS_OF, team="recruiting")  # said on the day itself
    assert "Lena Okafor" not in names()


def test_a_surfaced_reach_the_ledger_asks_to_confirm_first_says_so_as_on_todays_calls(simulation):
    # Ways it could fail: the Budget tab shows "Reach out now" alone for someone Today's calls says to confirm first
    # (they may still be a student); they drop off the budget (a check is still surfaced); someone else gets a gate.
    contact.record(today._demo_store()[0], "X900", "unconfirmed", at="2026-09-10T00:00:00+00:00",
                   note="Their bio says CS at a school.")
    people = {p["name"]: p for p in hiring.page("simulation")["people"]}
    assert people["Mara Quill"]["gate"] == "check_first"
    assert all(p["gate"] is None for name, p in people.items() if name != "Mara Quill")


def test_a_card_gi_sent_keeps_them_in_the_budget(simulation):
    store = today._demo_store()[0]
    rin = next(c for c in today.calls("simulation")["calls"] if c["name"] == "Rin Vale")
    contact.record(store, "A900", "pinged", at=today.DEMO_AS_OF, role_id="mts-research", evidence=rin["evidence"],
                   score=rin["score"])
    rin = next(c for c in today.calls("simulation")["calls"] if c["name"] == "Rin Vale")
    assert rin["gate"]["state"] == "hold"  # nothing new since the card: nobody writes again
    assert "Rin Vale" in [p["name"] for p in hiring.page("simulation")["people"]]  # but she is the likeliest hire
    for kind, team in (("sent", "recruiting"), ("follow_up", "recruiting"), ("replied", "recruiting"),
                       ("invited", "events")):
        contact.record(store, "A900", kind, at=today.DEMO_AS_OF, team=team, by="Dana Kest",
                       **({"until": "2026-09-24", "event_id": "gi-2026-09-games"} if kind == "invited" else {}))
        assert "Rin Vale" in [p["name"] for p in hiring.page("simulation")["people"]], kind  # hiring's own talk
    contact.record(store, "A900", "sent", at=today.DEMO_AS_OF, team="sales", by="Omar Example")
    assert "Rin Vale" not in [p["name"] for p in hiring.page("simulation")["people"]]  # another team's ask holds


def test_parallel_saves_leave_one_whole_plan(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setitem(hiring.PLANS, "live", tmp_path / "plan.json")
    plans = [plan().model_copy(update={"assumptions": plan().assumptions.model_copy(update={"yearly_budget": n})})
             for n in range(40)]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda p: hiring.save("live", p), plans))
    assert hiring.Plan.model_validate_json((tmp_path / "plan.json").read_text())
    assert [f.name for f in tmp_path.iterdir()] == ["plan.json"]  # no temporary file left behind


def test_the_event_cost_comes_from_the_event_records(simulation):
    past = events.page("simulation")["past"]
    ec = hiring.event_cost("simulation")
    assert ec["per_qualified"] == round(sum(r["cost"] for r in past) / sum(r["qualified"] for r in past), 2)
    assert ec["events"] == len(past)


def test_a_saved_plan_that_does_not_read_shows_starting_values_and_is_kept(client, monkeypatch, tmp_path):  # noqa: F811
    saved = tmp_path / "hiring" / "plan.json"
    saved.parent.mkdir()
    saved.write_text('{"assumptions": {"year": 2027, "yearly_budget": null}}')
    monkeypatch.setitem(hiring.PLANS, "live", saved)
    monkeypatch.setattr(today, "TIMELINES", tmp_path / "missing.sqlite")
    page = client.get("/api/hiring?mode=live").json()
    assert page["problem"].startswith(f"The saved plan at {saved} could not be read")
    assert page["projection"]["lines"] == [] and "null" in saved.read_text()  # the file is left for a person to see
    body = {"mode": "live", "assumptions": {**page["assumptions"], "yearly_budget": 500000}, "lines": []}
    assert client.put("/api/hiring", json=body).status_code == 200
    assert client.get("/api/hiring?mode=live").json()["problem"] is None and not saved.with_suffix(".tmp").exists()


def test_the_payroll_export_gives_totals_and_head_counts_never_a_row(tmp_path, monkeypatch):
    export = tmp_path / "payroll.csv"
    export.write_text("Employee Name,Department,Work Location,Base Salary\n"
                      "Ada Example,Research,London,\"$120,000\"\nBo Example,Research,,80000.50\n")
    monkeypatch.setitem(hiring.PAYROLL, "live", export)
    base = hiring.payroll("live", plan().assumptions)
    assert (base["error"], base["people"], base["cost"]) == (None, 2, 250001)  # 200,000.50 plus 25%
    assert base["offices"] == {"London": 1, "Office not given": 1} and base["teams"] == {"Research": 2}
    assert "Ada" not in json.dumps(base) and "120000" not in json.dumps(base)  # no name and no one's salary
    export.write_text("team,office,annual_salary\nresearch,London,£180000\n")
    bad = hiring.payroll("live", plan().assumptions)
    assert bad["error"].endswith("line 2: the salary must be a plain dollar amount, up to $10,000,000")
    assert "180000" not in bad["error"] and "counts new hires only" in bad["error"]  # never a cell's value
    assert hiring.project(plan(), {}, None, bad)["payroll"] == 0  # a bad export counts nothing, and says so
    export.write_text("team,office,annual_salary\nresearch,London,$120,000\n")  # unquoted: one field too many
    assert "line 2 has more fields than the header" in hiring.payroll("live", plan().assumptions)["error"]
    export.write_text("team,office,annual_salary,currency\nresearch,London,90000,GBP\n")
    assert hiring.payroll("live", plan().assumptions)["error"].endswith("line 2: salaries must be in US dollars")
    export.write_text("team,office\nresearch,London\n")
    assert "needs an annual_salary column" in hiring.payroll("live", plan().assumptions)["error"]
    export.write_text("team,office,annual_salary\n")
    assert hiring.payroll("live", plan().assumptions)["people"] == 0  # just the header: nobody on payroll yet
    export.write_text("")
    assert hiring.payroll("live", plan().assumptions)["error"].endswith("the export is empty")  # more likely a failed export
    export.write_text("team,office,annual_salary\nresearch,London,90000,,\n,,,\n")
    assert hiring.payroll("live", plan().assumptions)["people"] == 1  # trailing blank cells, as spreadsheets leave them


def test_payroll_skips_blank_and_unpaid_rows_and_matches_the_jds_offices(tmp_path, monkeypatch):
    export = tmp_path / "payroll.csv"
    export.write_text("team,office,annual_salary\nresearch,NYC,100000\nops,new york city,100000\n"
                      "ops,Remote,100000\nops,NYC,0\n,,\n")
    monkeypatch.setitem(hiring.PAYROLL, "live", export)
    base = hiring.payroll("live", plan().assumptions)
    assert (base["error"], base["people"], base["unpaid"], base["cost"]) == (None, 3, 1, 375000)
    assert base["offices"] == {"New York City": 2, "Remote": 1} and base["unmatched"] == ["Remote"]


def test_a_hire_sits_in_one_of_its_roles_offices(client, simulation):  # noqa: F811
    page = client.get("/api/hiring?mode=simulation").json()
    roles = {r["id"]: r for r in page["roles"]}
    assert roles["mts-research"]["offices"] == ["New York City", "Geneva", "London", "Paris"]
    body = {"mode": "simulation", "assumptions": page["assumptions"],
            "lines": [{"role_id": "mts-research", "office": "London", "start_month": 6},
                      {"role_id": "backend", "start_month": 6}]}
    lines = client.put("/api/hiring", json=body).json()["projection"]["lines"]
    assert [line["office"] for line in lines] == ["London", "New York City"]  # the JD's first office by default
    body["lines"][1]["office"] = "London"  # the backend JD lists New York City only
    assert client.put("/api/hiring", json=body).status_code == 422


def test_live_starts_empty_and_saves_to_research_private(client, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setitem(hiring.PLANS, "live", tmp_path / "hiring" / "plan.json")
    monkeypatch.setattr(today, "TIMELINES", tmp_path / "missing.sqlite")
    page = client.get("/api/hiring?mode=live").json()
    assert page["people"] == [] and page["missing"] and page["projection"]["lines"] == []
    assert page["assumptions"]["salary_estimates"] == {} and page["invented"] is False  # no invented numbers live
    body = {"mode": "live", "assumptions": {**page["assumptions"], "yearly_budget": 750000,
                                            "salary_estimates": {"backend": 220000}},
            "lines": [{"role_id": "backend", "count": 1, "start_month": 1}]}
    assert client.put("/api/hiring", json=body).json()["projection"]["total"] == 275000
    assert (tmp_path / "hiring" / "plan.json").exists()


def test_a_ledger_that_cant_be_read_lists_nobody_and_says_why(simulation, monkeypatch):
    real = contact.history
    broken = {"person_id": "x", "kind": "unreadable", "at": "2026-01-01T00:00:00+00:00", "team": "recruiting",
              "ledger": "the web app", "note": "DatabaseError"}
    monkeypatch.setattr(contact, "history", lambda store, pid: [*real(store, pid), {**broken, "person_id": pid}])
    monkeypatch.setattr(contact, "unreadable", lambda store: contact._broken("the web app", "DatabaseError"))
    page = hiring.page("simulation")
    assert page["people"] == []  # fails closed
    assert page["ledger_problem"].startswith("Nobody surfaced is listed: the web app's contact ledger can't be read")
    monkeypatch.undo()
    assert hiring.page("simulation")["ledger_problem"] is None and hiring.page("simulation")["people"]
