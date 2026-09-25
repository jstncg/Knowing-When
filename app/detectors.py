"""Detectors: pure functions of the events visible as of a date.

Each detector is registered with timeline.detector(id, family) and returns activations with a window and the ids of
the events it read. A window says when the mechanism makes the person reachable; readiness.py stacks, decays and
combines windows. Detectors never look at the clock, only at ``as_of``.

A person's view may include related subjects (their employer org, coauthors, GI itself) so that org-level and
colleague-level events reach person-level detectors: see ``view`` and ``detect``.

Event types read, by source. Existing sources emit them today; the rest are the documented shape the extraction lane
produces, which the tests use as fixtures.
  crustdata_v1: profile_change (the quote is the JSON change list; a change of basic_profile.current_title is a
      promotion in place, an added experience.employment_details.current is a role start), linkedin_post
  sec: acquisition_closed, auditor_change, late_filing, annual_report_filed (org subject); officer_departure,
      officer_appointment (person subject "name|company"; the title is read from ``title`` when present, else from
      the quote)
  warn / nsf: warn_notice (org), grant_started, grant_end_expected
  news (journey's news adapter, the watchlist's employers): acquisition and layoffs_reported (org)
  papers: paper_v1, paper_revised, paper_accepted, affiliation_seen, affiliation_change (year precision),
      coauthor_link (the quote names the coauthor as "Name (id) on 'title'", and openalex adds " | at Inst A; Inst B
      | last": where the coauthor was on that work and their position)
  gi clock: gi_source_first_seen, gi_source_changed (subject "gi")
  extraction (claude_extract_v1, person pages): job_started, job_ended, role_announced (a forward-looking "incoming
      X at Y" or "will join Y"; event_date is the stated start, often null), placement_end_expected, promotion,
      self_stated_availability (event_date is the stated date), contact_constraint (event_date is the date the
      person named), retention_or_commitment, new_responsibility, layoff, job_ended and company_closure (own_ends);
      and, under the names in EXTRACTED_AS, acquisition, publication, talk_or_conference and project_release
  bridges (Wayback page history, Exa mentions, Crustdata company): paper_talk, self_stated_availability,
      gi_attention, equity_refresh (person), headcount_change (org)
  openalex citations: gi_citation
  trend matching (trend_match_v1): similar_path (the quote names the past hire whose path this matches),
      associate_joined (the quote names who joined which lab)
  posts (POST_TYPES; the quote is the person's own text, then REPLY_CONTEXT and the post it answered: an author name
      or "@handle", and its text when known): extraction.read_posts reads them like a page into the extraction
      types above and the early signs, each dated by its post: work_in_progress, technical_ask, just_submitted,
      gi_topic. The posts themselves feed posting_burst and new_field_contact (who they reply to and mention).
  github_v2 (GITHUB_TYPES: repos they create, repos they star, pushes, pull requests and comments): read like posts
      (READ_TYPES) into the same signs
  note_open and note_wait (tier 0, typed by their author in timeline.add_private_note; a note_wait's event_date is
      the day it waits until); an untyped private_note is context no detector reads
  documented, no source yet: pi_departure and coauthor_departure as typed events (both detectors also read
      coauthor_link), conference_deadline, conference_attending
Optional fields read when a source states them: ``title`` on officer events. TimelineEvent does not store it yet.
"""

import functools
import json
import os
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median
import unicodedata

from . import timeline
from .models import parse_time

# Windows that closed longer ago than LOOKBACK are not emitted (every family's decay has run out by then, see
# readiness.HALF_LIFE_DAYS), nor are windows opening later than LOOKAHEAD: a stated date or a milestone within a year
# is worth watching, one further out is not yet.
LOOKBACK = timedelta(days=180)
LOOKAHEAD = timedelta(days=365)
# Two reports of one event (same subject, type, dates this close) are one event.
SAME_EVENT_DAYS = 2
# A headline or post about their employer's deal or layoffs stays a reason this many days after it became public, as
# long as the stale-sign rail allows (contact.STALE_DAYS).
HEADLINE_DAYS = 21

DEPARTURE_TYPES = ("officer_departure", "job_ended", "affiliation_change")
ROLE_START_TYPES = ("job_started", "officer_appointment", "affiliation_change")
CADENCE_TYPES = ("paper_v1", "paper_revised")  # posting cadence is posting_burst's
# What a person wrote in public: one event per post, comment or reply, observed when it was published.
POST_TYPES = ("linkedin_post", "linkedin_comment", "x_post", "x_reply")
GITHUB_TYPES = ("github_repo", "github_star", "github_activity")  # app/sources/github.py: what engineers build in public
READ_TYPES = POST_TYPES + GITHUB_TYPES  # what extraction.read_posts reads for early signs
LAUNCH_TYPES = ("launch_announced", "project_release")  # a launch, and the name the post reader gives a release
SIGN_TYPES = ("work_in_progress", "technical_ask", "just_submitted")  # the early signs (the "reason" detectors)
# What a replay of a past window reads: posts, and stars, whose starred_at is exact. A repo is dated when it was
# created, not when it went public, and public activity goes back only 90 days, so both count in live calls only.
REPLAY_TYPES = POST_TYPES + ("github_star",)
# Sources whose events are never read or replayed again: github_v1 quoted stars with today's repo description.
RETIRED_SOURCES = ("github_v1",)


def item(event):
    """The key of one post or GitHub item, shared by every event read from it. A repo's pushes share its URL, and two
    pushes can share their text, never their time."""
    return event.get("source_url"), event.get("source_version_hash"), event.get("observed_at")
# A comment or reply ends with the post it answered: someone else's words, context only.
REPLY_CONTEXT = "\n\nIn reply to "
# A post that puts the person's own paper out links it and says so (pre-registered 2026-09-23): news, never a sign.
PAPER_LINK = re.compile(r"\b(?:arxiv\.org/(?:abs|pdf|html)/|openreview\.net/(?:forum|pdf)\?id=)", re.I)
PAPER_OUT = re.compile(r"\b(?:(?:our|my) (?:new|latest|first) (?:paper|preprint)"
                       r"|(?:['’]s|is) (?:now )?(?:out(?! of\b)(?!-)|live|(?:up )?on arxiv)"
                       r"|we (?:release|introduce|present))\b", re.I)


def own_words(text):
    """The person's own words in a post: before the post they answered. A quote post with no words of its own is
    stored as the reply context alone (the quote is stripped), so it has none."""
    text = text or ""
    return "" if text.startswith(REPLY_CONTEXT.lstrip()) else text.split(REPLY_CONTEXT, 1)[0]


RELEASE = re.compile(r"^Released (\S+) of (\S+?)(?:: (.*))?$", re.S)  # app/sources/github.py's quote
BUILD_WORDS = ("nightly", "snapshot", "canary", "build", "ci", "autobuild", "automated")
BUILT = re.compile(rf"({'|'.join(BUILD_WORDS)})(\d\w*)?")  # one part of a tag: nightly, build42, ci
BUILT_NAME = re.compile(rf"\b(?:{'|'.join(BUILD_WORDS)})\b", re.I)
DATED = re.compile(r"20\d{6}|20\d{2}-\d{2}-\d{2}")  # a date in a tag
# A date with a time of day (20260811-0932, 20260811T093214Z), or a Unix timestamp in seconds or milliseconds:
# nobody tags by hand to the minute.
TIMED = re.compile(r"(?:20\d{6}|20\d{2}-\d{2}-\d{2})[-_.T]?[0-2]\d[-:.]?[0-5]\d(?:[-:.]?[0-5]\d)?(?!\d)"
                   r"|(?<!\d)1\d{9}(?:\d{3})?(?!\d)")
WORDS = re.compile(r"[^\W\d_]+|\d+")  # letters in any script, or digits


def _built(tag):
    """A tag a build names: a build word ending it, or followed by a number or another build word (nightly-2026,
    v2.0-ci, build-42), not a package named for one (build-tools-v1.2.0, ci-helper-v0.3)."""
    parts = [p for p in re.split(r"[-_./+@]", tag.lower()) if p]
    for k, part in enumerate(parts):
        if m := BUILT.fullmatch(part):
            after = parts[k + 1] if k + 1 < len(parts) else ""
            if m.group(2) or not after or after[0].isdigit() or BUILT.fullmatch(after) or after == "release":
                return True
    return False


def automated_release(text):
    """A GitHub release a build made, not a person.

    That is a build tag (nightly, snapshot, canary, build, CI); a tag stamped with a time of day or a timestamp
    (rules-20260811-0932), whatever the release is named; or a tag stamped with a date whose release name opens with a
    build word or adds nothing but the tag's own words, dates and build words. "v20260920: Brambleworld 2.0" and
    "2026.09.13" (a calendar version) are kept as releases.
    """
    m = RELEASE.match((text or "").strip())
    if not m:
        return False
    tag, name = m.group(1), (m.group(3) or "").strip()
    if _built(tag) or TIMED.search(tag):
        return True
    if not DATED.search(tag):
        return False
    if BUILT_NAME.match(name):  # "Nightly build for 2026-09-13", "Build #42"
        return True
    said = set(WORDS.findall(BUILT_NAME.sub(" ", DATED.sub(" ", TIMED.sub(" ", name.lower())))))
    return said - {"release", "v"} <= set(WORDS.findall(tag.lower()))


# app/sources/github.py's quotes: what the item was, then the person's own words in it (a repo's description, commit
# messages, a pull request's or issue's title, a release's name). A comment is their words already, before
# REPLY_CONTEXT. A star or a fork is someone else's repo: no words of theirs.
FEED = re.compile(r"^(?P<what>(?:Created|Starred|Forked|Pushed to|Released \S+ of|[A-Z][a-z_]+ (?:pull request|issue) on) "
                  r"[\w.-]+/[\w.-]+)(?P<rest>(?::(?: .*)?| \([^()]*\))?)$", re.S)
# The language github.py adds after a created repo's description: a name, never a description's own "(beta)".
LANGUAGE = re.compile(r" \([A-Z][\w+#.-]*(?: [A-Z][\w+#.-]*)?\)$")


def their_words(event):
    """What a draft may quote as theirs: their own words in a post or GitHub item, never what they answered and
    never the feed's framing ("Created owner/repo: An app (Swift)" is "An app"; "Starred owner/repo" has none)."""
    said = plain(own_words(event.get("quote")))
    github = event.get("event_type") in GITHUB_TYPES or "//github.com/" in (event.get("source_url") or "")
    if not github or not (m := FEED.match(said)):
        return said
    if m["what"].startswith(("Starred", "Forked")):
        return ""
    words = m["rest"][2:] if m["rest"].startswith(": ") else ""
    return (LANGUAGE.sub("", words) if m["what"].startswith("Created") else words).strip()


def released(event):
    """Whether a GitHub item is a release (github.py's "Released TAG of owner/repo"), not a comment that says so."""
    return event["event_type"] == "github_activity" and REPLY_CONTEXT not in (event["quote"] or "") \
        and bool(RELEASE.match((event["quote"] or "").strip()))


def as_said(events):
    """The view with each work_in_progress or just_submitted tag on a post that puts their paper out (paper_out) read
    as a publication. A paper that is out is news, as Just happened lists it, never an early sign."""
    out = {item(e) for e in events if e["event_type"] in POST_TYPES and paper_out(e["quote"])}
    return [{**e, "event_type": "publication"} if e["event_type"] in ("work_in_progress", "just_submitted")
            and item(e) in out else e for e in events]


def as_built(events):
    """The view with launches from GitHub read as what they are. Only a release is a launch.

    A launch the post reader found in a push, a pull request, an issue or a comment is read as work in progress: what
    they are building, not something they shipped. One found in a star or a fork (someone else's repo) is dropped. A
    repo they created is never a launch or an early sign on its own (Justin, 2026-09-24): a new repo is often empty,
    and a note about one reads as watching their GitHub, so the reader's launch or sign on it is dropped. A post about
    the repo or a release of it is its own item and counts.
    """
    source = {item(e): e for e in events if e["event_type"] in GITHUB_TYPES}
    out = []
    for e in events:
        src = source.get(item(e)) if e["event_type"] in LAUNCH_TYPES + SIGN_TYPES else None
        if src is None or released(src) or e["event_type"] in SIGN_TYPES and src["event_type"] != "github_repo":
            out.append(e)
        elif src["event_type"] not in ("github_repo", "github_star") and not src["quote"].startswith("Forked "):
            out.append({**e, "event_type": "work_in_progress"})
    return out


def unread(view):
    """The items (``item``) of the posts and code in a view that the post reader read nothing off.

    Their words may be anything (a curse, politics, a casual reply), so no card or page quotes them, only their day
    and link, and no draft is given such a post to cite. A release is their work (github.py writes its line), so it
    counts as read.
    """
    tagged = {item(e) for e in view if e["event_type"] not in READ_TYPES and e.get("source_url")}
    return {item(e) for e in view if e["event_type"] in READ_TYPES and item(e) not in tagged and not released(e)}


def read_from(event, events):
    """The GitHub item the post reader read ``event`` from (same item), else the event itself: its words, not the
    reader's copy of them, are what a draft may quote."""
    return next((e for e in events if e["event_type"] in GITHUB_TYPES and item(e) == item(event)), event)


def made_by_hand(events):
    """The events without GitHub releases a build made (automated_release) or anything read from one (same release
    URL). A build is never a launch, a launch-week hold, a public moment or the person's work in a draft."""
    builds = {e["source_url"] for e in events if e["event_type"] == "github_activity"
              and "/releases/" in (e.get("source_url") or "") and automated_release(e["quote"])}
    return [e for e in events if not automated_release(e["quote"]) and e.get("source_url") not in builds]


def paper_out(text):
    """The person's own words link a paper and say it is out."""
    own = own_words(text)
    return bool(PAPER_LINK.search(own) and PAPER_OUT.search(own))
MENTION = re.compile(r"(?<![\w@])@(\w{1,15})")
# Page extraction's names for events a detector reads under another name. No answer-key source emits these, so the
# replay sees exactly what it did before.
PAPER_TYPES = ("paper_v1", "publication", "paper_accepted", "paper_talk", "talk_or_conference")
EXTRACTED_AS = {"acquisition_closed": ("acquisition",), "paper_v1": ("publication",),
                "paper_talk": ("talk_or_conference",), "launch_announced": ("project_release",)}
# The person's own pages saying their job ended or will. Departures from SEC filings and page history are not read
# here: in the replay they are the outcome.
PAGE_EXTRACTOR = "claude_extract_v1"
POST_READING = f"{PAGE_EXTRACTOR}:posts:"  # extraction.READING: the post reader's stamp, their own posts read
OWN_END_TYPES = ("layoff", "job_ended", "company_closure")
FINANCE_TITLE = re.compile(r"controller|chief financial|\bCFO\b|chief accounting|accounting|audit|financ|treasur", re.I)
COAUTHOR_ID = re.compile(r"\(([^()]+)\) on '")
# openalex.coauthor_quote: "Name (id) on 'title' | at Inst A; Inst B | last"
COAUTHOR_AT = re.compile(r"\((?P<id>[^()]+)\) on '.*' \| at (?P<places>[^|]*) \| (?P<position>\w+)$")
# A shared work older than this says nothing about where the colleague is now.
COLLEAGUE_GAP = timedelta(days=730)
FIXED_TERM = re.compile(r"\b(interim|intern|internship|student|ph\.?d|visiting|postdoc|postdoctoral|resident|residency|"
                        r"fixed[- ]term|temporary|contract)\b", re.I)


def day_of(event):
    """The first day the event's date can mean, or None when it has no date."""
    d = event.get("event_date")
    if not d:
        return None
    return date.fromisoformat({"year": d + "-01-01", "month": d + "-01", "day": d}[event["date_precision"]])


def observed_day(event):
    return parse_time(event["observed_at"]).date()


def profile_start(event, first_read=None):
    """When a job on their LinkedIn profile began, for the first-year hold.

    A profile that gives only the year ("since 2026") is read as the latest day it can mean, the year's end or the
    first day a read showed it, so the hold lasts until they are surely a year in.
    """
    if event["date_precision"] != "year":
        return day_of(event)
    return min(date(int(event["event_date"]), 12, 31), first_read or observed_day(event))


def stamp(day):
    return f"{day.isoformat()}T00:00:00+00:00"


def clusters(events):
    """Group events that report the same underlying happening; each group is one piece of evidence."""
    groups, by_key = [], defaultdict(list)
    for e in sorted(events, key=lambda e: (day_of(e) or observed_day(e), e["observed_at"])):
        by_key[(e["subject_id"], e["event_type"])].append(e)
    for same in by_key.values():
        group = [same[0]]
        for e in same[1:]:
            if ((day_of(e) or observed_day(e)) - (day_of(group[-1]) or observed_day(group[-1]))).days <= SAME_EVENT_DAYS:
                group.append(e)
            else:
                groups.append(group)
                group = [e]
        groups.append(group)
    return groups


def _window(events, strength, opens, closes, holds=()):
    return {"strength": strength, "window_open": stamp(opens), "window_close": stamp(closes),
            "evidence_event_ids": [e["id"] for e in events], "holds": list(holds)}


def _typed(events, *types, subject=None, dated=True):
    return [e for e in events if e["event_type"] in types
            and (subject is None or e["subject_id"] == subject) and (not dated or day_of(e))]


def _changes(event):
    """Crustdata's change list, when the event carries one."""
    if event["event_type"] != "profile_change":
        return []
    try:
        changes = json.loads(event["quote"])
    except ValueError:
        return []
    return changes if isinstance(changes, list) else []


def _new_employment(event):
    return any(c.get("field") == "experience.employment_details.current" for c in _changes(event))


def _title_change(event):
    return any(c.get("field") == "basic_profile.current_title" for c in _changes(event)) and not _new_employment(event)


def _title(event):
    return event.get("title") or event["quote"]


def role_starts(events, subject):
    """(day, event) per role the subject started, oldest first."""
    starts = [(day_of(e), e) for e in _typed(events, *ROLE_START_TYPES, subject=subject)]
    starts += [(day_of(e), e) for e in _typed(events, "profile_change", subject=subject) if _new_employment(e)]
    return sorted(starts, key=lambda pair: pair[0])


def own_ends(events, subject):
    """Dated statements on the person's pages or in their own posts that their job ended or will: a layoff, a
    departure, the employer closing."""
    return [e for e in _typed(events, *OWN_END_TYPES, subject=subject)
            if e["extractor"] == PAGE_EXTRACTOR or e["extractor"].startswith(POST_READING)]


def said_open(events, subject, since, today):
    """Their own recent "I'm open": a post of theirs saying they're available, posted on or after ``since`` and in
    the last OPEN_DAYS.

    It is newer word from them than what started a hold, so a first-year or launch-week hold gives way to it. Only a
    post is dated by when they said it: a page's line is dated by when it was fetched, so a stale "on the job market"
    found after a new job lifts nothing. A GI human's note is not their word either.
    """
    return any(since <= observed_day(e) <= today < observed_day(e) + timedelta(days=OPEN_DAYS)
               for e in _typed(events, "self_stated_availability", subject=subject, dated=False)
               if e["extractor"].startswith(POST_READING))


def stated_end(events, subject, start, event):
    """The role that started on ``start`` has a stated end after it (a placement end, or their own layoff,
    departure or employer closing) or a fixed-term title (interim, student, postdoc)."""
    ends = [e for e in _typed(events, "placement_end_expected", subject=subject) + own_ends(events, subject)
            if day_of(e) > start]
    return bool(ends or FIXED_TERM.search(_title(event)))


def org_of(event):
    """The employer an event belongs to: the org subject itself or the company half of an SEC person id."""
    if event["subject_type"] == "org":
        return event["subject_id"]
    if "|" in event["subject_id"]:
        return event["subject_id"].split("|", 1)[1]
    return None


def _detector(detector_id, family):
    """Register with the timeline registry. The detector sees ``as_of`` as a day, and only windows within LOOKBACK
    and LOOKAHEAD are kept (a private note's wait is kept however far ahead)."""
    def register(fn):
        @timeline.detector(detector_id, family)
        @functools.wraps(fn)
        def run(events, subject_id, as_of):
            today = parse_time(as_of).date()
            floor, ceiling = stamp(today - LOOKBACK), stamp(today + LOOKAHEAD)
            # A GI human's "not until 2028" holds however far off it is.
            return [a for a in fn(events, subject_id, today)
                    if floor <= a["window_close"] and (a["window_open"] <= ceiling or family == "private")]
        return run
    return register


def _each(events, strength, after, subject=None, *types, before=timedelta(0)):
    """One window per underlying event of the given types: [date - before, date + after]."""
    return [_window(group, strength, day_of(group[0]) - before, day_of(group[0]) + after)
            for group in clusters(_typed(events, *types, subject=subject))]


# Career.

@_detector("tenure_milestone", "career")
def tenure_milestone(events, subject, today):
    """1, 2 and 4 years into the current role, plus the person's own typical tenure; none once it has a stated end."""
    starts = role_starts(events, subject)
    if not starts:
        return []
    start, event = starts[-1]
    if event["date_precision"] == "year" or stated_end(events, subject, start, event):
        return []
    milestones = {365: 0.4, 730: 0.5, 1460: 0.5}
    gaps = [(b - a).days for (a, _), (b, _) in zip(starts, starts[1:])]
    if len(gaps) >= 2:
        milestones[int(median(gaps))] = 0.6
    return [_window([event], strength, start + timedelta(days=days) - timedelta(days=30),
                    start + timedelta(days=days) + timedelta(days=60))
            for days, strength in milestones.items() if days > 0]


@_detector("exec_departure", "career")
def exec_departure(events, subject, today):
    """An officer at the person's employer left."""
    others = [e for e in _typed(events, "officer_departure") if e["subject_id"] != subject]
    return [_window(g, 0.5, day_of(g[0]), day_of(g[0]) + timedelta(days=90)) for g in clusters(others)]


def _places(text):
    return {p.strip() for p in text.split(";") if p.strip()}


def year_rounded(event):
    """An OpenAlex work known only by its year is dated January 1, which is not when it was public."""
    return event["extractor"].startswith("openalex") and (event.get("event_date") or "").endswith("-01-01")


def colleague_moves(events, subject):
    """Coauthors who shared an institution with the subject and later list only other ones.

    Read from the subject's own day-dated OpenAlex works, each seen on its publication day, so nothing counts before
    the paper was public. A work that places the subject somewhere new is the subject's own move, the outcome itself,
    so reading stops there. Works that state no affiliation for the subject are skipped, and a January 1 work can stop
    the reading but counts no move, since that date may stand for the year alone.

    Returns one dict per move: the day, the before and after coauthor events, the shared institutions it left, whether
    the colleague was the last (senior) author on the shared work, and whether the subject was (junior: the colleague
    worked under the subject). The subject is last when no linked coauthor on the work is last, only first or middle.
    """
    works = defaultdict(lambda: {"own": set(), "links": [], "day": None, "rounded": False})
    for e in events:
        if e["subject_id"] != subject or e.get("date_precision") != "day" or e["extractor"] != "openalex_works":
            continue
        work = works[e["source_url"]]
        work["day"], work["rounded"] = day_of(e), year_rounded(e)
        if e["event_type"] == "affiliation_seen":
            work["own"].add(e["quote"].rsplit(" on '", 1)[0])
        elif e["event_type"] == "coauthor_link" and (m := COAUTHOR_AT.search(e["quote"])):
            work["links"].append((m["id"], _places(m["places"]), m["position"], e))
    home, last, moves = set(), {}, []
    for work in sorted((w for w in works.values() if w["own"]), key=lambda w: w["day"]):
        if home and not work["own"] & home:
            break  # the subject has moved; everything from here is the outcome
        if work["rounded"]:
            continue
        home |= work["own"]
        subject_last = bool(work["links"]) and all(p in ("first", "middle") for _, _, p, _ in work["links"])
        for coauthor, places, position, e in work["links"]:
            if not places:
                continue
            before = last.get(coauthor)
            if (before and before["shared"] and not places & before["places"]
                    and work["day"] - before["day"] <= COLLEAGUE_GAP):
                moves.append({"day": work["day"], "events": [before["event"], e], "left": before["shared"],
                              "senior": before["position"] == "last", "junior": before["subject_last"]})
            last[coauthor] = {"places": places, "shared": places & work["own"], "position": position,
                              "day": work["day"], "event": e, "subject_last": subject_last}
    return moves


@_detector("pi_departure", "career")
def pi_departure(events, subject, today):
    """A stated PI move, or the senior author of a shared-institution paper turning up elsewhere."""
    moved = [_window(m["events"], 0.6, m["day"], m["day"] + timedelta(days=120))
             for m in colleague_moves(events, subject) if m["senior"]]
    return _each(events, 0.6, timedelta(days=120), subject, "pi_departure") + moved


@_detector("team_exodus", "career")
def team_exodus(events, subject, today):
    """Two or more departures from one org within 60 days, stated or seen as colleagues leaving a shared institution.

    Colleagues who worked under the subject (the subject was last author) leaving is ordinary lab churn, not counted.
    """
    by_org = defaultdict(list)
    for group in clusters(_typed(events, *DEPARTURE_TYPES)):
        if (org := org_of(group[0])) and group[0]["date_precision"] != "year":
            by_org[org].append((day_of(group[0]), group))
    for move in colleague_moves(events, subject):
        if move["junior"]:
            continue
        for place in move["left"]:
            by_org[place].append((move["day"], move["events"][1:]))
    out = []
    for moves in by_org.values():
        moves.sort(key=lambda m: m[0])
        for (first_day, first), (second_day, second) in zip(moves, moves[1:]):
            if (second_day - first_day).days <= 60:
                out.append(_window(first + second, 0.6, second_day, second_day + timedelta(days=90)))
                break
    return out


@_detector("coauthor_departure", "career")
def coauthor_departure(events, subject, today):
    """A coauthor's stated departure, or a colleague seen leaving a shared institution.

    Seniors (the colleague was last author) are pi_departure; juniors (the subject was last author) are ordinary lab
    churn.
    """
    coauthors = {m for e in _typed(events, "coauthor_link", subject=subject, dated=False)
                 for m in COAUTHOR_ID.findall(e["quote"])}
    moved = [e for e in _typed(events, *DEPARTURE_TYPES)
             if e["subject_id"] in coauthors and e["date_precision"] != "year"]
    moved += _typed(events, "coauthor_departure", subject=subject)
    seen = [_window(m["events"], 0.5, m["day"], m["day"] + timedelta(days=90))
            for m in colleague_moves(events, subject) if not m["senior"] and not m["junior"]]
    return [_window(g, 0.5, day_of(g[0]), day_of(g[0]) + timedelta(days=90)) for g in clusters(moved)] + seen


@_detector("placement_end", "career")
def placement_end(events, subject, today):
    """A fixed-term placement (postdoc, fellowship, contract) is ending."""
    return _each(events, 0.6, timedelta(days=30), subject, "placement_end_expected", before=timedelta(days=90))


def _forced_move(events, subject, *types):
    """From when the end is seen (or its earlier date) to 90 days past it: they are on the market until they land."""
    return [_window(g, 0.6, min(day_of(g[0]), observed_day(g[0])), day_of(g[0]) + timedelta(days=90))
            for g in clusters([e for e in own_ends(events, subject) if e["event_type"] in types])]


@_detector("own_departure", "career")
def own_departure(events, subject, today):
    """They say they were laid off or left their job, or will: a forced move. A new role they announce or start holds
    it (hold_role_claim, hold_short_tenure)."""
    return _forced_move(events, subject, "layoff", "job_ended")


@_detector("company_closure", "employer")
def company_closure(events, subject, today):
    """Their page says their employer is closing: everyone there has to move."""
    return _forced_move(events, subject, "company_closure")


@_detector("retention_cliff", "career")
def retention_cliff(events, subject, today):
    """12 and 24 months after the employer's acquisition closed."""
    out = []
    for group in clusters(_typed(events, "acquisition_closed", *EXTRACTED_AS["acquisition_closed"])):
        closed = day_of(group[0])
        for months, strength in ((12, 0.5), (24, 0.6)):
            cliff = closed + timedelta(days=int(months * 30.44))
            out.append(_window(group, strength, cliff - timedelta(days=30), cliff + timedelta(days=60)))
    return out


# Work.

@_detector("paper_v1", "work")
def paper_v1(events, subject, today):
    return _each(events, 0.4, timedelta(days=45), subject, "paper_v1", *EXTRACTED_AS["paper_v1"])


@_detector("paper_accepted", "work")
def paper_accepted(events, subject, today):
    return _each(events, 0.5, timedelta(days=60), subject, "paper_accepted")


@_detector("launch", "work")
def launch(events, subject, today):
    """Their own launch (a project or product they shipped): a reason to write about their work for a month after
    it. hold_imminent_launch keeps anyone from engaging before it and in launch week."""
    return _each(made_by_hand(events), 0.4, timedelta(days=30), subject, "launch_announced",
                 *EXTRACTED_AS["launch_announced"])


@_detector("acquired", "work")
def acquired(events, subject, today):
    """Their own post that their company was acquired (the reader's acquisition tag on it): a reason to write about
    their work for a month after it. acquisition_closed reads it too, as an employer reason."""
    return _each(events, 0.4, timedelta(days=30), subject, "acquisition")


@_detector("paper_talk", "work")
def paper_talk(events, subject, today):
    return _each(events, 0.4, timedelta(days=14), subject, "paper_talk", *EXTRACTED_AS["paper_talk"],
                 before=timedelta(days=14))


@_detector("rhythm_change", "work")
def rhythm_change(events, subject, today):
    """The person went quiet against their own paper cadence, or burst above it (posts are posting_burst's)."""
    posts = sorted(_typed(events, *CADENCE_TYPES, subject=subject), key=day_of)
    if len(posts) < 4:
        return []
    gap = timedelta(days=max(median((day_of(b) - day_of(a)).days for a, b in zip(posts, posts[1:])), 1))
    quiet_after = max(3 * gap, timedelta(days=60))
    out = []
    if today - day_of(posts[-1]) >= quiet_after:
        opens = day_of(posts[-1]) + quiet_after
        out.append(_window(posts[-4:], 0.3, opens, opens + timedelta(days=90)))
    recent = [e for e in posts if today - day_of(e) <= timedelta(days=30)]
    if len(recent) >= 3 and len(recent) >= 3 * 30 / gap.days:
        out.append(_window(recent, 0.4, day_of(recent[0]), day_of(recent[0]) + timedelta(days=45)))
    return out


@_detector("own_precedent", "work")
def own_precedent(events, subject, today):
    """The event types that preceded each of the person's earlier moves are recurring now."""
    starts = role_starts(events, subject)
    if len(starts) < 2:
        return []
    lead = timedelta(days=180)
    dated = [e for e in events if e["subject_id"] == subject and day_of(e) and e["event_type"] not in ROLE_START_TYPES
             and not _new_employment(e)]
    precursors = [{e["event_type"] for e in dated if start - lead < day_of(e) < start} for start, _ in starts]
    common = set.intersection(*precursors)
    if not common:
        return []
    latest = starts[-1][0]
    recurring = [e for e in dated if e["event_type"] in common and day_of(e) > latest and today - day_of(e) <= lead]
    if {e["event_type"] for e in recurring} != common:
        return []
    earlier = [e for e in dated if e["event_type"] in common and day_of(e) <= latest]
    opens = min(day_of(e) for e in recurring)
    return [_window(recurring + earlier, 0.5, opens, opens + lead)]


# Reason: early signs in the person's own words, before anything is public. Work in progress, a technical ask (for
# data, compute, feedback or help), or a submission that is in but not out. Each is something specific to write about,
# enough for a note about their work, never a pitch on its own (readiness.OPENERS). An ask goes stale fastest. Papers,
# launches, job news and "open to work" are the public moments these try to beat, never signs (a post that puts their
# paper out is a publication in the view: as_said).

@_detector("work_in_progress", "reason")
def work_in_progress(events, subject, today):
    return _each(events, 0.5, timedelta(days=21), subject, "work_in_progress")


@_detector("technical_ask", "reason")
def technical_ask(events, subject, today):
    return _each(events, 0.7, timedelta(days=14), subject, "technical_ask")


@_detector("just_submitted", "reason")
def just_submitted(events, subject, today):
    return _each(events, 0.6, timedelta(days=30), subject, "just_submitted")


# Drift: movement toward GI's field, read from the pattern of posts rather than any one of them. More posts on GI's
# topics than their own past, new people in the field they talk to, or a burst of posting. With a sign to write about
# it is a pitch; alone it is never a reach (readiness.SUPPORT).

FIELD_PATH = Path(os.getenv("GI_FIELD") or Path(__file__).resolve().parents[1] / "research/private/early-signals/field.json")


def _field(path=FIELD_PATH):
    """{name: since}: the people who make up GI's field (its team, the field's builders) and the day it became public
    that each was part of it (an announcement, their first public work there), so a replay never counts talking to
    someone before anyone could know.

    The file names real people, so it lives outside git: {"field": [{"match": "@handle" or a name, "since": date}]}.
    When it is missing the field is empty and new_field_contact never fires.
    """
    if not path.exists():
        return {}
    return {row["match"].lower(): date.fromisoformat(row["since"]) for row in json.loads(path.read_text())["field"]}


FIELD = _field()


def contacts(event):
    """Who a post talks to: the author it replies to ("@handle" or a name) and the handles it mentions."""
    own = own_words(event["quote"])
    context = event["quote"][len(own):].strip().removeprefix(REPLY_CONTEXT.strip()).strip()
    names = {f"@{h.lower()}" for h in MENTION.findall(own)}
    if context:
        names.add(context.split(":", 1)[0].strip().lower())
    return names


@_detector("topic_drift", "drift")
def topic_drift(events, subject, today):
    """Posts or GitHub items on GI's topics (gi_topic) in the last 60 days at twice or more their share in the 90 days
    before."""
    posts = {item(e): day_of(e) for e in _typed(events, *READ_TYPES, subject=subject) if e.get("source_url")}
    tagged = {item(e): e for e in _typed(events, "gi_topic", subject=subject) if item(e) in posts}
    recent_from, base_from = today - timedelta(days=60), today - timedelta(days=150)

    def share(lo, hi):
        days = [d for d in posts.values() if lo < d <= hi]
        on_topic = [e for key, e in tagged.items() if lo < posts[key] <= hi]
        return on_topic, len(on_topic) / len(days) if days else 0.0

    recent, recent_share = share(recent_from, today)
    _, base_share = share(base_from, recent_from)
    if len(recent) < 2 or recent_share < 2 * base_share:
        return []
    opens = max(day_of(e) for e in recent)
    return [_window(recent, 0.4, opens, opens + timedelta(days=45))]


@_detector("new_field_contact", "drift")
def new_field_contact(events, subject, today):
    """Two or more people in GI's field (FIELD) they replied to or mentioned in the last 60 days, none of them in the
    90 days before."""
    posts = sorted(_typed(events, *POST_TYPES, subject=subject), key=day_of)
    seen, new = {}, {}
    for e in posts:
        day = day_of(e)
        for name in contacts(e):
            since = FIELD.get(name)
            if since and since <= day and (name not in seen or day - seen[name] > timedelta(days=90)):
                new[name] = e
            seen[name] = day
    fresh = [e for e in new.values() if today - day_of(e) <= timedelta(days=60)]
    if len(fresh) < 2:
        return []
    opens = max(day_of(e) for e in fresh)
    return [_window(fresh, 0.4, opens, opens + timedelta(days=30))]


@_detector("posting_burst", "drift")
def posting_burst(events, subject, today):
    """Three or more posts in the last 30 days, and three times or more their monthly rate over the 90 days before."""
    posts = sorted(_typed(events, *POST_TYPES, subject=subject), key=day_of)
    recent = [e for e in posts if today - day_of(e) < timedelta(days=30)]
    before = [e for e in posts if timedelta(days=30) <= today - day_of(e) < timedelta(days=120)]
    if len(recent) < 3 or len(recent) < 3 * len(before) / 3:
        return []
    return [_window(recent, 0.3, day_of(recent[-1]), day_of(recent[-1]) + timedelta(days=30))]


# Pattern: signals written by trend matching (trend_match_v1). The person now looks like past hires at GI or a lab like
# it did before they joined, or someone close to them just joined one. The path predicts a move, so a call only lists
# it (readiness.CONTEXT_ONLY); someone close joining is a sign they would listen.

@_detector("similar_path", "pattern")
def similar_path(events, subject, today):
    return _each(events, 0.5, timedelta(days=60), subject, "similar_path")


@_detector("associate_joined", "pattern")
def associate_joined(events, subject, today):
    return _each(events, 0.5, timedelta(days=90), subject, "associate_joined")


# Employer.

@_detector("acquisition_closed", "employer")
def acquisition_closed(events, subject, today):
    """Their employer was bought. A filing that the deal closed unsettles roles for months; a headline or their own
    post about the deal is news only for HEADLINE_DAYS."""
    return (_each(events, 0.5, timedelta(days=180), None, "acquisition_closed")
            + _each(events, 0.5, timedelta(days=HEADLINE_DAYS), None, *EXTRACTED_AS["acquisition_closed"]))


@_detector("warn_notice", "employer")
def warn_notice(events, subject, today):
    """Layoffs at their employer: a WARN filing, for the months before the layoffs it gives notice of, or a headline
    that says the employer is laying people off, news only for HEADLINE_DAYS."""
    return (_each(events, 0.6, timedelta(days=120), None, "warn_notice")
            + _each(events, 0.6, timedelta(days=HEADLINE_DAYS), None, "layoffs_reported"))


@_detector("auditor_change", "employer")
def auditor_change(events, subject, today):
    return _each(events, 0.5, timedelta(days=120), None, "auditor_change")


LATE_CHECK = "Find out why the filing was late and whether it lands on them: {quote!r} ({url})."


@_detector("late_filing", "employer")
def late_filing(events, subject, today):
    """A late 10-K or 10-Q: the close broke. Pressure on them or a one-off is a fact to check, so it carries one."""
    return [{**w, "falsifier": LATE_CHECK.format(quote=g[0]["quote"][:160], url=g[0]["source_url"] or "no URL")}
            for g, w in zip(clusters(_typed(events, "late_filing")), _each(events, 0.6, timedelta(days=120), None, "late_filing"))]


@_detector("grant_end", "employer")
def grant_end(events, subject, today):
    return _each(events, 0.5, timedelta(days=30), None, "grant_end_expected", before=timedelta(days=90))


# GI.

@_detector("gi_citation", "gi")
def gi_citation(events, subject, today):
    return _each(events, 0.5, timedelta(days=60), subject, "gi_citation")


@_detector("gi_attention", "gi")
def gi_attention(events, subject, today):
    return _each(events, 0.4, timedelta(days=30), subject, "gi_attention")


# Self.

OPEN_DAYS = 60  # a self-stated availability's window runs this long past the later of seen and stated


@_detector("self_stated_availability", "self")
def self_stated_availability(events, subject, today):
    """"Available from October" is an invitation to talk now about a role that starts then.

    The window opens when the statement is seen (or on an earlier stated date) and runs 60 days past the later of the
    two. A request about *when to be contacted* is a contact_constraint instead (stated_follow_up).
    """
    out = []
    for group in clusters(_typed(events, "self_stated_availability", subject=subject, dated=False)):
        seen, stated = observed_day(group[0]), day_of(group[0]) or observed_day(group[0])
        out.append(_window(group, 0.9, min(seen, stated), max(seen, stated) + timedelta(days=OPEN_DAYS)))
    return out


AFTER_PERIOD = re.compile(r"\b(after|until|till|through)\b", re.I)


def _next_period(day, precision):
    """The first day after the month (or year) that starts on ``day``."""
    if precision == "year":
        return date(day.year + 1, 1, 1)
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


@_detector("stated_follow_up", "self")
def stated_follow_up(events, subject, today):
    """"Please reach out after 4 October": the window opens on the date the person named.

    "After March 2027" or "until August" names a whole month or year, so the window opens on the 1st after it.
    """
    out = []
    for g in clusters(_typed(events, "contact_constraint", subject=subject)):
        opens, precision = day_of(g[0]), g[0]["date_precision"]
        if precision in ("month", "year") and AFTER_PERIOD.search(g[0]["quote"]):
            opens = _next_period(opens, precision)
        out.append(_window(g, 0.9, opens, opens + timedelta(days=60)))
    return out


# Private.

@_detector("private_note", "private")
def private_note(events, subject, today):
    """A GI human's first-hand read, as its author typed it; the newest typed note is the current one.

    note_open (they said they are open to a move) is a reason, enough alone, for 90 days. note_wait (not until a day)
    opens that window on the day: until then readiness holds and watches it, as it does a date the person named
    themselves. An untyped private_note ("I don't know her", attending a GI event) is context for the reader, not a
    reason.
    """
    notes = [e for e in _typed(events, "note_open", "note_wait", subject=subject) if e["tier"] == 0]
    if not notes:
        return []
    note = max(notes, key=lambda e: e["observed_at"])  # "looking now" replaces an older "not until March"
    return [_window([note], 0.7, day_of(note), day_of(note) + timedelta(days=90))]


# Calendar.

def _hold(events, opens, closes, name):
    return _window(events, 1.0, opens, closes, holds=[name])


@_detector("calendar_quiet_conference", "calendar")
def calendar_quiet_conference(events, subject, today):
    """Hold around a submission deadline and during a conference the person attends."""
    spans = {"conference_deadline": (timedelta(days=21), timedelta(days=2)),
             "conference_attending": (timedelta(days=3), timedelta(days=7))}
    out = []
    for e in _typed(events, *spans, subject=subject):
        before, after = spans[e["event_type"]]
        if day_of(e) - before <= today + timedelta(days=120):
            out.append(_hold([e], day_of(e) - before, day_of(e) + after, e["event_type"]))
    return out


@_detector("calendar_quiet_close", "calendar")
def calendar_quiet_close(events, subject, today):
    """Quarter close and audit season for people in finance roles.

    Quarter close holds the first 12 days of each calendar quarter. Audit season runs from 75 days before the
    anniversary of the employer's last 10-K through the day after it (the filing ends the audit), or 15 January to 31
    March when no filing is in view.
    """
    roles = [e for e in _typed(events, "officer_appointment", "job_started", subject=subject, dated=False)
             if FINANCE_TITLE.search(_title(e))]
    roles += [e for e in _typed(events, "profile_change", subject=subject, dated=False)
              if any(FINANCE_TITLE.search(str(c.get("to", ""))) for c in _changes(e))]
    if not roles:
        return []
    started = max(day_of(e) or observed_day(e) for e in roles)
    if any(started < day_of(e) <= today for e in own_ends(events, subject)):
        return []  # the finance role has ended: there is no close to work through
    filing = max(_typed(events, "annual_report_filed"), key=day_of, default=None)
    spans = []
    for year in (today.year - 1, today.year, today.year + 1):
        for month in (1, 4, 7, 10):
            spans.append((date(year, month, 1), date(year, month, 12), "quarter_close"))
        if filing:
            try:
                anniversary = day_of(filing).replace(year=year)
            except ValueError:  # a 10-K filed on 29 February, in a year without one
                anniversary = date(year, 2, 28)
            spans.append((anniversary - timedelta(days=75), anniversary + timedelta(days=1), "audit_season"))
        else:
            spans.append((date(year, 1, 15), date(year, 3, 31), "audit_season"))
    upcoming = sorted(s for s in spans if s[1] >= today)
    return [_hold(roles[-1:] + ([filing] if filing and name == "audit_season" else []), opens, closes, name)
            for opens, closes, name in upcoming[:2]]


# Holds.

@_detector("hold_recent_promotion", "holds")
def hold_recent_promotion(events, subject, today):
    """Hold for 180 days after a promotion, new responsibility, officer appointment or title change."""
    promoted = _typed(events, "officer_appointment", "promotion", "new_responsibility", subject=subject)
    promoted += [e for e in _typed(events, "profile_change", subject=subject) if _title_change(e)]
    return [_hold(g, day_of(g[0]), day_of(g[0]) + timedelta(days=180), "recent_promotion") for g in clusters(promoted)]


@_detector("hold_short_tenure", "holds")
def hold_short_tenure(events, subject, today):
    """Under a year in the current role.

    No hold once the role has a stated end, which is the moment itself, nor while their own "I'm open", said since
    the start, is open (said_open). A job start seen only on their LinkedIn profile (profile_job_started) holds too,
    and feeds nothing else.
    """
    # A start known only by its year places nothing, unless the profile gives it (profile_start).
    profile = _typed(events, "profile_job_started", subject=subject)
    first_read = {}  # a re-read of the same job never moves a year-only start later
    for e in profile:
        first_read[e["quote"]] = min(first_read.get(e["quote"], observed_day(e)), observed_day(e))
    starts = sorted([(d, e) for d, e in role_starts(events, subject) if e["date_precision"] != "year"]
                    + [(profile_start(e, first_read[e["quote"]]), e) for e in profile], key=lambda pair: pair[0])
    if not starts:
        return []
    start, event = starts[-1]
    # Only a profile read after the start was seen can say that job ended: LinkedIn lags.
    ended = [e for e in _typed(events, "profile_job_ended", subject=subject) if observed_day(e) > observed_day(event)]
    if today - start >= timedelta(days=365) or stated_end(events, subject, start, event) or ended \
            or said_open(events, subject, start, today):  # they said they're open since starting it
        return []
    return [_hold([event], start, start + timedelta(days=365), "short_tenure")]


# Days a grant holds: new equity for six months; a retention award until 60 days before its first vest a year out, so
# an offer can land after it vests.
GRANT_HOLD_DAYS = {"equity_refresh": (180, "equity_refresh"), "retention_or_commitment": (365 - 60, "retention_grant")}


@_detector("hold_equity_refresh", "holds")
def hold_equity_refresh(events, subject, today):
    """Hold after new equity (GRANT_HOLD_DAYS): six months, or until 60 days before a retention grant's first vest."""
    return [_hold(g, day_of(g[0]), day_of(g[0]) + timedelta(days=days), name)
            for g in clusters(_typed(events, *GRANT_HOLD_DAYS, subject=subject))
            for days, name in [GRANT_HOLD_DAYS[g[0]["event_type"]]]]


@_detector("hold_imminent_launch", "holds")
def hold_imminent_launch(events, subject, today):
    """The person's own launch, or one at an org in their view (their employer): nobody engages launch week, unless
    they said they're open from 30 days before it on and that is still open (said_open)."""
    launches = [e for e in _typed(made_by_hand(events), "launch_announced", *EXTRACTED_AS["launch_announced"])
                if e["subject_id"] == subject or (e["subject_type"] == "org" and e["subject_id"] != "gi")]
    return [_hold(g, day_of(g[0]) - timedelta(days=30), day_of(g[0]) + timedelta(days=7), "imminent_launch")
            for g in clusters(launches) if not said_open(events, subject, day_of(g[0]) - timedelta(days=30), today)]


@_detector("hold_not_looking", "holds")
def hold_not_looking(events, subject, today):
    """"Not looking; no recruiters": an undated contact constraint holds for a year, until they say otherwise."""
    out = []
    for e in _typed(events, "contact_constraint", subject=subject, dated=False):
        if day_of(e):
            continue
        seen = observed_day(e)
        later = [observed_day(x) for x in _typed(events, "self_stated_availability", "contact_constraint",
                                                  subject=subject, dated=False) if observed_day(x) > seen]
        out.append(_hold([e], seen, min([seen + timedelta(days=365), *later]), "not_looking"))
    return out


# Holds that a GI human resolves by checking a fact, not by waiting: readiness turns an open signal under one of these
# into verify_first. Under an opt-out hold readiness is quiet whatever else is open.
VERIFY_HOLDS = ("announced_next_role", "conflicting_role_claims")
OPT_OUT_HOLDS = ("not_looking",)
ACCEPTED_FALSIFIER = "Confirm whether the announced role was accepted: {quote!r} ({url})."
STILL_FALSIFIER = "Confirm where they work now: {moved!r} conflicts with {still!r} ({url})."
LEGAL_SUFFIX = re.compile(r"\b(inc|corp|corporation|co|llc|ltd|plc|lp)\b\.?", re.I)


def employer_named(event):
    """The employer in a "Title, Employer" role quote or Crustdata change, without its legal suffix."""
    text = next((str(c["to"]) for c in _changes(event) if c.get("field") == "experience.employment_details.current"
                 and c.get("to")), event["quote"])
    return LEGAL_SUFFIX.sub("", text.rpartition(",")[2]).strip(" .") if "," in text else ""


def _names(name, text):
    return bool(name) and re.search(rf"\b{re.escape(name)}\b", text, re.I) is not None


@_detector("hold_role_claim", "holds")
def hold_role_claim(events, subject, today):
    """The timeline says the person's next role is settled, or may be.

    An announced next role with a stated start, or on the person's own page or an official source (tier 1), is the
    person or the new employer saying it: a plain hold until a year past the start. An undated third-party claim
    ("incoming research scientist at Y") is a fact to check: a verify hold for 180 days from sighting. So is any
    announcement the person contradicts by saying they are looking at the same time or later, a role start dated from
    30 days before to 180 days after they said so, and a move followed by a later mention of them at the old employer:
    both cannot be current.
    """
    out = []
    availability = _typed(events, "self_stated_availability", subject=subject, dated=False)
    for group in clusters(_typed(events, "role_announced", subject=subject, dated=False)):
        seen, e = observed_day(group[0]), group[0]
        looking = [x for x in availability if observed_day(x) >= seen]
        if (confirmed := [x for x in group if x["tier"] == 1 or day_of(x)]) and not looking:
            start = day_of(confirmed[0]) or seen
            out.append(_hold(group, min(seen, start), start + timedelta(days=365), "accepted_next_role"))
            continue
        falsifier = ACCEPTED_FALSIFIER.format(quote=e["quote"][:160], url=e["source_url"] or "no URL")
        out.append({**_hold(group + looking[:1], seen, seen + timedelta(days=180), "announced_next_role"),
                    "falsifier": falsifier + (f" It conflicts with {looking[0]['quote'][:160]!r}." if looking else "")})
    for said in availability:
        said_day = observed_day(said)
        for start, e in role_starts(events, subject):
            if timedelta(days=-30) <= start - said_day <= timedelta(days=180):
                out.append({**_hold([said, e], min(start, said_day), max(start, said_day) + timedelta(days=90),
                                    "conflicting_role_claims"),
                            "falsifier": ACCEPTED_FALSIFIER.format(quote=e["quote"][:160], url=e["source_url"] or "no URL")
                            + f" It conflicts with {said['quote'][:160]!r}."})
    starts = role_starts(events, subject)
    if len(starts) >= 2:
        (moved_day, moved), (_, before) = starts[-1], starts[-2]
        old, new = employer_named(before), employer_named(moved)
        still = [e for e in events if e["subject_id"] == subject and day_of(e) and day_of(e) > moved_day
                 and e["event_type"] not in ROLE_START_TYPES and not _new_employment(e)
                 and _names(old, e["quote"]) and not _names(new, e["quote"])]
        if still:
            e = still[-1]
            out.append({**_hold([moved, e], moved_day, day_of(e) + timedelta(days=90), "conflicting_role_claims"),
                        "falsifier": STILL_FALSIFIER.format(moved=_title(moved)[:160], still=e["quote"][:160],
                                                            url=e["source_url"] or "no URL")})
    return out


# Running.

def colleagues(store, subject_id):
    """Other officers at the same employer, for SEC person ids ("name|company"): their 5.02 changes are this person's
    context."""
    if "|" not in subject_id:
        return []
    company = subject_id.split("|", 1)[1]
    return sorted({e["subject_id"] for e in store.all("timeline_event")
                   if e["subject_id"] != subject_id and e["subject_id"].partition("|")[2] == company})


# Letters styled with Unicode's mathematical alphabets, as posts set in bold or italic are ("𝐈 𝐭𝐮𝐫𝐧𝐞𝐝"), and the
# older letterlike ones those alphabets borrow ("ℎ" in italic "𝑇ℎ𝑒").
STYLED = re.compile("[\U0001D400-\U0001D7FF"
                    "\u2102\u210A-\u210E\u2110-\u2112\u2115\u2119-\u211D\u2124\u2128\u212C\u212D\u212F-\u2131\u2133\u2134]")


def plain(text):
    """Text with its styled letters as plain ones (STYLED), everything else as written."""
    return STYLED.sub(lambda m: unicodedata.normalize("NFKC", m[0]), text)


def view(store, subject_id, as_of, related=()):
    """The subject's timeline plus those of related subjects (employer org, coauthors, "gi") and SEC colleagues.

    GitHub launches are read as what they are (as_built), a post that puts their paper out as the paper (as_said), and
    a post set in styled letters in plain ones (plain), so a draft can quote it and a check can read it.
    """
    ids = dict.fromkeys((subject_id, *related, *colleagues(store, subject_id)))
    return as_said(as_built([{**e, "quote": plain(e["quote"])} if STYLED.search(e.get("quote") or "") else e
                             for sid in ids for e in timeline.timeline(store, sid, as_of)]))


def own_copy(events):
    """A detector's own copy of the view.

    Timeline events are flat records (the fields of TimelineEvent and its subclasses, and the store's id, revision and
    updated_at, are all scalars; test_today checks it), so copying each dict is a full copy. It is some 60 times
    cheaper than copy.deepcopy, which was most of a live call's time.
    """
    return [dict(e) for e in events]


def detect(events, subject_id, as_of):
    """Run every registered detector over one view; an activation may cite only events in it.

    Each detector gets its own copy of the view (own_copy). An event with a future event_date stays visible when it
    was observed by as_of: an announced start or deal close is knowable in advance. An activation that rests only on
    GitHub items that are not releases, or on what was read from them (a paper only when the item does not link it),
    is marked code_only.
    """
    visible = {e["id"]: e for e in events}
    github = {item(e): e for e in events if e["event_type"] in GITHUB_TYPES}

    # A GitHub item that is not a release, or what the post reader read from one; a paper only when the item does
    # not link it.
    def code(event):
        src = github.get(item(event))
        return src is not None and not released(src) and not (
            event["event_type"] in PAPER_TYPES and PAPER_LINK.search(own_words(src["quote"])))

    activations = []
    for detector_id, (family, fn) in timeline.DETECTORS.items():
        for raw in fn(own_copy(events), subject_id, as_of) or []:
            activation = timeline.Activation.model_validate({**raw, "detector_id": detector_id, "family": family})
            if not set(activation.evidence_event_ids) <= visible.keys():
                raise timeline.LeakageError(f"{detector_id} cited an event not observed by {as_of}")
            activation.code_only = all(code(visible[i]) for i in activation.evidence_event_ids)
            # One event also told in a post: the post first, so what a note quotes is theirs, not a commit message.
            activation.evidence_event_ids.sort(key=lambda i: code(visible[i]))
            activations.append(activation.model_dump())
    return activations
