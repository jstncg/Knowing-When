"""The early-sign replay: did the engine reach out on an early sign before a person's public moment?

A moment is the public news everyone reacts to (a paper, a launch, job news).
For each person, the same detectors and readiness scorer run weekly over the
timeline as it was known then, in the 8 weeks before each moment and in the
same person's other 8-week stretches, when nothing followed. The noise test
compares how often each sign appears in the two, on one half of the people;
the scorecard scores the other half against random timing and against waiting
for the news. detectors.detect raises LeakageError if a detector cites anything
later. A window is replayed only where the pull holds everything the person
posted (pull_coverage), and only from posts and stars (detectors.REPLAY_TYPES).
Nothing here retrieves or contacts anyone; the replay never writes to the store.
The method was pre-registered in the project's unpublished pre-registration log.
"""

from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from math import comb
from pathlib import Path
import random
import statistics
import sys
from typing import Callable, Literal, Protocol

from pydantic import AliasChoices, BaseModel, Field, field_validator

from .detectors import GITHUB_TYPES, POST_TYPES, REPLAY_TYPES, REPLY_CONTEXT, RETIRED_SOURCES, item
from .models import iso, parse_time
from .sources.social import linkedin_url  # the canonical form journey.agreeing compares against
from .sources import apify, github


class AnswerKeyError(ValueError):
    pass


class Readiness(Protocol):
    score: float
    action: str


Scorer = Callable[[list[dict], str], Readiness]


def _read(path):
    if not path.exists():
        raise AnswerKeyError(f"People file missing: {path}.")
    return json.loads(path.read_text())


def _day(value, name):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as err:
        raise AnswerKeyError(f"{name} must be an ISO date, got {value!r}") from err


def _readiness_dict(value):
    data = value.model_dump() if hasattr(value, "model_dump") else value if isinstance(value, dict) else vars(value)
    if not {"score", "action", "reasons"} <= data.keys():
        raise TypeError("A Readiness needs score, action and reasons")
    return data


def _rate(numerator, denominator):
    return numerator / denominator if denominator else None


MOMENT_KINDS = ("paper", "release", "launch", "job")  # MTS: a lead-author paper or release in GI's area, a launch, job news
OPENING = ("work_in_progress", "technical_ask", "just_submitted")  # an early sign to write about: what a reach must rest on
EARLY_SIGNS = (*OPENING, "topic_drift", "new_field_contact", "posting_burst")
STRETCH = timedelta(days=56)
LAST_WEEK = timedelta(days=7)  # never replayed: posts are read a calendar week at a time, next to that week's news
WARM_UP = timedelta(days=150)  # the longest look-back of the drift detectors, so every window has a full baseline
KEEP_LIFT, KEEP_P = 2.0, 0.05  # a sign is kept when it is twice as common before a moment, at p < 0.05
LIVE_ONLY = (*(t for t in GITHUB_TYPES if t not in REPLAY_TYPES),  # repos and pushes: see detectors.REPLAY_TYPES
             "profile_job_started", "profile_job_ended")  # a LinkedIn profile shows the day it was read
COMPLETE_AT = 50  # a platform that returned this many of a person's items may have hit its cap
COVERAGE_VERSION = 2  # pull coverage records keep each pull's own window and end day; older ones are replaced
MIN_POSTS = 12  # posts, comments and replies over the pull, one a month: fewer cannot show an early sign to score


ROLES = Path(__file__).resolve().parents[1] / "config/roles.json"  # read per people file: a dropped-in role counts


class Moment(BaseModel):
    date: str  # the day it became public
    kind: Literal[MOMENT_KINDS]
    source_url: str = Field(min_length=1)
    what: str = ""


class Person(BaseModel):
    """One watched person: the span their posts were pulled for (until: the pull's end, today when open), and
    their public moments (any, or none). The pull's people file (scripts/social_pull.py) is the same file."""

    subject_id: str = Field(min_length=1, validation_alias=AliasChoices("subject_id", "person_id"))
    role: str = Field(min_length=1)  # a config/roles.json id
    since: str
    until: str = Field(default_factory=lambda: date.today().isoformat())
    name: str = ""
    x_handle: str = ""  # where they post, which the app links as their public profile
    linkedin_url: str = ""
    github: str = ""
    moments: list[Moment] = Field(default_factory=list)
    unscored: str = ""  # why a person with a moment is kept as context and never scored (e.g. a wrong moment date)

    def anchors(self):
        """Their public profiles as PersonContext anchors."""
        handle = self.x_handle.strip().lstrip("@")
        return {k: v for k, v in (("linkedin", linkedin_url(self.linkedin_url)), ("x", handle and f"https://x.com/{handle}"),
                                  ("github", self.github and f"https://github.com/{self.github}")) if v}

    @field_validator("until", mode="before")
    @classmethod
    def _open_until(cls, value):
        return value or date.today().isoformat()

    @field_validator("name", "x_handle", "linkedin_url", "github", "unscored", mode="before")
    @classmethod
    def _none_is_blank(cls, value):
        """A people file writes an account the person does not have as null."""
        return "" if value is None else value


def load_moments(path):
    """The people file, a list or {"people": [...]}; real people, so it lives under research/private. A row whose role
    config/roles.json doesn't have is left out with a warning: one bad row never stops a run for everyone."""
    data = _read(Path(path))
    try:
        people = [Person.model_validate(row) for row in (data["people"] if isinstance(data, dict) else data)]
    except (KeyError, ValueError) as err:
        raise AnswerKeyError(f"{path}: {err}") from err
    known = {r["id"] for r in json.loads(ROLES.read_text())["roles"]}
    for p in people:
        if p.role not in known:
            print(f"{path}: left out {p.subject_id}: role {p.role!r} is not in config/roles.json", file=sys.stderr)
    return [p for p in people if p.role in known]


def posts_in_pull(rows, person, types=POST_TYPES):
    """The person's own items of these types (posts, comments and replies) inside their pull's window."""
    return sum(1 for r in rows if r["subject_id"] == person.subject_id
               and r["event_type"] in types and person.since <= r["observed_at"][:10] < person.until)


def scoreable(store, people):
    """(people scored, {subject: why not} for everyone else): a person is scored when they have a moment, are not
    marked unscored, their pull is recorded and a pull of their posts is complete to their end day, they posted at
    least MIN_POSTS times over it, and it is complete for both a window before a moment and an ordinary one (so
    nobody sits on one side only). Decided from the pull, before any read."""
    rows, records = store.all("timeline_event"), coverage(store)
    scored, skipped = [], {}
    for p in people:
        complete_from = complete(p, records.get(p.subject_id))[0]
        windows = stretches(p, complete_from=complete_from) if complete_from is not None else []
        before = sum(1 for *_, m in windows if m)
        if not p.moments:
            skipped[p.subject_id] = "watchlist"
        elif p.unscored:
            skipped[p.subject_id] = f"unscored: {p.unscored}"
        elif p.subject_id not in records:
            skipped[p.subject_id] = "no pull coverage record: run social_pull.py ingest on their raw pull"
        elif complete_from is None:
            skipped[p.subject_id] = "no pull of their posts is complete to their end day: a run failed or ran short"
        elif (n := posts_in_pull(rows, p)) < MIN_POSTS:
            skipped[p.subject_id] = f"too quiet: {n} posts, comments and replies in the pull"
        elif not before or before == len(windows):
            skipped[p.subject_id] = (f"one-sided: {before} window(s) before a moment, {len(windows) - before} "
                                     f"ordinary, with the pull complete from {complete_from or p.since}")
        else:
            scored.append(p)
    return scored, skipped


def stretches(person, warm_up=WARM_UP, complete_from=""):
    """(start, end, moment or None), every window starting after the warm-up, counted from since or, when later,
    the day the pull holds everything from (complete_from):
    - the 8 weeks before each moment, unless another moment falls inside them (the person was already in the news);
      of two moments on one day, the first listed;
    - the person's other 8-week stretches with no moment in them or in the 8 weeks after, both inside the pull."""
    since = max(_day(person.since, "since"), _day(complete_from, "complete_from") if complete_from else date.min)
    since, until = since + warm_up, _day(person.until, "until")
    days = [_day(m.date, "moment date") for m in person.moments]
    out = [(day - STRETCH, day, m) for n, (day, m) in enumerate(zip(days, person.moments))
           if since <= day - STRETCH and day <= until and days.index(day) == n
           and not any(day - STRETCH <= d < day for d in days)]
    start = since
    while start + 2 * STRETCH <= until:
        if not any(start <= day < start + 2 * STRETCH for day in days):
            out.append((start, start + STRETCH, None))
        start += STRETCH
    return out


def weekly(start, end):
    """The end of each week of a window but its last: the replay sees only what was public by then."""
    return [iso(datetime.combine(start + timedelta(days=d - 1), time(23, 59, 59), timezone.utc))
            for d in range(7, (end - LAST_WEEK - start).days + 1, 7)]


class ReadOnce:
    """The store as a moment replay reads it: read once, one person's records and their related subjects' (other
    watched people dropped), and the sources of the moments a window leads up to never in view."""

    def __init__(self, store, keep=lambda row: True, hidden=(), rows=None):
        self._store, self._keep, self._hidden, self._rows = store, keep, frozenset(hidden), rows if rows is not None else {}

    def hiding(self, urls):
        return ReadOnce(self._store, self._keep, urls, self._rows)

    def all(self, kind):
        if kind not in self._rows:
            rows = self._store.all(kind)
            self._rows[kind] = [r for r in rows if self._keep(r)] if kind == "timeline_event" else rows
        rows = self._rows[kind]
        return [r for r in rows if r.get("source_url") not in self._hidden] if kind == "timeline_event" else rows

    def __getattr__(self, name):
        return getattr(self._store, name)


def pull_coverage(people, raw, per_person=COMPLETE_AT, asked=None):
    """Per person and platform (x, linkedin_posts, github_stars), [the first day a saved pull (raw) holds everything
    they posted there, the day it ran to]: the since it asked with ("" for none), unless the platform returned
    enough of their items to have hit its cap, which takes the newest first (then its oldest item's day), or its
    run failed (then None, never complete). The window each person was asked for, and the cap, are the pull's own
    (raw["asked"] and raw["per_person"], else asked: {person id: [since, until]} and per_person): the people file
    may have changed since. A person whose window is unknown is not recorded, since the pull may have asked for
    less at either end. Only platforms this pull holds an item or a failed run for: a pull says nothing about
    people it did not ask about, and an account that returned nothing (quiet, or a wrong handle) constrains
    nothing. people are sources.social.Person."""
    windows = raw.get("asked") or asked or {}
    everything = [replace(p, since="", until="") for p in people]
    events, _ = apify.events(everything, {k: v for k, v in raw.items() if k in apify.PARSERS})
    by_id = {p.person_id: p for p in everything}
    events += [e for pid, pulled in raw.get("github", {}).items() if pid in by_id for e in github.events(by_id[pid], pulled)]
    platform = {"x_post": "x", "x_reply": "x", "linkedin_post": "linkedin_posts", "github_star": "github_stars"}
    days = defaultdict(list)
    for e in events:
        if e["event_type"] in platform:
            days[(e["subject_id"], platform[e["event_type"]])].append(e["observed_at"][:10])
    failed = set()
    for f in raw.get("failed", []):
        if f.get("github"):
            failed.add(("github_stars", f["github"].lower()))
        elif f.get("actor") == apify.X_SEARCH:
            failed.add(("x", f["input"]["searchTerms"][0].split()[0].removeprefix("from:").lower()))
        elif f.get("actor") == apify.LINKEDIN_POSTS:
            failed |= {("linkedin_posts", u) for u in f["input"].get("targetUrls", [])}
    enough = min(raw.get("per_person", per_person), COMPLETE_AT)
    out = {}
    for p in people:
        if p.person_id not in windows:
            continue
        accounts = {"x": p.x_handle.lower(), "linkedin_posts": p.linkedin_url, "github_stars": p.github.lower()}
        since, until = windows[p.person_id]
        platforms = {}
        for name, account in accounts.items():
            seen, broke = days[(p.person_id, name)], (name, account) in failed
            if account and (seen or broke):  # with the day the pull ran to: a shorter pull says nothing later
                platforms[name] = [None if broke else max(since, min(seen)) if len(seen) >= enough else since, until]
        if platforms:
            out[p.person_id] = platforms
    return out


def _pulls(value):
    """A platform's [complete day, until] per pull, from one pull, several, or a bare day (open-ended)."""
    if not isinstance(value, list):
        return [[value, ""]]
    return [value] if value and not isinstance(value[0], list) else value


def save_coverage(store, found):
    """Add each pull's [complete day, until] to the person's platforms; complete() picks among them."""
    for pid, platforms in found.items():
        key = f"pull-coverage:{pid}"
        saved = store.get(key) or {}
        merged = {name: _pulls(v) for name, v in saved.get("platforms", {}).items()} \
            if saved.get("version") == COVERAGE_VERSION else {}  # an older record is replaced, not merged
        for name, pull in platforms.items():
            for one in _pulls(pull):
                if one not in merged.setdefault(name, []):
                    merged[name].append(one)
        store.put(key, "pull_coverage", {"person_id": pid, "version": COVERAGE_VERSION, "platforms": merged})


def _complete_from(pulls, person):
    """The earliest complete day among the pulls that ran to the person's until (or open-ended); None when only
    failed runs, or no pull, did: a shorter pull says nothing about the months after it."""
    days = [day for day, until in _pulls(pulls) if day is not None and (not until or until >= person.until)]
    return min(days) if days else None


def coverage(store):
    """Per person with a pull coverage record, each platform's pulls. A record saved by older code (before pulls
    kept their end day and their own window) can claim more than its pull holds, so its person reads as unrecorded
    until ingest runs again and replaces it."""
    return {r["person_id"]: r["platforms"] for r in store.all("pull_coverage") if r.get("version") == COVERAGE_VERSION}


def complete(person, platforms):
    """(the day the person's posts are complete from, None when a posts run failed; whether their stars are
    complete over the whole pull). Posts cut the windows: they are the signs. Stars only feed drift, so stars
    that ran out or failed are left out of the person's replay instead. No record: complete from since."""
    platforms = platforms or {}
    days = [_complete_from(platforms[k], person) for k in ("x", "linkedin_posts") if k in platforms]
    stars = _complete_from(platforms["github_stars"], person) if "github_stars" in platforms else person.since
    return (None if None in days else max(days, default="")), stars is not None and stars <= person.since


# GI's own accounts as a reply or a quote names them (sources/apify.py: "@handle" on X, the author's name on LinkedIn).
GI_ACCOUNTS = ("@gen_intuition", "general intuition")


def answers_gi(row):
    """Whether a post replies to or quotes GI's own account. From someone a replay follows to GI, it was likely
    written from inside ("we're hiring, DM me" under GI's funding post): their outcome, so a replay never sees it."""
    quote, head = row.get("quote") or "", REPLY_CONTEXT.lstrip()  # a quote with no words of their own starts with it
    to = quote[len(head):] if quote.startswith(head) else quote.partition(REPLY_CONTEXT)[2]
    return row["event_type"] in POST_TYPES and to.split(":", 1)[0].strip().lower() in GI_ACCOUNTS


def unreplayed(rows):
    """Items the replay never reads, nor anything read from them: repos and pushes, and retired sources' items that
    no current source holds too (a star of a repo with no description quotes the same under both)."""
    current = {item(r) for r in rows if r["event_type"] in REPLAY_TYPES and r["extractor"] not in RETIRED_SOURCES}
    return {item(r) for r in rows if r["event_type"] in LIVE_ONLY} | \
        {item(r) for r in rows if r["extractor"] in RETIRED_SOURCES} - current


def replay_moments(store, people, scorer: Scorer, *, run, others=()):
    """One observation per window and week: activations, readiness and action as of then, and whether it was an
    early reach. People with a pull coverage record are replayed only where the pull is complete. Everyone in
    people or others (every listed id) stays out of another person's view, and their posts answering GI's own
    account (answers_gi) out of their own, with anything read from them."""
    everyone = {p.subject_id for p in people} | set(others)
    rows = store.all("timeline_event")
    live_only = unreplayed(rows) | {item(r) for r in rows if answers_gi(r)}
    stars = {item(r) for r in rows if r["event_type"] == "github_star"}

    def replayable(row, subject, keep_stars):
        return (row["subject_id"] == subject or row["subject_id"] not in everyone) and \
            row["event_type"] not in LIVE_ONLY and row["extractor"] not in RETIRED_SOURCES and \
            item(row) not in live_only and (keep_stars or item(row) not in stars)

    records = coverage(store)
    observations = []
    for person in people:
        subject = person.subject_id
        complete_from, keep_stars = complete(person, records.get(subject))
        if complete_from is None:
            continue  # a run of their pull failed: no window is complete
        read = ReadOnce(store, rows={"timeline_event": [row for row in rows if replayable(row, subject, keep_stars)]})
        for start, end, moment in stretches(person, complete_from=complete_from):
            view = read.hiding(m.source_url for m in person.moments if _day(m.date, "moment date") >= end)
            for as_of in weekly(start, end):
                activations = run(view, subject, as_of)
                observations.append({
                    "subject_id": subject, "role": person.role, "window": [start.isoformat(), end.isoformat()],
                    "moment": moment.model_dump() if moment else None, "as_of": as_of,
                    "activations": activations, **scored(scorer, activations, as_of),
                })
    return observations


def scored(scorer, activations, as_of):
    """The call, and whether it is an early reach: a reach_now the early signs alone make and the rest of the
    evidence alone does not. A reach an "open to work" post, a paper that is out or a layoff makes by itself
    reacts to news the crowd can see too, whatever early signs come with it."""
    readiness = _readiness_dict(scorer(activations, as_of))
    signs = [a for a in activations if a["detector_id"] in EARLY_SIGNS]
    rest = [a for a in activations if a["detector_id"] not in EARLY_SIGNS]
    early = readiness["action"] == "reach_now" and \
        _readiness_dict(scorer(signs, as_of))["action"] == "reach_now" and \
        _readiness_dict(scorer(rest, as_of))["action"] != "reach_now"
    return {"readiness": readiness, "action": readiness["action"], "early": early}


def split(people, seed=0):
    """Two halves by a seeded hash of the subject id: the noise test picks signs on one, the scorecard scores the other."""
    ranked = sorted(people, key=lambda p: hashlib.sha256(f"{seed}:{p.subject_id}".encode()).hexdigest())
    half = len(ranked) // 2
    return {p.subject_id for p in ranked[:half]}, {p.subject_id for p in ranked[half:]}


def _windows(observations, subjects=None):
    grouped = defaultdict(list)
    for o in sorted(observations, key=lambda o: o["as_of"]):
        if subjects is None or o["subject_id"] in subjects:
            grouped[(o["subject_id"], *o["window"])].append(o)
    return list(grouped.values())


def fisher_greater(a, b, c, d):
    """One-sided Fisher exact p for [[a, b], [c, d]]: a or more of the a + b windows before a moment with the sign,
    if the sign were as common there as in the c + d ordinary windows."""
    rows, with_sign = a + b, a + c
    total = comb(a + b + c + d, with_sign)
    return sum(comb(rows, x) * comb(c + d, with_sign - x) for x in range(a, min(rows, with_sign) + 1)) / total if total else None


def edge_p(summary):
    """One-sided Fisher p for a policy's summary: its reaches before moments against its reaches in the same people's
    ordinary stretches. Random timing deals the same reaches to random windows, so its rate is the pooled one, and this
    is the chance a gap over random timing this big comes from luck. None without both kinds of window."""
    seen, alarms = summary["seen_coming"], summary["fired_with_nothing_after"]
    moments, ordinary = summary["moments"], summary["ordinary_stretches"]
    return fisher_greater(seen, moments - seen, alarms, ordinary - alarms) if moments and ordinary else None


def noise(observations, subjects=None):
    """Per sign: how often it appears in the 8 weeks before a moment against the same people's ordinary stretches.

    A sign appears in a window when, at one of its weekly replays, the detector fired with a window that opened
    inside it by then. Windows of one person are not independent; people_* counts say how many people carry each
    rate."""
    windows = _windows(observations, subjects)
    pre = [w for w in windows if w[0]["moment"]]
    ordinary = [w for w in windows if not w[0]["moment"]]

    def has(window, sign):
        return any(a["detector_id"] == sign and o["window"][0] <= (a["window_open"] or "")[:10] <= o["as_of"][:10]
                   for o in window for a in o["activations"])

    signs = sorted({a["detector_id"] for w in windows for o in w for a in o["activations"]} | set(EARLY_SIGNS),
                   key=lambda s: (s not in EARLY_SIGNS, s))
    table = {}
    for sign in signs:
        a, c = sum(has(w, sign) for w in pre), sum(has(w, sign) for w in ordinary)
        pre_rate, ordinary_rate = _rate(a, len(pre)), _rate(c, len(ordinary))
        lift = pre_rate / ordinary_rate if pre_rate is not None and ordinary_rate else None
        p = fisher_greater(a, len(pre) - a, c, len(ordinary) - c)
        table[sign] = {
            "before_moment": a, "before_moment_windows": len(pre), "ordinary": c, "ordinary_windows": len(ordinary),
            "before_moment_rate": pre_rate, "ordinary_rate": ordinary_rate, "lift": lift, "p": p,
            "people_before_moment": len({w[0]["subject_id"] for w in pre if has(w, sign)}),
            "people_ordinary": len({w[0]["subject_id"] for w in ordinary if has(w, sign)}),
            "kept": sign in EARLY_SIGNS and a > 0 and p is not None and p < KEEP_P
                    and (lift is None or lift >= KEEP_LIFT),
        }
    return {"windows": {"before_moment": len(pre), "ordinary": len(ordinary),
                        "people": len({w[0]["subject_id"] for w in windows})}, "signs": table}


def rescored(observations, scorer, drop):
    """The observations scored again without the dropped signs' activations: the call the kept signs alone make.
    Drift only turns a note into a pitch, so dropping it changes the track, never the timing."""
    out = []
    for o in observations:
        activations = [a for a in o["activations"] if a["detector_id"] not in drop]
        out.append({**o, "activations": activations, **scored(scorer, activations, o["as_of"])})
    return out


def early_reach(observation):
    """A reach that rests on early signs (see scored): the only kind that counts as seeing a moment coming."""
    return observation["early"]


def _lead_weeks(window, as_of):
    return round((_day(window[0]["window"][1], "moment") - parse_time(as_of).date()).days / 7, 1)


def _summary(pre, ordinary, fired):
    """fired(window) -> the as_of of its first reach, or None."""
    firsts = [(w, fired(w)) for w in pre]
    leads = [_lead_weeks(w, at) for w, at in firsts if at]
    alarms = sum(fired(w) is not None for w in ordinary)
    return {"moments": len(pre), "seen_coming": len(leads), "seen_rate": _rate(len(leads), len(pre)),
            "lead_weeks": {"values": sorted(leads), "median": statistics.median(leads) if leads else None},
            "ordinary_stretches": len(ordinary), "fired_with_nothing_after": alarms,
            "false_alarm_rate": _rate(alarms, len(ordinary))}


def scorecard(observations, subjects=None, seed=0, draws=200):
    """How many moments the engine saw coming on an early sign and how many weeks early, and how often it did
    so in stretches nothing followed; against the engine's own week-by-week calls dealt to random windows, and
    against waiting for the news."""
    windows = _windows(observations, subjects)
    pre = [w for w in windows if w[0]["moment"]]
    ordinary = [w for w in windows if not w[0]["moment"]]

    def first(w, reach):
        return next((o["as_of"] for o in w if reach(o)), None)

    engine = _summary(pre, ordinary, lambda w: first(w, early_reach))
    any_reach = _summary(pre, ordinary, lambda w: first(w, lambda o: o["action"] == "reach_now"))
    # Random timing keeps the engine's own runs of weeks and its count of them, and only moves them between windows.
    calls = {id(w): [early_reach(o) for o in w] for w in windows}
    rng, runs = random.Random(seed), []
    for _ in range(draws):
        dealt = list(calls.values())
        rng.shuffle(dealt)
        by_window = dict(zip(calls, dealt))

        def fired(w, by_window=by_window):
            return next((o["as_of"] for o, hit in zip(w, by_window[id(w)]) if hit), None)
        runs.append(_summary(pre, ordinary, fired))
    mean = lambda key: statistics.fmean(r[key] or 0 for r in runs) if runs else None  # noqa: E731
    medians = [r["lead_weeks"]["median"] for r in runs if r["lead_weeks"]["median"] is not None]
    return {
        "engine": engine,
        "any_reach": any_reach,
        "random_timing": {"draws": draws, "seen_rate": mean("seen_rate"), "false_alarm_rate": mean("false_alarm_rate"),
                          "median_lead_weeks": statistics.median(medians) if medians else None},
        "obvious_news": {"seen_rate": 0.0, "lead_weeks": 0, "false_alarm_rate": 0.0},
        "definitions": {
            "seen_coming": "moments with a reach_now resting on early signs (work in progress, a technical ask, a "
                           "submission, with drift) at a weekly replay 8 weeks to 1 week before them: the early signs "
                           "alone make the reach and the rest of the evidence alone does not",
            "lead_weeks": "weeks from that first reach to the moment, per moment seen coming",
            "false_alarm_rate": "the same people's ordinary 8-week stretches (no moment in them or the 8 weeks after) "
                                "with such a reach, over all of them",
            "any_reach": "the same counts for every reach_now, whatever it rests on (an open-to-work post included)",
            "random_timing": "the engine's own week-by-week calls, each window's dealt to a random window, averaged "
                             "over seeded draws: same number and runs of reaches, random timing",
            "obvious_news": "reaching out when the moment is public: never early, never wrong, and with everyone else",
        },
    }
