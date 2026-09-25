"""The contact ledger, and the gate every path that could reach a person goes through.

readiness decides whether now is the moment; this gate never re-decides that. It holds a person the contact history
says to leave alone, and asks for a check before a reach nobody has vouched for. engine.decide (the web app's pages and
inbox), routing.route / routing.send (the Slack card), the Events page (events.py) and the accounts list (accounts.py,
team sales) all call check().

The ledger lives in the store its path reads: the web app's (data/pilot.sqlite, people keyed person:<mode>:<hash>) or
the timing run's (research/private/social/timelines.sqlite, people keyed by their subject id). history() also reads the
other ledger's CROSS entries for the same person, matched by a profile URL both know them by (a web-app candidate's
profile, a subject's anchors), so an opt-out, a send or a partner hold recorded in either holds every path. Everything
else stays in the ledger that recorded it.

The ledger is append-only: one entry per thing that happened between GI and a person, or that GI confirmed about them.
  pinged         a card about them went to GI's channel (with the call's evidence and score): never another one;
                 team sales: a line on GTM's accounts list, which holds hiring for the quiet period only
  sent           a GI person sent the note
  follow_up      the one follow-up went out
  replied        they answered: a conversation is open
  not_now        they asked to wait until a day; also a note_wait on their timeline, so readiness holds until the day
                 and then treats it as a reason to write
  never          they asked not to be contacted: every role, every sender, for good
  wrong_person   the profile is not them
  identity       two independent pages link the profile to them (their site links their X, ...); for a Slack card,
                 every profile the engine reads for them is tied to a page on another site
  checked        someone checked that an old trigger still stands
  undergraduate  a note about their work only, never a pitch, until the day given, if any
  unconfirmed    whether they're still a student isn't confirmed (the note says why): every path asks to check first,
                 naming it, and a card's draft is about their work only, until a later undergraduate entry settles it
                 (with ``until`` their graduation day, if it has passed); the Events page still invites them, and its
                 host pitches nothing
  invited        asked to a GI event (``event_id``; the note names it) on ``until``: nothing else goes out before then
  partner_staff  works at a company GI partners or sells to: a founder decides before anyone approaches them, until the
                 day given, if any. An account in talks with GI (talks_note, app/accounts.py) holds on its own until
                 its own end; any other partner_staff entry replaces every hold before it (partner_holds)
  knows          someone at GI knows them (``by``; how in ``note``), said on the Slack card: a way in for their next
                 card, never a hold
  hired          accepted GI's offer (added to New starts): no longer a candidate, for every role and team; a second
                 entry with ``until`` ends it (taken off New starts before their start)

Entries are keyed by the person, not the role: a subject id's role prefix (mts-research:ann, backend:ann) is dropped,
so every role reads one history. That needs one slug per human: the same person keeps their slug in every role, and two
people never share one.

Every entry names its team: recruiting, events, sales or marketing. The gate reads every team's entries, so a person
has one open ask from GI at a time, and a hold names who owns it.
"""

import re
from collections import defaultdict
from datetime import date, timedelta
from functools import lru_cache
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from sqlalchemy.exc import SQLAlchemyError

from .models import iso, parse_time
from .store import Store

FOLLOW_UP_DAYS = 7    # one follow-up, about a week after the note with no reply
QUIET_DAYS = 90       # after a note goes out (or a line on GTM's list), no new approach for this long; a card, for good
STALE_DAYS = 21       # a trigger nobody has seen or checked for this long becomes check first
WEEKLY_CAP = 3        # cards per role per week: the few worth GI's attention, not everyone who qualifies
TOTAL_CAP = 4         # cards per week across every role: what GI's two ops people can act on

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
SHARED = Path("/mnt/project-files/private/social")  # the project's shared folder, in the cloud


def _here(path):
    """A path from the environment, read against the repo when relative (the scripts run from it)."""
    return Path(path) if Path(path).is_absolute() else ROOT / path


def _sqlite(url):
    """The file a sqlite:/// URL names, else None (another database, or none)."""
    return _here(url.removeprefix("sqlite:///")) if url and url.startswith("sqlite:///") else None


# The pull's folder (scripts/social_pull.py, the daily run): SOCIAL_DIR, else the shared folder in the cloud, else
# research/private/social.
SOCIAL = (_here(os.getenv("SOCIAL_DIR")) if os.getenv("SOCIAL_DIR")
          else SHARED if SHARED.parent.parent.is_dir() else ROOT / "research/private/social")
# The timing run's store (today.TIMELINES): the one contact ledger the engine, route.py, the Slack buttons, the daily
# run and its pull, the replay drill, accounts.py and scripts/contacts.py all read and write. TIMELINES_DB, else the
# pull's timelines.sqlite. DATABASE_URL never picks it.
TIMELINES = _here(os.getenv("TIMELINES_DB")) if os.getenv("TIMELINES_DB") else SOCIAL / "timelines.sqlite"
# The web app's store (app/main.py): a sqlite DATABASE_URL, else data/pilot.sqlite. An older launchd job may carry a
# DATABASE_URL naming the timing store; the web app's is then its default, as it was. A DATABASE_URL of another kind
# (PostgreSQL) is no file, so nothing crosses to or from it.
WEB = _sqlite(os.getenv("DATABASE_URL")) or ROOT / "data/pilot.sqlite"
if WEB.resolve() == TIMELINES.resolve():
    WEB = ROOT / "data/pilot.sqlite"
# The two ledgers: the web app's and the timing run's. Each reads the other's CROSS entries.
LEDGERS = {"the web app": WEB, "the timing run": TIMELINES}
# What crosses: what went to them, what they said, that they accepted GI's offer, and a founder's hold while their
# company is in talks with GI.
CROSS = frozenset({"never", "sent", "follow_up", "replied", "not_now", "invited", "hired", "partner_staff"})
# The pause switch: while this file exists nothing posts to Slack (routing._post), cards or briefs.
PAUSE = Path(os.getenv("GI_PAUSE_FILE") or ROOT / "research/private/sending-paused.json")

Kind = Literal["pinged", "sent", "follow_up", "replied", "not_now", "never", "wrong_person", "identity",
               "checked", "undergraduate", "unconfirmed", "invited", "partner_staff", "knows", "hired"]


class Entry(BaseModel):
    person_id: str = Field(min_length=1)
    kind: Kind
    at: str
    role_id: str = ""
    team: Literal["recruiting", "events", "sales", "marketing"] = "recruiting"
    by: str = ""                # the GI person, when known
    until: str | None = None    # a day: when a not_now ends, or an undergraduate's expected graduation
    links: list[str] = Field(default_factory=list)     # identity: the pages that link the profile to them
    evidence: list[str] = Field(default_factory=list)  # pinged: the timeline event ids the call rested on
    score: float | None = None  # pinged: the call's readiness
    note: str = ""
    event_id: str = ""          # from the events loop (an invite, a wait said at the door, a follow-up): the event's id

    @model_validator(mode="after")
    def complete(self):
        parse_time(self.at)
        if self.until:
            self.until = date.fromisoformat(self.until[:10]).isoformat()
        if self.kind == "not_now" and not self.until:
            raise ValueError("not_now needs the day they asked us to wait until")
        if self.kind == "invited" and not self.until:
            raise ValueError("invited needs the event's day")
        if self.kind == "identity" and len({(urlparse(u).hostname or "").removeprefix("www.") for u in self.links} - {""}) < 2:
            raise ValueError("identity needs two links on two different sites")
        return self


# What an unconfirmed entry says when its note is empty.
UNSURE = "We couldn't confirm whether they're a student: confirm before pitching the role."


class Clearance(BaseModel):
    state: Literal["clear", "check_first", "hold"]
    reason: str = ""
    rapport_only: bool = False  # an undergraduate: a note about their work, never the role
    follow_up_due: bool = False


SAME_SITE = {"twitter.com": "x.com", "mobile.twitter.com": "x.com"}


PAGE_QUERY = {"scholar.google.com": "user", "openreview.net": "id"}  # sites whose page is named by a query parameter


def _page(url):
    """(site, path) of a URL, so a link can be matched to a profile: no scheme, www. or m., lowercase, no end slash,
    no query (a share link's ?s=21 is the same page), but for the sites whose page a query names (PAGE_QUERY); a
    country's LinkedIn (uk.linkedin.com) or Google Scholar (scholar.google.co.uk) is the one site."""
    parts = urlparse(url if "://" in url else f"https://{url}")
    host = (parts.hostname or "").removeprefix("www.").removeprefix("m.")
    host = "linkedin.com" if host.endswith(".linkedin.com") else \
        "scholar.google.com" if host.startswith("scholar.google.") else SAME_SITE.get(host, host)
    key = PAGE_QUERY.get(host)
    named = [q for q in parts.query.split("&") if key and q.split("=", 1)[0] == key]
    return host, parts.path.lower().rstrip("/") + (f"?{named[0]}" if named else "")


def _author(url):
    """A link's page, with a LinkedIn post (/posts/<slug>_...) read as its author's profile (/in/<slug>); an X post
    or a GitHub repo is already a page under its author's profile."""
    site, path = _page(url)
    if site == "linkedin.com" and path.startswith("/posts/") and "_" in path:
        return site, "/in/" + path.removeprefix("/posts/").split("_", 1)[0]
    return site, path


def tied(links, profiles):
    """The sites of the profiles these links are, or are pages of (their site's about page counts as their site, and
    a post counts as its author's profile)."""
    pages = [_author(u) for u in links]
    return {site for site, path in map(_page, profiles) if site
            and any(s == site and (p == path or p.startswith(path + "/")) for s, p in pages)}


def untied(records, profiles):
    """The profiles no identity record ties to a page on another site. ``records`` are the records' links: a record
    ties a profile when one of its links is the profile or a page of it and another is on a different site. With
    one profile read, one record from it to their site is enough; with several, each needs a tie."""
    return [u for u in profiles if not any(
        tied(links, [u]) and any(_page(v)[0] not in ("", _page(u)[0]) for v in links) for links in records)]


def person_key(subject_id):
    """The ledger's key for a subject: its role prefix dropped, so one person has one history across roles."""
    prefix, _, rest = subject_id.partition(":")
    return rest if rest and prefix in role_prefixes() else subject_id


def role_prefixes():
    """The role ids a subject id can start with (config/roles.json's and the specs'), read again when either changes,
    so a role dropped in while the app runs counts at once."""
    return _prefixes(CONFIG, (CONFIG / "roles.json").stat().st_mtime_ns, (CONFIG / "role-specs").stat().st_mtime_ns)


@lru_cache(maxsize=1)
def _prefixes(config, *_changed):
    return frozenset({p.stem for p in (config / "role-specs").glob("*.json")}
                     | {r["id"] for r in json.loads((config / "roles.json").read_text())["roles"]})


def record(store, person_id, kind, at=None, now=None, supersedes=None, **fields):
    """Add one entry. A not_now also becomes a note_wait, which is how readiness holds and later reopens.

    ``now`` is a workspace's clock for that note, such as the simulation's (by default the real time), and
    ``supersedes`` the note it corrects.
    """
    entry = Entry(person_id=person_key(person_id), kind=kind, at=at or iso(), **fields)
    if kind == "not_now":
        from .timeline import add_private_note  # timeline imports engine, which imports this module

        # The note is stamped now, whatever ``at`` says, and add_private_note refuses a day already past:
        # a wait is recorded when they ask for it.
        add_private_note(store, person_id, f"Asked us to wait until {entry.until}. {entry.note}".strip(),
                         entry.by or "contact ledger", supersedes=supersedes, note_type="wait", wait_until=entry.until,
                         now=now)
    return store.put(f"contact:{uuid4().hex}", "contact", entry.model_dump())


def history(store, person_id):
    """The person's entries, with the other ledger's CROSS entries for them (``elsewhere``), oldest first."""
    key = person_key(person_id)
    own = [e for e in store.all("contact") if e["person_id"] == key]
    return sorted(own + elsewhere(store, person_id), key=lambda e: parse_time(e["at"]))


_opened = {}


def _file(store):
    engine = getattr(store, "engine", None)  # a test's in-memory store has none
    database = engine.url.database if engine is not None and engine.dialect.name == "sqlite" else None
    return Path(database).resolve() if database and database != ":memory:" else None


def profiles_of(store, person_id):
    """The profile pages a ledger's store knows this person by: a timing subject's anchors, or the profile URLs of the
    web app's candidates linked to them."""
    context = store.get("person-context:" + person_id) or store.get("person-context:" + person_key(person_id))
    urls = list((context or {}).get("anchors", {}).values())
    if person_id.startswith("person:"):
        urls += [c["profile_url"] for c in store.all("candidate") if c.get("person_id") == person_id]
    return _pages(urls)


def _pages(urls):
    """The (site, path) of each URL among these (an OpenAlex id is not a page)."""
    return {_page(u) for u in urls if isinstance(u, str) and u.startswith(("https://", "http://"))}


def elsewhere(store, person_id):
    """The other ledger's CROSS entries for the person this store calls ``person_id``: entries under the same key, and
    the people there who share a profile page with them, each marked with the ledger it came from. Only between the
    two LEDGERS: a demo's or a test's own store reads nothing across, and an invented simulation person never
    crosses. Read-only; a ledger with no file on this machine is skipped. One that can't be read gives an
    "unreadable" entry, which check() holds on: the gate fails closed, and pages still load."""
    own = _file(store)
    if own not in {path.resolve() for path in LEDGERS.values()} or person_id.startswith("person:simulation:"):
        return []
    found, pages = [], None
    for name, path in LEDGERS.items():
        if not path.exists() or path.resolve() == own:
            continue
        pages = profiles_of(store, person_id) if pages is None else pages
        try:
            entries, people = _read(_open(path))
        except BROKEN as e:
            found.append({**Entry(person_id=person_key(person_id), kind="checked", at="1970-01-01T00:00:00+00:00",
                                  note=type(e).__name__).model_dump(), "kind": "unreadable", "ledger": name})
            continue
        ids = {person_key(person_id)} | {key for key, shown in people if shown & pages}
        found += [{**e, "ledger": name} for pid, kind, e in entries if pid in ids and kind in CROSS]
    return found


BROKEN = (SQLAlchemyError, OSError, ValueError, KeyError, TypeError, AttributeError)  # what a ledger that can't be read raises


def _read(other):
    """The other ledger read whole, as elsewhere() needs it: its contact entries as (person, kind, entry), and each of
    its timing subjects and web candidates as (person key, profile pages). Raises what a broken ledger raises, on any
    row, so unreadable() and elsewhere() never disagree."""
    people = [(person_key(c["subject_id"]), _pages((c.get("anchors") or {}).values()))
              for c in other.all("person_context")]
    people += [(c["person_id"], _pages([c.get("profile_url")])) for c in other.all("candidate") if c.get("person_id")]
    return [(e["person_id"], e["kind"], e) for e in other.all("contact")], people


def _broken(ledger, why):
    return f"{ledger[:1].upper()}{ledger[1:]}'s contact ledger can't be read ({why}), so no one is cleared until it's fixed."


def unreadable(store):
    """Why the other ledger can't be read, in check()'s words, else None. check() then holds everyone, so the Monday
    brief and the Hiring budget say this once rather than leave an empty week unexplained. Read-only."""
    own = _file(store)
    if own not in {path.resolve() for path in LEDGERS.values()}:
        return None
    for name, path in LEDGERS.items():
        if path.exists() and path.resolve() != own:
            try:
                _read(_open(path))
            except BROKEN as e:
                return _broken(name, type(e).__name__)
    return None


def _open(path):
    """The other ledger's store, opened once per file (a rebuilt file is opened afresh)."""
    key = (path, path.stat().st_ino)
    return _opened.get(key) or _opened.setdefault(key, Store(f"sqlite:///{path}"))


def _day(entry):
    return entry["at"][:10]


def unsure(history, now):
    """Why to check whether they're still a student before the role is raised: an ``unconfirmed`` entry no later
    undergraduate entry settles, as its note (else UNSURE) and when it was noted; else None. A card's gate names it
    and keeps the draft to their work (check); the Events page's host pitches nothing (events.page)."""
    now = parse_time(now) if isinstance(now, str) else now
    # newest first; of two at one time, the one recorded later (sorted keeps the order they were recorded in)
    entry = next((e for e in reversed(sorted(history, key=lambda e: parse_time(e["at"])))
                  if e["kind"] in ("unconfirmed", "undergraduate") and parse_time(e["at"]) <= now), None)
    if not entry or entry["kind"] != "unconfirmed":
        return None
    said = (entry["note"] or "").strip().rstrip(":;,-–— ") or UNSURE
    ends = re.search(r"[.!?…][\"”’')]*$", said)
    return f"{said}{'' if ends else '.'} Noted {_day(entry)}; until it's settled, nobody raises the role."


TALKS = re.compile(r" is in talks with GI \(account (.+)\)$")  # the note an account in talks puts on its holds


def talks_note(name, account_id):
    return f"{name} is in talks with GI (account {account_id})"


def talks_account(entry):
    """The id of the account in talks whose hold this partner_staff entry is, else None."""
    return m[1] if (m := TALKS.search(entry.get("note") or "")) else None


def partner_holds(entries, today):
    """The partner_staff holds in force on ``today`` (a day), from ``entries`` in time order: one per account in
    talks (its latest entry, so an end ends only that account's hold), and the latest other partner_staff entry,
    which replaces every hold before it (they left, or work somewhere else now)."""
    holds = {}
    for e in entries:
        if e["kind"] != "partner_staff":
            continue
        talks = talks_account(e)
        if not talks:
            holds = {}
        holds[talks or ""] = e
    return [e for e in holds.values() if not e["until"] or e["until"] > today]


def _by(entry, word="by"):
    """Who owns the entry, with their team: " by Dana (recruiting)"; the team alone when nobody is named."""
    team = entry.get("team", "recruiting")
    who = f"{entry['by']} ({team})" if entry["by"] else team if team != "recruiting" else ""
    return f" {word} {who}" if who else ""


def title(role_id):
    """A role's job title from config/roles.json, as every surface names it (the Monday brief, Today's calls, the
    Events page), else its id: a role no longer listed, or a file that won't read, still holds with its id."""
    try:
        return next((r["title"] for r in json.loads((CONFIG / "roles.json").read_text())["roles"]
                     if r.get("id") == role_id and r.get("title")), role_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return role_id


def check(history, now, call=None, signals=None, role_id=None, profiles=None):
    """Whether GI may reach this person now, given what already happened: clear, check_first or hold.

    ``call`` is readiness's reach_now for ``role_id`` (the undergraduate check reads it). One card per person, ever:
    after a card, no role and no new reason gets another, however long ago it was, unless they asked us to come back.
    A line on GTM's accounts list holds them for the quiet period only.

    ``signals``, the events behind the call, turn on the checks for a path with no human review before the card (the
    Slack card): identity across two links, a stale trigger, undergraduates rapport only. ``profiles`` are the profile
    URLs the engine reads for them (their context's anchors): with signals, identity counts only when the records tie
    every one of those profiles to a page on another site (untied). engine.decide's reviewed path confirms identity and
    rechecks sources itself.

    An open ``unconfirmed`` entry (``unsure``) asks to check first on every path once no hold applies, and any check
    first then names it, after its own reason, and keeps the draft to their work (rapport_only).
    """
    now = parse_time(now) if isinstance(now, str) else now
    today = now.date().isoformat()
    past = sorted((e for e in history if parse_time(e["at"]) <= now), key=lambda e: parse_time(e["at"]))

    def last(*kinds, entries=past):
        return next((e for e in reversed(entries) if e["kind"] in kinds), None)

    def hold(reason, **extra):
        return Clearance(state="hold", reason=reason, **extra)

    doubt = unsure(past, now)

    def check_first(reason=""):
        if not doubt:
            return Clearance(state="check_first", reason=reason)
        return Clearance(state="check_first", rapport_only=True, reason=" ".join(filter(None, [reason, doubt])))

    if broken := last("unreadable"):  # the other ledger (elsewhere) couldn't be read: nobody is cleared
        return hold(_broken(broken["ledger"], broken["note"]))
    if never := last("never"):
        return hold(f"They asked not to be contacted ({_day(never)}): quiet for every role and every sender.")
    if hired := joining(past, now):  # before an identity doubt: whoever they are, nobody approaches a new hire
        return hold(f"Accepted GI's offer ({_day(hired)}): they're joining, so nobody approaches them as a candidate.")
    if (who := last("identity", "wrong_person")) and who["kind"] == "wrong_person":
        return check_first(f"Marked the wrong person on {_day(who)}: confirm who they are with two independent links.")
    if held := partner_holds(past, today):
        partner = max(held, key=lambda e: parse_time(e["at"]))
        return hold(f"Works at a GI partner{_by(partner, 'per')}, noted {_day(partner)}: a founder decides before "
                    "anyone from GI approaches them.")
    answer = last("replied", "not_now")
    if answer and answer["kind"] == "replied":
        return hold(f"In conversation since {_day(answer)}{_by(answer, 'with')}: no new approach.")
    if answer and answer["until"] > today:
        return hold(f"They asked us to wait until {answer['until']}.")
    if (invite := last("invited")) and invite["until"] >= today:
        return hold(f"Invited to {invite['note'] or 'a GI event'} on {invite['until']}{_by(invite)}: one ask at a "
                    "time, so nothing else goes out before then.")
    # Their answer (a wait that is now over) settles what went out before it: they asked for this day.
    since = [e for e in past if not answer or parse_time(e["at"]) > parse_time(answer["at"])]
    if (sent := last("sent", "follow_up", entries=since)) and (days := (now - parse_time(sent["at"])).days) < QUIET_DAYS:
        back = (parse_time(sent["at"]) + timedelta(days=QUIET_DAYS)).date().isoformat()
        if sent["kind"] == "follow_up":
            return hold(f"Followed up on {_day(sent)} with no reply: quiet until {back}.")
        if days < FOLLOW_UP_DAYS:
            return hold(f"Sent on {_day(sent)}{_by(sent)}: waiting for a reply.")
        return hold(f"No reply since {_day(sent)}: one follow-up is due{_by(sent, 'from')}, then quiet until {back}.",
                    follow_up_due=True)
    # One card per person, for every role and for good: no quiet period ends it, and no new reason. Only their own
    # answer does (a wait they asked for, now over, settles what went before it: ``since``).
    cards = [e for e in since if e["kind"] == "pinged"]
    if ping := last("pinged", entries=[e for e in cards if e.get("team", "recruiting") != "sales"]):
        seen = f"On a card from {_day(ping)}" + (f" ({title(ping['role_id'])})" if ping["role_id"] else "")
        return hold(f"{seen}: one card per person, ever.")
    # A line on GTM's accounts list (team sales) is not a hiring card: it holds them for the quiet period only.
    if (listed := last("pinged", entries=cards)) and (now - parse_time(listed["at"])).days < QUIET_DAYS:
        back = (parse_time(listed["at"]) + timedelta(days=QUIET_DAYS)).date().isoformat()
        return hold(f"On GTM's accounts list from {_day(listed)}: one team at a time, until {back}.")

    if signals is None:
        return check_first() if doubt else Clearance(state="clear")
    if not (who := last("identity")):
        read = f" ({', '.join(profiles) or 'none on file'})" if profiles is not None else ""
        return check_first("Confirm it's them before any reach: tie each profile the engine reads for them"
                           f"{read} to a page of theirs on another site, such as their X bio linking their GitHub or "
                           "their site.")
    wrong = last("wrong_person")
    confirmed = [e for e in past if e["kind"] == "identity" and (not wrong or parse_time(e["at"]) > parse_time(wrong["at"]))]
    if profiles is not None and (not profiles or (loose := untied([e["links"] for e in confirmed], profiles))):
        what = f"{', '.join(loose)} untied" if profiles else "none on file"
        return check_first(f"The identity record from {_day(who)} doesn't tie every profile the engine reads for them "
                           f"to a page on another site ({what}): record a link from each to a page of theirs on "
                           "another site.")
    newest = max([s["observed_at"] for s in signals] + [e["at"] for e in past if e["kind"] == "checked"],
                 key=parse_time, default=None)
    if not newest or now - parse_time(newest) > timedelta(days=STALE_DAYS):
        return check_first(f"The newest sign is from {newest[:10] if newest else 'an unknown day'}, over three weeks "
                           "old: check nothing has superseded it, then record the check.")
    if doubt:
        return check_first()
    rapport_only = undergraduate(past, now)
    if rapport_only:
        from .readiness import news, opener  # readiness imports timeline, which imports engine, which imports this

        if not opener(call) and not news(call, signals):
            return hold("An undergraduate: a note about their work only, and there is nothing of theirs to write about yet.")
    return Clearance(state="clear", rapport_only=rapport_only)


def undergraduate(history, now):
    """Whether the ledger says they're an undergraduate as of ``now``: a note about their work only, never the role."""
    now = parse_time(now) if isinstance(now, str) else now
    student = next((e for e in reversed(sorted(history, key=lambda e: parse_time(e["at"])))
                    if e["kind"] == "undergraduate" and parse_time(e["at"]) <= now), None)
    return bool(student) and (not student["until"] or student["until"] > now.date().isoformat())


def room(store, role_id, now, cap=WEEKLY_CAP):
    """Cards this role may still post this week (pings from the 7 days up to ``now``, not after: a replay's
    morning never sees a later card)."""
    now = parse_time(now) if isinstance(now, str) else now
    return max(cap - sum(1 for e in store.all("contact") if e["kind"] == "pinged" and e["role_id"] == role_id
                         and now - timedelta(days=7) < parse_time(e["at"]) <= now), 0)


def room_all(store, now, cap=TOTAL_CAP):
    """Hiring cards every role together may still post this week, on top of each role's own cap (room). Only
    recruiting's cards count: GTM's accounts list (team sales) keeps its own cap."""
    now = parse_time(now) if isinstance(now, str) else now
    return max(cap - sum(1 for e in store.all("contact") if e["kind"] == "pinged"
                         and e.get("team", "recruiting") == "recruiting"
                         and now - timedelta(days=7) < parse_time(e["at"]) <= now), 0)


def pause(by, why, at=None):
    """Stop every card and brief from leaving until resume(), recorded with who, when and why."""
    if not why.strip():
        raise ValueError("Say why sending is paused")
    PAUSE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE.write_text(json.dumps(record := {"at": at or iso(), "by": by, "why": why.strip()}, indent=1))
    return record


def resume():
    """Lift the pause: the pause it lifted, or None when there was none."""
    was = paused()
    PAUSE.unlink(missing_ok=True)
    return was


def paused():
    """The pause in force, {at, by, why}, or None. A pause file that can't be read is a pause: it fails closed."""
    if not PAUSE.exists():
        return None
    try:
        found = json.loads(PAUSE.read_text())
        return {"at": str(found["at"]), "by": str(found.get("by") or ""), "why": str(found["why"])}
    except (OSError, ValueError, KeyError, TypeError):
        return {"at": "", "by": "", "why": f"{PAUSE} can't be read, so sending stays paused until it's fixed or removed"}


def said(pause):
    """The pause in words: "Sending is paused (since 2026-09-24, by Justin): a wrong card went out." """
    since = ", ".join(filter(None, [f"since {pause['at'][:10]}" if pause["at"] else "", f"by {pause['by']}" if pause["by"] else ""]))
    return f"Sending is paused{f' ({since})' if since else ''}: {pause['why'].rstrip('.')}."


def follow_ups_due(store, now, teams=("recruiting", "events")):
    """(person_id, reason) for everyone whose note from one of ``teams`` went out a week ago with no reply and no
    follow-up yet. Ops' lists (the Monday brief, route.py) read hiring's and events' notes: GTM's are its own. None
    while the other ledger can't be read: the gate holds everyone then, follow-ups too."""
    if unreadable(store):
        return []
    people = defaultdict(list)
    for e in store.all("contact"):
        people[e["person_id"]].append(e)
    return [(pid, c.reason) for pid, entries in people.items() if (c := check(entries, now)).follow_up_due
            and _last_sent(entries, now).get("team", "recruiting") in teams]


def _last_sent(entries, now):
    """The newest note that went out by ``now``: the one a follow-up would answer, picked as check() picks it (of two
    at the same time, the one recorded last)."""
    at = parse_time(now) if isinstance(now, str) else now
    sent = sorted((e for e in entries if e["kind"] in ("sent", "follow_up") and parse_time(e["at"]) <= at),
                  key=lambda e: parse_time(e["at"]))
    return sent[-1] if sent else {}


def joining(history, now):
    """Their latest hired entry by ``now``, if it is still in force (no until, or an until after today), else None:
    they accepted GI's offer, so they are not a candidate for any role or team."""
    now = parse_time(now) if isinstance(now, str) else now
    entries = sorted((e for e in history if e["kind"] == "hired" and parse_time(e["at"]) <= now),
                     key=lambda e: parse_time(e["at"]))  # stable: of two at the same time, the later-recorded one
    hired = entries[-1] if entries else None
    return hired if hired and (not hired.get("until") or hired["until"] > now.date().isoformat()) else None


def opted_out(history):
    return any(e["kind"] in ("never", "unreadable") for e in history)  # a ledger that can't be read counts as a never


def relink(store, from_person, to_person):
    """Two records turned out to be one person: their history becomes one."""
    for e in store.all("contact"):
        if e["person_id"] == from_person:
            store.put(e["id"], "contact", {**e, "person_id": to_person}, expected=e["revision"])


def forget(store, person_id):
    """Only for the web app's invented people, whose replay resets them: a real person's history is never removed."""
    if not person_id.startswith("person:simulation:"):
        raise ValueError("Only an invented (simulation) person's contact history can be reset")
    for e in store.all("contact"):
        if e["person_id"] == person_id:
            store.remove(e["id"])


LEGACY = ("opt_out", "last_contact_at", "active_conversation")


def migrate(store):
    """The web app's old person flags become ledger entries, then leave the person record, so no opt-out is lost.

    Mark-sent used to set last_contact_at and active_conversation together and hold for good; it becomes a
    sent, which follows today's rule (a follow-up, then 90 days quiet).
    """
    for p in store.all("person"):
        if not any(p.get(flag) for flag in LEGACY):
            continue
        if p.get("opt_out"):
            store.put(f"contact:legacy-never:{p['id']}", "contact",
                      Entry(person_id=p["id"], kind="never", at=p["updated_at"]).model_dump(), only_new=True)
        if p.get("last_contact_at"):
            store.put(f"contact:legacy-sent:{p['id']}", "contact",
                      Entry(person_id=p["id"], kind="sent", at=p["last_contact_at"]).model_dump(), only_new=True)
        store.put(p["id"], "person", {k: v for k, v in p.items() if k not in LEGACY}, expected=p["revision"])
