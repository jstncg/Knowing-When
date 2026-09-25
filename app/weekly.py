"""The week in GI's one Slack channel (#weekly-recap), so ops never has to open another page.

Every Monday the brief (app/inbox.py) goes out as one short post: the few decisions, most urgent first. Whatever a
decision needs to act on goes in that post's thread, one reply each: a person's card with its ledger buttons, the
next event's invites and a follow-up with their drafts and an "I sent it" button, a new start's open items with a
Done button each. On the first Monday of a month the founders' one-pager (app/month.py) follows as one more post.
A week with nothing to decide stays silent, unless the pull is broken (no pull, data over 2 days old, the last daily
run failed or stopped short, or pulling paused): then one plain line says what is wrong and the command that checks or
fixes it (pull_alert). The daily run posts the same line once per problem (alert_once).

Everything posts through routing (send for a person's card, send_digest for the rest), so the pause switch, the
gates and the caps hold, and each card sent is recorded in the ledger; the post names only the people routing.sendable
keeps, so it never names someone whose card then stays back. Through the Slack app (scripts/slack_app.py run
--monday) the replies go in the post's thread; a webhook can't thread, so there they follow the post. It contacts no
one: every draft is sent by a person, and each button records what that person did, in the contact ledger or on New
starts. A visa never reaches Slack (starts.PRIVATE).
"""

import json
from datetime import date

from . import contact, events, inbox, month, routing, starts, today
from .models import iso

ACTIONS = {"ops_invited": "Invited", "ops_followed": "I sent it", "ops_done": "Done"}  # the buttons in the thread
SAID = {"ops_invited": "{by} invited {who}", "ops_followed": "{by} sent {who} their follow-up",
        "ops_done": "{who}: done, per {by}"}


def _header(text):
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150]}}


def _context(text):
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:3000]}]}


def _when(i):
    return " · *overdue*" if i["overdue"] else f" · by {inbox._day(i['due'])}" if i["due"] else ""


def _title(i, n):
    return f"*{n}. {routing._esc(inbox._clip(i['title'], 150))}*{_when(i)}"


def post(w):
    """The Monday post: each decision in a line or two, or None when there is nothing to decide."""
    items = w["decisions"][:inbox.BRIEF_MAX]
    if not items:
        return None
    head = f"Monday brief: week of {w['week_of']}"
    rest, pulled = w["decisions"][inbox.BRIEF_MAX:], inbox.fresh(w)
    blocks = [_header(head), *inbox.pull_warning(pulled), routing._section(
        f"*{len(w['decisions'])} {'thing' if len(w['decisions']) == 1 else 'things'} worth your attention, most urgent "
        "first.* What to act on is in this post's thread: each person's card, each draft and its button.")]
    blocks += [routing._section(f"{_title(i, n)}\n{routing._esc(inbox._clip(i['why']))}")
               for n, i in enumerate(items, start=1)]
    blocks.append(_context(_later(rest)
                           + f"From Today's calls, the Events page, New starts and the Hiring budget, as of {w['as_of']}. "
                           + (f"{routing._esc(pulled['line'])} " if pulled.get("line") else "")
                           + ("Invented people and numbers. " if (w.get("source") or "").startswith("Invented") else "")
                           + "This post contacts no one: every draft is sent by a person."))
    return {"text": head, "blocks": blocks}


def pull_alert(fresh):
    """One plain line for GI's channel when the pull is broken (today.freshness: no pull yet, data over 2 days old, the
    last run failed or stopped short, or pulling paused), with the one command that checks or fixes it. None
    otherwise: a plain quiet week stays silent, so silence still means nothing new."""
    if not fresh.get("broken"):
        return None
    verb = "To resume" if fresh["broken"] == "paused" else "To check"
    text = f"Check the pull: {fresh['warning']} {verb}, on the Mac: {fresh['fix']}"
    return {"text": text, "blocks": [routing._section(
        f"*Check the pull.* {routing._esc(fresh['warning'])} {verb}, on the Mac: `{routing._esc(fresh['fix'])}`")]}


def alert_once(fresh, to, state):
    """The pull alert, once per problem (what is broken and why, kept in ``state``, which the daily run and the Monday
    post share); the state goes once the pull is fine. The line posted, or None."""
    if (card := pull_alert(fresh)) is None:
        state.unlink(missing_ok=True)
        return None
    key = f"{fresh['broken']}:{fresh.get('why')}"
    try:
        said = json.loads(state.read_text()).get("said")
    except (OSError, ValueError, AttributeError):  # none yet, or unreadable: say it
        said = None
    if said == key:
        return None
    routing.send_digest(card, to)
    state.write_text(json.dumps({"said": key}))
    return card["text"]


def _later(rest):
    """What the post leaves out, and when it comes: a reach in the next morning's list (the daily run posts none on
    a Monday, so Tuesday's carries it, within the caps), anything else in next week's post."""
    reach = sum((i["thread"] or {}).get("kind") == "reach" for i in rest)
    wait = len(rest) - reach
    return ((f"{reach} more {'person' if reach == 1 else 'people'} to reach go in the next morning's list, within the "
             "week's cap. " if reach else "")
            + (f"{wait} more {'waits' if wait == 1 else 'wait'} for next week. " if wait else ""))


def _button(action_id, value):
    return {"type": "button", "action_id": action_id, "text": {"type": "plain_text", "text": ACTIONS[action_id]},
            "value": json.dumps(value)}


def _press(buttons, action_id, value):
    """A row's button, as the row's accessory; nothing without buttons."""
    return {"accessory": _button(action_id, value)} if buttons else {}


def _draft(draft, sender=None):
    """A draft as a quote, to copy and send by hand."""
    body = "\n".join(f">{routing._esc(line)}" for line in draft["body"].split("\n"))
    return f"_Draft{f' from {routing._esc(sender)}' if sender else ''}:_ *{routing._esc(draft['subject'])}*\n{body}"


def reply(i, n, mode, buttons=True):
    """The reply under the Monday post for its ``n``-th decision ``i``, when there is something to act on that names
    nobody new: a new start's open items, the next event's invites, a follow-up. None for a reach (its card goes
    through routing.send, in ``publish``) and for what the post says in full (a check, a budget line). ``buttons``:
    False for a webhook, since only the Slack app hears a press."""
    t, test = i["thread"] or {}, int(mode == "simulation")
    if t.get("kind") == "start":
        rows = [routing._section(_title(i, n))]
        for k, (key, said) in enumerate(zip(t["keys"], t["open"])):
            if key in starts.PRIVATE:  # starts.decisions never lists it; never on Slack even if it did
                continue
            rows.append({**routing._section(routing._esc(inbox._cap(said))), "block_id": f"ops:{k}",
                         **_press(buttons, "ops_done", {"m": mode, "t": test, "h": t["hire"], "k": key,
                                                        "n": starts.ITEMS[key][0][:60]})})
    elif t.get("kind") == "follow_up":
        value = {"m": mode, "t": test, "p": t["subject_id"], "n": t["name"][:80], "e": t.get("event_id"),
                 "i": t.get("invites_to_id")}
        rows = [routing._section(f"{_title(i, n)}\n{routing._esc(i['why'])}")]
        rows += [routing._section(_draft(t["draft"]))] if t.get("draft") else []
        rows += [{"type": "actions", "block_id": "ops:0", "elements": [_button("ops_followed", value)]}] if buttons else []
    elif t.get("kind") == "invites":
        rows = [routing._section(f"{_title(i, n)}\n{routing._esc(i['why'])}")]
        for k, p in enumerate(t["people"][:10]):  # a message holds 50 blocks
            tie = f" · {routing._esc(p['tie'])}" if p.get("tie") else ""
            rows.append({**routing._section(f"*{routing._esc(p['name'])}*: {routing._esc(p['call'])}{tie}"),
                         "block_id": f"ops:{k}",
                         **_press(buttons, "ops_invited", {"m": mode, "t": test, "e": t["event_id"],
                                                           "p": p["subject_id"], "n": p["name"][:80]})})
            if p.get("draft"):
                rows.append(routing._section(_draft(p["draft"], p.get("from"))))
    else:
        return None
    if test:  # the simulation's: marked, as the post is, since a webhook shows it apart from the post
        rows.append(_context("Test: invented people, and nothing here is real."
                             + (" Its buttons record nothing." if buttons else "")))
    return {"text": f"{'Test: ' if test else ''}{i['title']}", "blocks": rows}


def week(mode, now):
    """The week's brief with each reach's route, keeping only the reaches routing.sendable would post now, so the post
    never names someone whose card then stays back. (brief, {subject id: route})."""
    kept = {}
    w = inbox.week(mode, today.calls(mode, keep=kept))
    if w["missing"]:
        return w, {}
    reach = [i for i in w["decisions"] if (i["thread"] or {}).get("kind") == "reach"]
    routes = [kept[i["thread"]["subject_id"]] for i in reach if i["thread"]["subject_id"] in kept]
    ok = {r.subject_id: r for r in routing.sendable(today.source(mode)[0], routes, now)}
    return {**w, "decisions": [i for i in w["decisions"] if i not in reach or i["thread"]["subject_id"] in ok],
            "roles": [{**r, "reach": [p for p in r["reach"] if p["subject_id"] in ok]} for r in w["roles"]]}, ok


def brief(mode):
    """The week as every surface shows it (the web brief, the Home tab, the Monday post): ``week`` as of the
    workspace's now."""
    return week(mode, (today.source(mode) or (None, iso()))[1])[0]


def one_pager(mode):
    """The founders' one-pager as one post, or None when there is nothing to judge yet."""
    m = month.month(mode)
    if m["missing"]:
        return None
    first, *parts = month.text(m).split("\n\n")
    headline = first.split("\n", 1)[1] if "\n" in first else ""
    head = f"Monthly one-pager: {m['month']}"
    blocks = [_header(head), routing._section(f"*{routing._esc(headline)}*")]
    for part in parts[:-1]:
        title, _, rest = part.partition("\n")
        blocks.append(routing._section(f"*{routing._esc(title)}*\n{routing._esc(rest)}"))
    blocks.append(_context(routing._esc(parts[-1]) if parts else ""))
    card = {"text": head, "blocks": blocks}
    return routing.as_test(card) if m["invented"] else card


def first_monday(day):
    return day.weekday() == 0 and day.day <= 7


def publish(to, mode, day=None):
    """Post the week to GI's channel (``to``: the Slack app's poster, else a webhook URL), and on a month's first
    Monday the one-pager. False when nothing went out. Once the post is up, a reply that fails is printed rather than
    raised, so the post never goes out twice."""
    day = day or date.today()
    store, now = (today.source(mode) or (None, None))[:2]
    test, posted = mode == "simulation", False
    with routing.sending(store):  # one sender at a time: a morning list going out now finishes first, and counts
        if store is not None and not test:
            now = iso()  # as of once it has
        w, routes = week(mode, now)
        if w["missing"]:
            raise ValueError(w["missing"])
        card = post(w)
        if card is None:  # nothing to decide: silent, unless the pull is broken and the daily run hasn't said so already
            posted = bool(alert_once(inbox.fresh(w), to, today.PULL_ALERT)) if not test else False
        else:
            up = routing.send_digest(routing.as_test(card) if test else card, to)
            under = up.get("ts") if isinstance(up, dict) else None  # a webhook can't thread: replies follow the post
            for n, i in enumerate(w["decisions"][:inbox.BRIEF_MAX], start=1):
                t = i["thread"] or {}
                try:
                    if t.get("kind") == "reach":  # the person's card, with its ledger buttons; recorded as a card sent
                        route = routes[t["subject_id"]]
                        if test:
                            route = route.model_copy(update={"card": routing.as_test(route.card, route.name)})
                        routing.send(store, route, to, now, test=test, thread=under)
                    elif (card := reply(i, n, mode, buttons=not isinstance(to, str))) is not None:
                        routing.send_digest(card, to, thread=under)
                except Exception as e:  # noqa: BLE001 - the post is up: never post it twice
                    print(f"Reply {n} under the Monday post didn't go out: {_why(e)}")
            posted = True
    if first_monday(day):
        try:  # built here too: once the week is up, nothing after it may raise, or the week would go out again
            if (page := one_pager(mode)) is not None:
                routing.send_digest(page, to)
                posted = True
        except Exception as e:  # noqa: BLE001 - the week is out already: never post it twice
            if not posted:
                raise
            print(f"The monthly one-pager didn't go out: {_why(e)}")
    return posted


def _why(e):
    """What stopped a reply: the gates' and Slack's own words (never a token in either), else the type only."""
    return str(e) if isinstance(e, (ValueError, RuntimeError)) else type(e).__name__


def press(app, payload):
    """A thread button: record what the person who pressed it did, then swap the button for a line saying so."""
    action = payload["actions"][0]
    v, user = json.loads(action["value"]), payload["user"]
    kind = action["action_id"]
    by = app.names.get(user["id"]) or user.get("name") or user.get("username") or user["id"]
    who = routing._esc(v["n"] if kind == "ops_done" else routing._first(v["n"]))
    mention = f"<@{user['id']}>"
    line = f"{SAID[kind].format(by=mention, who=who)}, {app.today()}."
    if v.get("t"):
        line = f"Test, nothing recorded: {line}"
    elif v.get("m") != app.mode:
        line = f"Not recorded: this post is from the {v.get('m')} week, and the app is running the {app.mode} one."
    where = (payload["container"]["channel_id"], payload["container"]["message_ts"])
    message = payload.get("message") or {}
    with app.lock:  # one press at a time, record and reply: two quick presses never record twice or undo each other
        if not v.get("t") and v.get("m") == app.mode:
            try:
                if kind == "ops_invited":
                    events.mark(v["m"], v["e"], v["p"], "invited", by)
                elif kind == "ops_followed" and v.get("e"):
                    events.mark(v["m"], v["e"], v["p"], "sent", by, also_invite=v.get("i"))
                elif kind == "ops_followed":  # the ledger's one follow-up after a note with no reply
                    _follow_up(app.store, v["p"], app.clock(), by)
                elif v["k"] in starts.PRIVATE:
                    raise ValueError("only on the New starts page")
                else:
                    starts.update(v["m"], v["h"], v["k"], state="done")
            except (ValueError, LookupError) as e:
                line = f"Not recorded ({routing._esc(str(e))}): {line}"
        blocks, text = app.cards.setdefault(where, (message.get("blocks") or [], message.get("text") or ""))
        blocks = _done(blocks, action["block_id"], line, keep=line.startswith("Not recorded"))
        app.cards[where] = (blocks, text)
        return app.slack.call("chat.update", channel=where[0], ts=where[1], text=text or "Monday brief",
                              blocks=blocks)


def _follow_up(store, person_id, now, by):
    """Record the ledger's one follow-up while it is still due, as the team and role of the note it answers."""
    if store is None:
        raise ValueError("no timing store on this machine")
    if person_id not in dict(contact.follow_ups_due(store, now)):
        raise ValueError("no follow-up is due for them now")
    note = contact._last_sent([e for e in store.all("contact") if e["person_id"] == person_id], now)
    contact.record(store, person_id, "follow_up", at=now, by=by, team=note.get("team", "recruiting"),
                   role_id=note.get("role_id"))


def _done(blocks, block_id, line, keep=False):
    """The reply's blocks with ``line`` under the pressed row, and the row's button gone (kept when nothing was
    recorded, so it can be pressed again)."""
    out = []
    for b in blocks:
        if b.get("block_id") != block_id:
            out.append(b)
            continue
        if not keep and b["type"] == "section":
            out.append({k: v for k, v in b.items() if k != "accessory"})
        elif keep:
            out.append(b)
        out.append(_context(line))
    return out
