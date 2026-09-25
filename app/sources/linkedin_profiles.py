"""Who someone works for now and since when, from their public LinkedIn profile, for the live watchlist only.

  PROFILE  harvestapi/linkedin-profile-scraper, a profile URL in, the profile with its experience
           out; about $4 per 1,000 profiles in the no-email mode; no login or cookie.

A profile read today shows today's job, so it would show a past-moment person's move before
their replay reaches it. It is for live calls on watchlist people only: rows with an until
date are refused. The one thing it puts on the timeline is job_events: the start of their
latest job, seen the day the profile was read, which only the short-tenure hold reads.

roles() turns the actor's items into one row per person:
  status        current, no_current_role (every listed job has ended), no_experience
                (the profile came back without a job list) or not_returned
  current       the jobs with no end date, latest start first: title, company, started, and joined (when
                they joined that company: a promotion is a new job there)
  last_ended    the most recently ended job, with ended, for "left a job"
  moved_to      the profile's public address now, when it differs from the one asked for
                (LinkedIn redirected it, or it was asked by member id)
Dates are "YYYY-MM", or "YYYY" when the profile gives only a year: LinkedIn shows months.

links() finds a profile URL for people without one, free: in the author block of their tweets
in a saved X pull (bio and its links) and in their GitHub profile (bio, blog, linked accounts),
both written by the person. It never searches by name, and leaves the URL blank when there is
none or the places disagree. When neither place has one, the personal site a GitHub profile
names is listed for a person to open by hand, never fetched: a site can link someone else's
profile, and fetching an address a profile controls from Justin's Mac is not worth the risk for
a handful of links.
"""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import github
from .social import linkedin_url

PROFILE = "harvestapi~linkedin-profile-scraper"
PRICE = 0.004  # per profile, no-email mode
MODE = "Profile details no email ($4 per 1k)"
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
PRESENT = {"present", "current", "now", "today"}
RUN_GAP_MONTHS = 3  # a later job at the same company this soon after one ended is the same stay there
MEMBER_ID = re.compile(r"ACoA[A-Za-z0-9_-]{20,}")  # LinkedIn's opaque member id, not a public slug
# Only a profile link on its own (not inside another URL), and only its slug: trailing punctuation
# or a sub-page such as /details/experience/ is left off.
PROFILE_URL = re.compile(r"(?<![\w./=?&%@-])(?:https?://)?(?:(?:www|m|[a-z]{2})\.)?linkedin\.com/in/([\w%-]*\w)", re.I)


def profile_input(urls):
    return {"queries": list(urls), "profileScraperMode": MODE}


def worst_usd(urls):
    return round(PRICE * len(urls), 3)


# ------------------------------------------------------------------ dates and jobs

def _month(value):
    """ "YYYY-MM" or "YYYY" from {"month", "year", "text"}, "Mar 2024", "2024-03" or "2024"; "" when unknown."""
    if isinstance(value, dict):
        year, month = str(value.get("year") or ""), str(value.get("month") or "")
        if re.fullmatch(r"\d{4}", year):
            month = MONTHS.get(month[:3].lower()) or (int(month) if month.isdigit() else 0)
            return f"{year}-{month:02d}" if 1 <= month <= 12 else year
        value = value.get("text") or ""
    text = str(value or "").strip()
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", text):
        return f"{m[1]}-{int(m[2]):02d}"
    if m := re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", text):
        return f"{m[2]}-{MONTHS[m[1].lower()]:02d}" if m[1].lower() in MONTHS else m[2]
    if re.fullmatch(r"\d{4}", text):
        return text
    return ""


def _ended(job):
    """The end month, "" for a current job (no end, an empty one, or "Present"); an end that is
    there but not a date counts as ended, "unknown"."""
    end = job.get("endDate") or job.get("end_date") or job.get("end")
    if month := _month(end):
        return month
    text = str((end.get("text") if isinstance(end, dict) else end) or "").strip().lower()
    return "" if not text or text in PRESENT else "unknown"


def _company(job, parent=None):
    company = job.get("companyName") or job.get("company") or (parent or {}).get("companyName") or (parent or {}).get("company")
    return (company.get("name") if isinstance(company, dict) else company) or ""


def _jobs(profile):
    """Flat job list; a company block that groups several roles gives each role the company's name."""
    out = []
    experience = profile.get("experience")
    for job in experience if isinstance(experience, list) else []:
        if not isinstance(job, dict):
            continue
        grouped = job.get("positions") or job.get("roles")
        for role in grouped if isinstance(grouped, list) else [job]:
            if not isinstance(role, dict):
                continue
            out.append({"title": role.get("position") or role.get("title") or "",
                        "company": _company(role, job),
                        "started": _month(role.get("startDate") or role.get("start_date") or role.get("start")),
                        "ended": _ended(role)})
    return out


def _latest_first(jobs, key):
    return sorted(jobs, key=lambda j: "" if j[key] == "unknown" else j[key], reverse=True)


def _months(month, end=False):
    """A "YYYY-MM" or "YYYY" as a count of months; a year alone is its last month for an end, its first for a start."""
    return int(month[:4]) * 12 + (int(month[5:7]) - 1 if len(month) == 7 else 11 if end else 0)


def _joined(job, jobs):
    """When they joined the company of a current job: the start of the earliest job there in an unbroken run up to it
    (at most a few months between), since LinkedIn lists a promotion as a new job at the same company."""
    since = job["started"]
    same = [j for j in jobs if j["company"].casefold() == job["company"].casefold() and j["started"]]
    for j in _latest_first(same, "started"):
        if since and j["started"] < since and (not j["ended"] or (
                j["ended"] != "unknown" and _months(since) - _months(j["ended"], end=True) <= RUN_GAP_MONTHS)):
            since = j["started"]
    return since


def roles(people, items):
    """{person_id: row} for everyone asked, matched on the canonical profile URL or public identifier."""
    by_url = {_key(p.linkedin_url): p.person_id for p in people if p.linkedin_url}
    asked = {person_id: url for url, person_id in by_url.items()}
    found, jobs_seen = {}, {}
    for item in (i for i in items if isinstance(i, dict)):
        person_id = next((by_url[u] for k in _asked_as(item) if (u := _key(k)) in by_url), None)
        jobs = _jobs(item)
        # A second item for the same profile (an error row, an empty retry) never replaces a fuller one.
        if not person_id or len(jobs) < jobs_seen.get(person_id, 0) or (person_id in found and not jobs):
            continue
        jobs_seen[person_id] = len(jobs)
        current = _latest_first([j for j in jobs if not j["ended"]], "started")
        ended = _latest_first([j for j in jobs if j["ended"]], "ended")
        found[person_id] = {
            "status": "current" if current else "no_current_role" if jobs else "no_experience",
            "current": [{**{k: j[k] for k in ("title", "company", "started")}, "joined": _joined(j, jobs)} for j in current],
            "last_ended": ended[0] if ended else None,
            "headline": item.get("headline") or "",
            **({"moved_to": now} if (now := _now_at(item)) and _key(now) != asked[person_id] else {}),
        }
    return {p.person_id: {"linkedin_url": p.linkedin_url, **found.get(p.person_id, {"status": "not_returned"})}
            for p in people if p.linkedin_url}


def _asked_as(item):
    """The addresses an item may be known by, the one we asked for first: the actor echoes the
    query (originalQuery: {"query": url}), and LinkedIn may have redirected it to a new slug."""
    out = []
    for k in ("originalQuery", "query", "linkedinUrl", "url"):
        value = item.get(k)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            out += [v for v in value.values() if isinstance(v, str)]
    if item.get("publicIdentifier"):
        out.append("https://www.linkedin.com/in/" + str(item["publicIdentifier"]))
    return out


def _now_at(item):
    """The profile's address as LinkedIn serves it now, by public slug; "" for an opaque member-id
    URL or anything that is not a /in/<slug> profile address."""
    slug = str(item.get("publicIdentifier") or "")
    if not slug and linkedin_url(url := str(item.get("linkedinUrl") or "")):
        slug = urlsplit(url).path.strip("/").split("/")[1]
    return "" if not slug or MEMBER_ID.fullmatch(slug) else linkedin_url("https://www.linkedin.com/in/" + slug) or ""


def _key(url):
    """The canonical URL with its slug decoded, so j%C3%B6rg and jörg are one profile."""
    return unquote(linkedin_url(url) or "").lower()


# ------------------------------------------------------------------ free link finding

def _urls_in(value):
    """Every LinkedIn profile URL in the strings of a nested JSON value, canonical."""
    if isinstance(value, dict):
        return set().union(set(), *(_urls_in(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(set(), *(_urls_in(v) for v in value))
    if not isinstance(value, str):
        return set()
    slugs = (unquote(slug).lower() for slug in PROFILE_URL.findall(value))
    return {"https://www.linkedin.com/in/" + slug for slug in slugs if not re.search(r"[/\s]", slug)}  # %2F is no slug


def x_bios(raw_files):
    """{handle lowercased: URLs} from the author block of every tweet in saved X pulls."""
    out = {}
    for path in raw_files:
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        for item in raw.get("apidojo~tweet-scraper", []) if isinstance(raw, dict) else []:
            author = item.get("author") or {}
            if handle := (author.get("userName") or "").lower():
                out.setdefault(handle, set()).update(_urls_in(author))
    return out


def github_links(login, http, token=""):
    """(URLs in the GitHub profile's bio and blog and its linked accounts, the blog address) in two free calls."""
    about = github.profile(login, token, http)
    return _urls_in([about["bio"], about["blog"], about["social"]]), about["blog"].strip()


def links(people, bios, github_found, sites=None):
    """[{person_id, linkedin_url, source}] for people without a URL; blank when none was found or places disagree
    (then disagree lists them), and personal_site names the site to check by hand when nothing was found."""
    out = []
    for p in people:
        if p.linkedin_url:
            continue
        seen = {"x_bio": bios.get(p.x_handle.lower(), set()) if p.x_handle else set(),
                "github": github_found.get(p.person_id, set())}
        urls = set().union(*seen.values())
        site = (sites or {}).get(p.person_id, "")
        out.append({"person_id": p.person_id, "linkedin_url": next(iter(urls)) if len(urls) == 1 else "",
                    "source": ", ".join(k for k, v in seen.items() if v) or "none found",
                    **({"disagree": sorted(urls)} if len(urls) > 1 else {}),
                    # Listed for a person to open by hand when nothing was found; never fetched or used.
                    **({"personal_site": site} if site and not urls else {})})
    return out


SIDE_JOB = re.compile(r"\b(?:advis[eo]r|board|investor|angel|mentor|volunteer|ambassador)\b", re.I)
EXTRACTOR = "linkedin_profile_v1"


def job_events(rows, pulled_on):
    """profile_job_started per person: the start of their latest current job that is not an advisory or board
    seat, at the month (or year) the profile gives, observed the day it was read. Only hold_short_tenure reads it (no
    reach in a job's first year; detectors.profile_start reads a year alone as its latest day); a profile's start
    never counts toward a reach, as "two years in the seat" never does."""
    out = []

    def add(person_id, row, kind, day, quote):
        if len(day) in (4, 7) and day[:4].isdigit():  # else no date the profile gives
            out.append({"subject_type": "person", "subject_id": person_id, "event_type": kind, "event_date": day,
                        "date_precision": "month" if len(day) == 7 else "year",
                        "observed_at": f"{pulled_on}T00:00:00+00:00", "source_url": row.get("linkedin_url") or "",
                        "quote": quote, "tier": 1, "extractor": EXTRACTOR,
                        "source_version_hash": hashlib.sha256(quote.encode()).hexdigest()})

    for person_id, row in rows.items():
        if job := main_job(row):
            add(person_id, row, "profile_job_started", job.get("started") or "",
                f"{job.get('title') or 'A job'} at {job.get('company') or '?'} since {job.get('started')}")
        elif row.get("status") == "no_current_role" and (last := row.get("last_ended")):
            # No job now: the one that ended lifts a first-year hold an earlier read started.
            add(person_id, row, "profile_job_ended", last.get("ended") or "",
                f"{last.get('title') or 'A job'} at {last.get('company') or '?'}, {last.get('started') or '?'} to "
                f"{last.get('ended')}")
    return out


def names(company, text):
    """Whether ``text`` names ``company`` as whole words: "Meta" is not in "Metaculus", nor "X" in "next"."""
    company = (company or "").strip()
    return bool(company) and re.search(rf"(?<!\w){re.escape(company)}(?!\w)", text or "", re.I) is not None


def main_job(row):
    """The job a profile row is about: of its current jobs that are not advisory or board seats, the one the
    headline names, else the latest started (the first listed when none gives a start). None when there is none."""
    jobs = [j for j in row.get("current") or [] if not SIDE_JOB.search(j.get("title") or "")]
    return next((j for j in jobs if names(j.get("company"), row.get("headline"))), None) or \
        max(jobs, key=lambda j: j.get("started") or "", default=None)
