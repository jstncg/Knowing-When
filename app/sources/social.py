"""What the social intakes share: who to pull, their window, and the event every post, comment or repo becomes.

app.sources.apify (X and LinkedIn) and app.sources.github turn what a person made public into
TimelineEvents with the same shape, so the post reader reads them alike: the person's own words
first, then "In reply to <who>: <parent>" when they answered someone; observed_at is when it
became public.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import date

from ..crustdata import linkedin_url as _canonical_profile
from ..models import iso, utcnow
from ..timeline import TimelineEvent

QUOTE_LIMIT = 4000
PARENT_LIMIT = 300
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
COUNTRY_HOST = re.compile(r"^(https?://)[a-z]{2}\.linkedin\.com/", re.I)


def linkedin_url(value):
    """A profile URL in Crustdata's canonical form; country hosts (uk.linkedin.com) count as www."""
    return _canonical_profile(COUNTRY_HOST.sub(r"\1www.linkedin.com/", value or ""))


@dataclass(frozen=True)
class Person:
    """One person to pull: their store id, where they post, and the window wanted (ISO dates, both optional)."""
    person_id: str
    x_handle: str = ""
    linkedin_url: str = ""
    since: str = ""
    until: str = ""
    github: str = ""

    @classmethod
    def from_dict(cls, row):
        handle = (row.get("x_handle") or "").strip().lstrip("@")
        if handle and not HANDLE.fullmatch(handle):
            raise ValueError(f"not an X handle: {handle!r}")
        url = row.get("linkedin_url") or ""
        if url and not linkedin_url(url):
            raise ValueError(f"not a LinkedIn profile URL: {url!r}")
        github = (row.get("github") or "").strip()
        if github and not GITHUB_LOGIN.fullmatch(github):
            raise ValueError(f"not a GitHub login: {github!r}")
        since, until = (date.fromisoformat(row[k]).isoformat() if row.get(k) else "" for k in ("since", "until"))
        return cls(row["person_id"], handle, linkedin_url(url) or "", since, until, github)


def in_window(when, person):
    day = when.date().isoformat()
    return when <= utcnow() and (not person.since or day >= person.since) and (not person.until or day < person.until)


def responding(own, to, parent):
    """The post reader's shape: the person's words, a blank line, then what they answered (the reader
    treats everything before "In reply to" as theirs)."""
    return f"{own.strip()}\n\nIn reply to {to}" + (f": {parent.strip()[:PARENT_LIMIT]}" if parent.strip() else "")


def post_event(person, event_type, when, url, quote, extractor):
    quote = quote.strip()[:QUOTE_LIMIT]
    return TimelineEvent(
        subject_type="person", subject_id=person.person_id, event_type=event_type,
        event_date=when.date().isoformat(), date_precision="day", observed_at=iso(when),
        source_url=url, source_version_hash=hashlib.sha256(quote.encode()).hexdigest(),
        quote=quote, tier=1, extractor=extractor,
    ).model_dump()
