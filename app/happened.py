"""Just happened: a public moment of the last ~30 days (a paper out, a launch, leaving a job, the employer closing,
an acquisition at their company, open to work, two years in the seat), beside the early signs.

GI misses obvious moments too, because the news does not reach it when it happens. This pass reads only what is
already on the timeline: the post read tags these moments on the same posts it reads for early signs (a paper out
keeps its own type there, never an early sign), and other sources add papers. When the read calls a post work in
progress or just submitted but the post links the paper on arXiv or OpenReview, the paper is out (the as-of view
reads that tag as the paper too, detectors.as_said, so readiness counts it; this pass also reads it off raw events).
It makes no call of its own, and it changes nothing readiness decides or the scorecard replays: it labels
readiness's reaches and lists the moments beside them. Posts count from any platform.

A LinkedIn profile read (app/sources/linkedin_profiles) adds leaving a job and two years in the seat for the
watchlist. It is passed in by the caller, never read here, and shows the jobs of the day it was read, so it counts
only for a call on or after that day. It gives months, not days, so its moments count this month or last.
"""

import json
import re
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel

from . import readiness
from .detectors import POST_TYPES, RELEASE, REPLY_CONTEXT, automated_release, item, made_by_hand, paper_out
from .models import parse_time
from .sources.linkedin_profiles import SIDE_JOB, main_job, names

WINDOW_DAYS = 30  # for about a month after the moment they are unusually reachable
KIND_OF = {
    "publication": "paper", "paper_v1": "paper", "paper_accepted": "paper",
    "project_release": "launch", "launch_announced": "launch",
    "job_ended": "left_job", "layoff": "left_job",
    "company_closure": "company_closed",
    "acquisition": "acquired",  # their own post; an SEC 2.01 (acquisition_closed) is the acquirer's filing
    "self_stated_availability": "open_to_work",
}  # and "two_years", from a LinkedIn profile only
LABEL = {"paper": "paper out", "launch": "launch", "release": "new release", "left_job": "left a job", "company_closed": "employer closing",
         "acquired": "acquisition at their company", "open_to_work": "open to work",
         "two_years": "two years in the seat"}
LATE_DAYS = 90  # a moment the person tells late, in their own post, is still shown if it is at most this old
OWN_WORK = ("work_in_progress", "just_submitted")  # the read's own-work tags
SIDE_ROLE = SIDE_JOB  # an advisory or board seat is not a job they left or hold
PROFILES = Path("research/private/social/linkedin-profiles.json")  # scripts/social_pull.py profiles


# What a release names: a product and its version ("Tide Journal v2.4", "Brambleworld 2.0"); a bare year is not one.
SHIPPED = re.compile(r"\b(?P<name>[A-Z][\w'&+-]*(?: [A-Z][\w'&+-]*){0,5}) (?P<version>v?\d+(?:\.\d+)+|v\d+)\b")
OPENERS = frozenset("""introducing announcing presenting meet welcome today excited just finally new our my the this big
news shipped shipping released releasing launched launching""".split())  # "Introducing Tide Journal v2.4"
VERSION_TAG = re.compile(r"v?\d+(?:\.\d+)+(?:[-+][\w.]+)?|v\d+(?:[-+][\w.]+)?", re.I)  # v2.0.0, 2026.09.13, v3-rc1
NOT_A_NAME = frozenset("version chapter part section season episode figure table step level rated rating top".split())
# Someone else's tool or platform the post names: "built in Godot 4.3", "for Unity 6.1", "supports iOS 17.2".
BEFORE = re.compile(r"\b(?:in|with|for|on|to|using|via|from|of|and|or|than|like|supports?|requires?)\s+$", re.I)


def shipped(text):
    """The product and version a release post names ("Tide Journal v2.4"); for a GitHub release, the one its own
    words name, else its repo and a version tag ("tidewire v2.0.0"), never a build's; else "": never an opening
    word, a chapter or a rating, and never a tool it was built in or for."""
    said = text.partition(REPLY_CONTEXT)[0]
    if m := RELEASE.match(said.strip()):
        tagged = f"{m[2].rsplit('/', 1)[-1]} {m[1]}" if VERSION_TAG.fullmatch(m[1]) else ""
        return "" if automated_release(said) else shipped(m[3] or "") or tagged
    for m in SHIPPED.finditer(said):
        words, opened = m["name"].split(), False
        while words and words[0].lower() in OPENERS:
            words, opened = words[1:], True
        if not words or words[0].lower() in NOT_A_NAME or (not opened and BEFORE.search(said[:m.start()])):
            continue
        return " ".join([*words, m["version"]])
    return ""


def released(event):
    """Whether a launch is a release of something they make: a project release, or a post naming a version."""
    return event.get("event_type") == "project_release" or bool(shipped(event.get("quote") or ""))


class Happened(BaseModel):
    kind: str  # a KIND_OF value
    date: str  # when it happened: the date the source states (at its precision), else the day it became public
    day: int  # days since, as of the call (0 is the day itself)
    source_url: str
    quote: str
    event_id: str
    note: str = ""  # for a moment known to the month: "this month" or "last month"
    release: bool = False  # a launch that is a release of something they make (``released``): never "a launch"


def _span(event):
    """(first, last) day the source's stated date can mean (a day, a month or a year), or None."""
    value, precision = event.get("event_date"), event.get("date_precision")
    if not value or precision not in ("day", "month", "year"):
        return None
    if precision == "day":
        return (date.fromisoformat(value[:10]),) * 2
    year, month = int(value[:4]), int(value[5:7]) if precision == "month" else 12
    first = date(year, month if precision == "month" else 1, 1)
    return first, date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)


def _when(event, today, public, on_post):
    """The day a moment is dated by. On a page, a month or year from its first day, past or not (a CV lists papers
    under the month or year, whenever in it they came out). Otherwise a stated date once past, to its last day;
    else the day it became public (an exit announced ahead, or their own post saying it this month)."""
    span = _span(event)
    if not span or span[0] > today:
        return public
    if event["date_precision"] != "day" and not on_post:
        return span[0]
    return span[1] if span[1] <= today else public


def _kind(event, posts):
    """The moment an event is, if any: its type's, or a paper out when the read calls a post their own work and
    the post says their paper is out and links it."""
    if event["event_type"] in OWN_WORK:
        return "paper" if item(event) in posts and paper_out(posts[item(event)]["quote"]) else None
    return KIND_OF.get(event["event_type"])


def recent(events, subject_id, as_of, days=WINDOW_DAYS) -> list[Happened]:
    """The person's public moments of the last ``days`` in a view (journey.view), newest first: their own, and
    their employer closing. A moment is dated by the date its source states, else the day it became public; a
    stated date not yet past (an exit announced ahead, "my last day is 31 October") counts from the announcement.
    One the person tells late in their own post ("I was laid off in July") is shown with how long ago it was, up
    to LATE_DAYS; a page seen lately that states an old date is old news."""
    today, now = parse_time(as_of).date(), parse_time(as_of).isoformat()
    events = made_by_hand(events)  # a release a build made is never a moment
    posts = {item(e): e for e in events if e["event_type"] in POST_TYPES and e["subject_id"] == subject_id}
    found = {}
    for e in events:
        kind = _kind(e, posts)
        own = e["subject_id"] == subject_id or (e["subject_type"] == "org" and kind == "company_closed")
        if not kind or not own or e["observed_at"] > now:
            continue
        public, on_post = parse_time(e["observed_at"]).date(), item(e) in posts
        when = _when(e, today, public, on_post)
        told_late = on_post and (today - public).days < days and (today - when).days < LATE_DAYS
        if (today - when).days < days or told_late:
            shown = public.isoformat() if when == public else e["event_date"]
            key = (kind, e["source_url"] or e["id"])
            found[key] = min(found.get(key, (when, shown, e)), (when, shown, e), key=lambda w: w[0])
    return sorted((Happened(kind=kind, date=shown, day=(today - when).days, source_url=e["source_url"] or "",
                            quote=e["quote"], event_id=e["id"], release=kind == "launch" and released(e))
                   for (kind, _), (when, shown, e) in found.items()),
                  key=lambda h: h.date, reverse=True)


def load_profiles(path=PROFILES):
    """The saved LinkedIn profile read ({"pulled_on", "people"}), or {} when there is none."""
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else {}


def _months_back(today, month):
    """How many months before today's a "YYYY-MM" is: 0 this month, 1 last month, negative ahead."""
    return (today.year - int(month[:4])) * 12 + today.month - int(month[5:7])


def from_profile(profiles, subject_id, as_of) -> list[Happened]:
    """Leaving a job and two years in the seat, this month or last, from the person's LinkedIn profile row.

    Left a job: their latest job ended then, it was not an advisory or board seat, and they hold no job now
    other than such a seat. Two years: the job their headline names (else their latest start) began two years before then.
    Month dates only: a year alone cannot place a moment in a month."""
    today, row = parse_time(as_of).date(), (profiles or {}).get("people", {}).get(subject_id)
    if not row or today.isoformat() < profiles.get("pulled_on", "9999"):
        return []
    url = row.get("linkedin_url") or ""
    jobs = [j for j in row.get("current") or [] if not SIDE_ROLE.search(j.get("title") or "")]
    found = []

    def add(kind, month, quote):
        back = _months_back(today, month)
        if back in (0, 1):
            first = date(int(month[:4]), int(month[5:7]), 1)
            found.append(Happened(kind=kind, date=month, day=(today - first).days, source_url=url, quote=quote,
                                  event_id=f"linkedin-profile:{subject_id}:{kind}",
                                  note="this month" if back == 0 else "last month"))

    last = row.get("last_ended") or {}
    if len(end := last.get("ended") or "") == 7 and not jobs and not SIDE_ROLE.search(last.get("title") or ""):
        add("left_job", end, f"{last.get('title') or 'A job'} at {last.get('company') or '?'}, "
                             f"{last.get('started') or '?'} to {end}")
    main = main_job(row)
    if main and len(start := main.get("started") or "") == 7:
        add("two_years", f"{int(start[:4]) + 2}{start[4:]}", f"{main.get('title') or 'A job'} at "
                                                            f"{main.get('company') or '?'} since {start}")
    return found


def employer(profiles, subject_id, as_of, on_file=""):
    """Where to say they work: the people file's employer unless their LinkedIn profile read, when it came before
    ``as_of``, says otherwise. A current job there (not an advisory or board seat) at another company is newer than
    the file, so it wins: the one their headline names, else the latest started. No such job now: "", since a
    draft that names a past employer as their current one is wrong. No read, or a read that says nothing (no jobs
    listed, not returned): the file's."""
    row = (profiles or {}).get("people", {}).get(subject_id)
    if not row or parse_time(as_of).date().isoformat() < profiles.get("pulled_on", "9999"):
        return on_file
    if row.get("status") not in ("current", "no_current_role"):
        return on_file
    jobs = [j for j in row.get("current") or [] if j.get("company") and not SIDE_ROLE.search(j.get("title") or "")]
    if on_file and any(names(j["company"], on_file) or names(on_file, j["company"]) for j in jobs):
        return on_file
    main = main_job(row)
    return (main.get("company") or "") if main else ""


def kind(call, fresh):
    """What a reach is about: "just_happened" when it rests on a public moment of the last month, even beside an
    early sign (once it is public, the reach is no longer ahead of everyone), else "early_sign" when readiness
    reaches on an early sign, else None (a reach on older signals, or no reach)."""
    if call is None or call.action != "reach_now":
        return None
    evidence = {i for ids in call.evidence.values() for i in ids}
    if any(h.event_id in evidence and h.day <= WINDOW_DAYS for h in fresh):  # one told late is older: not "just"
        return "just_happened"
    return "early_sign" if readiness.opener(call) else None  # never a post that puts their paper out (paper_out)


def moments(view, subject_id, as_of, days=WINDOW_DAYS, profiles=None) -> list[Happened]:
    """recent(), plus the LinkedIn profile's moments of a kind the timeline does not already show, newest first."""
    found = recent(view, subject_id, as_of, days)
    kinds = {h.kind for h in found}
    return sorted(found + [h for h in from_profile(profiles, subject_id, as_of) if h.kind not in kinds],
                  key=lambda h: h.date, reverse=True)


def watch(store, subject_id, as_of, role=None, days=WINDOW_DAYS, profiles=None):
    """(readiness's call, the person's public moments of the last ``days``): the moments GI should see this week
    whether or not readiness reaches on them. A moment alone does not make a reach (a paper says nothing about
    whether they would move); the call says what readiness makes of it. ``profiles``: load_profiles()."""
    from . import journey  # journey imports the detectors; imported here to keep this module light

    call = journey.assess(store, subject_id, as_of, role)
    return call, moments(journey.view(store, subject_id, as_of), subject_id, as_of, days, profiles)


def label(h: Happened) -> str:
    """What the moment is, in a few words: a new version is a release, never a launch."""
    return LABEL["release" if h.release else h.kind]


def line(h: Happened) -> str:
    """One line for a card: what, when, how far into the month, and the source."""
    return f"{label(h)} · {h.date} · " + (h.note or f"day {h.day} of about {WINDOW_DAYS}")
