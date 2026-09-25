"""The hiring budget: what the people the engine surfaces would cost GI if hired, and how a plan fits the year.

Each role's first-year cost is its salary (the midpoint of GI's posted range from app/pay.py; with none
posted, an estimate the user types, labelled as theirs), plus overhead for benefits and payroll taxes,
plus a one-time hiring cost that depends on how the hire is found: GI's own outreach, a GI event (the
events' cost per qualified conversation times the conversations a hire takes), or an agency (a fee on
the first-year salary). Every figure is the role's cost to GI, never a guess at what a person earns now.

A plan is lines of hires: a surfaced person or a number of open seats for a role, each with a start month
and a source. The projection counts each line's salary and overhead from its start month to the year's
end, plus its hiring cost, against a yearly hiring budget. Live plans are saved to research/private/hiring/;
the simulation's are kept in memory with invented numbers, and its reset clears them.

Only a yearly USD range counts as posted: the budget is in dollars a year, so an hourly or other-currency
range asks for the user's estimate instead. The ranges come from config/pay-bands.json, the one file the
Slack card's pay line reads too; the simulation never reads it (pay.for_mode), so it prices its invented estimates.

Each hire sits in one of its role's offices (the JD's locations). Current payroll comes in as a baseline
from the payroll tool's CSV export (team, office, annual salary; names are never read), so what is left
of the yearly budget counts the people GI already pays. The page shows only its total and head counts,
never a row.
"""

import csv
import json
import math
import os
import tempfile
import threading
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import contact, events, pay, today

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "config" / "roles.json"
PLANS = {"live": ROOT / "research/private/hiring/plan.json"}
PAYROLL = {"simulation": ROOT / "tests/fixtures/hiring/payroll.csv", "live": ROOT / "research/private/hiring/payroll.csv"}
# The columns a payroll export may use for what the baseline needs; every other column is ignored.
COLUMNS = {"team": ("team", "department"), "office": ("office", "location", "work location", "work_location"),
           "salary": ("annual_salary", "annual salary", "salary", "base_salary", "base salary"),
           "currency": ("currency", "salary currency", "salary_currency")}
# How payroll tools often write an office the JDs name another way: matched after the JD's own names.
OFFICE_ALIASES = {"nyc": "New York City", "new york": "New York City", "new york, ny": "New York City",
                  "new york, new york": "New York City", "ny": "New York City"}
DEMO = ROOT / "tests/fixtures/hiring/assumptions.json"
SOURCES = {"outreach": "GI's own outreach", "event": "A GI event", "agency": "An agency"}
SURFACED = ("reach_now", "verify_first")  # the engine's reach-outs and checks: who it puts in front of GI now
_memory = {}  # the simulation's saved plan
_saving = threading.Lock()  # the API runs saves on a thread pool: one write at a time
# Finite numbers only, no strings or booleans passed off as numbers, and no fields the page doesn't know.
STRICT = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
MAX_DOLLARS = 1e9  # a bound on any amount typed in, so no total can overflow
Salary = Annotated[float, Field(gt=0, le=10_000_000)]


class Assumptions(BaseModel):
    model_config = STRICT
    year: int = Field(ge=2020, le=2100)
    yearly_budget: float = Field(ge=0, le=MAX_DOLLARS)
    overhead_pct: float = Field(ge=0, le=200)  # benefits and payroll taxes, as a share of salary
    agency_fee_pct: float = Field(ge=0, le=100)
    outreach_cost: float = Field(default=0, ge=0, le=10_000_000)  # per hire found by GI's own outreach
    conversations_per_hire: float = Field(default=3, ge=0, le=100)  # qualified event conversations a hire takes
    salary_estimates: dict[str, Salary] = Field(default_factory=dict)  # the user's, for a role with no posted range
    note: str = Field(default="", max_length=500)


class Line(BaseModel):
    model_config = STRICT
    role_id: str = Field(min_length=1, max_length=100)
    subject_id: str | None = Field(default=None, max_length=200)  # a surfaced person, or None for open seats
    name: str | None = Field(default=None, max_length=200)
    office: str | None = Field(default=None, max_length=100)  # one of the role's offices; its first when None
    count: int = Field(default=1, ge=1, le=50)
    start_month: int = Field(ge=1, le=12)
    source: Literal["outreach", "event", "agency"] = "outreach"


class Plan(BaseModel):
    model_config = STRICT
    assumptions: Assumptions
    lines: list[Line] = Field(default_factory=list, max_length=200)


def _roles():
    return json.loads(ROLES.read_text())["roles"]


def default_year(as_of):
    """This year's plan until September, then next year's."""
    day = date.fromisoformat(as_of[:10])
    return day.year + 1 if day.month >= 9 else day.year


def load(mode, as_of):
    """(the saved plan, or starting values, and why a saved live plan could not be read, or None)."""
    if mode == "simulation":
        if "plan" not in _memory:
            _memory["plan"] = Plan.model_validate(json.loads(DEMO.read_text()))
        return _memory["plan"], None
    path = PLANS[mode]
    start = Plan(assumptions=Assumptions(year=default_year(as_of), yearly_budget=0, overhead_pct=25, agency_fee_pct=20,
                                         note="Starting values: set your budget, overhead and any salary estimates."))
    if not path.exists():
        return start, None
    try:
        return Plan.model_validate_json(path.read_text()), None
    except (OSError, ValueError) as err:  # left as it is until the next save, so nothing is lost by reading it
        return start, f"The saved plan at {path} could not be read, so this shows starting values; " \
                      f"Update or Add replaces the file. ({str(err).splitlines()[0]})"


def check(plan, as_of):
    """Refuse what the page can't cost or would count twice: roles not in config/roles.json, an office the
    role's JD doesn't list, a year already over, the same person twice."""
    offices = {r["id"]: r.get("locations") or [] for r in _roles()}
    if unknown := sorted(({line.role_id for line in plan.lines} | set(plan.assumptions.salary_estimates)) - set(offices)):
        raise ValueError(f"Not a role in config/roles.json: {', '.join(unknown)}")
    if wrong := sorted({f"{line.office} for {line.role_id}" for line in plan.lines
                        if line.office and line.office not in offices[line.role_id]}):
        raise ValueError(f"Not an office in the role's JD: {', '.join(wrong)}")
    if plan.assumptions.year < int(as_of[:4]):
        raise ValueError(f"{plan.assumptions.year} is over: plan this year or a later one")
    people = [line.subject_id for line in plan.lines if line.subject_id]
    if twice := sorted({sid for sid in people if people.count(sid) > 1}):
        raise ValueError(f"The same person is in the plan twice: {', '.join(twice)}")


def save(mode, plan):
    if mode == "simulation":
        _memory["plan"] = plan
        return
    path = PLANS[mode]
    path.parent.mkdir(parents=True, exist_ok=True)
    with _saving, tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp", delete=False) as tmp:
        try:
            tmp.write(plan.model_dump_json(indent=2))
            tmp.close()
            os.replace(tmp.name, path)  # whole or not at all: a read during a save never sees half a file
        except BaseException:
            Path(tmp.name).unlink(missing_ok=True)
            raise


def reset():
    _memory.clear()


def event_cost(mode):
    """GI's event spend per qualified conversation across all its past events, or None before the first one.
    From the event records alone: no one's call is run for it."""
    found = events.workspace(mode)
    if not found:
        return None
    store, as_of, records, _ = found
    day = date.fromisoformat(as_of[:10])
    past = [events.results(store, e, records["guests"], records["spend"], records["pipeline"])
            for e in records["events"] if e.day <= day]
    cost, qualified = sum(r["cost"] for r in past), sum(r["qualified"] for r in past)  # every event's spend counts
    return {"per_qualified": round(cost / qualified, 2), "events": len(past), "qualified": qualified} if qualified else None


def posted(role_id, mode):
    """GI's posted range for the role when the plan can use it (yearly, in USD), else None. None in the simulation."""
    band = pay.for_mode(mode).get(role_id)
    return band if band and band["interval"] == "year" and band["currency"] == "USD" else None


def salary(role_id, a, whose="your estimate", *, mode):
    """(the role's yearly salary for the plan, where it comes from), or (None, why not). ``mode``: the workspace, whose
    posted ranges it reads (pay.for_mode: none in the simulation)."""
    band = pay.for_mode(mode).get(role_id)
    if posted(role_id, mode):
        return (band["min"] + band["max"]) / 2, f"midpoint of GI's posted range, USD {band['min']:,.0f} to {band['max']:,.0f}"
    why = (f"GI's post gives a range per {band['interval']} in {band['currency']}, not dollars a year" if band
           else "the demo shows no posted range" if mode == "simulation" else "GI's post states no range")
    if (est := a.salary_estimates.get(role_id)) is not None:
        return est, f"{whose}: {why}"
    return None, f"{why}: add your estimate"


def hiring_cost(source, base, a, per_conversation):
    """The one-time cost of finding one hire, or None when it can't be worked out."""
    if source == "outreach":
        return a.outreach_cost
    if source == "agency":
        return None if base is None else base * a.agency_fee_pct / 100
    return None if per_conversation is None else per_conversation * a.conversations_per_hire


def _dollars(v):
    """Whole dollars, halves up, so every total on the page adds up exactly."""
    return math.floor(v + 0.5)


def first_year(base, a, hire):
    return None if base is None or hire is None else _dollars(base * (1 + a.overhead_pct / 100) + hire)


def _amount(text):
    """A yearly salary in dollars, 0 when the cell is blank or zero (unpaid, a contractor paid elsewhere)."""
    text = str(text).replace("$", "").replace(",", "").strip()
    value = float(text) if text else 0.0
    if not (math.isfinite(value) and 0 <= value <= 10_000_000):
        raise ValueError
    return value


def _office(name, known):
    """The payroll's office as the JDs name it: the same name in any case, else a common alias, else as is."""
    by_key = {k.casefold(): k for k in known}
    key = " ".join(name.split()).casefold()
    return by_key.get(key) or OFFICE_ALIASES.get(key) or name


def _where(path):
    return str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)


def payroll(mode, a):
    """The people GI pays now, from the payroll tool's CSV export: head count by office and team, and what
    they cost in the plan's year (salaries for the whole year plus overhead). None without an export; with a
    bad one, why, naming the line but never a cell's value. Only totals and head counts leave this function.
    Rows with no salary (blank or zero) are left out and counted; offices are matched to the JDs' names."""
    path = PAYROLL[mode]
    if not path.exists():
        return None
    where = _where(path)
    known = {o for r in _roles() for o in r.get("locations") or []}
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, restkey="\0extra")
            if not reader.fieldnames:
                raise ValueError("the export is empty")
            heads = {(h or "").strip().lower() for h in reader.fieldnames}
            rows = list(reader)
        col = {k: next((c for c in names if c in heads), None) for k, names in COLUMNS.items()}
        if heads and not col["salary"]:
            raise ValueError("it needs an annual_salary column")
        total, offices, teams, people, unpaid = 0.0, {}, {}, 0, 0
        for n, raw in enumerate(rows, start=2):  # line 1 is the header
            if any((v or "").strip() for v in raw.get("\0extra") or []):  # trailing blank cells are fine
                raise ValueError(f"line {n} has more fields than the header: put amounts with commas in quotes")
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k != "\0extra"}
            if not any(row.values()):
                continue  # a blank row, as spreadsheets leave at the end
            if col["currency"] and row.get(col["currency"], "").upper() not in ("", "USD"):
                raise ValueError(f"line {n}: salaries must be in US dollars")
            try:
                amount = _amount(row.get(col["salary"], ""))
            except ValueError:
                raise ValueError(f"line {n}: the salary must be a plain dollar amount, up to $10,000,000") from None
            if not amount:
                unpaid += 1
                continue
            total, people = total + amount, people + 1
            office = _office(row.get(col["office"], "") if col["office"] else "", known) or "Office not given"
            team = (row.get(col["team"], "") if col["team"] else "") or "Team not given"
            offices[office] = offices.get(office, 0) + 1
            teams[team] = teams.get(team, 0) + 1
    except (OSError, UnicodeDecodeError, csv.Error, ValueError, AttributeError, TypeError, KeyError) as err:
        return {"path": where, "error": f"The payroll export at {where} could not be read, so this counts new hires "
                                        f"only: {err}", "people": 0, "cost": 0, "offices": {}, "teams": {},
                "unpaid": 0, "unmatched": []}
    return {"path": where, "error": None, "people": people, "cost": _dollars(total * (1 + a.overhead_pct / 100)),
            "offices": offices, "teams": teams, "unpaid": unpaid,
            "unmatched": sorted(o for o in offices if o not in known and o != "Office not given")}


def project(plan, roles, per_conversation, baseline=None):
    """Each line's cost in the plan's year (salary and overhead from its start month, plus hiring), totals per
    role and overall, and what is left of the yearly budget after current payroll (``baseline``, from
    ``payroll``) and the plan. A line that can't be costed is left out of the totals and listed in
    ``missing``."""
    a, lines, missing = plan.assumptions, [], []
    for i, line in enumerate(plan.lines):
        base = roles[line.role_id]["salary"] if line.role_id in roles else None
        hire = hiring_cost(line.source, base, a, per_conversation)
        months = 13 - line.start_month
        needs = "a salary" if base is None else "an event cost" if hire is None else None
        if needs:
            missing.append(i)
            cost = None
        else:
            cost = _dollars((base * months / 12 * (1 + a.overhead_pct / 100) + hire) * line.count)
        offices = roles.get(line.role_id, {}).get("offices") or []
        lines.append({**line.model_dump(), "office": line.office or (offices[0] if offices else "Office not given"),
                      "index": i, "months": months, "cost": cost, "needs": needs,
                      "full_year": None if base is None else _dollars(base * (1 + a.overhead_pct / 100) * line.count),
                      "role_title": roles.get(line.role_id, {}).get("title", line.role_id), "source_label": SOURCES[line.source]})
    hires_cost = sum(line["cost"] or 0 for line in lines)
    current = baseline["cost"] if baseline and not baseline["error"] else 0
    total = current + hires_cost
    budget = _dollars(a.yearly_budget)  # whole dollars, like the costs, so "over" never reads "over by $0"
    now = baseline["offices"] if baseline and not baseline["error"] else {}
    planned = {}
    for line in lines:
        planned[line["office"]] = planned.get(line["office"], 0) + line["count"]
    offices = [{"office": o, "now": now.get(o, 0), "planned": planned.get(o, 0), "desks": now.get(o, 0) + planned.get(o, 0)}
               for o in sorted(set(now) | set(planned), key=lambda o: (-(now.get(o, 0) + planned.get(o, 0)), o))]
    by_role = {}
    for line in lines:
        role = by_role.setdefault(line["role_id"], {"role_id": line["role_id"], "title": line["role_title"], "hires": 0,
                                                    "cost": 0, "uncosted": 0})
        role["hires"] += line["count"]
        role["cost"] += line["cost"] or 0
        role["uncosted"] += line["cost"] is None
    return {"lines": lines, "by_role": list(by_role.values()), "total": total, "budget": budget,
            "hires_cost": hires_cost, "payroll": current, "offices": offices,
            "left": budget - total, "over": total > budget,
            "hires": sum(line["count"] for line in lines), "missing": missing,
            "uncosted_hires": sum(line["count"] for line in lines if line["cost"] is None),
            "unsalaried_hires": sum(line["count"] for line in lines if line["full_year"] is None),
            "run_rate": sum(line["full_year"] or 0 for line in lines)}


def costs(mode, a):
    """({role id: its salary, where that comes from, and its cost per hire by source}, the events' cost)."""
    ec = event_cost(mode)
    per_conversation = ec["per_qualified"] if ec else None
    roles, bands = {}, pay.for_mode(mode)
    for r in _roles():
        base, basis = salary(r["id"], a, "an estimate invented for the demo" if mode == "simulation" else "your estimate",
                             mode=mode)
        by_source = {s: hiring_cost(s, base, a, per_conversation) for s in SOURCES}
        roles[r["id"]] = {"id": r["id"], "title": r["title"], "offices": r.get("locations") or [],
                          "pay": pay.line(r["id"], bands), "posted": bool(posted(r["id"], mode)),
                          "salary": base, "basis": basis, "estimate": a.salary_estimates.get(r["id"]),
                          "hiring_cost": by_source, "first_year": first_year(base, a, by_source["outreach"])}
    return roles, ec


OWN_ASK = ("pinged", "sent", "follow_up", "replied", "invited")  # hiring's own approach and their answer to it
HIRING_TEAMS = ("recruiting", "events")


def _held(store, c, as_of):
    """Whether the contact ledger holds this person for a reason of their own or another team's: they said
    never or not now, they work at a partner, or sales or marketing owns the ask. Hiring's own approach is
    not one (a card, a message, a follow-up, an event invite, their reply): whoever GI is already talking
    to is the likeliest hire, so it never drops them from the budget. ``as_of`` is the workspace's full clock."""
    if not store:
        return False
    entries = [e for e in contact.history(store, c["subject_id"])
               if not (e["kind"] in OWN_ASK and e.get("team", "recruiting") in HIRING_TEAMS)]
    return contact.check(entries, as_of).state == "hold"


def page(mode):
    """The Budget tab: roles with their cost per hire, the surfaced people with their first-year cost, the plan."""
    calls = today.calls(mode)
    as_of = calls["as_of"] or date.today().isoformat()
    plan, problem = load(mode, as_of)
    a = plan.assumptions
    roles, ec = costs(mode, a)
    found = today.source(mode)
    store, clock = (found[0], found[1]) if found else (None, as_of)
    people, seen = [], set()
    ledger = contact.unreadable(store) if store else None  # the gate fails closed on it: say why nobody is listed
    for c in calls["calls"]:
        key = contact.person_key(c["subject_id"])
        if c["action"] not in SURFACED or key in seen or _held(store, c, clock):
            continue
        seen.add(key)
        role = roles.get(c["role"]["id"])
        people.append({"subject_id": c["subject_id"], "name": c["name"], "role_id": c["role"]["id"],
                       "role_title": c["role"]["title"], "action": c["action"], "headline": c["headline"],
                       "gate": (c["gate"] or {}).get("state"),  # the contact ledger's word, as Today's calls badges it
                       "why": (c["trigger"] or {}).get("what") or c["why_now"], "profile_url": c["profile_url"],
                       "first_year": role["first_year"] if role else None,
                       "planned": any(line.subject_id == c["subject_id"] for line in plan.lines)})
    baseline = payroll(mode, a)
    return {"as_of": calls["as_of"], "source": calls["source"], "missing": calls["missing"], "problem": problem,
            "ledger_problem": ledger and f"Nobody surfaced is listed: {ledger[:1].lower()}{ledger[1:]}",
            "assumptions": a.model_dump(), "roles": list(roles.values()), "people": people, "event_cost": ec,
            "sources": SOURCES, "payroll": baseline, "payroll_path": _where(PAYROLL[mode]),
            "projection": project(plan, roles, ec["per_qualified"] if ec else None, baseline),
            "invented": mode == "simulation", "min_year": int(as_of[:4])}


def update(mode, body):
    """Save a plan sent from the Budget tab and return the page as it now stands. The plan is costed before it
    is saved, so a plan the page can't show is never kept."""
    plan = Plan.model_validate({"assumptions": body.get("assumptions"), "lines": body.get("lines") or []})
    found = today.source(mode)
    check(plan, found[1] if found else date.today().isoformat())
    roles, ec = costs(mode, plan.assumptions)
    project(plan, roles, ec["per_qualified"] if ec else None)
    save(mode, plan)
    return page(mode)
