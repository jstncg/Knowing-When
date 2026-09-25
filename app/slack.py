"""GI's Slack app: cards posted as the app, buttons on them that write to the contact ledger, and a Home tab.

It talks to Slack over Socket Mode, a connection the app opens from the Mac, so nothing on the Mac is open to the
internet. The buttons on a person's card write what happened to the contact ledger (app/contact.py):
  I sent it          sent, by whoever pressed it; the card then offers They replied and Not now
  They replied       replied: a conversation is open, so no new approach
  Not now            not_now, until the day they asked us to wait for (a form asks for it)
  Do you know them?  knows: who at GI knows them and how (a form asks); their next card's way in names them
A test card's buttons record nothing. The Home tab shows the week (app/inbox.py): per role who to reach, who to check
first and who is held, the waits ending, the next event and the follow-ups due.

The tokens come only from load(): the environment, else the repo's .env. Nothing here prints or logs one, and no error
message carries one. Setup: docs/slack-app-setup.md.
"""

import asyncio
import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from dotenv import dotenv_values

from . import contact, inbox, routing, weekly
from .models import iso, parse_time

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "slack-app-manifest.json"
API = "https://slack.com/api/"
NAMES = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL")
PREFIX = {"SLACK_BOT_TOKEN": "xoxb-", "SLACK_APP_TOKEN": "xapp-"}
LEDGER, LOG = "ledger", "ledger_log"  # the card's block of ledger buttons, and the lines saying what was recorded
LABEL = {"sent": "I sent it", "replied": "They replied", "not_now": "Not now", "know_them": "Do you know them?"}
NEXT = {"sent": ("replied", "not_now"), "replied": (), "not_now": (), "know_them": None}  # None: the buttons stay
HOW = ("Worked together", "Studied together", "Met them", "Friends")
WAIT_DAYS = 90  # the Not now form's first guess at the day they asked for
HOME_FRESH = 600  # seconds the Home tab's week is reused before it is built again
MONDAY_HOUR = 9  # the brief goes out on Monday from this hour, the Mac's local time
# What to do about Slack's commonest refusals, in plain words.
HINT = {"not_in_channel": "invite the app to the channel: /invite @GI Timing",
        "channel_not_found": "SLACK_CHANNEL should be the channel's ID, such as C0123ABCD (channel details, bottom)",
        "invalid_auth": "the token is wrong, or was regenerated: copy it again",
        "not_authed": "no token was sent", "token_revoked": "the token was revoked: make a new one",
        "missing_scope": "the app lacks a permission: in the app's settings, App Manifest, paste "
                         "config/slack-app-manifest.json again and save, then Install App, Reinstall to Workspace"}
# An app-level token (xapp-) that lacks a scope is fixed only by a new token, never by the manifest.
APP_HINT = {"missing_scope": "the app token lacks connections:write: in Basic Information, App-Level Tokens, "
                             "generate a new token with that scope and put it in SLACK_APP_TOKEN"}


def load(root=ROOT):
    """{name: value} for the app's three settings: the environment first, else the repo's .env; '' when unset. A
    setting in the environment wins even when empty, so SLACK_BOT_TOKEN= turns the app off for one run."""
    env = dotenv_values(root / ".env")
    found = {n: (os.environ[n] if n in os.environ else env.get(n)) or "" for n in NAMES}
    for name, prefix in PREFIX.items():
        if found[name] and not found[name].startswith(prefix):
            raise ValueError(f"{name} should start with {prefix}: check which token went where")
    return found


class SlackError(RuntimeError):
    pass


class Slack:
    """Slack's Web API with one token. An error names the method and Slack's error code, never the token."""

    def __init__(self, token, client=None):
        if not token:
            raise ValueError("No Slack token set")
        self._token = token
        self.http = client or httpx.Client(timeout=15)

    def __repr__(self):
        return "Slack(token hidden)"

    def call(self, method, **payload):
        headers = {"Authorization": f"Bearer {self._token}"}
        if payload:
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            response = self.http.post(API + method, headers=headers,
                                      content=json.dumps(payload) if payload else None)
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:  # the type only: nothing of the request goes in the message
            raise SlackError(f"Slack {method}: {type(e).__name__}") from None
        if not data.get("ok"):
            error = data.get("error") or str(response.status_code)
            hints = {**HINT, **APP_HINT} if self._token.startswith(PREFIX["SLACK_APP_TOKEN"]) else HINT
            raise SlackError(f"Slack {method}: {error}" + (f" ({hints[error]})" if error in hints else ""))
        return data


class Poster:
    """Posts to GI's channel as the app. routing.send and routing.send_digest take one in place of a webhook URL;
    a person's card gets the ledger buttons."""

    def __init__(self, slack, channel):
        if not channel:
            raise ValueError("SLACK_CHANNEL is not set: the channel's ID, such as C0123ABCD")
        self.slack, self.channel = slack, channel

    def post(self, card, route=None, test=False, thread=None):
        """``thread``: the ts of the message to post under (the morning list), else a message of its own."""
        blocks = card["blocks"]
        if route is not None:  # above the card's last line, which says nothing is sent until a person sends it
            at = len(blocks) - 1 if blocks and blocks[-1]["type"] == "context" else len(blocks)
            blocks = [*blocks[:at], buttons(route, test), *blocks[at:]]
        sent = self.slack.call("chat.postMessage", channel=self.channel, text=card["text"], blocks=blocks,
                               unfurl_links=False, unfurl_media=False, **({"thread_ts": thread} if thread else {}))
        return {"channel": sent.get("channel"), "ts": sent.get("ts")}


def poster(settings=None):
    """A Poster when the app is set up (SLACK_BOT_TOKEN and SLACK_CHANNEL), else None, and --send uses the webhook."""
    settings = settings or load()
    if settings["SLACK_BOT_TOKEN"] and settings["SLACK_CHANNEL"]:
        return Poster(Slack(settings["SLACK_BOT_TOKEN"]), settings["SLACK_CHANNEL"])
    return None


def _text(text):
    return {"type": "plain_text", "text": text}


def _button(action_id, value, style=None):
    return {"type": "button", "action_id": action_id, "value": value, "text": _text(LABEL.get(action_id, "Refresh")),
            **({"style": style} if style else {})}


def _actions(value, ids):
    return {"type": "actions", "block_id": LEDGER, "elements": [_button(i, value) for i in ids]}


def buttons(route, test=False):
    """The ledger buttons for a person's card. Each carries who the card is about, never anything secret."""
    value = json.dumps({"p": route.subject_id, "r": route.role["id"], "n": route.name[:80], "t": int(test)})
    return _actions(value, ("sent", "not_now", "know_them"))


def logged(blocks, line, actions=None):
    """The card's blocks with ``line`` added to what was recorded, above the ledger buttons, and those buttons
    swapped for ``actions`` (a list of blocks; [] removes them; None leaves them as they are)."""
    log = next((b for b in blocks if b.get("block_id") == LOG), None)
    text = (f"{log['elements'][0]['text']}\n" if log else "") + line
    new = {"type": "context", "block_id": LOG, "elements": [{"type": "mrkdwn", "text": text[-3000:]}]}
    rest = [b for b in blocks if b.get("block_id") not in (LOG, LEDGER)]
    current = [b for b in blocks if b.get("block_id") == LEDGER]
    at = next((i for i, b in enumerate(blocks) if b.get("block_id") in (LOG, LEDGER)), None)
    if at is None:
        at = len(rest) - 1 if rest and rest[-1]["type"] == "context" else len(rest)
    return [*rest[:at], new, *(current if actions is None else actions), *rest[at:]]


def form(kind, about, channel, ts, today, thread=None):
    """The form a button opens: Not now asks for the day they gave, Do you know them? asks how. ``thread``: the
    thread the card is in (the morning list's), where a line about it goes if the card was not kept."""
    first = routing._first(about["n"])
    meta = json.dumps({"about": about, "channel": channel, "ts": ts, **({"thread": thread} if thread else {})})
    note = {"type": "input", "block_id": "note", "optional": True,
            "label": _text("What they said (optional)" if kind == "not_now" else "Anything else (optional)"),
            "element": {"type": "plain_text_input", "action_id": "text", "multiline": True, "max_length": 500}}
    if kind == "not_now":
        title, said = "Not now", ("Every card about them waits until that day. Then the end of the wait counts as a "
                                  "reason to write.")
        ask = {"type": "input", "block_id": "until", "label": _text(f"{first} asked us to wait until"),
               "element": {"type": "datepicker", "action_id": "day",
                           "initial_date": (date.fromisoformat(today) + timedelta(days=WAIT_DAYS)).isoformat()}}
    else:
        title, said = "Do you know them?", "Their next card will say to ask you first."
        ask = {"type": "input", "block_id": "how", "label": _text(f"How do you know {first}?"),
               "element": {"type": "radio_buttons", "action_id": "pick",
                           "options": [{"text": _text(h), "value": h} for h in HOW]}}
    if about.get("t"):
        said = "This is a test card: nothing will be recorded."
    return {"type": "modal", "callback_id": kind, "private_metadata": meta, "title": _text(title),
            "submit": _text("Save"), "close": _text("Cancel"),
            "blocks": [{"type": "context", "elements": [{"type": "mrkdwn", "text": said}]}, ask, note]}


def home(mode):
    """The Home tab: the week's brief as of now (weekly.brief, the week the Monday post names), with a refresh
    button. The invented people's week is marked as a test."""
    week = weekly.brief(mode)
    if week["missing"]:
        blocks = [{"type": "header", "text": _text("GI this week")}, routing._section(routing._esc(week["missing"]))]
    elif (card := inbox.card(week)) is None:  # nothing to decide: say how fresh the data is, so a broken pull
        fresh = week.get("fresh") or {}         # never reads as a quiet week
        blocks = [{"type": "header", "text": _text(f"Monday brief: week of {week['week_of']}")},
                  routing._section(" ".join(["Nothing to decide this week.", *(
                      [routing._esc(fresh["line"])] if fresh.get("line") else [])])), *inbox.pull_warning(fresh)]
    else:
        blocks = (routing.as_test(card) if mode == "simulation" else card)["blocks"]
    refresh = {"type": "actions", "elements": [_button("home_refresh", "home")]}
    return {"type": "home", "blocks": [*blocks[:99], refresh]}


def monday_due(now, last_week):
    """The Monday a brief is due for (its ISO day), when it is Monday from MONDAY_HOUR and this week's has not gone."""
    week = (now.date() - timedelta(days=now.weekday())).isoformat()
    return week if now.weekday() == 0 and now.hour >= MONDAY_HOUR and week != last_week else None


def _day(day):
    return datetime.strptime(day, "%Y-%m-%d").strftime("%-d %B %Y")


class App:
    """What the listener does with each thing Slack sends: a button pressed, a form saved, the Home tab opened.

    handle() answers at once (Slack wants an answer within three seconds) and returns the work to do after.
    ``store`` is the timing run's, whose ledger routing.send reads; None when this machine has none, and then a
    press on a real card says it was not recorded."""

    def __init__(self, slack, store, mode="simulation", team=(), clock=iso):
        self.slack, self.store, self.mode, self.clock = slack, store, mode, clock
        self.names = {m["slack_user_id"]: m["name"] for m in team if m.get("slack_user_id")}
        self.cards = {}  # (channel, ts): (blocks, text), the newest copy of each card this app has seen or written
        self.lock = threading.Lock()  # one card change at a time, so two presses never write over each other
        self.week = None  # (built at, Home view)

    def handle(self, envelope):
        """(the answer's payload or None, the work to do after answering or None)."""
        kind, payload = envelope.get("type"), envelope.get("payload") or {}
        if kind == "events_api":
            event = payload.get("event") or {}
            if event.get("type") == "app_home_opened" and event.get("tab") == "home":
                return None, lambda: self.publish_home(event["user"])
        elif kind == "interactive" and payload.get("type") == "block_actions":
            return None, lambda: self.press(payload)
        elif kind == "interactive" and payload.get("type") == "view_submission":
            return self.submit(payload)
        return None, None

    def today(self):
        """Today where the app runs (the Mac's time zone), as the person pressing a button sees it."""
        return parse_time(self.clock()).astimezone().date().isoformat()

    def press(self, payload):
        action = payload["actions"][0]
        if action["action_id"] == "home_refresh":
            return self.publish_home(payload["user"]["id"], fresh=True)
        if action["action_id"] in weekly.ACTIONS:  # a button in the Monday post's thread: an invite, a follow-up, a start
            return weekly.press(self, payload)
        if action["action_id"] not in LABEL:
            return None  # a link button: Slack opens the link itself
        about = json.loads(action["value"])
        channel, ts = payload["container"]["channel_id"], payload["container"]["message_ts"]
        message = payload.get("message") or {}
        with self.lock:  # only this app changes a card, so a copy it wrote is newer than the one pressed on
            self.cards.setdefault((channel, ts), (message.get("blocks") or [], message.get("text") or ""))
        if action["action_id"] in ("not_now", "know_them"):
            return self.slack.call("views.open", trigger_id=payload["trigger_id"],
                                   view=form(action["action_id"], about, channel, ts, self.today(),
                                             message.get("thread_ts") if message.get("thread_ts") != ts else None))
        line = self.record(action["action_id"], about, payload["user"])
        return self.change((channel, ts), line, action["action_id"], action["value"])

    def submit(self, payload):
        view = payload["view"]
        kind, meta = view.get("callback_id"), json.loads(view.get("private_metadata") or "{}")
        if kind not in ("not_now", "know_them") or "about" not in meta:
            return None, None
        values = {block: next(iter(v.values()), {}) for block, v in view["state"]["values"].items()}
        note = ((values.get("note") or {}).get("value") or "").strip()
        if kind == "not_now":
            until = (values.get("until") or {}).get("selected_date")
            if not until or until <= self.today():
                return {"response_action": "errors", "errors": {"until": "Pick a day after today."}}, None
            fields = {"until": until, "note": note}
        else:
            how = ((values.get("how") or {}).get("selected_option") or {}).get("value")
            if how not in HOW:
                return {"response_action": "errors", "errors": {"how": "Pick one."}}, None
            fields = {"how": how, "note": note}
        return None, lambda: self.saved(kind, meta, payload["user"], **fields)

    def saved(self, kind, meta, user, **fields):
        line = self.record(kind, meta["about"], user, **fields)
        where = (meta["channel"], meta["ts"])
        if where in self.cards:
            return self.change(where, line, kind, None)
        # The card was not kept (the app restarted while the form was open): say it under the card instead.
        return self.slack.call("chat.postMessage", channel=meta["channel"], thread_ts=meta.get("thread") or meta["ts"],
                               text=line)

    def record(self, kind, about, user, until=None, how="", note=""):
        """Write the press to the ledger; the line the card shows for it."""
        mention, first = f"<@{user['id']}>", routing._esc(routing._first(about["n"]))
        line = {"sent": f"{mention} sent it", "replied": f"{first} replied, per {mention}",
                "not_now": f"Not now: {first} asked us to wait until {_day(until) if until else ''}, per {mention}",
                "know_them": f"{mention} knows {first} ({how.lower()}): an intro from them lands better than a "
                             "cold note"}[kind] + f", {_day(self.today())}."
        if about.get("t"):
            return f"Test card, nothing recorded: {line}"
        if self.store is None:
            return f"Not recorded, no timing store on this machine: {line}"
        by = self.names.get(user["id"]) or user.get("name") or user.get("username") or user["id"]
        if kind == "know_them" and user["id"] not in self.names:  # only a teammate on the list is a way in
            line = (f"{mention} knows {first} ({how.lower()}), but isn't matched to GI's team list (their "
                    f"slack_user_id in the team file), so the next card asks to check who that is first, "
                    f"{_day(self.today())}.")
        kind = "knows" if kind == "know_them" else kind
        try:
            contact.record(self.store, about["p"], kind, at=self.clock(), role_id=about["r"], by=by, until=until,
                           note=f"{how}. {note}".strip() if kind == "knows" else note)
        except ValueError as e:  # said on the card, where the person who pressed looks
            return f"Not recorded ({e}): {line}"
        print(f"Recorded {kind.replace('_', ' ')} for {about['n']}, from {by}.")
        return line

    def change(self, where, line, kind, value):
        """Add the line to the newest copy of the card and swap its buttons, one change at a time. A press the ledger
        refused leaves the buttons, so it can be pressed again."""
        with self.lock:
            blocks, text = self.cards[where]
            nxt = None if line.startswith("Not recorded") else NEXT[kind]
            actions = None if nxt is None else [_actions(value or _value(blocks), nxt)] if nxt else []
            blocks = logged(blocks, line, actions)
            done = self.slack.call("chat.update", channel=where[0], ts=where[1], text=text or "Card", blocks=blocks)
            self.cards[where] = (blocks, text)
            return done

    def publish_home(self, user_id, fresh=False):
        if self.mode == "live" and user_id not in self.names:  # the real week: GI's team only
            return self.slack.call("views.publish", user_id=user_id, view={"type": "home", "blocks": [routing._section(
                "This tab shows GI's hiring week to the team. Ask whoever runs GI Timing to add your Slack member ID "
                "to the team file.")]})
        if fresh or not self.week or time.monotonic() - self.week[0] > HOME_FRESH:
            self.week = (time.monotonic(), home(self.mode))
        return self.slack.call("views.publish", user_id=user_id, view=self.week[1])


def _value(blocks):
    """The ledger buttons' value on a card, for the buttons that replace them."""
    ledger = next((b for b in blocks if b.get("block_id") == LEDGER), {"elements": [{}]})
    return ledger["elements"][0].get("value", "{}")


async def _work(job):
    try:
        await asyncio.to_thread(job)
    except (SlackError, ValueError) as e:
        print(f"Could not finish: {e}")
    except Exception as e:  # noqa: BLE001 - one bad press must not stop the app; the type only, in case of secrets
        print(f"Could not finish: {type(e).__name__}")


async def listen(app, app_token, connect=None, forever=True, client=None):
    """Socket Mode: open a connection with the app-level token, answer each envelope at once, then do its work off
    the loop. Reconnects when Slack asks to or the connection drops. Never prints the connection's address."""
    if connect is None:
        from websockets.asyncio.client import connect
    opener, wait, jobs, stop = Slack(app_token, client), 1, set(), False
    while not stop:
        try:
            url = (await asyncio.to_thread(opener.call, "apps.connections.open"))["url"]
            async with connect(url) as ws:
                print("Connected to Slack. Waiting for button presses; Ctrl-C to stop.")
                wait = 1
                async for raw in ws:
                    envelope = json.loads(raw)
                    if envelope.get("type") == "disconnect":
                        stop = envelope.get("reason") == "link_disabled"
                        if stop:
                            print("Socket Mode is off for the app in Slack's settings: stopping.")
                        break  # else Slack is moving the connection: open a new one
                    if not envelope.get("envelope_id"):
                        continue  # hello
                    try:
                        answer, job = app.handle(envelope)
                    except Exception as e:  # noqa: BLE001 - answer anyway, so Slack doesn't retry a bad envelope
                        print(f"Could not read a message from Slack: {type(e).__name__}")
                        answer, job = None, None
                    await ws.send(json.dumps({"envelope_id": envelope["envelope_id"],
                                              **({"payload": answer} if answer else {})}))
                    if job:
                        task = asyncio.create_task(_work(job))
                        jobs.add(task)
                        task.add_done_callback(jobs.discard)
        except (OSError, SlackError, ValueError) as e:
            print(f"Slack connection lost ({e if isinstance(e, SlackError) else type(e).__name__}); "
                  f"trying again in {wait}s")
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)
        except Exception as e:  # noqa: BLE001 - websockets' own errors can carry the address: the type only
            print(f"Slack connection lost ({type(e).__name__}); trying again in {wait}s")
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)
        stop = stop or not forever
    await asyncio.gather(*jobs)


async def mondays(post, state, clock=datetime.now, every=600):
    """Each Monday from MONDAY_HOUR, ``post()`` once: the week's brief. ``state`` is a JSON file holding the last
    Monday posted, so a restart never posts it twice."""
    while True:
        try:
            last = json.loads(state.read_text()).get("week") if state.exists() else None
        except (OSError, ValueError, AttributeError):
            print(f"Can't read {state.name}: fix or delete it. No brief until then.")
            last = monday_due(clock(), None)  # as if this week's had gone, so nothing posts twice
        if week := monday_due(clock(), last):
            try:
                posted = await asyncio.to_thread(post)
                state.parent.mkdir(parents=True, exist_ok=True)
                state.write_text(json.dumps({"week": week}))
                print(f"Posted the brief for the week of {week}." if posted is not False else
                      f"Nothing to decide for the week of {week}: no brief.")
            except (SlackError, ValueError, RuntimeError) as e:
                print(f"Brief not posted: {e}")
            except Exception as e:  # noqa: BLE001 - never takes the buttons down with it; the type only
                print(f"Brief not posted: {type(e).__name__}")
        await asyncio.sleep(every)
