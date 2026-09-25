"""The replay drill: step the timing store day by day over past weeks, as if the pull had run every morning, and
list what would have fired, how long after the post, which public moments got no card, and any rail that gave way.

    uv run python scripts/replay_drill.py [--days 42] [--end YYYY-MM-DD] [--hour 13] [--people FILE] [--sheet 20]
    uv run python scripts/replay_drill.py --demo      the invented people (tests/fixtures/posts), ending 2026-09-15

Free: no model calls (drafts are the free template), nothing is sent, and the timing store is never written.
With --redraft (paid, within --max-usd for the whole drill, $1.00): as route.py --redraft does each morning, the
model writes a draft for a card whose free draft fails its checks, checked the same way, and the card stays held if
it still fails. It is never given the DIAMOND fact (config/gi-facts.json), which is some replay cases' own outcome.
Its drafts are cached in route.py's own cache (research/private/routing/drafts.sqlite), so a rerun, or a later
morning with the same items, pays nothing: a cached draft is always used, and one that needs a new call is tried
only while the drill's spend plus that draft's worst case fits the cap. A model error holds that card, the drill
carries on and the report says so. Without a model key the drill runs free and says so. The
drill works on a copy in a temp directory. Each card it would post is recorded in the copy's ledger as pinged and
sent that morning, as if ops sent every card, so the next days test the never-twice rail and the weekly cap.
Identity entries already in the ledger count from the first day (who someone is does not change with time);
nothing else is backdated; the team and contacts files are today's. Each day runs route.py's own selection
(routing.pick) over routing.route for everyone with a context (or a people file's watchlist). The method was pre-registered
in the project's unpublished pre-registration log (2026-09-24, the replay drill, and its second run). Writes
research/private/routing/drill-<end>-<days>d-<run time>.json, with each person's calls morning by morning and each
public moment once (per person, kind and day, whatever pages report it): carded by a card that cites it, carded early by
a card in the 8 weeks before it, or else with its mornings and the one reason they add up to, which makes it held by
design (already on a card, a first-year hold, another hold) or missed (a failed draft, identity, or never reach now),
each draft that failed its checks once per person and text (what it said and the checks it failed), and a rating sheet (the same name, ending -sheet.md) of up to --sheet people (20): each one's
first card, or their first reach held only to confirm who they are, without the engine's confidence, score or why it
was held. A run never overwrites an earlier run's files, and it ends by saying what changed since the newest earlier
run in the same folder of the same people (--people) and mornings (--days, --end): cards gained, lost or moved by
person, holds by reason, moments carded, the commit and the time. A sheet card for someone GI's team file lists (a
replay case who has since joined GI) says so, so it is rated as a replay rather than a live card. So does a card that
rests on one of their GitHub repos (the repo, anything read from it, or its description quoted for a push): a repo is
dated by its creation and described as it is today (app/sources/github.py), so a replay can't vouch for it; the report
counts those cards, and the moments only they reached. A replay case's posts answering or quoting GI's own account
(the people file's, --cases, or --people's) are hidden, with anything read from them, as the scorecard hides them:
likely written from inside GI, their outcome.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import itertools
import json
from pathlib import Path
import random
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import contact, happened, journey, outreach, pay, routing, today  # noqa: E402
from app import scorecard as sc  # noqa: E402
from app.detectors import item, made_by_hand, their_words  # noqa: E402
from app.models import parse_time  # noqa: E402
from app.sources import x_follows  # noqa: E402
from app.store import Store  # noqa: E402

DEMO = Path("tests/fixtures/posts")
CASES = Path("research/private/early-signals/people.json")  # the replay cases, beside the watchlist
OUT = Path("research/private/routing")
EARLY_DAYS = 56  # a card up to 8 weeks before a moment is early; the scorecard's window


class Cached:
    """The copy, read once per kind until the drill writes that kind (only the ledger changes), with the items in
    ``hidden`` (and anything read from them) left out of the timelines."""

    def __init__(self, store, hidden=frozenset()):
        self._store, self._rows, self._hidden = store, {}, hidden

    def all(self, kind):
        if kind not in self._rows:
            rows = self._store.all(kind)
            self._rows[kind] = [r for r in rows if item(r) not in self._hidden] if kind == "timeline_event" else rows
        return self._rows[kind]

    def put(self, key, kind, payload, **kw):
        self._rows.pop(kind, None)
        return self._store.put(key, kind, payload, **kw)

    def __getattr__(self, name):
        return getattr(self._store, name)


def copy_store(args, folder):
    """A throwaway copy of the timing store (or the invented people) in ``folder``, and the drill's end day: the
    last morning whose pull hour has passed."""
    if args.demo:
        store = Store(f"sqlite:///{folder}/drill.sqlite")
        routing.seed(store, json.loads((args.fixtures / "timeline.json").read_text()))
        for entry in json.loads((args.fixtures / "ledger.json").read_text()):
            contact.record(store, **entry)
        return store, args.end or "2026-09-15"
    if not today.TIMELINES.exists():
        sys.exit(f"The drill copies the timing store; none at {today.TIMELINES}.")
    source, target = sqlite3.connect(today.TIMELINES), sqlite3.connect(f"{folder}/drill.sqlite")
    source.backup(target)
    source.close(), target.close()
    now = datetime.now(timezone.utc)
    last = now.date() if now.hour >= args.hour else now.date() - timedelta(days=1)
    return Store(f"sqlite:///{folder}/drill.sqlite"), args.end or last.isoformat()


def backdate_identity(store, first):
    """Identity entries count from the drill's first morning: who someone is does not change with time."""
    for e in [e for e in store.all("contact") if e["kind"] == "identity" and e["at"] > first]:
        fields = {k: v for k, v in e.items() if k not in ("id", "revision", "updated_at", "person_id", "kind", "at")}
        contact.record(store, e["person_id"], "identity", at=first, **fields)


def rails(result, store, as_of, carded, week):
    """What went wrong with this card, if anything (each is a bug): the rails the pre-registration lists. ``week``
    is the drill's cards of the 7 days before this morning."""
    subject, past = result.subject_id, contact.history(store, result.subject_id)
    past = [e for e in past if parse_time(e["at"]) <= parse_time(as_of)]
    wrong = []
    if contact.person_key(subject) in carded:
        wrong.append(f"carded twice (first on {carded[contact.person_key(subject)]})")
    if not any(e["kind"] == "identity" for e in past):
        wrong.append("no identity entry")
    if result.call.holds:
        wrong.append(f"carded during a hold ({', '.join(result.call.holds)})")
    if any(e["kind"] == "never" for e in past):
        wrong.append("asked never to be contacted")
    if sum(1 for c in week if c["role"] == result.role["id"]) >= contact.WEEKLY_CAP:
        wrong.append(f"over {contact.WEEKLY_CAP} cards for the role in 7 days")
    if len(week) >= contact.TOTAL_CAP:
        wrong.append(f"over {contact.TOTAL_CAP} cards across roles in 7 days")
    return wrong


def lag_hours(result, rows, as_of):
    """Hours from the newest item behind the card to the card: with a daily pull, up to about 24 is the floor."""
    seen = [rows[i]["observed_at"] for ids in result.call.evidence.values() for i in ids if i in rows]
    seen = [s for s in seen if parse_time(s) <= parse_time(as_of)]
    return round((parse_time(as_of) - max(map(parse_time, seen))).total_seconds() / 3600, 1) if seen else None


IDENTITY = ("Confirm it's them before any reach", "The identity record from")  # contact.check's two identity asks
THEIR_SITE = "https://their-site.invalid/"  # a stand-in record's page on another site, for someone read on one site


def read_of(store, subject_id):
    """The profile URLs the engine reads for them (their context's anchors), as routing.route passes the gate."""
    ctx = journey.load_context(store, subject_id)
    return [u for u in (ctx.anchors.values() if ctx else []) if u.startswith(("https://", "http://"))]


def one_site(read):
    """Whether the profiles read are all on one site (x.com and twitter.com are one site, as tied() reads)."""
    return len({site for site, _ in map(contact._page, read)}) == 1


def only_identity(store, result, as_of):
    """Whether confirming who they are is all that holds this reach: whether, with a stand-in identity record tying
    each profile the engine reads for them to a page on another site (their other profiles, or a stand-in site for
    someone read on one), the gate clears it. contact.check stops at its first ask, so a stale sign or anything
    after the identity asks shows only then."""
    read = read_of(store, result.subject_id)
    try:
        stand_in = contact.Entry(person_id=contact.person_key(result.subject_id), kind="identity", at=as_of,
                                 links=read + ([THEIR_SITE] if one_site(read) else []))  # nothing read: no record
    except ValueError:  # nothing read
        return False
    by_id = {e["id"]: e for e in made_by_hand(journey.view(store, result.subject_id, as_of))}
    behind = [by_id[i] for ids in result.call.evidence.values() for i in ids if i in by_id]
    history = [*contact.history(store, result.subject_id), stand_in.model_dump()]
    return contact.check(history, as_of, result.call, behind, result.role["id"], profiles=read).state == "clear"


# app/sources/github.py dates a repo by its creation unless it opened in the pull's last 90 days of events, and quotes
# its description as it is today, so the scorecard replays posts and stars only (detectors.REPLAY_TYPES). The drill
# replays what the daily run reads, repos included, and says which cards a replay can't vouch for.
ON_A_REPO = "The call rests on a repo of theirs, dated by its creation unless it opened lately, described as it is today"
REPO_WORDS = "The draft quotes a repo's description, which is today's"


def replay_clean(result, as_of, rows, repos):
    """Why a replay can't vouch for this card, else "": what the card shows (its trigger items, routing.signals: the
    newest evidence of each reason) or the draft cites is one of their ``repos`` (their github_repo rows) or anything
    the post reader read from one, or the draft quotes a repo's description whole or its first phrase (for a push
    that says nothing, or a model draft that saw it). An old repo item listed behind a reason whose strength comes
    from newer items is not flagged, nor is a repo's text read without being shown, cited or quoted (the sender pick
    reads their work, the draft checks read the items), nor a repo item among drift's evidence past the two shown
    (drift only makes a note a pitch, never a reach)."""
    repos = [e for e in repos if parse_time(e["observed_at"]) <= parse_time(as_of)]
    ids = [s["id"] for s in result.signals] + list(result.outreach.get("cites") or [])
    if {item(rows[i]) for i in ids if i in rows} & {item(e) for e in repos}:
        return ON_A_REPO
    draft = " ".join(" ".join(result.outreach.get(k) or "" for k in ("body", "note", "after")).split())
    said = {" ".join(w.split()) for e in repos for w in (their_words(e), routing._phrase(their_words(e)))
            if len(outreach.terms(w)) >= 3}
    return REPO_WORDS if any(w in draft for w in said) else ""


def card_of(result, as_of, rows, wrong=(), held="", repos=()):
    """What the drill keeps of a card: what the rating sheet shows, and why it was held when it was."""
    return {"as_of": as_of, "subject_id": result.subject_id, "name": result.name, "role": result.role["id"],
            "kind": result.kind, "track": result.call.track, "lag_hours": lag_hours(result, rows, as_of),
            "trigger": [{k: s[k] for k in ("date", "what", "quote", "source_url")} for s in result.signals],
            "why_now": result.call.explanation, "draft": result.outreach.get("body", ""),
            "subject_line": result.outreach.get("subject", ""), "route_in": result.route_in,
            "channel": result.channel.kind, "sender": result.sender.get("name", ""),
            "happened": [happened.line(h) for h in result.happened], "rails": list(wrong), "held": held,
            "message": result.message,  # what the draft is about (routing.MESSAGE's key)
            "not_replay_clean": replay_clean(result, as_of, rows, repos),
            "cites": sorted({i for ids in result.call.evidence.values() for i in ids}),  # the events the call rests on
            "cited_urls": sorted({s["source_url"] for s in result.signals if s.get("source_url")}
                                 | {u for u in [(result.opener or {}).get("source_url")] if u})}


def held_draft(drafts, result, as_of):
    """A draft that failed its checks, once per person and text: what it said (subject, body, the LinkedIn note and
    the message once connected), the checks it failed and the mornings it held the card."""
    o = result.outreach
    key = (contact.person_key(result.subject_id), o["subject"], o["body"], o.get("note", ""), o.get("after", ""))
    d = drafts.setdefault(key, {"subject_id": result.subject_id, "name": result.name, "role": result.role["id"],
                                "first": as_of[:10], "mornings": 0, "model": o.get("model", ""),
                                "subject": o["subject"], "body": o["body"], "note": o.get("note", ""),
                                "after": o.get("after", ""), "failed": o["checks"]["problems"]})
    d["last"], d["mornings"] = as_of[:10], d["mornings"] + 1


def state_of(call):
    """A morning's call for someone the engine did not say to reach: its action, and until when and why."""
    if not call:
        return "no call (nothing of theirs in view)"
    return call.action + (f" until {call.until[:10]}" if call.until else "") + \
        (f" (holds: {', '.join(call.holds)})" if call.holds else "")


# What a moment's mornings with no card add up to, first match wins: (words in a morning's state, the reason). A
# morning that said reach now but was held for a draft or for who they are is the miss to fix, whatever holds later.
REASONS = (
    (("one card per person", "Sent on", "No reply since", "Followed up on"), "by design: already on a card"),
    (("The draft fails a check",), "miss: the draft failed its checks"),
    (IDENTITY, "miss: who they are is not confirmed"),
    (("short_tenure",), "by design: first-year hold after a job move"),
)


def reason(why, carded_before):
    """The one reason a public moment got no card, from its mornings' states (``why``, spans): by design (already
    on a card, a first-year hold, another hold the call or the gate names) or a miss (a draft that failed its
    checks, identity, or no morning that said reach now, with the call it said instead)."""
    said = [w["state"] for w in why]
    if carded_before:
        return "by design: already on a card"
    for words, because in REASONS:
        if any(w in s for s in said for w in words):
            return because
    if held := [s.split("(holds: ", 1)[1].rstrip(")") for s in said if "(holds: " in s]:
        return f"by design: held ({held[0]})"
    if held := [s.split("held: ", 1)[1].split(":")[0] for s in said if "held: " in s]:
        return re.sub(r"\d{4}-\d{2}-\d{2}", "<day>", f"by design: {held[0]}")  # one line per reason, not per day
    calls = Counter(re.match(r"[a-z_]+(?: call)?", s)[0] for s in said)  # reach_now, watch_until, quiet, no call
    return f"miss: never reach now ({calls.most_common(1)[0][0]})" if calls else "miss: no morning in the drill saw it"


def same_moment(moments, key, h):
    """The moment already listed that this listing is, however a later morning dates it (the day it went public,
    then the day it names; a post's day, then the profile's month): the same person and kind, holding its event or
    page, or dated to the same month when either has only a month."""
    return next((m for (k, kind, _), m in moments.items() if k == key and kind == h.kind and (
        h.event_id in m["event_ids"] or (h.source_url and h.source_url in m["urls"])
        or ((len(h.date) == 7 or len(m["date"]) == 7) and h.date[:7] == m["date"][:7]))), None)


def spans(states, lo=None, hi=None):
    """A person's mornings as runs of the same state, [{from, to, state}], between the days lo and hi."""
    out = []
    for day, state in sorted(states.items()):
        if (lo and day < lo) or (hi and day > hi):
            continue
        if out and out[-1]["state"] == state:
            out[-1]["to"] = day
        else:
            out.append({"from": day, "to": day, "state": state})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=42)
    ap.add_argument("--end", help="the last morning (default today)")
    ap.add_argument("--hour", type=int, default=13, help="the pull's hour, UTC")
    ap.add_argument("--people", type=Path, help="a people file: its watchlist only")
    ap.add_argument("--cases", type=Path, default=CASES,
                    help="the people file whose replay cases' posts to GI's own account are hidden (with --people, that file)")
    ap.add_argument("--demo", action="store_true", help="the invented people, ending 2026-09-15")
    ap.add_argument("--fixtures", type=Path, default=DEMO, help="with --demo: the invented people to load")
    ap.add_argument("--team", type=Path, default=Path("research/private/gi-team.json"))
    ap.add_argument("--contacts", type=Path, default=Path("research/private/contacts.json"))
    ap.add_argument("--sheet", type=int, default=20, help="a rating sheet of up to N cards (0: none)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--redraft", action="store_true", help="paid: the model redoes a free draft that fails its checks")
    ap.add_argument("--max-usd", type=float, default=1.00, help="with --redraft: the most the whole drill spends")
    ap.add_argument("--max-calls", type=int, default=30, help="with --redraft: the most model calls in the drill")
    ap.add_argument("--model", default=outreach.MODEL, help="with --redraft: the model that writes")
    args = ap.parse_args(argv)
    if args.redraft and args.model != outreach.MODEL:
        ap.error(outreach.UNPRICED)
    args.started = time.monotonic()

    with tempfile.TemporaryDirectory() as folder:  # the copy of real data never outlives the run
        ledgers = contact.LEDGERS
        if not args.demo:  # the web app's opt-outs, sends and answers hold the copy's people, as they do route.py's
            contact.LEDGERS = {**ledgers, "the timing run": Path(folder) / "drill.sqlite"}
        try:
            return drill(args, *copy_store(args, folder))
        finally:
            contact.LEDGERS = ledgers


def replay_cases_to_gi(raw, cases_file):
    """The items of the posts in which a replay case (someone in the people file with a moment) answers or quotes
    GI's own account (scorecard.answers_gi): likely written from inside GI, their outcome, so the drill never sees
    them, as the scorecard never does."""
    if not cases_file or not cases_file.exists():
        return frozenset()
    cases = {contact.person_key(p.subject_id) for p in sc.load_moments(cases_file) if p.moments}
    return frozenset(item(r) for r in raw.all("timeline_event")
                     if contact.person_key(r["subject_id"]) in cases and sc.answers_gi(r))


def drill(args, raw, end):
    hidden = replay_cases_to_gi(raw, None if args.demo else args.people or args.cases)
    store = Cached(raw, hidden)
    mornings = [datetime.combine(date.fromisoformat(end) - timedelta(days=n), datetime.min.time(), timezone.utc)
                + timedelta(hours=args.hour) for n in range(args.days - 1, -1, -1)]
    backdate_identity(raw, mornings[0].isoformat())
    if args.demo:
        args.team, args.contacts, args.out = Path("tests/fixtures/routing/team.json"), args.fixtures / "contacts.json", None
    team = json.loads(args.team.read_text())["members"] if args.team.exists() else []
    contacts = json.loads(args.contacts.read_text()) if args.contacts.exists() else {}
    profiles = {} if args.demo else happened.load_profiles(happened.PROFILES)
    pulled = {} if args.demo else x_follows.load(x_follows.FOLLOWS)
    everyone = sorted(r["subject_id"] for r in store.all("person_context"))
    subjects = [p.subject_id for p in sc.load_moments(args.people) if not p.moments] if args.people else everyone
    rows = {e["id"]: e for e in store.all("timeline_event")}
    repos = {}  # each person's repos, whose date and description a replay can't vouch for
    for e in (e for e in rows.values() if e["event_type"] == "github_repo"):
        repos.setdefault(e["subject_id"], []).append(e)
    names = {s: (ctx.name if (ctx := journey.load_context(store, s)) else s) for s in subjects}

    paid, budget = writer_for(args)
    cache = (raw if args.demo else drafts_cache()) if paid else None  # the invented people's drafts die with the copy
    days, cards, failures, moments, held, problems, drafts = [], [], [], {}, Counter(), {}, {}
    not_tried, failed = [], []  # the model drafts', by morning
    states, for_identity, on_one_site = {s: {} for s in subjects}, {}, set()
    for morning in mornings:
        as_of = morning.isoformat()
        # The X follows count from the day they were read, as the LinkedIn profile read does (happened.py).
        follows = {pid: r for pid, r in pulled.items() if str(r.get("read_on") or "9999")[:10] <= as_of[:10]}
        cohort = {s: ((ctx.name if (ctx := journey.load_context(store, s)) else s),
                      outreach.written_by(journey.view(store, s, as_of), s)) for s in everyone}
        found, calls = [], {}

        def one(subject, writer=None):
            ctx = journey.load_context(store, subject)
            return routing.route(store, subject, as_of, team, contacts.get(subject, []),
                                 profile_url=(ctx.anchors.get("homepage", "") if ctx else ""), cohort=cohort,
                                 profiles=profiles, follows=follows, call=calls[subject], writer=writer,
                                 bands=pay.for_mode("simulation" if args.demo else "live"))

        for subject in subjects:
            ctx = journey.load_context(store, subject)
            call = calls[subject] = journey.assess(store, subject, as_of, ctx.role if ctx else None)  # route.py's
            result = one(subject)
            found += [result] if result else []
            states[subject][as_of[:10]] = state_of(call)
            fresh = result.happened if result else \
                happened.watch(store, subject, as_of, ctx.role if ctx else None, profiles=profiles)[1]
            for h in fresh:  # every public moment Just happened lists, carded or not: once, whatever pages report it
                key = contact.person_key(subject)
                m = same_moment(moments, key, h) or moments.setdefault((key, h.kind, h.date), {
                    "subject_id": subject, "name": ctx.name if ctx else subject, "kind": h.kind, "date": h.date,
                    "moment": happened.line(h), "url": h.source_url, "first_listed": as_of[:10], "urls": [],
                    "event_ids": []})
                m["last_listed"] = as_of[:10]
                m["urls"] += [h.source_url] if h.source_url and h.source_url not in m["urls"] else []
                m["event_ids"] += [h.event_id] if h.event_id not in m["event_ids"] else []
        if paid:  # route.py --redraft's step: the model tries each free draft that fails, within the drill's cap
            writer = outreach.drafter(paid, budget, cache, args.model, as_of, args.max_usd, leave_out=LEFT_OUT)
            found, untried, errors = routing.redraft(found, lambda r: one(r.subject_id, writer))
            not_tried += [f"{as_of[:10]}: {untried}"] if untried else []
            failed += [f"{as_of[:10]}: {e}" for e in errors]
        kept, quiet = routing.pick(store, found, as_of)
        held.update(re.sub(r"\d{4}-\d{2}-\d{2}", "<day>", r.gate.reason.split(":")[0].split(" (")[0]) for r in quiet)
        for r in quiet:
            states[r.subject_id][as_of[:10]] = f"reach_now, held: {r.gate.reason}"
            if r.gate.reason.startswith("The draft fails a check"):
                for problem in r.outreach["checks"]["problems"]:  # each check, with the people whose drafts failed it
                    problems.setdefault(problem, set()).add(contact.person_key(r.subject_id))
                held_draft(drafts, r, as_of)
            if r.gate.state == "check_first" and r.gate.reason.startswith(IDENTITY) and r.outreach["checks"]["passes"]:
                if one_site(read_of(store, r.subject_id)):
                    on_one_site.add(contact.person_key(r.subject_id))
                if contact.person_key(r.subject_id) not in for_identity and only_identity(store, r, as_of):
                    for_identity[contact.person_key(r.subject_id)] = card_of(r, as_of, rows, held=r.gate.reason,
                                                                             repos=repos.get(r.subject_id, []))
        week = [c for c in cards if parse_time(c["as_of"]) > morning - timedelta(days=7)]
        carded = {contact.person_key(c["subject_id"]): c["as_of"][:10] for c in cards}
        today_cards = []
        for result in kept:
            wrong = rails(result, store, as_of, carded, week + today_cards)
            role_id = result.role["id"]
            card = card_of(result, as_of, rows, wrong, repos=repos.get(result.subject_id, []))
            states[result.subject_id][as_of[:10]] = "reach_now, carded"
            failures += [f"{as_of[:10]} {result.name}: {w}" for w in wrong]
            today_cards.append(card)
            evidence = [i for ids in result.call.evidence.values() for i in ids]
            contact.record(store, result.subject_id, "pinged", at=as_of, role_id=role_id, evidence=evidence,
                           score=result.call.score)
            contact.record(store, result.subject_id, "sent", at=as_of, role_id=role_id, note="replay drill")
        cards += today_cards
        days.append({"as_of": as_of, "cards": [c["name"] for c in today_cards]})
        print(f"{as_of[:10]}: " + ("; ".join(f"{c['name']} ({c['role']}, {c['kind'] or 'reach'}, "
                                                f"{c['lag_hours']}h after the newest item)" for c in today_cards)
                                     or "no card"))

    first, last = mornings[0].date(), date.fromisoformat(end)
    for m in moments.values():  # carded: a card that cites it; early: a card in the 8 weeks before it
        day = date.fromisoformat(m["date"]) if len(m["date"]) == 10 else date.fromisoformat(m["first_listed"])
        close = day + timedelta(days=happened.WINDOW_DAYS)
        theirs = [c for c in cards if contact.person_key(c["subject_id"]) == contact.person_key(m["subject_id"])]
        citing = [c for c in theirs if set(c["cites"]) & set(m["event_ids"]) or set(c["cited_urls"]) & set(m["urls"])]
        early = [c for c in theirs if -EARLY_DAYS <= (date.fromisoformat(c["as_of"][:10]) - day).days < 0]
        by = citing or early
        m["card_days_from_moment"] = (date.fromisoformat(by[0]["as_of"][:10]) - day).days if by else None
        m["only_by_cards_not_replay_clean"] = bool(by) and all(c["not_replay_clean"] for c in by)
        listed = close > last or m["last_listed"] == last.isoformat()  # its 30 days, or its listing, go on past the end
        if not by:  # why: their calls on the mornings the drill listed it, and the one reason they add up to
            m["why"] = spans(states.get(m["subject_id"], {}), m["first_listed"], m["last_listed"])
            m["reason"] = reason(m["why"], any(c["as_of"][:10] <= m["last_listed"] for c in theirs))
        m["outcome"] = "carded" if citing else "carded early" if early else "still open" if listed else \
            "began before the drill" if day < first else \
            "held by design" if m["reason"].startswith("by design") else "missed"  # a hold or rail did its job, or not
    lags = sorted(c["lag_hours"] for c in cards if c["lag_hours"] is not None)
    count = Counter(m["outcome"] for m in moments.values())
    carded_people = {contact.person_key(c["subject_id"]) for c in cards}
    for_identity = [c for key, c in for_identity.items() if key not in carded_people]
    for c in cards + for_identity:  # a replay case who has since joined GI: the sheet says so
        c["on_gi_team"] = on_team(store, c["subject_id"], team)
    reached = {s for s, by_day in states.items() if any(v.startswith("reach_now") for v in by_day.values())}
    reached_people = {contact.person_key(s) for s in reached}
    summary = {"mornings": len(mornings), "people": len(subjects), "cards": len(cards),
               "people_carded": len(carded_people), "people_ever_reach_now": len(reached_people),
               "held_only_for_identity": len(for_identity), "held_for_identity_with_one_site": len(on_one_site - carded_people),
               "draft_check_problems": {k: len(v) for k, v in sorted(problems.items(), key=lambda kv: -len(kv[1]))},
               "median_lag_hours": statistics.median(lags) if lags else None, "max_lag_hours": lags[-1] if lags else None,
               "cards_not_replay_clean": sum(bool(c["not_replay_clean"]) for c in cards),
               "moments_listed": len(moments), "moments_carded": count["carded"],
               "moments_carded_early": count["carded early"],
               "moments_reached_only_by_cards_not_replay_clean":
                   sum(m["only_by_cards_not_replay_clean"] for m in moments.values()),
               "moments_not_carded_why": dict(Counter(m["reason"] for m in moments.values() if "reason" in m).most_common()),
               "moments_missed": count["missed"], "moments_held_by_design": count["held by design"],
               "moments_without_a_card": count["missed"] + count["held by design"],
               "moments_still_open": count["still open"],
               "rails_failures": len(failures), "held_person_mornings": dict(held.most_common()),
               "replay_posts_to_gi_hidden": len(hidden),
               "model_drafts": ({"calls": budget.used, "usd": round(outreach.usd(budget.tokens), 4),
                                 "cap_usd": args.max_usd, "cap_is": "per drill", "left_out": list(LEFT_OUT),
                                 "not_tried": not_tried, "failed": failed} if paid else
                                "off" if not args.redraft else "off: " + NO_KEY)}
    print(f"\n{summary['cards']} cards to {summary['people_carded']} of {summary['people']} people over "
          f"{summary['mornings']} mornings; hours from the newest item to the card: median {summary['median_lag_hours']}, "
          f"max {summary['max_lag_hours']}.")
    if unclean := [c for c in cards if c["not_replay_clean"]]:
        print(f"{len(unclean)} of the cards rest on a GitHub repo's creation date or today's description, which a "
              f"replay can't vouch for: " + ", ".join(f"{c['name']} {c['as_of'][:10]}" for c in unclean) +
              f"; {summary['moments_reached_only_by_cards_not_replay_clean']} moments were reached only by such cards.")
    print(f"{summary['people_ever_reach_now']} of {summary['people']} people were reach now on some morning; of "
          f"those never carded, {summary['held_only_for_identity']} were held only to confirm who they are; "
          f"{summary['held_for_identity_with_one_site']} people held for identity are read on one site only.")
    print(f"{summary['moments_listed']} public moments listed: {count['carded']} carded (a card that cites it), "
          f"{count['carded early']} carded early (a card in the {EARLY_DAYS} days before it), {count['missed']} missed, "
          f"{count['held by design']} held by design (a hold or rail kept the card, as it should), "
          f"{count['began before the drill']} began before the drill, {count['still open']} still open.")
    if reasons := summary["moments_not_carded_why"]:
        print("Why the rest got no card: " + "; ".join(f"{n} x {why}" for why, n in reasons.items()))
    for m in moments.values():
        if m["outcome"] not in ("carded", "carded early"):
            print(f"  {m['outcome']}: {m['name']}: {m['moment']} {m['url']} ({m['reason']})")
            for span in m.get("why", []):
                print(f"      {span['from']}..{span['to']}: {span['state']}")
    if problems:
        print("Checks the held drafts failed, in people: " + "; ".join(
            f"{n} x {why}" for why, n in summary["draft_check_problems"].items()))
        print(f"  {len(drafts)} held drafts, each with its text and the checks it failed, are in the report")
    for s in sorted(reached):
        print(f"  {names[s]}: " + " | ".join(f"{x['from']}..{x['to']} {x['state']}" for x in spans(states[s])
                                      if x["state"].startswith("reach_now")))
    print("Held, in person-mornings: " + "; ".join(f"{n} x {why}" for why, n in held.most_common()) if held
          else "Nobody held.")
    print(f"Rails failures: {len(failures)}" + "".join(f"\n  {f}" for f in failures))
    if hidden:
        print(f"Hidden: {len(hidden)} posts in which a replay case answers or quotes GI's own account (their outcome).")
    if paid:
        print(f"Model drafts: {budget.used} calls, ${outreach.usd(budget.tokens):.3f} of the drill's "
              f"${args.max_usd:.2f} cap" + "".join(f"\n  {n}" for n in not_tried + failed))

    report = {"method": "pre-registered 2026-09-24: the replay drill (and its later runs)",
              "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "commit": commit(),
              "seconds": round(time.monotonic() - getattr(args, "started", time.monotonic())),
              "end": end, "hour_utc": args.hour, "people_file": str(args.people) if args.people else None,
              "summary": summary, "days": days, "cards": cards,
              "held_only_for_identity": for_identity, "held_drafts": list(drafts.values()),
              "moments": list(moments.values()), "rails_failures": failures,
              "people": {s: spans(by_day) for s, by_day in states.items()}}
    if args.out is None:
        return report
    args.out.mkdir(parents=True, exist_ok=True)
    before = latest(args.out, report)
    stem = claim(args.out, f"drill-{end}-{args.days}d-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    text = json.dumps(report, indent=1)  # before the file opens, so a failure leaves no empty file
    with open(path := stem.with_name(f"{stem.name}.json"), "x") as f:
        f.write(text)
    print(f"wrote {path}")
    if args.sheet:
        text = rating_sheet(cards + for_identity, args.sheet)
        with open(sheet := stem.with_name(f"{stem.name}-sheet.md"), "x") as f:
            f.write(text)
        print(f"wrote {sheet}")
    print("\n".join(changes(before, report)))
    return report


NO_KEY = "ANTHROPIC_API_KEY isn't set, so a card whose free draft fails a check stays held."
DRAFTS = Path(__file__).resolve().parents[1] / OUT / "drafts.sqlite"  # route.py's model-draft cache, from anywhere
LEFT_OUT = ("diamond",)  # GI facts the drill's model drafts never get: DIAMOND's lead authors now work at GI


def writer_for(args):
    """(the paid settings, their budget) with --redraft and a model key; else (None, None), and without a key the
    drill says so once and runs free. The key is only looked for, never read out."""
    if not args.redraft:
        return None, None
    from posts import _paid_settings  # scripts/ is on the path when this script runs

    config, budget = _paid_settings(args.max_calls, args.model)
    if not config.get("anthropic_key"):
        print(f"Model drafts are off: {NO_KEY}")
        return None, None
    return config, budget


def drafts_cache():
    DRAFTS.parent.mkdir(parents=True, exist_ok=True)
    return Store(f"sqlite:///{DRAFTS}")


def commit():
    """The commit the drill ran on, and whether the working tree had changes of its own."""
    def git(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1]).stdout
    try:
        head = git("rev-parse", "--short", "HEAD").strip() or "unknown"
        return head + (" plus local changes" if git("--no-optional-locks", "status", "--porcelain",
                                                      "--untracked-files=no").strip() else "")
    except OSError:
        return "unknown"


def latest(folder, like):
    """The newest earlier run's report in this folder (by when it was written) that drilled the same people over the
    same mornings as ``like`` (same_run), or None."""
    for path in sorted(folder.glob("drill-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            found = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(found, dict) and isinstance(found.get("summary"), dict) and same_run(found, like):
            return {**found, "file": path.name}
    return None


def same_run(a, b):
    """Whether two runs drilled the same people over the same mornings, so their counts compare: the same last
    morning, number of mornings and number of people, and the same people file (--people) where both name theirs."""
    files = [r["people_file"] for r in (a, b) if "people_file" in r]  # a run from before this was kept names none
    return (a.get("end"), a["summary"].get("mornings"), a["summary"].get("people")) == \
        (b.get("end"), b["summary"].get("mornings"), b["summary"].get("people")) and len(set(files)) < 2


def changes(before, after):
    """What changed since the earlier run of the same people and mornings (latest), in a few plain lines: cards gained, lost or moved by person, holds by
    reason (only those that changed), moments carded, and the commit and time of this run."""
    this = f"This run: {after['commit']}, {after['seconds']} s, {after['summary']['mornings']} mornings to {after['end']}."
    if not before:
        return ["", "No earlier run of the same people and mornings in this folder to compare with.", this]
    was, now = before["summary"], after["summary"]
    firsts = [{c["name"]: c["as_of"][5:10] for c in sorted(r.get("cards", []), key=lambda c: c["as_of"], reverse=True)}
              for r in (before, after)]
    gained = [f"{n} ({d})" for n, d in firsts[1].items() if n not in firsts[0]]
    lost = [f"{n} ({d})" for n, d in firsts[0].items() if n not in firsts[1]]
    moved = [f"{n} ({firsts[0][n]} to {d})" for n, d in firsts[1].items() if n in firsts[0] and firsts[0][n] != d]
    was_held = was.get("held_person_mornings", {})
    holds = [(why, was_held.get(why, 0), now["held_person_mornings"].get(why, 0))
             for why in {**was_held, **now["held_person_mornings"]}]
    holds = sorted((h for h in holds if h[1] != h[2]), key=lambda h: -abs(h[2] - h[1]))
    when = before.get("run_at", "")[:16].replace("T", " ") or before["file"]
    return ["", f"Since the last run ({when}, {before.get('commit', 'commit unknown')}, {was.get('mornings', '?')} "
                f"mornings to {before.get('end', '?')}):",
            f"  Cards: {was.get('cards', len(before.get('cards', [])))} to {now['cards']}. Gained: {', '.join(gained) or 'none'}. Lost: "
            f"{', '.join(lost) or 'none'}." + (f" Moved: {', '.join(moved)}." if moved else ""),
            "  Holds, in person-mornings: " + ("; ".join(f"{why}: {a} to {b}" for why, a, b in holds) or "no change") + ".",
            f"  Public moments: {was.get('moments_carded', '?')} carded to {now['moments_carded']}"
            + (f", {was['moments_carded_early']} carded early to {now['moments_carded_early']}"
               if "moments_carded_early" in was else " (the earlier run counted any card near a moment, not one that "
                                                     f"cites it), {now['moments_carded_early']} carded early")
            + (f", {was['moments_missed']} missed to {now['moments_missed']}, {was['moments_held_by_design']} held by "
               f"design to {now['moments_held_by_design']}." if "moments_missed" in was else
               f", {was.get('moments_without_a_card', '?')} with no card to {now['moments_without_a_card']} (now "
               f"{now['moments_missed']} missed and {now['moments_held_by_design']} held by design; the earlier run "
               "did not split them)."),
            this]


def claim(folder, stem):
    """The first of stem, stem-2, stem-3, ... that no earlier run wrote, so a run never overwrites another's files."""
    for n in itertools.count(1):
        name = stem if n == 1 else f"{stem}-{n}"
        if not any((folder / f"{name}{tail}").exists() for tail in (".json", "-sheet.md")):
            return folder / name


PROFILE_SITES = ("x.com", "github.com", "linkedin.com")  # a team member's source_url can be any page: only these
ON_TEAM = "On GI's team now (the team file lists them): this is the replay before they joined. Rate it as a replay."


def on_team(store, subject_id, team):
    """Whether GI's team file lists them: one of their profiles is a member's X, GitHub or LinkedIn page."""
    ctx = journey.load_context(store, subject_id)
    theirs = contact._pages(ctx.anchors.values() if ctx else [])
    members = [u for m in team for u in (m.get("x_handle") and f"https://x.com/{m['x_handle'].lstrip('@')}",
                                         m.get("github") and f"https://github.com/{m['github']}",
                                         m.get("linkedin_url"), m.get("source_url"))]
    profiles = {(site, path) for site, path in contact._pages(members) if site in PROFILE_SITES}  # not a lab page
    return bool(theirs & profiles)


def rating_sheet(cards, n, seed=0):
    """Up to n cards, one per person (their first), in a seeded random order, without the engine's confidence,
    score, card label or why a card was held."""
    firsts = {}
    for c in sorted(cards, key=lambda c: (bool(c.get("held")), c["as_of"])):  # a card beats one held for identity
        firsts.setdefault(contact.person_key(c["subject_id"]), c)
    picked = random.Random(seed).sample(sorted(firsts.values(), key=lambda c: (c["as_of"], c["subject_id"])),
                                        min(n, len(firsts)))
    out = ["# Would you send it?", "",
           "For each card: send as is, send after an edit, or don't send, and one word on why. The engine's",
           "confidence and score are left off so they don't sway you. Some would first need a check that it's",
           "the right person: judge each as if it is.", ""]
    for k, c in enumerate(picked, 1):
        out += [f"## {k}. {c['name']} ({c['role']}), as of {c['as_of'][:10]}", ""]
        out += [f"*{ON_TEAM}*", ""] if c.get("on_gi_team") else []
        out += [f"*{c['not_replay_clean']}: a replay can't vouch for it.*", ""] if c.get("not_replay_clean") else []
        out += ["**What changed:**"]
        out += [f"- {t['date']}: {t['what']}: \"{t['quote'][:200]}\" {t['source_url']}" for t in c["trigger"]]
        out += ["", f"**Why now:** {c['why_now']}", "", f"**Draft ({c['channel']}, from {c['sender']}):**", ""]
        out += ([f"Subject: {c['subject_line']}", ""] if c["subject_line"] else []) + [c["draft"], ""]
        out += [f"**Route in:** {c['route_in'].get('level', '')}", "",
                "**Your call:** send as is / send after an edit / don't send. Why, in a word: ____", ""]
    return "\n".join(out)


if __name__ == "__main__":
    main()
