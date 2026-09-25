"""The event loop: each GI event as a hiring sensor. It proposes; it contacts no one.

Events are plain research dinners or evals nights. Before one: which researchers to add, from
the watchlist and from companies GI could hire from, ranked by the timing engine's call and held
when the contact ledger says someone else owns an open ask, and what the event will cost, from
GI's own past events. Each invite is a draft from the GI person who knows them (a coauthor, or
whoever talked with them at an earlier event), else from the host whose topics fit, claiming no
tie; someone whose follow-up is due gets the invite inside it, from the same person. An invite a host sends is recorded in that ledger (team "events"),
so nobody else writes to them before the evening. On the day: a brief per host. After: door notes
become private notes on the person's timeline, which the engine reads (a wait also goes in the
ledger); follow-up drafts fall due within 48 hours, and a sent one is recorded in the ledger;
each event gets a cost per qualified conversation.

GI's own records feed it: its events and hosts, guest lists (Luma exports), card spend
(Ramp exports, tagged by event id in the memo) and who later entered the hiring pipeline
(Ashby). Real records go in research/private/events/; tests/fixtures/events/ holds invented
ones. Public programs add who will be in town: speakers, authors, organizers and panelists
only, so no guest list can enter.
"""

import csv
import json
import os
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from typing import Callable, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from . import contact, detectors, journey, outreach, readiness, role_compiler, routing, timeline, today
from . import scorecard as sc
from .models import parse_time

FOLLOW_UP = timedelta(hours=48)
PIPELINE_WINDOW = timedelta(days=90)  # a pipeline entry this soon after the event counts toward it
TRIP_SLACK = timedelta(days=3)  # a public appearance this close to the event puts them in town
SOON = timedelta(days=60)  # a window opening this soon after the event makes the meeting worth it
ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "config" / "roles.json"
RECORDS = {"simulation": ROOT / "tests/fixtures/events", "live": ROOT / "research/private/events"}
# Live: the people file of the social pull. Its watchlist (people with no public moment yet) is who to invite
# when GI's own event records name nobody.
WATCHLIST = Path(os.getenv("WATCHLIST") or ROOT / "research/private/early-signals/people.json")
LINKS = (("x", "X"), ("linkedin", "LinkedIn"), ("github", "GitHub"), ("homepage", "Site"))


class Host(BaseModel):
    name: str = Field(min_length=2)
    topics: list[str] = Field(default_factory=list)


class GIEvent(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=2)
    day: date
    city: str = Field(min_length=2)
    format: Literal["dinner", "salon", "talk", "workshop"]
    roles: list[str]
    topics: list[str] = Field(default_factory=list)
    capacity: int = Field(gt=0)
    hosts: list[Host] = Field(default_factory=list)


class Guest(BaseModel):
    event_id: str
    name: str = Field(min_length=2)
    subject_id: str = ""  # set when the guest is a watched person
    rsvp: Literal["yes", "no", "waitlist"]
    checked_in: bool = False


class Spend(BaseModel):
    day: date
    merchant: str
    amount: float
    memo: str = ""


class Profile(BaseModel):
    subject_id: str = Field(min_length=1)
    name: str = Field(min_length=2)
    role: str
    city: str = ""
    topics: list[str] = Field(default_factory=list)
    employer: str = ""  # for a researcher at a company GI could hire from, who is not on the watchlist
    links: dict[str, str] = Field(default_factory=dict)  # x, linkedin, github or homepage, when no timeline has them


class Budget(BaseModel):
    """What GI plans to spend on events in a period (budget.json)."""
    name: str
    starts: date
    ends: date
    amount: float = Field(ge=0)


class Appearance(BaseModel):
    name: str = Field(min_length=2)
    role: Literal["speaker", "author", "organizer", "panelist"]


class Program(BaseModel):
    """A public program: conference, workshop or meetup schedule."""
    name: str = Field(min_length=2)
    city: str = Field(min_length=2)
    starts: date
    ends: date
    url: str = Field(min_length=8)
    appearances: list[Appearance] = Field(default_factory=list)


def _rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load(folder):
    """GI's event records from one folder: events.json, guests.csv, ramp.csv, pipeline.json,
    people.json, programs.json and budget.json. A missing file reads as empty."""
    folder = Path(folder)

    def read(name, default):
        return json.loads((folder / name).read_text()) if (folder / name).exists() else default

    def rows(name):
        return _rows(folder / name) if (folder / name).exists() else []

    return {
        "events": [GIEvent.model_validate(e) for e in read("events.json", [])],
        "guests": [Guest.model_validate({**r, "checked_in": r.get("checked_in", "").lower() in ("yes", "true", "1")})
                   for r in rows("guests.csv")],
        "spend": [Spend(day=r["Date"], merchant=r["Merchant"], amount=r["Amount"], memo=r.get("Memo", ""))
                  for r in rows("ramp.csv")],
        "pipeline": read("pipeline.json", []),
        "people": [Profile.model_validate(p) for p in read("people.json", [])],
        "programs": [Program.model_validate(p) for p in read("programs.json", [])],
        "budget": [Budget.model_validate(b) for b in read("budget.json", {}).get("periods", [])],
    }


def _norm(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def _shared(a, b):
    """Topics of ``a`` that ``b`` also has, in ``a``'s wording."""
    theirs = {_norm(t) for t in b}
    return list(dict.fromkeys(t for t in a if _norm(t) in theirs))


def _title(role):
    rid = role_compiler.role_id(role)
    return next((r["title"] for r in json.loads(ROLES.read_text())["roles"] if r["id"] == rid), role)


# ----------------------------------------------------------------- notes

def _tag(event):
    return f"At {event.name} ({event.id}, {event.day}): "


def record_note(store, event, subject_id, kind, text, author, wait_until=None, now=None):
    """A door note: open to a move, waits until a day, or context. It lands on the person's
    timeline as a private note, so the engine reads it like any other, and replaces an earlier
    note from the same event. A wait is their answer to GI, so it goes in the contact ledger as a
    not_now, which writes that note."""
    earlier = notes_from(store, event, [subject_id]).get(subject_id, {}).get("id")
    if kind == "wait":
        return contact.record(store, subject_id, "not_now", at=now, now=now, supersedes=earlier, team="events",
                              by=author, until=wait_until, event_id=event.id, note=_tag(event) + text.strip())
    return timeline.add_private_note(store, subject_id, _tag(event) + text.strip(), author, supersedes=earlier,
                                     note_type="open" if kind == "open" else None, now=now)


def notes_from(store, event, subject_ids):
    """The event's door notes, newest per person."""
    out = {}
    for sid in subject_ids:
        for note in timeline.private_notes(store, sid):
            if _tag(event) in note["quote"]:
                out.setdefault(sid, note)
    return out


def _said(note, event):
    """What they said, from a door note: the text after the event's tag."""
    return note["quote"].split(_tag(event), 1)[1]


QUALIFIED = {"note_open", "note_wait"}  # a named opening or a named day; context alone is not


# ------------------------------------------------------------- past events

def _tags(spend):
    """The words of a spend row's memo, where an event id tags it."""
    return re.findall(r"[\w-]+", spend.memo)


def budget(records, day, past):
    """This period's event budget against event spend so far and what is still to come for the events left in it
    (each one's estimate from ``past``, the past events' results, less what was already paid toward it), or None
    without a budget for the day."""
    period = next((b for b in records["budget"] if b.starts <= day <= b.ends), None)
    if not period:
        return None
    ids = {e.id for e in records["events"]}
    paid = [s for s in records["spend"] if s.day <= day]
    spent = round(sum(s.amount for s in paid if period.starts <= s.day and ids & set(_tags(s))), 2)
    planned = []
    for e in sorted(records["events"], key=lambda e: e.day):
        if day < e.day <= period.ends:
            est = estimate(e, past)
            deposit = sum(s.amount for s in paid if e.id in _tags(s))  # in any period: it is not owed again
            planned.append({"event_id": e.id, "name": e.name, "day": e.day.isoformat(), "mid": est["mid"] if est else None,
                            "to_come": round(max(est["mid"] - deposit, 0), 2) if est else None})
    return {"period": period.name, "amount": period.amount, "spent": spent, "planned": planned,
            "left": round(period.amount - spent - sum(p["to_come"] or 0 for p in planned), 2)}


def results(store, event, guests, spend, pipeline):
    """What one past event cost and produced."""
    came = [g for g in guests if g.event_id == event.id and g.checked_in]
    yes = [g for g in guests if g.event_id == event.id and g.rsvp == "yes"]
    cost = round(sum(s.amount for s in spend if event.id in _tags(s)), 2)
    notes = notes_from(store, event, [g.subject_id for g in came if g.subject_id])
    qualified = [sid for sid, n in notes.items() if n["event_type"] in QUALIFIED]
    ids = {g.subject_id for g in came if g.subject_id}
    entered = sorted({p["subject_id"] for p in pipeline if p["subject_id"] in ids
                      and event.day <= date.fromisoformat(p["entered_at"][:10]) <= event.day + PIPELINE_WINDOW})
    return {"event_id": event.id, "name": event.name, "day": event.day.isoformat(), "format": event.format,
            "cost": cost, "came": len(came), "said_yes": len(yes),
            "show_up": round(len(came) / len(yes), 2) if yes else None,
            "per_head": round(cost / len(came), 2) if came else None,
            "notes": len(notes), "qualified": len(qualified),
            "cost_per_qualified": round(cost / len(qualified), 2) if qualified else None,
            "entered_pipeline": entered}


def estimate(event, past):
    """Expected cost of an upcoming event from past ones of the same format: per head times expected turnout."""
    same = [r for r in past if r["format"] == event.format and r["per_head"]]
    if not same:
        return None
    heads = [r["per_head"] for r in same]
    turnout = round(event.capacity * median([r["show_up"] for r in same if r["show_up"]] or [1.0]))
    return {"based_on": [r["event_id"] for r in same], "expected_guests": turnout,
            "low": round(min(heads) * turnout), "mid": round(median(heads) * turnout), "high": round(max(heads) * turnout)}


# ---------------------------------------------------------------- invites

def call_text(call):
    """The engine's call in the words Today's calls uses, with its date when it has one."""
    until = f" until {call.until[:10]}" if call and call.until else ""
    return today.HEADLINE[call.action if call else None] + until


def _why(call):
    if not call or not call.reasons:
        return []
    words = readiness.label(call.reasons[0])
    return [words[0].upper() + words[1:]]


def _when(day, year=None):
    """A day as "Sep 12", with its year when it is not ``year`` ("Sep 2026" when the source gives only a month)."""
    day = str(day or "")
    if len(day) >= 10:
        return f"{date.fromisoformat(day[:10]):%b} {int(day[8:10])}" + (f", {day[:4]}" if day[:4] != str(year) else "")
    return f"{date.fromisoformat(day + '-01'):%b %Y}" if len(day) == 7 else day


def _on(day, year=None):
    """ "on Sep 12", or "in Aug 2026" / "in 2025" when the source gives only a month or a year."""
    return f"{'on' if len(str(day or '')) >= 10 else 'in'} {_when(day, year)}"


def _quote(text):
    """The quote as a sentence ends it: one trailing period dropped (an ellipsis kept), None when empty."""
    text = " ".join(str(text or "").split())
    text = text if len(text) <= 140 else text[:139].rsplit(" ", 1)[0] + "…"
    text = text[:-1] if text.endswith(".") and not text.endswith("..") else text
    end = text[-2:-1] if text[-1:] in "\"'”’" else text[-1:]  # the mark before a closing quote ends it too
    return f"“{text}”" + ("" if end and end in "?!…." else ".") if text.strip(". ") else None


def _words(quote, kind=None):
    """What they said, as an invite or a host brief may repeat it, or None to withhold it: their own words only, never
    the post they answered, and nothing a card's draft would not quote (a layoff or a lost job; for a post, anything
    personal). A paper's title is their work, as on the cards."""
    if quote in routing.NOTES:  # withheld, or a post the reader read nothing off
        return None
    words = detectors.own_words(str(quote or ""))
    return None if (outreach.LOSS.search(words) if kind in routing.PAPERS else outreach.unquotable(words)) else words


def _and(items):
    items = list(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else "".join(items)


def links(store, subject_id, extra=None):
    """Their public profiles (X, LinkedIn, GitHub, site) from their timeline's anchors and any ``extra``."""
    ctx = journey.load_context(store, subject_id)
    anchors = {**(extra or {}), **(ctx.anchors if ctx else {})}
    return [{"label": label, "url": anchors[key]} for key, label in LINKS if str(anchors.get(key, "")).startswith("https://")]


NOW = {"reach_now": "The engine says reach out now",
       "verify_first": "The engine wants that confirmed before anyone pitches them, and a chat is the easy way to confirm it",
       "watch_until": "The engine is waiting, so this is a no-pitch chance to meet them first",
       "respect_follow_up": "They asked us to wait, so nobody pitches them",
       "quiet": "The engine has no reason to reach out, so this is only a chance to meet them",
       None: "There is nothing to reach out about yet, so this is only a chance to meet them"}


def why_them(store, as_of, subject_id, call, talk_about=(), where="", hold=None, employer=""):
    """Why this person, in one or two plain sentences: what happened, then why meeting them matters now.

    ``hold`` is the contact ledger's: with one, nobody pitches them, whatever the engine's call. ``employer``
    is where a researcher GI listed works, when nothing of theirs has been read."""
    trigger = None
    if call:
        found = routing.signals(call, {e["id"]: e for e in journey.view(store, subject_id, as_of)})
        opener = readiness.opener(call)
        trigger = next((t for t in found if t["id"] == opener), found[0] if found else None)
    year = as_of[:4]
    if trigger:
        words = _words(trigger["quote"], trigger.get("type"))
        note = trigger["quote"] if trigger["quote"] in routing.NOTES else routing.WITHHELD
        said = f" {note}." if words is None else (  # not their words: no quotes
            f": {q}" if (q := _quote(words)) else ".")
        first = f"{trigger['what'][0].upper()}{trigger['what'][1:]} {_on(trigger['date'], year)}{said}"
    else:
        first = ("Nothing they posted gives a reason right now." if call else
                 f"A researcher at {employer}, where GI could hire." if employer else "Nothing of theirs has been read yet.")
    action = call.action if call else None
    now = NOW[action]
    if hold:
        now = "Whatever the engine's call, nobody pitches them now"  # the rule under it says why
    elif action == "reach_now" and call.earliest_close:
        now += f", before {_when(call.earliest_close, year)}"
    elif action in ("watch_until", "respect_follow_up") and call.until:
        until = _when(call.until, year)
        now = now.replace("waiting", f"waiting until {until}").replace("to wait", f"to wait until {until}")
    fit = [f"they work on {_and(talk_about)}, which the night covers"] if talk_about else []
    fit += [where] if where else []
    return f"{first} {now}" + (f"; {', and '.join(fit)}" if fit else "") + "."


def _how(knows):
    """How a teammate knows them, as the Slack card says it (routing._said): what they picked on the form, never the
    free text after it, which may be something told in confidence."""
    return (knows.get("note") or "").split(".")[0].strip()


def invite_from(store, as_of, person, event, team, past):
    """Who at GI sends this person's invite, and the tie, never invented: a teammate who worked with them
    (routing's confirmed paths: coauthored, or pushed to the same repo, recently enough to be remembered),
    else a teammate who said on a Slack card that they know them (the ledger's "knows"), else whoever talked
    with them at an earlier GI event, else the host whose topics fit best, with no tie claimed. Having been
    at the same employer at the same time, or working together long ago, is not knowing them: it is a
    ``hint`` to ask that teammate first, never a tie the note claims."""
    mates = [routing.Teammate.model_validate(m) for m in team or []]
    found = routing.paths(timeline.timeline(store, person.subject_id, as_of), mates, as_of) if mates else []
    hint = next((f"{p.detail}: ask {p.teammate.name.split()[0]} whether they know them" for p in found
                 if p.level == "likely"), None)
    if tie := next((p for p in found if p.level == "confirmed"), None):
        return {"from": tie.teammate.name, "tie": tie.detail, "kind": tie.kind, "work": tie.work, "hint": None}
    # The Slack card's own rule (routing.knows): a teammate on the list is a tie; anyone else, a name to check.
    known, unlisted = routing.knows(contact.history(store, person.subject_id), mates, as_of)
    if known:
        return {"from": known[0]["by"], "kind": "knows", "hint": None,
                "tie": f"{known[0]['by']} knows them" + (f" ({how})" if (how := _how(known[0])) else "")}
    if unlisted and not hint:
        hint = f"{unlisted[0]['by']} said on Slack they know them, but " + (
            "isn't on GI's team list" if mates else "GI's team list isn't loaded") + ": check who that is first"
    for e in sorted(past, key=lambda e: e.day, reverse=True):
        note = notes_from(store, e, [person.subject_id]).get(person.subject_id)
        if note and note.get("author"):
            return {"from": note["author"], "kind": "met", "event": e.name, "hint": hint,
                    "tie": f"{note['author']} talked with them at {e.name} on {_when(e.day.isoformat(), as_of[:4])}"}
    host = max(event.hosts, key=lambda h: len(_shared(h.topics, person.topics)), default=None)
    return {"from": host.name if host else "", "tie": None, "kind": None, "hint": hint}


def _opened_with(opener):
    """Their own words to open with, or for code, the project: a commit log read back sounds like watching. Never
    the day they wrote it: a post's day depends on a time zone we don't know."""
    url = urlparse(opener.get("source_url") or "")
    if (url.hostname or "").removeprefix("www.") == "github.com":
        repo = "/".join(url.path.strip("/").split("/")[:2])
        return f"Saw your recent work on {repo}." if repo.count("/") == 1 else ""
    said = _quote(_words(opener.get("quote"), opener.get("event_type")))
    return f"Saw what you wrote: {said}" if said else ""


def invite_draft(store, as_of, person, event, call, sender):
    """The invite as its sender would write it, in GI's outreach voice, to edit and send by hand. It opens with
    the tie on file or with something of their own, and claims no tie that isn't on file."""
    if sender["kind"] == "coauthor" and sender.get("work"):
        hook = f"Hope all is well since we wrote '{sender['work']}' together."
    elif sender["kind"] == "met":
        hook = f"It was good to talk at {sender['event']}."
    else:
        by_id = {e["id"]: e for e in journey.view(store, person.subject_id, as_of)}
        opener = by_id.get(readiness.opener(call)) if call else None
        hook = _opened_with(opener) if opener else ""
    topics = _shared(person.topics, event.topics) or event.topics[:2]
    body = " ".join(filter(None, [
        hook, f"We're hosting {event.name} at General Intuition in {event.city} on {event.day:%A, %B} {event.day.day},",
        f"a small evening of researchers talking about {_and(topics)}. Would love to have you there if you're free."]))
    greeting = f"Hey {person.name.split()[0]},"
    sign = sender["from"].split()[0] if sender["from"] else ""
    return {"subject": f"{event.name}, {event.day:%B} {event.day.day}",
            "body": f"{greeting} {body[:1].lower()}{body[1:]}\n\n{sign}".rstrip()}


def _invite_line(event):
    return (f"We're also hosting {event.name} at General Intuition on {event.day:%A, %B} {event.day.day}. "
            "Would love to have you there if you're free.")


CONFIRM = "Confirm first"  # Today's calls' word for a reach the contact ledger asks to check first


def _confirm_first(entry, doubt):
    """An invite entry for a reach the ledger doubts (contact.unsure: they may still be a student), as Today's calls
    shows it: Confirm first with the ledger's note, ranked with the checks, never the engine's "reach out now"."""
    if doubt and entry["action"] == "reach_now":
        entry |= {"action": "verify_first", "call": CONFIRM, "check": doubt, "rank": 1}
    return entry


def _closes_before(call, event):
    """A reach whose window closes before the night: write to them now rather than wait for the event."""
    if call and call.action == "reach_now" and call.earliest_close and parse_time(call.earliest_close).date() < event.day:
        return f"reach out directly: the engine's window closes {call.earliest_close[:10]}, before the night"
    return None


def _asked_to_wait(call):
    if call and call.action == "respect_follow_up":
        return f"asked to be contacted after {call.until[:10]}" if call.until else "asked us to wait"
    return None


def _rank(call, event, alone=False):
    """``alone``: the wait is on holds alone (_holds_alone), so its end is no decision."""
    action = call.action if call else None
    if action == "reach_now":
        return 0
    if action == "verify_first":
        return 1
    if action == "watch_until" and not alone and call.until and parse_time(call.until).date() <= event.day + SOON:
        return 2  # a window about to open, or a hold about to lift off a reason; a first year alone ranks with the quiet
    return 3


def _holds_alone(store, subject_id, as_of, call, role=None):
    """Whether a wait waits on holds alone (a first year in a new role): no reason behind it and no window opening
    after it, as Today's calls judges it (today.hold_only). When it ends the call is quiet, so it ranks with the quiet.
    Readiness alone can't tell it from a hold that lifts onto a window."""
    return bool(call and call.action == "watch_until" and call.holds and not call.reasons
                and today.hold_only(journey.detect(store, subject_id, as_of, role=role or None), as_of))


def in_town(profile, event, programs):
    """A public program in the event's city, close to its day, that names this person (by name: confirm identity)."""
    for p in programs:
        if (_norm(p.city) == _norm(event.city) and p.starts - TRIP_SLACK <= event.day <= p.ends + TRIP_SLACK
                and any(_norm(a.name) == _norm(profile.name) for a in p.appearances)):
            return p
    return None


def suggest_invites(event, people, guests, calls, programs=(), gate: Callable[[str], str | None] | None = None,
                    max_extra=6, pending=(), alone=frozenset(), unsure=None):
    """Watched people to add to an event, best first, and the ones held back with the reason.

    Only people for the event's roles who live in its city or appear in a public program
    there that week. A person who asked to be contacted after a date, or whom the contact
    gate refuses (``gate(subject_id)`` gives the reason), is held, never invited. ``pending``
    are people a host already invited who have not answered: they are skipped and keep a seat. ``alone``: the people
    whose wait is on holds alone (_holds_alone), who rank with the quiet. ``unsure``: the ledger's doubt per person
    (contact.unsure): their reach is Confirm first (_confirm_first), so nobody reaches out directly before the night.
    """
    listed = {g.subject_id for g in guests if g.event_id == event.id and g.subject_id}
    waiting = set(pending) - listed  # invited, not yet on the guest list
    seats = event.capacity - sum(g.event_id == event.id and g.rsvp == "yes" for g in guests) - len(waiting)
    invited = listed | waiting
    picks, held = [], []
    for p in people:
        if p.role not in event.roles or p.subject_id in invited:
            continue
        trip = in_town(p, event, programs)
        if not trip and _norm(p.city) != _norm(event.city):
            continue
        call, doubt = calls.get(p.subject_id), (unsure or {}).get(p.subject_id)
        refused = _asked_to_wait(call) or (gate(p.subject_id) if gate else None) or (
            None if doubt else _closes_before(call, event))
        if refused:
            held.append({"subject_id": p.subject_id, "name": p.name, "reason": refused})
            continue
        where = (f"{trip.name} puts them in {event.city} that week ({trip.url}); confirm it is them" if trip
                 else f"Lives in {event.city}")
        picks.append(_confirm_first({"subject_id": p.subject_id, "name": p.name, "action": call.action if call else None,
                                     "call": call_text(call), "rank": _rank(call, event, p.subject_id in alone),
                                     "why": [where, *_why(call)], "talk_about": _shared(p.topics, event.topics)}, doubt))
    picks.sort(key=lambda x: (x["rank"], -len(x["talk_about"]), x["name"]))
    return picks[:max(0, min(max_extra, seats))], held


# ------------------------------------------------------------ host briefs

def _rule(call, role, hold=None, year=None):
    action = call.action if call else None
    if hold:
        return f"No pitch: {hold}"
    if action == "reach_now" and call.track == "pitch":
        return f"If they ask what's next, the {_title(role)} role is fair to raise."
    if action == "verify_first":
        return f"No pitch. Listen for whether this still holds: {readiness.label(call.reasons[0])}." if call.reasons \
            else "No pitch. Listen for what the engine flagged."
    if action == "watch_until" and call.until and call.holds:  # held back; its day may be a hold's end or a window's
        return (f"No pitch while they're held back ({_and([readiness.label(h) for h in call.holds])}); the engine "
                f"looks again on {_when(call.until, year)}. Talk about their work.")
    if action == "watch_until" and call.until:
        return f"No pitch: their window opens {_when(call.until, year)}. A good talk now makes that note warm."
    return "Talk about their work. No pitch."


def host_briefs(event, people, calls, gate: Callable[[str], str | None] | None = None, suggested=(), knows=None):
    """Per host: the watched people coming, and the ``suggested`` ones if a host invites them, with what to
    talk about and what not to do.

    ``gate(subject_id)`` is the contact ledger's hold, if any: a held person gets no pitch. ``knows`` maps a
    person to the GI person who knows them: when that is one of the hosts, the person is theirs.
    """
    briefs = {h.name: {"host": h.name, "people": [], "if_invited": []} for h in event.hosts}
    for p, key in [*((p, "people") for p in people), *((p, "if_invited") for p in suggested)]:
        if not event.hosts:
            break
        # The host who knows them; else the one with the most shared topics, then the one with fewer people so far.
        host = next((h for h in event.hosts if h.name == (knows or {}).get(p.subject_id)), None) or max(
            event.hosts, key=lambda h: (len(_shared(h.topics, p.topics)),
                                        -len(briefs[h.name]["people"]) - len(briefs[h.name]["if_invited"])))
        call, hold = calls.get(p.subject_id), gate(p.subject_id) if gate else None
        briefs[host.name][key].append({"subject_id": p.subject_id, "name": p.name,  # held: the call is no badge to act on
                                       "action": call.action if call and not hold else None,
                                       "call": "No pitch" if hold else call_text(call),
                                       "why": _why(call), "talk_about": _shared(p.topics, host.topics + event.topics),
                                       "rule": _rule(call, p.role, hold, event.day.year)})
    return [b for b in briefs.values() if b["people"] or b["if_invited"]]


# ------------------------------------------------------------- follow-ups

def _draft(person, event, note, host, met=True):
    """The follow-up in the voice of whoever talked with them, signed by them. With nobody on file as having
    talked with them (``met`` false), it claims no meeting: a host writes as the event's host."""
    first = person.name.split()[0]
    topics = _shared(person.topics, event.topics) or event.topics[:1] or ["your work"]
    opener = (f"Hey {first}, good to meet you at {event.name} on {event.day:%A}. " if met
              else f"Hey {first}, thanks for coming to {event.name} on {event.day:%A}. ")
    if note["event_type"] == "note_open":  # what they said is only that they're open: the topic is GI's suggestion
        ask = ("You mentioned you'd be up for a proper conversation. " if met
               else "I heard you'd be up for a proper conversation. ") + \
            f"Would next week work for a longer chat about {topics[0]}?"
    else:  # a note GI kept for itself: nothing in it is put back to them, and nothing claims a talk about the topic
        ask = ("Thanks for coming. " if met else "") + \
            f"If a{' longer' if met else ''} chat about {topics[0]} would be useful, I'm happy to find a time."
    sign = host.split()[0] if host and host != "our team" else ""
    return {"subject": f"{event.name}: following up", "body": f"{opener}{ask}\n\n{sign}".rstrip()}


def follow_ups(store, event, people, now):
    """One follow-up per person with a door note from this event, from the host who wrote it, due 48 hours
    after the event. Not for someone who asked us to wait at the door: the engine reopens them on their day.

    ``now`` is an aware datetime. Each draft is for a person to edit and send. ``sent`` is the
    ledger's record that a host sent it; until then ``hold`` says why not to: another team owns
    an open ask, or they asked to be contacted later. A sent follow-up is GI's note to them, so
    the ledger's usual rules follow: one follow-up a week on with no reply, then quiet.
    """
    by_id = {p.subject_id: p for p in people}
    due = parse_time(f"{event.day}T23:00:00+00:00") + FOLLOW_UP  # from an evening event: 7pm in New York
    out = []
    for sid, note in notes_from(store, event, list(by_id)).items():
        if note["event_type"] == "note_wait":
            continue
        person = by_id[sid]
        # Whoever talked to them, host or not; else the host whose topics fit best, who claims no meeting.
        met = bool(note.get("author"))
        host = note["author"] if met else (
            max(event.hosts, key=lambda h: len(_shared(h.topics, person.topics))).name if event.hosts else "our team")
        sent = next((e for e in contact.history(store, sid) if e["kind"] == "sent" and e.get("event_id") == event.id),
                    None)
        out.append({"subject_id": sid, "name": person.name, "note": _said(note, event), "host": host, "met": met,
                    "due": due.isoformat(),
                    "overdue": not sent and now > due, "draft": _draft(person, event, note, host, met),
                    "sent": {"by": sent["by"], "at": sent["at"][:10]} if sent else None,
                    "hold": None if sent else ledger_hold(store, sid, now, event) or _asked_to_wait(
                        journey.assess(store, sid, now.isoformat(), person.role))})
    return sorted(out, key=lambda f: (not f["overdue"], f["name"]))


# --------------------------------------------------------------- workspace

def ledger_hold(store, subject_id, now, event=None):
    """The contact ledger's reason to leave a person alone now, whichever team owns the open ask, or None.

    With ``event``, that event's own invite does not count: its hosts are the ones who asked. A Slack
    card counts for the ledger's quiet period, not for good: an invite is not a second card.
    """
    moment = parse_time(now) if isinstance(now, str) else now
    history = contact.history(store, subject_id)
    own = {(e["at"], e["by"]) for e in history if event and e["kind"] == "invited" and e.get("event_id") == event.id}
    entries = [e for e in history
               if not (event and e["kind"] == "invited" and e.get("event_id") == event.id)
               and not (e["kind"] == "sent" and (e["at"], e["by"]) in own)  # the follow-up that carried this invite
               and not (e["kind"] == "pinged" and (moment - parse_time(e["at"])).days >= contact.QUIET_DAYS)]
    # A doubt about whether they're still a student holds no invite (an invite pitches nothing): the host brief says it
    clearance = contact.check([e for e in entries if e["kind"] != "unconfirmed"], now)
    return clearance.reason if clearance.state != "clear" else None


def invites_sent(store, event, subject_ids):
    """{subject_id: ledger entry} for the invites to this event a host recorded."""
    return {sid: e for sid in subject_ids for e in contact.history(store, sid)
            if e["kind"] == "invited" and e.get("event_id") == event.id}


def overview(store, as_of, records, exclude=frozenset(), blocked=None, team=()):
    """The Events page: the next event's plan, the last event's door notes and follow-ups, and past results.

    ``as_of`` is the workspace's clock (an ISO time); calls are the engine's, and holds the ledger's, as of then.
    ``exclude``: people never to suggest (by contact.person_key), such as the scorecard's moment people under any
    of their ids; one already coming stays in
    their host's brief, with no pitch. ``blocked``: why nobody can be suggested (the list of people to
    exclude could not be read), so none is, and nobody coming is pitched. ``team``: GI's teammates, for
    who knows whom (today.source's roster).
    """
    day = date.fromisoformat(as_of[:10])
    evs = sorted(records["events"], key=lambda e: e.day)
    past = [e for e in evs if e.day <= day]
    nxt = next((e for e in evs if e.day > day), None)
    people = [p for p in records["people"] if contact.person_key(p.subject_id) not in exclude]
    names = {p.subject_id: p.name for p in people}
    calls = {p.subject_id: journey.assess(store, p.subject_id, as_of, p.role) for p in records["people"]}
    doubts = {p.subject_id: d for p in records["people"] if (d := contact.unsure(contact.history(store, p.subject_id), as_of))}
    out = {"past": [results(store, e, records["guests"], records["spend"], records["pipeline"]) for e in reversed(past)],
           "next": None, "last": None}
    out["budget"] = budget(records, day, out["past"])
    if past:
        last = past[-1]
        came = {g.subject_id for g in records["guests"] if g.event_id == last.id and g.checked_in and g.subject_id}
        notes = notes_from(store, last, came)
        out["last"] = {"event": last.model_dump(mode="json"),
                       "people": [{"subject_id": p.subject_id, "name": p.name,
                                   "action": calls[p.subject_id].action if calls[p.subject_id] else None,
                                   "call": call_text(calls[p.subject_id]),
                                   "note": _said(notes[p.subject_id], last) if p.subject_id in notes else None}
                                  for p in people if p.subject_id in came],
                       "follow_ups": follow_ups(store, last, people, parse_time(as_of))}
        for f in out["last"]["follow_ups"] if blocked else []:  # who is never contacted is unknown: nobody is, yet
            f["hold"] = f["hold"] or (None if f["sent"] else UNREAD_HOLD)
    # One note from one person: someone whose follow-up from the last event is due is invited in it, by its sender.
    following = {f["subject_id"]: f for f in (out["last"] or {}).get("follow_ups", []) if not f["sent"] and not f["hold"]}
    if nxt:
        sent = invites_sent(store, nxt, list(names))
        picks, held = ([], []) if blocked else suggest_invites(
            nxt, people, records["guests"], calls, records["programs"], lambda sid: ledger_hold(store, sid, as_of),
            pending=sent, alone={p.subject_id for p in people
                                 if _holds_alone(store, p.subject_id, as_of, calls[p.subject_id], p.role)}, unsure=doubts)
        listed = {g.subject_id for g in records["guests"] if g.event_id == nxt.id}
        coming = {g.subject_id for g in records["guests"] if g.event_id == nxt.id and g.rsvp == "yes"} | (
            set(sent) - listed)  # a declined invite is not coming
        yes = sum(g.event_id == nxt.id and g.rsvp == "yes" for g in records["guests"])
        by_id = {p.subject_id: p for p in records["people"]}
        senders = {p.subject_id: invite_from(store, as_of, p, nxt, team, past)
                   for p in records["people"] if p.subject_id in coming or p.subject_id in {x["subject_id"] for x in picks}}
        for sid, f in following.items():
            if sid in senders and sid in {x["subject_id"] for x in picks}:
                senders[sid] = {"from": f["host"], "kind": "met" if f["met"] else None, "hint": senders[sid]["hint"],
                                "tie": f"{f['host']} talked with them at {out['last']['event']['name']}" if f["met"] else None}
                text, _, sign = f["draft"]["body"].rpartition("\n\n")
                f["draft"]["body"] = f"{text or sign} {_invite_line(nxt)}" + (f"\n\n{sign}" if text else "")
                f["invites_to"], f["invites_to_id"] = nxt.name, nxt.id
        for pick in picks:
            person, sender = by_id[pick["subject_id"]], senders[pick["subject_id"]]
            trip = in_town(person, nxt, records["programs"])
            where = (f"a public program puts them in {nxt.city} that week (confirm it is them)" if trip
                     else f"they live in {nxt.city}")
            pick |= {"why_them": why_them(store, as_of, pick["subject_id"], calls[pick["subject_id"]], pick["talk_about"], where,
                                          hold=doubts.get(pick["subject_id"]),
                                          employer=person.employer),
                     "links": links(store, pick["subject_id"], person.links) + (
                         [{"label": trip.name, "url": trip.url}] if trip else []),
                     "from": sender["from"], "tie": sender["tie"], "hint": sender["hint"],
                     "follow_up": out["last"]["event"]["name"] if pick["subject_id"] in following else None,
                     # one note: the follow-up's draft carries the invite, so there is no second one to send
                     "draft": None if pick["subject_id"] in following else
                     invite_draft(store, as_of, person, nxt, calls[pick["subject_id"]], sender)}
        def no_pitch(sid):  # coming anyway: the host talks about their work, and pitches only whom the rules allow
            return blocked or (MOMENT_GUEST if contact.person_key(sid) in exclude else None) or \
                ledger_hold(store, sid, as_of, nxt) or doubts.get(sid)

        briefs = host_briefs(nxt, [p for p in records["people"] if p.subject_id in coming], calls, no_pitch,
                             suggested=[by_id[p["subject_id"]] for p in picks],
                             knows={sid: s["from"] for sid, s in senders.items() if s["tie"]})
        suggested = {p["subject_id"]: p for p in picks}
        for b in briefs:
            for entry in b["people"] + b["if_invited"]:  # the host's own tie to them, to open the talk with
                sender = senders.get(entry["subject_id"]) or {}
                entry["tie"] = sender.get("tie") if sender.get("from") == b["host"] else None
            for entry in b["if_invited"]:
                entry |= {k: suggested[entry["subject_id"]][k] for k in ("why_them", "links")}
            for entry in b["people"]:
                hold = no_pitch(entry["subject_id"])
                person = by_id[entry["subject_id"]]
                entry |= {"why_them": why_them(store, as_of, entry["subject_id"], calls[entry["subject_id"]], hold=hold,
                                               employer=person.employer),
                          "links": links(store, entry["subject_id"], person.links)}
        out["next"] = {"event": nxt.model_dump(mode="json"), "said_yes": yes, "estimate": estimate(nxt, out["past"]),
                       "seats_left": max(nxt.capacity - yes - len(set(sent) - listed), 0),
                       "invite": picks, "held": held, "blocked": blocked,
                       # who can be named as having sent an invite: the hosts, the team, and each note's sender
                       "senders": list(dict.fromkeys([h.name for h in nxt.hosts] + [m["name"] for m in team or []]
                                                     + [p["from"] for p in picks if p["from"]])),
                       "invited": [{"subject_id": sid, "name": names[sid], "by": e["by"], "at": e["at"][:10]}
                                   for sid, e in sent.items()],
                       "briefs": briefs}
    return out


def workspace(mode):
    """(store, as_of, records, where) for a data mode: the same store and clock Today's calls reads, or None. Like
    Today's calls, it first writes a "hired" entry for anyone on New starts the ledger doesn't know yet."""
    found = today.source(mode)
    folder = RECORDS[mode]
    if not found or not (folder / "events.json").exists():
        return None
    store, as_of, _, _, where = found
    from . import starts  # starts reads today's workspace, as this module does: imported here

    starts.hold_all(mode)  # whoever accepted an offer is never invited as a candidate
    return store, as_of, load(folder), where


def _open(mode, event_id, subject_id):
    found = workspace(mode)
    if not found:
        raise LookupError(f"No GI event records yet at {RECORDS[mode]}.")
    store, as_of, records, _ = found
    event = next((e for e in records["events"] if e.id == event_id), None)
    person = next((p for p in records["people"] if p.subject_id == subject_id), None)
    if not event or not person:
        raise LookupError("Unknown event or person")
    return store, as_of, records, event, person


MOMENT_GUEST = "one of the scorecard's moment people, coming anyway: talk about their work only"
UNREAD_HOLD = "the people file doesn't read, and it says who is never followed up"
UNREAD_MARK = "The people file could not be read, and it says who is never invited or followed up: the Events page says why"


def page(mode):
    found = workspace(mode)
    if not found:
        return {"as_of": None, "source": None, "missing": f"No GI event records yet at {RECORDS[mode]}.",
                "watchlist": watchlist(mode)}
    store, as_of, records, where = found
    store = sc.ReadOnce(store)  # every person's view reads the same rows: read the store once per request
    exclude, error = _moment_people(mode)
    team = today.source(mode)[2] or []
    return {"as_of": as_of[:10], "source": where, "missing": None, "people_error": error,
            **overview(store, as_of, records, exclude, blocked=error and "Nobody is suggested until the people file "
                                                                        "reads: it says who must never be invited.",
                       team=team),
            "watchlist": watchlist(mode, has_events=True) if not records["people"] else None}


def _moment_people(mode):
    """(the scorecard's moment people, never to invite, and why the people file could not be read). Live only:
    a file that doesn't read says so, and the page then suggests nobody rather than risk one of them."""
    if mode != "live":
        return frozenset(), None
    people, error = _people_file()
    return replay_keys(people), error


def replay_keys(people):
    """Everyone with a moment row in a people file, by contact.person_key: a replay case under any of their ids (a
    second, role-prefixed row without a moment is still them). Never invited, and never carded (inbox.listed)."""
    return frozenset(contact.person_key(p.subject_id) for p in people if p.moments)


def _people_file():
    """(the social pull's people, or [], and why it could not be read, or None)."""
    if not WATCHLIST.exists():
        return [], None
    try:
        return sc.load_moments(WATCHLIST), None
    except (sc.AnswerKeyError, ValueError, TypeError, OSError) as err:  # a bad file says so, not a server error
        return [], f"The people file could not be read: {err}"


def watchlist(mode, has_events=False):
    """Live, with no event records naming people: the social pull's watchlist as who to invite to GI's next
    event, by the engine's call, held by the contact ledger, each with why and their public profiles. None
    in simulation (its event records name the invented people) or without timelines or a people file."""
    found = today.source(mode)
    if mode != "live" or not found or not WATCHLIST.exists():
        return None
    store, as_of, *_ = found
    store = sc.ReadOnce(store)
    rank = {"reach_now": 0, "verify_first": 1, "watch_until": 2}
    people, error = _people_file()
    picks, held, replay = [], [], replay_keys(people)
    for p in people:
        if contact.person_key(p.subject_id) in replay:  # a moment person, under any id, is the scorecard's replay
            continue
        ctx = journey.load_context(store, p.subject_id)
        role = (ctx.role if ctx else None) or p.role
        call = journey.assess(store, p.subject_id, as_of, role) if ctx else None
        name = p.name or (ctx.name if ctx else p.subject_id)
        if refused := _asked_to_wait(call) or ledger_hold(store, p.subject_id, as_of):
            held.append({"subject_id": p.subject_id, "name": name, "reason": refused})
            continue
        doubt = contact.unsure(contact.history(store, p.subject_id), as_of)
        picks.append(_confirm_first({"subject_id": p.subject_id, "name": name, "role": _title(role),
                                     "action": call.action if call else None, "call": call_text(call),
                                     "rank": 3 if _holds_alone(store, p.subject_id, as_of, call, role) else  # as the quiet
                                     rank.get(call.action if call else None, 3),
                                     "why_them": why_them(store, as_of, p.subject_id, call, hold=doubt),
                                     "links": links(store, p.subject_id, p.anchors())}, doubt))
    picks.sort(key=lambda x: (x["rank"], x["name"]))
    return {"invite": picks, "held": held, "error": error, "has_events": has_events,
            "people_file": str(WATCHLIST.relative_to(ROOT) if WATCHLIST.is_relative_to(ROOT) else WATCHLIST)}


def add_note(mode, event_id, subject_id, kind, text, author, wait_until=None):
    """Log a door note in a workspace and return the engine's new call for that person."""
    store, as_of, records, event, person = _open(mode, event_id, subject_id)
    if event.day > date.fromisoformat(as_of[:10]):
        raise ValueError(f"{event.name} hasn't happened yet")
    if not any(g.event_id == event.id and g.subject_id == subject_id and g.checked_in for g in records["guests"]):
        raise ValueError(f"{person.name} isn't checked in at {event.name}")
    record_note(store, event, subject_id, kind, text, author, wait_until, now=as_of)
    call = journey.assess(store, subject_id, as_of, person.role)
    return {"subject_id": subject_id, "call": call_text(call),
            "holding": [readiness.label(h) for h in call.holds] if call else []}


def _invite_hold(mode, store, as_of, person, event):
    """Why this person can't be recorded as invited to ``event`` now, or None."""
    if event.day <= date.fromisoformat(as_of[:10]):
        return f"{event.name} has already happened"
    if reason := _replay_hold(mode, person, "never invited"):
        return reason
    hold = _asked_to_wait(journey.assess(store, person.subject_id, as_of, person.role)) or \
        ledger_hold(store, person.subject_id, as_of)
    return f"{person.name} is held: {hold}" if hold else None


def _replay_hold(mode, person, never):
    """Why no ask to this person may be recorded from here, by the people file: they are one of its replay cases
    (``never`` says what they never get), or it won't read, so nobody can be told apart from one. Else None."""
    moment, error = _moment_people(mode)
    if error:  # a fixed sentence: a press's refusal goes to Slack, never the file's path or rows
        return UNREAD_MARK
    if contact.person_key(person.subject_id) in moment:
        return f"{person.name} is one of the scorecard's moment people: {never}"
    return None


def mark(mode, event_id, subject_id, kind, by, also_invite=None):
    """Record in the contact ledger that a host sent this person the invite (``invited``) or the
    follow-up (``sent``). Nothing is sent from here.

    ``also_invite``: the next event's id, when the follow-up's draft carried its invite. Both go in
    together, from the same person, or neither: one note is one ask."""
    store, as_of, records, event, person = _open(mode, event_id, subject_id)
    if kind == "invited":
        if reason := _invite_hold(mode, store, as_of, person, event):
            raise ValueError(reason)
        contact.record(store, subject_id, "invited", at=as_of, team="events", by=by, event_id=event.id,
                       until=event.day.isoformat(), note=event.name)
    elif kind == "sent":
        if reason := _replay_hold(mode, person, "never followed up from here"):  # as the page offers them none
            raise ValueError(reason)
        found = next((f for f in follow_ups(store, event, [person], parse_time(as_of)) if f["subject_id"] == subject_id),
                     None)
        if not found or found["sent"]:
            raise ValueError(f"No follow-up to send {person.name} after {event.name}"
                             if not found else f"Already followed up by {found['sent']['by']} on {found['sent']['at']}")
        if found["hold"]:
            raise ValueError(f"{person.name} is held: {found['hold']}")
        nxt = next((e for e in records["events"] if e.id == also_invite), None) if also_invite else None
        if also_invite and not nxt:
            raise LookupError("Unknown event")
        if nxt and (reason := _invite_hold(mode, store, as_of, person, nxt)):  # checked before either is recorded
            raise ValueError(reason)
        if nxt:  # only the invite the page put in this follow-up's draft: a pick for that night, window, seats and all
            shown = page(mode)["last"] or {}
            carried = next((f for f in shown.get("follow_ups", []) if f["subject_id"] == subject_id), {})
            if shown.get("event", {}).get("id") != event.id or carried.get("invites_to_id") != nxt.id:
                raise ValueError(f"{person.name}'s follow-up carries no invite to {nxt.name}")
        contact.record(store, subject_id, "sent", at=as_of, team="events", by=by, event_id=event.id,
                       note=f"Follow-up after {event.name}" + (f", with the invite to {nxt.name}" if nxt else ""))
        if nxt:
            contact.record(store, subject_id, "invited", at=as_of, team="events", by=by, event_id=nxt.id,
                           until=nxt.day.isoformat(), note=f"{nxt.name}, in the follow-up after {event.name}")
    else:
        raise ValueError("Mark an invite or a follow-up")
    return {"subject_id": subject_id, "hold": ledger_hold(store, subject_id, as_of)}
