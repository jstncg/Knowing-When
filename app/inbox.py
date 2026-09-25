"""The Monday brief: the few decisions worth GI's ops team's attention this week, most urgent first.

Each decision says what to do, why now, the best way in, what it costs when it is a hire, and where
to act (a ready draft opens in one tap when the engine drafted one). They come from Today's calls (who
to reach, who to check first, waits ending this week), the Events page (follow-ups due, invites for the
next event, event spend) and the Hiring budget (a plan over the year's budget, a role with no salary to
cost it). It is ops only: GTM is a separate tool. The brief reads those pages, so it shows nothing they
don't; with no decision to make it stays silent.

Behind the list, the week in detail: per role, who to reach (the engine's reach-now calls the contact
ledger clears and whose drafts pass their checks, strongest first, at most the weekly card cap and
contact.TOTAL_CAP across every role), who to check first (a failing draft among them) and who another team holds; the waits that end this week; the next event and who to invite;
follow-ups due; and event spend against budget. It contacts no one: app/weekly.py posts it to GI's own channel as
one post, with each reach's card (routing.send) and each draft in its thread. ``card`` is the Home tab's form.
"""

import math
import re
from datetime import date, timedelta

from . import contact, events, hiring, routing, starts, today

WEEK = timedelta(days=7)
BRIEF_MAX = 8  # decisions on the card: the few worth attention; the rest wait on the page
INVITE_LEAD = timedelta(days=7)  # invites go out a week before the night
AREAS = ("Hiring", "Events", "Budget")  # the order within a day


def _monday(day):
    return day - timedelta(days=day.weekday())


def watchlist(mode):
    """(who the brief may name, why nobody if so, the replay cases by contact.person_key), from one read of the
    people file. Live: its watchlist (``listed``); a missing or unreadable file names nobody, in a fixed sentence
    (the Events page says what is wrong with one that won't read). The simulation: everyone (None), no replay cases."""
    if mode != "live":
        return None, None, frozenset()
    why = "The people file ({}) {}: it says who is a replay case that never gets a card{}."
    if not events.WATCHLIST.exists():
        return frozenset(), why.format(events.WATCHLIST.name, "is missing", ""), frozenset()
    people, error = events._people_file()
    if error:
        return frozenset(), why.format(events.WATCHLIST.name, "doesn't read", ", and the Events page says why"), \
            frozenset()
    return frozenset(listed(people)), None, events.replay_keys(people)


def listed(people):
    """A people file's live watchlist, as route.py --people (so the daily run) and the brief both route it: its rows
    with no moment, by their own subject ids, and nobody with a moment row under any id (the scorecard's replay
    cases never get a card). The replay drill keeps its own selection."""
    replay = events.replay_keys(people)
    return [p.subject_id for p in people if contact.person_key(p.subject_id) not in replay]


def week(mode, calls=None):
    """The week's brief. ``calls`` is Today's calls when the caller already ran them (the engine runs once). Live, it
    names only the watchlist (``watchlist``)."""
    calls = calls or today.calls(mode)
    if calls["missing"]:
        return {"as_of": None, "missing": calls["missing"]}
    names = {contact.person_key(c["subject_id"]): c["name"] for c in calls["calls"]}  # a follow-up's name, anyone's
    allowed, unlisted, replay = watchlist(mode)

    def named(pid):
        """A follow-up or invite the brief may carry: someone the file doesn't list may still be owed a note, but
        never a replay case, and nobody until the file reads."""
        return not unlisted and contact.person_key(pid) not in replay

    if allowed is not None:
        calls = {**calls, "calls": [c for c in calls["calls"] if c["subject_id"] in allowed]}
    day = date.fromisoformat(calls["as_of"])
    as_of = calls["as_of"] + "T23:59:59+00:00"
    store = today.source(mode)[0]
    people, seen = [], set()
    for c in calls["calls"]:  # one person, one role: their strongest, as route.py cards them
        if (key := contact.person_key(c["subject_id"])) not in seen:
            seen.add(key)
            people.append(c)
    found = []
    for role_id in dict.fromkeys(c["role"]["id"] for c in people):
        mine = [c for c in people if c["role"]["id"] == role_id]
        reach, check, held = [], [], []
        for c in mine:
            if c["action"] not in ("reach_now", "verify_first"):
                continue
            routed = c["route"] is not None  # Today's calls already ran the gate on a reach it routed
            gate = c["gate"] if routed else _gate(store, c, as_of)
            if gate and gate["state"] == "hold":
                held.append({"name": c["name"], "reason": gate["reason"]})
            elif gate or c["action"] == "verify_first":
                check.append(_person(c, gate["reason"] if gate else None))
            else:
                reach.append(c)
        found.append((mine, reach, check, held, contact.room(store, role_id, as_of)))
    # Each role's cap, then the week's cap across every role (contact.TOTAL_CAP), which the strongest take, as
    # route.py cards them: people are strongest first (Today's calls orders them as routing.rank does).
    strongest = {id(c): n for n, c in enumerate(people)}
    reach_all = sorted((c for _, reach, _, _, room in found for c in reach[:room]),
                       key=lambda c: strongest[id(c)])[:contact.room_all(store, as_of)]
    kept, roles = {id(c) for c in reach_all}, []
    for mine, reach, check, held, room in found:
        shown = [c for c in reach if id(c) in kept]
        roles.append({
            "id": mine[0]["role"]["id"], "title": mine[0]["role"]["title"], "pay": mine[0]["role"]["pay"],
            "reach": [_person(c) for c in shown], "more": len(reach) - len(shown),
            "more_ids": [contact.person_key(c["subject_id"]) for c in reach if id(c) not in kept],  # the one-pager's count
            "cap_used": contact.WEEKLY_CAP - room, "all_used": len(shown) < min(room, len(reach)),
            "check_first": check, "held": held})
    # A wait the ledger holds past its end is nothing to decide: the gate as a card meets it, where a card holds for
    # good (events.ledger_hold lets an old card go, since an invite is not a card).
    ending = [{"name": c["name"], "role": c["role"]["title"], "until": c["until"], "headline": c["headline"]}
              for c in people if c["action"] in ("watch_until", "respect_follow_up") and c["until"]
              and not c.get("hold_only") and date.fromisoformat(c["until"]) < day + WEEK
              and contact.check([e for e in contact.history(store, c["subject_id"]) if e["kind"] != "unconfirmed"],
                                _after(c["until"])).state == "clear"]  # a doubt about school holds no wait
    ev = events.page(mode)
    last = ev.get("last") or {}
    due = [{"subject_id": f["subject_id"], "name": f["name"], "from": f["host"], "due": f["due"], "overdue": f["overdue"],
            "event_id": last["event"]["id"], "draft": f["draft"], "invites_to_id": f.get("invites_to_id")}
           for f in (last.get("follow_ups") or []) if not f["sent"] and not f["hold"] and named(f["subject_id"])]
    due += [{"subject_id": pid, "name": names.get(pid, pid), "from": None, "due": calls["as_of"], "overdue": False,
             "reason": reason}
            for pid, reason in contact.follow_ups_due(store, as_of) if named(pid)]
    nxt = ev.get("next")
    # One ask per person: whoever the brief already says to reach, check first or follow up with gets no separate
    # invite (a follow-up's draft carries it).
    asked = {contact.person_key(c["subject_id"]) for c in reach_all} | {
        contact.person_key(p["subject_id"]) for r in roles for p in r["check_first"]} | {
        contact.person_key(f["subject_id"]) for f in due}
    w = {"as_of": calls["as_of"], "week_of": _monday(day).isoformat(), "source": calls["source"], "missing": None,
         "ledger_problem": contact.unreadable(store),  # the gate holds everyone on it
         "people_problem": unlisted,  # nobody is named until the people file reads
         "roles": roles, "ending": sorted(ending, key=lambda e: e["until"]), "follow_ups": due,
         "event": {"id": nxt["event"]["id"], "name": nxt["event"]["name"], "day": nxt["event"]["day"],
                   "seats_left": nxt["seats_left"], "estimate": nxt["estimate"],
                   "invite": [{"subject_id": p["subject_id"], "name": p["name"], "call": p["call"],
                               "asked": contact.person_key(p["subject_id"]) in asked, "draft": p.get("draft"),
                               "from": p.get("from"), "tie": p.get("tie")}
                              for p in nxt["invite"] if named(p["subject_id"])],
                   "held": len(nxt["held"]),
                   "blocked": nxt.get("blocked") or (unlisted and "Nobody is invited until the people file reads.")}
         if nxt else None,
         "budget": ev.get("budget")}
    return w | {"decisions": decisions(w, mode, reach_all, as_of), "fresh": calls.get("fresh")}


def _after(day):
    """The first moment after a wait's last day: what the ledger says once the wait is over."""
    return f"{date.fromisoformat(day) + timedelta(days=1)}T00:00:00+00:00"


def _item(area, title, why, where, due=None, overdue=False, way_in=None, cost=None, act=None, ping=None, thread=None):
    """One decision. ``ping`` is who the card names to contact (subject and role), recorded when it is posted.
    ``thread`` is what its reply under the Monday post carries (app/weekly.py): a person's card, drafts, buttons."""
    return {"area": area, "title": title, "why": why, "where": where, "due": due, "overdue": overdue,
            "way_in": way_in, "cost": cost, "act": act, "ping": ping, "thread": thread}


def _way_in(route):
    """The way in, in the person's own card's words: who writes, who to ask first, and how."""
    return route["way_in"] if route else None


def _safe(url):
    """An https link Slack can carry whole: nothing that ends the link, breaks the line or starts a mention."""
    return isinstance(url, str) and bool(re.fullmatch(r"https://[^\s|<>\x00-\x1f\x7f]+", url))


def _act(route):
    """One tap to act: the drafted message or the page to write on, opened in its channel."""
    link = (route or {}).get("url") or ""
    return {"label": route.get("open") or "Open the draft", "url": link} if _safe(link) else None


def decisions(w, mode, reach, as_of):
    """The week's decisions, most urgent first: overdue, then by the day each must be made, then by area."""
    day = date.fromisoformat(w["as_of"])
    plan, problem = hiring.load(mode, as_of)
    roles, ec = hiring.costs(mode, plan.assumptions)
    items, first = [], []
    if broken := w.get("ledger_problem"):  # the gate fails closed on it: say why the week is empty
        first.append(_item("Hiring", "Fix the contact ledger", f"{broken} Every reach and invite waits until then.",
                           "Today's calls", overdue=True))
    if unlisted := w.get("people_problem"):
        first.append(_item("Hiring", "Fix the people file", f"{unlisted} Nobody is named to reach, check or invite "
                           "until then.", "Today's calls", overdue=True))
    for c in reach:
        items.append(_item(
            "Hiring", f"Reach {c['name']} for {c['role']['title']}",
            f"{_why(c)}" + (f" ({NOTE_LABEL[c.get('track')]})." if c.get("track") in NOTE_LABEL else "."),
            "Today's calls",
            due=c["closes"], way_in=_way_in(c["route"]), cost=_role_cost(roles.get(c["role"]["id"]), plan, mode),
            act=_act(c["route"]), ping={"subject_id": c["subject_id"], "role_id": c["role"]["id"],
                                        "evidence": c["evidence"], "score": c["score"]},
            thread={"kind": "reach", "subject_id": c["subject_id"]}))
    for r in w["roles"]:
        for p in r["check_first"]:
            items.append(_item("Hiring", f"Check {p['name']} before anyone writes ({r['title']})",
                               f"{_cap(p['why']).rstrip('.')}.",
                               "Today's calls"))
        if r["more"]:
            items.append(_item("Hiring", f"Choose which {r['title']} reach-outs wait",
                               f"{r['more']} more are ready past this week's cap of "
                               + (f"{contact.TOTAL_CAP} cards across all roles." if r["all_used"]
                                  else f"{contact.WEEKLY_CAP} cards for the role."),
                               "Today's calls"))
    found, unread = starts.decisions(mode, day, day + WEEK)
    for s in found or []:
        open_ = [f"{label[:1].lower()}{label[1:]} ({owner or 'no owner yet'}, "
                 f"{'overdue' if when < w['as_of'] else 'due ' + _day(when)})" for label, owner, when in s["items"]]
        items.append(_item("Hiring", f"Get {s['name']} ready to start {_day(s['start'])}", f"Open: {'; '.join(open_)}.",
                           "New starts", due=s["due"], overdue=s["overdue"],
                           thread={"kind": "start", "hire": s["id"], "open": open_, "keys": s["keys"]}))
    if unread:
        items.append(_item("Hiring", "Fix the new starts file", unread, "New starts"))
    for e in w["ending"]:
        items.append(_item("Hiring", f"Decide on {e['name']} ({e['role']})",
                           f"{e['headline']} ends {_day(e['until'])}: the engine looks again then.", "Today's calls",
                           due=e["until"]))
    e = w["event"]
    invitees = {contact.person_key(p["subject_id"]) for p in e["invite"]} if e else set()
    for f in w["follow_ups"]:
        # One ask per person: someone the next event would invite hears about it in their follow-up.
        also = f" Its draft also invites them to {e['name']} ({_day(e['day'])})." \
            if contact.person_key(f["subject_id"]) in invitees else ""
        items.append(_item("Events", f"Send {f['name']} their follow-up" + (f" from {f['from']}" if f["from"] else ""),
                           (f.get("reason") or "They talked with GI at the last event; the draft is ready.") + also,
                           "Events", due=(f["due"] or "")[:10] or None, overdue=f["overdue"],
                           thread={"kind": "follow_up", "subject_id": f["subject_id"], "name": f["name"],
                                   "event_id": f.get("event_id"), "draft": f.get("draft"),
                                   "invites_to_id": f.get("invites_to_id")}))
    if e:
        by = max(date.fromisoformat(e["day"]) - INVITE_LEAD, day).isoformat()
        est = e.get("estimate")
        cost = f"The night costs about {_money(est['mid'])}" if est else None
        invite = [] if w.get("people_problem") else [p for p in e["invite"] if not p["asked"]]  # one ask a week
        if invite:
            names = [p["name"] for p in invite]
            names = _and(names if len(names) <= 3 else names[:3] + [f"{len(names) - 3} more"])
            items.append(_item("Events", f"Approve {len(invite)} {'invite' if len(invite) == 1 else 'invites'} for "
                               f"{e['name']} ({_day(e['day'])})",
                               f"{names}: in town, strongest call first, each with a note ready to send. "
                               f"{e['seats_left']} seats open.",
                               "Events", due=by, cost=cost,
                               thread={"kind": "invites", "event_id": e["id"], "people": invite}))
        elif e.get("blocked") and not w.get("people_problem"):  # else said once, first
            items.append(_item("Events", f"Fix the people file before inviting anyone to {e['name']}", e["blocked"],
                               "Events", due=by))
    b = w["budget"]
    if b and b["left"] < 0:
        items.append(_item("Budget", f"Event spend is {_money(-b['left'])} over for {b['period']}",
                           f"{budget_line(b)} Cut the next event's cost or raise the budget.", "Events"))
    paid = hiring.payroll(mode, plan.assumptions)
    pr = hiring.project(plan, roles, ec["per_qualified"] if ec else None, paid)
    if problem:  # the saved plan could not be read: judging the starting values would be judging nothing
        items.append(_item("Budget", "Fix the saved hiring plan", problem, "Hiring budget"))
    elif not plan.assumptions.yearly_budget and (plan.lines or (paid and paid["people"])):
        items.append(_item("Budget", "Set the yearly people budget",
                           "Nothing says yet whether payroll and the planned hires fit.", "Hiring budget"))
    elif pr["over"]:
        items.append(_item("Budget", f"The {plan.assumptions.year} hiring plan is {_money(-pr['left'])} over budget",
                           f"Payroll and planned hires come to {_money(pr['total'])} of {_money(pr['budget'])}. "
                           "Move a start month back, drop a hire, or raise the budget.", "Hiring budget"))
    if paid and paid["error"]:
        items.append(_item("Budget", "Fix the payroll export", paid["error"], "Hiring budget"))
    if n := pr["uncosted_hires"]:
        items.append(_item("Budget", f"Cost {n} planned {'hire' if n == 1 else 'hires'}",
                           "A role in the plan has no salary estimate or event cost yet, so the totals leave it out.",
                           "Hiring budget"))
    return first + sorted(items, key=lambda i: (not i["overdue"], i["due"] or "9999-12-31", AREAS.index(i["area"])
                                        if i["area"] in AREAS else len(AREAS), i["title"]))


def _role_cost(role, plan, mode):
    """The role's first-year cost to GI, labelled as the role's and as an estimate: never the person's pay."""
    if not role or role["first_year"] is None:
        return None
    basis = ("midpoint of GI's posted range" if role["posted"] else
             "invented salary estimate" if mode == "simulation" else "your salary estimate")
    return (f"Role's first-year cost to GI: about {_money(role['first_year'])} ({basis} plus "
            f"{plan.assumptions.overhead_pct:g}% overhead and hiring)")


def _day(iso):
    """ "Sep 17", as a person reads a date; the brief is always about the weeks just ahead."""
    return f"{date.fromisoformat(iso[:10]):%b} {int(iso[8:10])}"


def _cap(text):
    return text[:1].upper() + text[1:]


def _why(c):
    return _cap((c["trigger"] or {}).get("what") or c["why_now"] or "The engine says reach out now")


def _and(items):
    return ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else "".join(items)


def _gate(store, c, as_of):
    """The ledger's word on someone Today's calls did not route: a check first, or a reach for a role not in config."""
    if c["action"] == "reach_now":
        return {"state": "check_first", "reason": "Its role is not in config/roles.json, so nothing was drafted or routed."}
    clearance = contact.check(contact.history(store, c["subject_id"]), as_of, role_id=c["role"]["id"])
    return None if clearance.state == "clear" else {"state": clearance.state, "reason": clearance.reason}


NOTE_LABEL = {today.NOTE: "a note that leads with their work", today.STUDENT: "their work only: an undergraduate",
              today.DEAL: "no role named: their company was just acquired"}


def _person(c, reason=None):
    return {"subject_id": c["subject_id"], "name": c["name"],
            "why": reason or (c["trigger"] or {}).get("what") or c["why_now"], "next_step": c["next_step"],
            "note": "" if reason else NOTE_LABEL.get(c.get("track"), "")}  # a reach that is a note, not a pitch


def _money(v):
    """Whole dollars, halves rounded up as the web page rounds them (Python's format rounds half to even)."""
    return f"${math.floor(v + 0.5):,}" if v >= 0 else f"-{_money(-v)}"


def budget_line(b):
    """The budget in one sentence: spent, still to come for each event left in the period, and what is left."""
    later = "".join(f", {_money(p['to_come'])} still to come for {p['name']}" if p["to_come"] is not None
                    else f", no estimate yet for {p['name']}" for p in b["planned"])
    left = f"{_money(b['left'])} left" if b["left"] >= 0 else f"{_money(-b['left'])} over"
    return f"{b['period']}: {_money(b['spent'])} of {_money(b['amount'])} spent{later}; {left}."


LINE_MAX = 280  # characters per line of the card


def _clip(text, n=LINE_MAX):
    return text if len(text) <= n else text[:n - 1].rsplit(" ", 1)[0] + "…"


def fresh(w):
    """When the week's data was pulled, and a warning when it is old (today.freshness); {} for the invented week,
    whose cards say it is invented."""
    return {} if (w.get("source") or "").startswith("Invented") else w.get("fresh") or {}


def pull_warning(fresh):
    """A card's first section when the data is old or the last pull broke, so a broken pull never reads as a quiet
    week; none otherwise."""
    warning, fix = fresh.get("warning"), fresh.get("fix")
    how = f" On the Mac: `{routing._esc(fix)}`" if fix else ""
    return [routing._section(f"*Check the pull first.* {routing._esc(warning)}{how}")] if warning else []


def card(w):
    """The Monday brief as a Slack Block Kit card for GI's own channel, or None when there is nothing to
    decide: it stays silent then."""
    esc = routing._esc
    items = w["decisions"]
    if not items:
        return None
    head = f"Monday brief: week of {w['week_of']}"
    invented, pulled = (w.get("source") or "").startswith("Invented"), fresh(w)
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": head}}, *pull_warning(pulled),
              routing._section(f"*{len(items)} {'thing' if len(items) == 1 else 'things'} worth your attention, "
                               "most urgent first.*")]
    for n, i in enumerate(items[:BRIEF_MAX], start=1):
        when = " · *overdue*" if i["overdue"] else f" · by {_day(i['due'])}" if i["due"] else ""
        lines = [f"*{n}. {esc(_clip(i['title'], 150))}*{when}", esc(_clip(i["why"]))]
        if i["way_in"]:
            lines.append(f"_Way in:_ {esc(_clip(i['way_in']))}")
        cost = [esc(i["cost"])] if i["cost"] else []
        act = i["act"] if i["act"] and _safe(i["act"].get("url")) else None  # whoever built the item
        text = "\n".join(lines + [" · ".join(cost + [f"<{act['url']}|{esc(act['label'])}>"])]) if act else ""
        if not text or len(text) > routing.SLACK_TEXT_MAX:  # a draft too long to link whole opens from the page
            text = "\n".join(lines + [" · ".join(cost + [f"On {esc(i['where'])}"])])
        blocks.append(routing._section(text))
    more = f"{len(items) - BRIEF_MAX} more on the Monday brief page. " if len(items) > BRIEF_MAX else ""
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                   f"{more}From Today's calls, the Events page and the Hiring budget, as of {w['as_of']}. "
                   + (f"{esc(pulled['line'])} " if pulled.get("line") else "")
                   + ("Invented people and numbers. " if invented else "") +
                   "This card contacts no one: every draft is sent by a person."}]})
    return {"text": head, "blocks": blocks}
