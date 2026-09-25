"""The founders' one-pager: the month so far on one page, to read in a minute or paste into an email.

A headline, then three parts. Pipeline per role: who to reach now, whom GI reached and who replied this month,
who is active (asked lately or talking), offers this month, the plan year's hires filled of planned, and who
starts soon. Spend against budget: the people budget (payroll plus the plan) and event spend this quarter.
What's slipping: whatever the Monday brief has as overdue, any budget item, and any role with fewer people in
sight than hires still to make. It reads the Monday brief, the contact ledger, the Hiring budget and New
starts, so it shows nothing they don't; like the brief, it never mentions a visa.

Only hiring's own outreach counts (recruiting and events). A Slack card to GI's ops is not reaching anyone,
and a sales or marketing note is not the hiring pipeline; a no from any team closes a person.
"""

from collections import defaultdict
from datetime import date, timedelta

from . import contact, events, hiring, inbox, role_compiler, starts, today
from .models import parse_time

SOON = timedelta(days=60)  # "starting soon"
ASKS = ("sent", "follow_up", "invited")  # GI wrote to them: a note, a follow-up or an event invite
TEAMS = ("recruiting", "events")  # hiring's own asks
CARD_DAYS = 30  # a card ops hasn't acted on yet still counts as someone to reach, for this long
FOOTNOTE = ("To reach: the engine's calls now, and anyone on a card in the last {card_days} days nobody has written to "
            "since. Reached, replied and offers: this month; reached counts GI's notes, follow-ups and event invites, "
            "never Slack cards. Active: asked in the last {days} days or talking, and not told no. {year} hires filled: "
            "accepted offers starting in {year}. Starting: within 60 days.")


def _histories(store, day):
    """Each person's ledger up to ``day``, oldest first, every team's."""
    out = defaultdict(list)
    for e in store.all("contact"):
        if parse_time(e["at"]).date() <= day:
            out[e["person_id"]].append(e)
    return {pid: sorted(es, key=lambda e: parse_time(e["at"])) for pid, es in out.items()}


def _hiring(e):
    return e.get("team", "recruiting") in TEAMS


def _state(entries, day):
    """Where a person stands for hiring: "active" (hiring asked within the quiet period, or they're talking),
    "carded" (on a recent card nobody has written to since, from any team), or None (a no, gone quiet, or
    never asked). Another team's note ends a card, never a conversation hiring has open."""
    kinds = (*ASKS, "replied", "not_now", "pinged")
    if any(e["kind"] == "never" for e in entries) or contact.joining(entries, f"{day.isoformat()}T23:59:59+00:00"):
        return None  # a no, or an accepted offer: counted under offers, never as someone still to reach
    latest = next((e for e in reversed(entries) if e["kind"] in kinds), None)
    last = next((e for e in reversed(entries) if e["kind"] in kinds and _hiring(e)), None)
    if not last or latest["kind"] == "not_now" or last["kind"] == "not_now":
        return None
    at = parse_time(last["at"])
    age = (day - at.date()).days
    if last["kind"] == "pinged":
        written = any(e["kind"] in ASKS and parse_time(e["at"]) > at for e in entries)  # another team's, since
        return "carded" if age < CARD_DAYS and not written else None
    if age < contact.QUIET_DAYS or (last["kind"] == "invited" and last["until"] >= day.isoformat()):
        return "active"
    return None


def month(mode):
    calls = today.calls(mode)
    w = inbox.week(mode, calls)
    if w["missing"]:
        return {"missing": w["missing"]}
    day = date.fromisoformat(w["as_of"])
    first = day.replace(day=1)
    store = today.source(mode)[0]
    roles = {r["id"]: r for r in w["roles"]}
    role_of = {}
    for c in calls["calls"]:  # strongest call first, as the week reads them
        role_of.setdefault(contact.person_key(c["subject_id"]), c["role"]["id"])
    # An event guest with no call yet (a researcher at a company GI could hire from) has the role they're invited for.
    for g in (events.workspace(mode) or (None, None, {"people": []}))[2]["people"]:
        role_of.setdefault(contact.person_key(g.subject_id), role_compiler.role_id(g.role))
    people = defaultdict(lambda: {"reached": set(), "replied": set(), "active": set(), "carded": set()})
    for pid, entries in _histories(store, day).items():
        role_id = next((e["role_id"] for e in reversed(entries) if e.get("role_id") and _hiring(e)), None) or role_of.get(pid)
        this_month = [e for e in entries if parse_time(e["at"]).date() >= first and _hiring(e)]
        if any(e["kind"] in ASKS for e in this_month):
            people[role_id]["reached"].add(pid)
        if any(e["kind"] == "replied" for e in this_month):
            people[role_id]["replied"].add(pid)
        if state := _state(entries, day):
            people[role_id][state].add(pid)

    plan, problem = hiring.load(mode, w["as_of"])
    year = plan.assumptions.year
    costs, ec = hiring.costs(mode, plan.assumptions)
    paid = hiring.payroll(mode, plan.assumptions)
    pr = hiring.project(plan, costs, ec["per_qualified"] if ec else None, paid)
    planned = {r["role_id"]: r["hires"] for r in pr["by_role"]}
    found, _ = starts.load(mode)
    hires = found.hires if found else []
    pipeline = []
    order = [r["id"] for r in hiring._roles()]  # config/roles.json's order, so rows don't move week to week
    for role_id in sorted(dict.fromkeys([*roles, *planned]), key=lambda r: order.index(r) if r in order else len(order)):
        r, p = roles.get(role_id), people[role_id]
        in_sight = p["carded"] | ({contact.person_key(x["subject_id"]) for x in r["reach"] + r["check_first"]}
                                  | set(r["more_ids"]) if r else set())
        row = {"role_id": role_id, "title": r["title"] if r else costs.get(role_id, {}).get("title", role_id),
               "to_reach": len(in_sight - p["active"]),
               "reached": len(p["reached"]), "replied": len(p["replied"]), "active": len(p["active"]),
               "offers": sum(h.role_id == role_id and first <= h.offer_on <= day for h in hires),
               "filled": sum(h.role_id == role_id and h.start.year == year for h in hires),
               "planned": planned.get(role_id, 0),
               "starting": sum(h.role_id == role_id and day < h.start <= day + SOON for h in hires)}
        if any(v for k, v in row.items() if k not in ("role_id", "title")):
            pipeline.append(row)
    slipping = [{"title": d["title"], "why": d["why"] if d["overdue"] else None} for d in w["decisions"]
                if d["overdue"] or d["area"] == "Budget"]  # a budget item's detail is in the spend part already
    for r in pipeline:  # fewer people in sight than hires still to make: the pipeline is thin
        if (still := r["planned"] - r["filled"]) > r["to_reach"] + r["active"]:
            sight = r["to_reach"] + r["active"]
            slipping.append({"title": f"Thin pipeline for {r['title']}",
                             "why": f"{_n(still, f'planned {year} hire')} still to make and "
                                    + (f"only {sight} in sight ({r['to_reach']} to reach, {r['active']} active)."
                                       if sight else "nobody in sight.")})
    people_budget = None if problem or not plan.assumptions.yearly_budget else {
        "year": year, "budget": pr["budget"], "total": pr["total"], "left": pr["left"],
        "payroll": paid["cost"] if paid and not paid["error"] else None,
        "hires": sum(planned.values()), "uncosted": pr["uncosted_hires"]}
    m = {"missing": None, "month": f"{day:%B %Y}", "as_of": w["as_of"], "source": w["source"], "year": year,
         "pipeline": pipeline, "plan_unreadable": bool(problem), "people_budget": people_budget,
         "event_budget": w["budget"], "slipping": slipping,
         "invented": (w["source"] or "").startswith("Invented"),
         "footnote": FOOTNOTE.format(days=contact.QUIET_DAYS, card_days=CARD_DAYS, year=year)}
    return {**m, "headline": _headline(m)}


def _n(n, word, words=None):
    return f"{n} {word if n == 1 else words or word + 's'}"


def _headline(m):
    """The month in one line: what's slipping and where both budgets stand."""
    money = inbox._money
    parts = [f"{_n(len(m['slipping']), 'thing')} slipping" if m["slipping"] else "Nothing slipping"]
    if b := m["people_budget"]:
        parts.append(f"people budget {money(abs(b['left']))} {'left' if b['left'] >= 0 else 'over'} for {b['year']}")
    if e := m["event_budget"]:
        parts.append(f"events {money(abs(e['left']))} {'left' if e['left'] >= 0 else 'over'} for {e['period']}")
    return " · ".join(parts)


def _role_line(r, year):
    parts = [f"{r['to_reach']} to reach" if r["to_reach"] else "",
             f"{r['reached']} reached this month" if r["reached"] else "",
             f"{r['replied']} replied this month" if r["replied"] else "",
             f"{r['active']} active" if r["active"] else "",
             f"{_n(r['offers'], 'offer')} accepted this month" if r["offers"] else "",
             f"{r['filled']} of {_n(r['planned'], f'planned {year} hire')} filled" if r["planned"] else "",
             f"{r['starting']} starting within 60 days" if r["starting"] else ""]
    return f"{r['title']}: {', '.join(p for p in parts if p)}"


def text(m):
    """The one-pager as plain text, to paste into an email or a message."""
    if m["missing"]:
        return m["missing"]
    money = inbox._money
    lines = [f"GI hiring and ops, {m['month']} (as of {m['as_of']})" + (": invented people and numbers" if m["invented"] else ""),
             m["headline"], "", "Pipeline per role"]
    lines += [f"- {_role_line(r, m['year'])}" for r in m["pipeline"]] or ["- Nobody in the pipeline yet."]
    lines += ["", "Spend against budget"]
    if b := m["people_budget"]:
        lines.append(f"- People {b['year']}: {money(b['total'])} of {money(b['budget'])} "
                     + (f"({money(b['left'])} left)" if b["left"] >= 0 else f"({money(-b['left'])} over)")
                     + f", for payroll and {_n(b['hires'], 'planned hire')}"
                     + (f"; {b['uncosted']} not costed yet" if b["uncosted"] else ""))
    elif m["plan_unreadable"]:
        lines.append("- People: the saved hiring plan could not be read; fix it on the Hiring budget page")
    else:
        lines.append("- People: no yearly budget set on the Hiring budget page")
    if m["event_budget"]:
        lines.append(f"- Events {inbox.budget_line(m['event_budget'])}")
    lines += ["", "What's slipping"]
    lines += [f"- {s['title']}" + (f": {s['why']}" if s["why"] else "") for s in m["slipping"]] or ["- Nothing overdue."]
    lines += ["", FOOTNOTE.format(days=contact.QUIET_DAYS, card_days=CARD_DAYS, year=m["year"])]
    return "\n".join(lines)
