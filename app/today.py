"""Today's calls: the engine's decision for everyone in a timelines store, as the seven-part ping of GI's brief.

Each person gets reach out now, check first, wait until a date, or stay quiet, from journey.assess. The ping carries
the person and a public profile link, the role, the trigger with its date and source, why now, the confidence and what
would prove it wrong. A reach also gets the draft and route in that routing.route builds; any other call gets a
placeholder.

Live reads the store the social pull and the post reader write, as of now. Simulation loads the invented people in
tests/fixtures as of 15 September 2026. The page also shows the company watcher's newest read (companies.preview).
Nothing here fetches, calls a model or sends.
"""

import html
import json
import re
import shutil
import tempfile
import threading
import weakref
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from . import contact, happened, journey, pay, readiness, routing
from .readiness import _day
from . import scorecard as sc
from .models import iso, parse_time, utcnow
from .sources import x_follows
from .store import Store

ROOT = Path(__file__).resolve().parents[1]
TIMELINES = contact.TIMELINES  # the one contact ledger (ledger()): every writer and reader opens this file
SCORECARDS = ROOT / "research/private/early-signals"  # scripts/posts.py scorecard writes <day>-scorecard.json here
TEAM, CONTACTS = ROOT / "research/private/gi-team.json", ROOT / "research/private/contacts.json"
PROFILES = ROOT / happened.PROFILES  # scripts/social_pull.py profiles
DAILY_LOG = contact.SOCIAL / "daily-runs.jsonl"  # scripts/daily.py's log, in the pull's folder
PULL_ALERT = DAILY_LOG.parent / "pull-alert.json"  # the broken-pull line last said, shared by the daily run and Monday
STALE_DAYS = 2  # data older than this gets a warning: a broken pull must not read as a quiet week
FOLLOWS = ROOT / x_follows.FOLLOWS  # scripts/social_pull.py follows
DEMO = [ROOT / "tests/fixtures" / name for name in ("posts", "routing", "demo", "events")]
DEMO_AS_OF = "2026-09-15T23:59:59+00:00"
DEMO_TEAM = ROOT / "tests/fixtures/routing/team.json"

HEADLINE = {"reach_now": "Reach out now", "verify_first": "Check first", "watch_until": "Wait",
            "respect_follow_up": "Wait", "quiet": "Stay quiet", None: "Nothing to go on yet"}
ORDER = ["reach_now", "verify_first", "respect_follow_up", "watch_until", "quiet", None]
PROFILE_KEYS = ("linkedin", "x", "github", "homepage")
PITCH, NOTE = "Pitch the role", "Lead with their work, then the role in one line"
STUDENT = "A note about their work only: they're an undergraduate, so the role stays out"
UNCONFIRMED = "A note about their work only until it's confirmed whether they're still a student, so the role stays out"
DEAL = "No role named: their company was just acquired, so the role stays out"
OPEN = {"x_dm": "Open the X message", "email": "Open the email", "linkedin": "Open their LinkedIn",
        "x_request": "Open the X message", "site": "Open their site", "none": ""}  # the channel button, per channel


def _profile(ctx, contacts):
    """A public profile link: the person's own anchors first, then a public contact route that is a page."""
    for key in PROFILE_KEYS:
        if (url := ctx.anchors.get(key, "")).startswith("https://"):
            return url
    for c in contacts:
        if c.get("kind") == "x":
            return f"https://x.com/{c['value'].lstrip('@')}"
        if c.get("kind") == "site":
            return c["value"]
    return ""


def _next_step(call, route=None):
    if call.action == "reach_now" and route and (by := routing.reach_by(route)) and routing._moment(route):
        return f"Reach out before {by}, about a month after the moment the note is about."
    if call.action == "reach_now":
        return f"Reach out before {_day(call.earliest_close)}, when the first window closes." if call.earliest_close \
            else "Reach out this week."
    if call.action in ("watch_until", "respect_follow_up") and call.until:
        return f"Look again on {_day(call.until)}."
    if call.action == "verify_first":
        return "Check the fact below before anyone writes."
    return "Nothing to do. The engine keeps watching their posts."


# The engine's own bookkeeping in its explanation: which windows are open and until when. The page says what happened
# in plain sentences instead, and keeps what holds the call back.
BOOKKEEPING = re.compile(r"^(Open on |\d+ independent reasons?:|Earliest window closes |No window open )")


def _why(ctx, call, signals, route, out=()):
    """Why now in plain sentences: the card's for a reach; for any other call, what happened, then what the engine
    says holds it back or what to do instead. ``out``: launch-week holds whose launch is out (launched)."""
    if route:
        return routing.why_now(route)
    said = routing.what_happened(SimpleNamespace(name=ctx.name, subject_id=ctx.subject_id, signals=signals, call=call))
    rest = [p for p in re.split(r"(?<=\.) (?=[A-Z])", call.explanation) if not BOOKKEEPING.match(p)]
    text = routing.dates(" ".join(filter(None, [said, *rest])), call.as_of) or call.explanation
    for hold in out:
        text = text.replace(readiness.label(hold), LAUNCHED[hold])
    return text


# A launch-week hold (hold_imminent_launch: from 30 days before the launch to 7 days after it) named for what it is
# once the launch is out; before it, readiness's "a launch in the next few weeks".
LAUNCHED = {"imminent_launch": "their launch week"}


def launched(activations, now):
    """The launch-week holds holding as of ``now`` (an ISO time) whose launch is out."""
    return {h for a in activations for h in a["holds"] if h in LAUNCHED and readiness._is_open(a, now)
            and (parse_time(a["window_close"]) - timedelta(days=7)).isoformat() <= now}


def held(hold, out):
    """A hold in plain words: LAUNCHED's once its launch is out (``out``: launched), else readiness's label."""
    return LAUNCHED[hold] if hold in out else readiness.label(hold)


def _way_in(route):
    """The card's best way in, in plain words on one line: who to ask first (or that nobody is known) and how the
    sender reaches them. The same words as the person's card, so the two never disagree."""
    lines = routing._way_in(route).removeprefix("*Route in*\n").split("\n")
    text = re.sub(r"<[^<>|]+\|([^<>]+)>", r"\1", " ".join(dict.fromkeys([lines[0], lines[-1]])))
    return html.unescape(text.replace("*", ""))


def _moments(fresh):
    """happened's public moments of the last month, newest first, as the page lists them."""
    return [{"kind": h.kind, "label": happened.label(h), "date": h.date, "day": h.day, "note": h.note,
             "line": happened.line(h), "source_url": h.source_url, "quote": h.quote} for h in fresh]


def hold_only(activations, as_of):
    """Whether a wait with no reason behind it waits on holds alone: no window of a sign opens after ``as_of``, as
    readiness.score counts one. A wait for a window (a fixed-term role ending, say) is a decision when it ends; a
    work anniversary, a retention cliff, a path like past hires' or the pattern before their past moves is never one
    (readiness.CONTEXT_ONLY)."""
    after = readiness.parse_time(as_of).isoformat()
    return not any(a["family"] not in readiness.SUPPRESSORS and a.get("detector_id") not in readiness.CONTEXT_ONLY
                   and a["window_open"] and a["window_open"] > after for a in activations)


def trigger_of(call, signals):
    """The signal a call's trigger (part 3) shows: the one that opened the call, else the first; None without any."""
    opener = readiness.opener(call)
    return next((s for s in signals if s["id"] == opener), signals[0] if signals else None)


def ping(store, ctx, call, as_of, team, contacts, profiles=None, follows=None, keep=None, bands=None):
    """One person's call laid out as the brief's seven parts, with what just happened to them whatever the call;
    missing parts say why they are missing. ``keep``: a dict that gets their route, when there is one (the Monday post
    sends their card through routing.send). ``bands``: the posted pay ranges the workspace shows (pay.for_mode)."""
    profile = _profile(ctx, contacts)
    role = routing.role_of(ctx)
    base = {"subject_id": ctx.subject_id, "name": ctx.name,
            "employer": happened.employer(profiles, ctx.subject_id, as_of, ctx.employer or ", ".join(ctx.affiliations)),
            "profile_url": profile,
            # as the card shows them: the profiles their identity record ties to them, and a public email or none
            "profiles": routing.tied(contact.history(store, ctx.subject_id), as_of, routing.read_profiles(ctx)),
            "email": next(({"address": c["value"], "source_url": c.get("source_url", "")} for c in contacts
                           if c.get("kind") == "email"), None),
            "role": {**role, "pay": pay.line(role["id"], bands)} if role else
            {"id": ctx.role, "title": f"{ctx.role} (not a role in config/roles.json)", "jd_url": "", "pay": pay.NONE}}
    view = journey.view(store, ctx.subject_id, as_of)
    if call is None:
        return {**base, "action": None, "headline": HEADLINE[None], "track": None, "until": None, "closes": None,
                "why_now": "Nothing of their own is in view yet: no posts, code or records have been pulled for them.",
                "trigger": None, "signals": [], "evidence": [], "score": None, "confidence": None, "falsifiers": [],
                "next_step": "Pull their posts.",
                "draft": None, "route": None, "kind": None, "gate": None,
                "happened": _moments(happened.moments(view, ctx.subject_id, as_of, profiles=profiles))}
    route = routing.route(store, ctx.subject_id, as_of, team, contacts, profile, call=call, profiles=profiles,
                          follows=follows, bands=bands) \
        if call.action == "reach_now" and role else None
    if route and keep is not None:
        keep[ctx.subject_id] = route
    signals = route.signals if route else routing.signals(call, {e["id"]: e for e in view})
    # a post shows only their own words, never the one they replied to or quoted (routing.own_quote); a note in a
    # quote's place (unread, withheld) stays as routing.signals put it
    by_id = {e["id"]: e for e in view}
    signals = [{**x, "quote": routing.own_quote(by_id[x["id"]])}
               if x["id"] in by_id and x["quote"] not in routing.NOTES else x for x in signals]
    fresh = route.happened if route else happened.moments(view, ctx.subject_id, as_of, profiles=profiles)
    return {
        **base, "action": call.action, "headline": HEADLINE[call.action],
        "track": UNCONFIRMED if route and route.role_free == "unconfirmed" else STUDENT if route and route.gate.rapport_only
        else DEAL if route and route.role_free == "acquisition" else
        {"pitch": PITCH, "rapport": NOTE}.get((route.call if route else call).track),  # after the gate
        "until": _day(call.until),  # the card's reach-by day: a Just happened reach's moment sets it
        "closes": (routing.reach_by(route) if route else _day(call.earliest_close)) if call.action == "reach_now" else None,
        # A wait on a hold alone (a first year in a new job): when it ends the call is quiet, not a decision.
        "hold_only": call.action == "watch_until" and bool(call.holds) and not call.reasons
        and hold_only(journey.detect(store, ctx.subject_id, as_of, view, ctx.role or None), as_of),
        "why_now": _why(ctx, call, signals, route, launched(journey.detect(store, ctx.subject_id, as_of, view,
                                                                           ctx.role or None),
                                                              parse_time(as_of).isoformat())
                        if set(call.holds) & set(LAUNCHED) else ()),
        "trigger": trigger_of(call, signals), "signals": signals,
        # What a card about them rests on, as routing.send records it (the ledger holds any second card for good).
        "evidence": [i for ids in call.evidence.values() for i in ids], "score": call.score,
        "confidence": call.confidence, "falsifiers": routing.own_claims(call.falsifiers[:4], view),
        "next_step": _next_step(call, route) if role else "Add their role to config/roles.json before drafting anything.",
        "draft": route.draft if route else None,
        # The contact ledger's hold or check (another team's open ask, an unconfirmed identity), when not clear; else a
        # draft that fails a check, which is never offered as ready (routing.send refuses its card).
        "gate": {"state": route.gate.state, "reason": route.gate.reason} if route and route.gate.state != "clear" else
        {"state": "draft", "reason": f"The draft fails a check ({'; '.join(route.outreach['checks']['problems'])}): "
                                     "fix it before anyone sends it."}
        if route and not route.outreach["checks"]["passes"] else None,
        "route": {"paths": [p.detail for p in route.paths[:3]], "channel": routing.CHANNELS[route.channel.kind],
                  "way_in": _way_in(route),
                  "sender": route.sender["name"], "sender_why": route.sender["why"],
                  # the Slack ask without the draft it carries: the page shows the draft once, in its own part
                  "ask": {**route.ask, "body": route.ask["body"].split("\n\n", 1)[0]} if route.ask else None,
                  "target": route.channel.target, "reason": route.channel.reason,
                  "url": "" if route.ask else route.channel.url,  # a warm intro's draft opens in the teammate's account
                  "open": OPEN[route.channel.kind]}
        if route else None,
        "kind": route.kind if route else happened.kind(call, fresh),  # a reach's early_sign or just_happened, else None
        "message": routing.MESSAGE[route.message] if route and route.message else None,  # what the draft is about
        "happened": _moments(fresh),
    }


_demo_lock = threading.Lock()


def _demo_store():
    """The simulation's store, built once: two first requests at once (a page load right after a reset) must not
    each build it, since the second would pull the first's file from under it."""
    with _demo_lock:
        return _seeded_demo()


@lru_cache(maxsize=1)
def _seeded_demo():
    # A new file each build: a request still reading the store a reset replaced keeps its file.
    path = Path(tempfile.mkdtemp(prefix="gi-demo-")) / "demo.sqlite"
    store = Store(f"sqlite:///{path}")
    weakref.finalize(store, shutil.rmtree, path.parent, True)  # its folder goes once no request holds the store
    contacts = {}
    for folder in DEMO:
        routing.seed(store, json.loads((folder / "timeline.json").read_text()))
        contacts |= {k: v for k, v in _json(folder / "contacts.json", {}).items() if k != "note"}
        for entry in _json(folder / "ledger.json", []):  # invented contact history, as route.py --demo reads it
            contact.record(store, **entry)
    return store, contacts


def _clear():
    with _demo_lock:  # not mid-build: a build that started before the reset would be kept, and one more made after
        _seeded_demo.cache_clear()


_demo_store.cache_clear = _clear  # a reset, and the tests, start the simulation afresh


def _json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def ledger():
    """The timing run's store (TIMELINES), the one contact ledger that Today's calls, route.py, the Slack buttons, the
    daily run, the replay drill and scripts/contacts.py share. Its folder is made if missing, so a note can be
    recorded before the first pull."""
    TIMELINES.parent.mkdir(parents=True, exist_ok=True)
    return Store(f"sqlite:///{TIMELINES}")


def source(mode):
    """(store, as_of, team, contacts, where) for a workspace, or None when live has no timelines yet."""
    if mode == "simulation":
        store, contacts = _demo_store()
        return store, DEMO_AS_OF, _json(DEMO_TEAM, {})["members"], contacts, "Invented people, as of 15 September 2026"
    if not TIMELINES.exists():
        return None
    team = _json(TEAM, {}).get("members")
    return ledger(), iso(), team, _json(CONTACTS, {}), "Live data from the daily pull, as of now"


def _profiles():
    """The LinkedIn profile read (left a job, two years in the seat), or {} when it is missing or unreadable: one bad
    file must not take the page down."""
    try:
        return happened.load_profiles(PROFILES)
    except (OSError, ValueError):
        return {}


def _reads(mode):
    """(the LinkedIn profile read, the X follows pull) for a workspace: none for invented people, {} when missing."""
    return ({}, {}) if mode == "simulation" else (_profiles(), x_follows.load(FOLLOWS))


def person(mode, subject_id):
    """(one person's ping as calls() lays it out, the workspace's source()), or None when they are not in it."""
    found = source(mode)
    if not found or not (ctx := journey.load_context(store := sc.ReadOnce(found[0]), subject_id)):
        return None
    _, as_of, team, contacts, _ = found
    call = journey.assess(store, subject_id, as_of, ctx.role or None)
    return ping(store, ctx, call, as_of, team or [], contacts.get(subject_id, []), *_reads(mode),
                bands=pay.for_mode(mode)), found


def calls(mode, keep=None, companies=False):
    """Everyone in the workspace's timelines store with the engine's call, reach-outs first. ``keep``: a dict that gets
    each reach's route by subject id (ping). ``companies``: also the company watcher's newest read, for the page and
    scripts/companies.py (the brief and the Monday post leave it out). It first writes a "hired"
    entry for anyone on New starts the ledger doesn't know yet (starts.hold_all), so no call reaches a new hire."""
    found = source(mode)
    if not found:
        return {"as_of": None, "source": None, "team_loaded": False, "calls": [],
                "missing": "No live data yet. Run the pull and the post reader first."}
    store, as_of, team, contacts, where = found
    from . import starts  # starts reads this module's workspace: imported here

    starts.hold_all(mode)  # whoever accepted an offer is held before any call is laid out
    store = sc.ReadOnce(store)  # every person's view reads the same rows: read the store once per request
    profiles, follows = _reads(mode)
    bands = pay.for_mode(mode)  # GI's posted ranges live; none in the invented week
    judged = []
    for row in store.all("person_context"):
        ctx = journey.PersonContext.model_validate(row)
        call = journey.assess(store, ctx.subject_id, as_of, ctx.role or None)
        judged.append((ping(store, ctx, call, as_of, team or [], contacts.get(ctx.subject_id, []), profiles, follows,
                            keep, bands), call))
    # As routing.rank orders route.py's cards: strongest first, the freshest trigger breaking ties.
    judged.sort(key=lambda pc: ((pc[0]["trigger"] or {}).get("date") or "", pc[0]["name"]), reverse=True)
    judged.sort(key=lambda pc: (ORDER.index(pc[1].action if pc[1] else None), pc[0]["gate"] is not None,
                                *(routing.strength(pc[1]) if pc[1] else ())))
    out = {"as_of": _day(as_of), "source": where, "team_loaded": team is not None,
           "calls": [p for p, _ in judged], "missing": None, "fresh": freshness(mode, store)}
    if companies:  # a preview beside the calls: live, it names only the watchlist
        from . import companies as watcher, inbox  # both read this module's workspace: imported here

        out["companies"] = watcher.preview(mode, store, out["calls"], inbox.watchlist(mode)[0])
    return out


def latest_scorecard(folder=None):
    """The newest scorecard report scripts/posts.py wrote, without its per-week observations, or None."""
    folder = folder or SCORECARDS
    reports = sorted(folder.glob("*-scorecard.json")) if folder.is_dir() else []
    if not reports:
        return None
    report = json.loads(reports[-1].read_text())
    report.pop("observations", None)
    for sign, row in report["noise"]["signs"].items():
        row |= {"label": readiness.label(sign), "early": sign in sc.EARLY_SIGNS}
    for card in report["scorecard"].values():  # the chance each policy's gap over random timing is luck
        for policy in ("engine", "any_reach"):
            if policy in card:
                card[policy]["p"] = sc.edge_p(card[policy])
    return {**report, "file": reports[-1].name}



def _runs(log):
    """The daily run's log lines, oldest first; [] when it is missing, and a line that won't read is skipped."""
    try:
        lines = log.read_text().splitlines()
    except (OSError, ValueError):  # missing, or not text
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _when(stamp):
    try:
        return parse_time(stamp).strftime("%-d %b %H:%M UTC") if isinstance(stamp, str) else str(stamp)
    except (TypeError, ValueError, AttributeError):
        return str(stamp)


def _pulled_day(value):
    try:
        return parse_time(str(value)[:10]).date()
    except ValueError:
        return None


def _shown(path):
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


HURT = ("pull skipped", "no one to pull", "read skipped")  # a run's notes that mean what came in is short


def _hurt(run):
    """What went wrong with the pull in a daily run's line, as (kind, details). The kind is in words that stay the
    same while the problem lasts: a failed actor or GitHub fetch, an error, a pull or read skipped. A run that pulled
    gets ('', ''), even one marked partial for a note (a capped search, a draft that fails a check), since that
    happens on ordinary days."""
    if not run:
        return "", ""
    failed = [str(f) for f in run.get("failed")] if isinstance(run.get("failed"), list) else []
    notes = run.get("notes") if isinstance(run.get("notes"), list) else []
    skipped = sorted({h for n in notes for h in HURT if str(n).startswith(h)})
    error = [f"error {run['error']}"] if run.get("error") else []
    kinds = sorted({"an Apify actor failed" if "~" in f else "a GitHub fetch failed" for f in failed}) + \
        (["error"] if error else []) + skipped
    if not kinds and run.get("status") == "failed":
        kinds = error = ["failed"]
    return ", ".join(kinds), ", ".join([*(f"{f} failed" for f in sorted(failed)), *error, *skipped])


def freshness(mode, store=None, now=None, log=None):
    """When the workspace's data was last pulled, as one line, with a warning when the daily run is paused, its last
    run went wrong (_hurt), or the data is more than STALE_DAYS old.

    Live reads the daily run's log in the pull's folder (scripts/daily.py). With no full pull in it, it falls back to
    the newest event written to the store; door notes and readings write events too, so that day is a guess and is
    said to be one. Simulation says it is invented.

    ``broken`` names what is wrong with the pull (paused, failed, none, stale), ``why`` the problem in words that stay
    the same while it lasts, and ``fix`` the one command that checks or fixes it. A recent guessed day is warned about
    but is not broken."""
    if mode == "simulation":
        return {"pulled": None, "stale": False, "warning": None, "broken": None, "why": None, "fix": None,
                "line": "Invented people, posts and contact history, as of 15 September 2026. Nothing here is real."}
    log = log or DAILY_LOG
    now, rows = now or utcnow(), _runs(log)
    day = max(filter(None, (_pulled_day(r["pulled"]) for r in rows if r.get("pulled"))), default=None)
    by, guessed = "by the daily run", False
    if day is None and store is not None:
        newest = max((r.get("updated_at") or "" for r in store.all("timeline_event")), default="")
        day, guessed = _pulled_day(newest) if newest else None, bool(newest)
        by = "(the newest event in the store: no full pull in the daily run's log)"
    last = rows[-1] if rows else None
    age = (now.date() - day).days if day else None
    ago = "" if age is None else " (today)" if age <= 0 else " (yesterday)" if age == 1 else f" ({age} days ago)"
    line = f"Data pulled {day.strftime('%-d %B %Y')}{ago} {by}." if day else "No pull on record."
    if last and last.get("at"):
        line += f" Last daily run {_when(last['at'])}: {last.get('status', 'unknown')}."
    warning = broken = why = None
    tail = f"tail -n 3 {_shown(log)}"
    since = f" since {day.strftime('%-d %B')}" if day else ""
    if last and last.get("status") == "paused":  # a pause first: its command is the fix, however old the data
        warning, broken, why = (f"The daily run is paused (daily.pause), so nothing new comes in until it is removed. "
                                f"Nothing has been pulled{since}."), "paused", "paused"
    elif (hurt := _hurt(last))[0]:
        warning, broken, why = (f"The last daily run ({_when(last.get('at'))}) went wrong: {hurt[1]}. Nothing has been "
                                f"pulled{since}. Its line in the daily run's log says more."), "failed", hurt[0]
    elif age is None:
        warning, broken, why = ("Nothing has been pulled yet, so no call here rests on anything new. Run "
                                "scripts/daily.py on the Mac."), "none", "none"
    elif age > STALE_DAYS:  # a guessed day stays one problem however many door notes move it
        warning, broken, why = (f"The data is {age} days old: nothing has been pulled{since}. A quiet list may be a "
                                "broken pull, not a quiet week. Check the daily run's log on the Mac."), "stale", \
            "guessed" if guessed else day.isoformat()
    elif guessed:
        warning = ("The daily run's log has no full pull, so this is only the newest event in the store (a door note or "
                   "a reading counts too). Check the daily run on the Mac.")
    fix = {"none": "uv run python scripts/daily.py --dry-run", "stale": tail, "failed": tail,
           "paused": f"rm {_shown(log.parent / 'daily.pause')}"}.get(broken)
    return {"pulled": day.isoformat() if day else None, "stale": age is None or age > STALE_DAYS, "warning": warning,
            "broken": broken, "why": why, "fix": fix, "line": line}
