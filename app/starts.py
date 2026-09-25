"""New starts: from an accepted offer to a first day, the few things that must be ready, each with an owner.

Per hire: the start date confirmed in writing, visa, relocation, office and desk, laptop and accounts. Each
item is due a set number of days before the start (never before the offer was accepted) and has one owner,
one of GI's ops people. Visa and relocation start as "Ask at the offer": only what the candidate says at the
offer stage decides them, typed in by the ops person. Nothing here infers either from a profile, a name or
anything the engine read. The Monday brief lists the items falling due; this page is where they are done.

Real hires go in research/private/hiring/starts.json (gitignored); the simulation's are invented
(tests/fixtures/hiring/starts.json) and kept in memory.
"""

import json
import os
import tempfile
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import contact, hiring, today
from .models import iso

ROOT = Path(__file__).resolve().parents[1]
FILES = {"live": ROOT / "research/private/hiring/starts.json"}
DEMO = ROOT / "tests/fixtures/hiring/starts.json"
# Each item: what it is, and how many days before the start it must be done.
ITEMS = {"start_date": ("Start date confirmed in writing", 30), "visa": ("Visa", 60),
         "relocation": ("Relocation", 30), "desk": ("Office and desk", 14), "laptop": ("Laptop and accounts", 7)}
ASKED = ("visa", "relocation")  # decided only by what the candidate says at the offer stage
PRIVATE = ("visa",)  # never outside this page, not even as a count: a count or a due day would give it away
STATES = {"ask": "Ask at the offer", "open": "To do", "done": "Done", "not_needed": "Not needed"}
Key = Literal["start_date", "visa", "relocation", "desk", "laptop"]
_memory = {}  # the simulation's
_saving = threading.RLock()  # one change at a time, from reading the file to writing it


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    owner: str = Field("", max_length=80)
    state: Literal["ask", "open", "done", "not_needed"] = "open"
    note: str = Field("", max_length=300)  # visa and relocation: what the candidate said at the offer stage


class Hire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=2, max_length=120)
    role_id: str
    office: str
    start: date
    offer_on: date  # the day the offer was accepted: nothing is due before it
    subject_id: str | None = None  # when the hire came from the plan, the engine's person
    items: dict[Key, Item]


class Starts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = ""
    owners: list[str] = Field(default_factory=list)  # GI's ops people
    default_owner: dict[Key, str] = Field(default_factory=dict)
    hires: list[Hire] = Field(default_factory=list)


def load(mode):
    """(the starts, why a saved live file could not be read, or None)."""
    if mode == "simulation":
        if "starts" not in _memory:
            _memory["starts"] = Starts.model_validate(json.loads(DEMO.read_text()))
        return _memory["starts"], None
    path = FILES[mode]
    if not path.exists():
        return Starts(), None
    try:
        return Starts.model_validate_json(path.read_text()), None
    except (OSError, ValueError) as err:  # left as it is: the next change fails rather than overwrite it
        return None, f"The new starts at {path} could not be read, so nothing is shown and nothing can be changed " \
                      f"until it is fixed. ({str(err).splitlines()[0]})"


def save(mode, starts):
    if mode == "simulation":
        _memory["starts"] = starts
        return
    path = FILES[mode]
    path.parent.mkdir(parents=True, exist_ok=True)
    with _saving, tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp", delete=False) as tmp:
        try:
            tmp.write(starts.model_dump_json(indent=2))
            tmp.close()
            os.replace(tmp.name, path)  # whole or not at all
        except BaseException:
            Path(tmp.name).unlink(missing_ok=True)
            raise


def reset():
    _memory.clear()


def _clock(mode):
    found = today.source(mode)
    return date.fromisoformat(found[1][:10]) if found else date.today()


def _open(mode):
    starts, problem = load(mode)
    if problem:
        raise ValueError(problem)
    return starts


def _check(hire):
    offices = {r["id"]: r.get("locations") or [] for r in hiring._roles()}
    if hire.role_id not in offices:
        raise ValueError(f"Not a role in config/roles.json: {hire.role_id}")
    if hire.office not in offices[hire.role_id]:
        raise ValueError(f"Not an office in the role's JD: {hire.office}")
    if hire.start < hire.offer_on:
        raise ValueError("The start date is before the offer was accepted")
    for key, item in hire.items.items():
        if item.state in ("ask", "not_needed") and key not in ASKED:
            raise ValueError(f"{ITEMS[key][0]} is always needed")


def add(mode, name, role_id, office, start, subject_id=None, offer_on=None):
    """An accepted offer: the hire and their five items, owned as the ops team set by default. ``offer_on`` is
    the day they accepted (by default today): nothing falls due before it."""
    with _saving:
        starts = _open(mode)
        day = _clock(mode)
        offer_on = offer_on or day
        if (date.fromisoformat(offer_on) if isinstance(offer_on, str) else offer_on) > day:
            raise ValueError("The offer date is in the future")
        if subject_id and any(h.subject_id == subject_id for h in starts.hires):
            raise ValueError(f"{name} is already on New starts")
        hire = Hire(id=uuid.uuid4().hex[:12], name=name.strip(), role_id=role_id, office=office,
                    start=start, offer_on=offer_on,
                    subject_id=subject_id or None,
                    items={k: Item(owner=starts.default_owner.get(k, ""), state="ask" if k in ASKED else "open")
                           for k in ITEMS})
        _check(hire)
        save(mode, starts.model_copy(update={"hires": [*starts.hires, hire]}))
        _ledger(mode, hire, f"Starts {hire.start.isoformat()} ({hire.role_id}).")
    return hire


def hold_all(mode, store=None, at=None):
    """Everyone on New starts with a subject id is held in the contact ledger (``store``, by default the workspace's):
    a hire saved before the ledger knew them, or while this machine had no timelines store, gets their entry now.
    Safe to re-run: someone whose hired entry still holds is left as it is; one whose entry was ended (an offer that
    fell through) and who is back on New starts is held again. Returns why the saved hires could not be read, else
    None."""
    found = today.source(mode) if store is None else (store, at)
    if not found:
        return load(mode)[1]
    at = found[1] or iso()
    with _saving:  # read under the lock too: a remove() in between would be held again, for good (in this process;
        # route.py run beside the web app is another process, which this lock can't cover)
        starts, problem = load(mode)
        if problem:
            return problem
        for h in starts.hires:
            if h.subject_id and not contact.joining(contact.history(found[0], h.subject_id), at):
                contact.record(found[0], h.subject_id, "hired", at=at, by="New starts",
                               note=f"Starts {h.start.isoformat()} ({h.role_id}).")


def _ledger(mode, hire, note, until=None):
    """Tell the contact ledger: someone who accepted an offer is no longer a candidate, on every path (Today's calls,
    Slack cards, invites, the brief), until they are taken off before their start. A hire typed in by hand has no
    subject id, so there is no one to hold."""
    found = today.source(mode)
    if hire.subject_id and found:
        contact.record(found[0], hire.subject_id, "hired", at=found[1], by="New starts", until=until, note=note)


def update(mode, hire_id, key, owner=None, state=None, note=None):
    """Change one item: its owner, its state or its note. The note is what the candidate said, as typed."""
    if key not in ITEMS:
        raise ValueError("Not an item on the checklist")
    with _saving:
        starts = _open(mode)
        hire = next((h for h in starts.hires if h.id == hire_id), None)
        if not hire:
            raise LookupError("No such hire")
        if owner and starts.owners and owner not in starts.owners:
            raise ValueError(f"Not one of the ops owners: {', '.join(starts.owners)}")
        if note and key not in ASKED:
            raise ValueError("Only visa and relocation take a note: what the candidate said at the offer stage")
        item = {**hire.items[key].model_dump(),
                **{k: v for k, v in (("owner", owner), ("state", state), ("note", note)) if v is not None}}
        changed = Hire.model_validate({**hire.model_dump(), "items": {**hire.model_dump()["items"], key: item}})
        _check(changed)
        save(mode, starts.model_copy(update={"hires": [changed if h.id == hire_id else h for h in starts.hires]}))
    return changed


def remove(mode, hire_id):
    with _saving:
        starts = _open(mode)
        hire = next((h for h in starts.hires if h.id == hire_id), None)
        if not hire:
            raise LookupError("No such hire")
        save(mode, starts.model_copy(update={"hires": [h for h in starts.hires if h.id != hire_id]}))
        if hire.start > (day := _clock(mode)):  # an offer that fell through: a candidate again. Once started, never.
            _ledger(mode, hire, "Taken off New starts before their start.", until=day.isoformat())


def due(hire, key):
    return max(hire.start - timedelta(days=ITEMS[key][1]), hire.offer_on)


def _open_items(hire):
    return [k for k, i in hire.items.items() if i.state in ("ask", "open")]


def view(hire, day):
    """One hire as the page shows it: each item with its due day, and whether it is late."""
    items = [{"key": k, "label": ITEMS[k][0], "owner": hire.items[k].owner, "state": hire.items[k].state,
              "note": hire.items[k].note, "asked": k in ASKED, "due": due(hire, k).isoformat(),
              "overdue": hire.items[k].state in ("ask", "open") and due(hire, k) < day} for k in ITEMS]
    return {"id": hire.id, "name": hire.name, "role_id": hire.role_id, "office": hire.office,
            "start": hire.start.isoformat(), "offer_on": hire.offer_on.isoformat(), "subject_id": hire.subject_id,
            "items": items, "open": len(_open_items(hire)), "started": hire.start <= day}


def page(mode):
    starts, problem = load(mode)
    day = _clock(mode)
    roles = [{"id": r["id"], "title": r["title"], "offices": r.get("locations") or []} for r in hiring._roles()]
    if problem:
        return {"as_of": day.isoformat(), "problem": problem, "hires": [], "owners": [], "roles": roles,
                "from_plan": [], "states": STATES}
    plan, _ = hiring.load(mode, day.isoformat())
    taken = {h.subject_id for h in starts.hires if h.subject_id}
    from_plan = [{"subject_id": line.subject_id, "name": line.name, "role_id": line.role_id, "office": line.office,
                  "start": f"{plan.assumptions.year}-{line.start_month:02d}-01"}
                 for line in plan.lines if line.subject_id and line.name and line.subject_id not in taken]
    hires = sorted((view(h, day) for h in starts.hires), key=lambda h: (h["started"], h["start"], h["name"]))
    return {"as_of": day.isoformat(), "problem": None, "hires": hires, "owners": starts.owners, "roles": roles,
            "from_plan": from_plan, "states": STATES,
            "note": starts.note if mode == "simulation" else ""}


def decisions(mode, day, horizon):
    """For the Monday brief: each hire with an item due by ``horizon`` (a date) or late, soonest first, with
    the open items as (label, owner, due). The visa is left out whole, due day and all: it is only on this page."""
    starts, problem = load(mode)
    if problem:
        return None, problem
    out = []
    for h in starts.hires:
        if h.start < day:
            continue  # already started: what is left is theirs and their manager's
        keys = [k for k in _open_items(h) if k not in PRIVATE and due(h, k) <= horizon]
        if not keys:
            continue
        first = min(due(h, k) for k in keys)
        out.append({"id": h.id, "name": h.name, "role_id": h.role_id, "start": h.start.isoformat(),
                    "due": first.isoformat(), "overdue": first < day, "keys": keys,
                    "items": [(ITEMS[k][0], h.items[k].owner, due(h, k).isoformat()) for k in keys]})
    return sorted(out, key=lambda x: (x["due"], x["name"])), None
