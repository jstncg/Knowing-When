"""WARN layoff notices, as dated org events, from Big Local News's consolidated file.

The federal WARN Act makes employers with 100+ workers file notice with the
state about 60 days before a mass layoff or closure, and states publish the
filings. That gives an employer-side, dated, free signal that a team is about
to be cut, weeks before anyone on it posts about it.

Source: Big Local News (Stanford) scrapes every state's WARN page daily and
publishes one consolidated CSV (44 states + DC, notices back to 1988) on
GitHub. It replaces per-state scrapers that kept breaking (TX refused us,
WA and NY moved), and it records when each notice was first seen:

  observed_at   `first_inserted_date`, the day BLN's scraper first saw the
                notice (median 7 days after the notice date). Amendments
                inherit their original's date. Notices from the initial
                bulk load of 2022-04-07/08 carry that load date instead of a
                sighting, so they fall back to the notice date itself.
  amendments    Each revision is a row; superseded rows are dropped so one
                notice is one event.

Coverage: WARN cannot see layoffs below the 100-employee / 50-affected
thresholds, most private startups, quiet attrition, hiring freezes, or
individual departures. Effective dates are the employer's plan and slip.
Notices name the employer's legal entity, so lookups match on a normalized
name (suffixes and punctuation removed), not on subsidiaries or brands.
"""

import csv
import io
import re
from dataclasses import dataclass

from ..timeline import TimelineEvent
from .http import Fetcher, content_hash, decode

SOURCE_URL = ("https://raw.githubusercontent.com/biglocalnews/warn-github-flow/transformer/"
              "data/warn-transformer/processed/integrated.csv")
BULK_LOAD_END = "2022-04-09"  # first_inserted_date before this is the initial backfill, not a sighting
ORG_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "the", "dba", "holdings", "group", "usa", "us",
}


def org_id(name: str) -> str:
    """Stable subject id for an employer: 'org:' + lower-cased core name."""
    words = re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("&", " and ")).split()
    core = [w for w in words if w not in ORG_SUFFIXES] or words
    return "org:" + "-".join(core)


@dataclass(frozen=True)
class WarnNotice:
    state: str
    company: str
    notice_date: str            # ISO day the filing is dated (first-seen day if the state gives none)
    effective_date: str | None  # ISO day layoffs begin, if the state gives one
    headcount: int | None
    action: str                 # closure, temporary layoff, or layoff
    location: str
    observed_at: str            # ISO time the notice was first knowable to us
    source_url: str
    source_version_hash: str

    @property
    def line(self) -> str:
        parts = [self.company, f"{self.state.upper()} WARN notice {self.notice_date}"]
        if self.effective_date:
            parts.append(f"effective {self.effective_date}")
        if self.headcount is not None:
            parts.append(f"{self.headcount} employees")
        parts += [p for p in (self.action, self.location) if p]
        return " | ".join(parts)[:300]

    def event(self) -> dict:
        return TimelineEvent(
            subject_type="org", subject_id=org_id(self.company), event_type="warn_notice",
            event_date=self.notice_date, date_precision="day", observed_at=self.observed_at,
            source_url=self.source_url, source_version_hash=self.source_version_hash,
            quote=self.line, tier=1, extractor="warn_bln_v1",
        ).model_dump()


def _day(value: str) -> str | None:
    return value[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value[:10]) else None


def _action(row: dict) -> str:
    if row["is_closure"] == "True":
        return "closure"
    return "temporary layoff" if row["is_temporary"] == "True" else "layoff"


def parse(body: bytes, states=None) -> list[WarnNotice]:
    """Current version of every notice in the consolidated CSV, optionally for some states."""
    wanted = {s.upper() for s in states} if states else None
    version = content_hash(body)
    notices = []
    for row in csv.DictReader(io.StringIO(decode(body))):
        if row["is_superseded"] == "True" or (wanted and row["postal_code"] not in wanted):
            continue
        company = " ".join(row["company"].split())
        first_seen = row["first_inserted_date"]
        notice_date = _day(row["notice_date"]) or _day(first_seen)
        if not company or not notice_date:
            continue
        observed = first_seen if first_seen[:10] >= BULK_LOAD_END else f"{notice_date}T00:00:00+00:00"
        notices.append(WarnNotice(
            state=row["postal_code"].lower(), company=company, notice_date=notice_date,
            effective_date=_day(row["effective_date"]),
            headcount=int(float(row["jobs"])) if row["jobs"] else None,
            action=_action(row), location=" ".join(row["location"].split()),
            observed_at=observed, source_url=SOURCE_URL, source_version_hash=version,
        ))
    return notices


def load(states=None, fetch=None) -> list[WarnNotice]:
    return parse((fetch or Fetcher())(SOURCE_URL), states)


def matches(query: str, company: str) -> bool:
    """The query's core name words all appear in the notice's employer name."""
    wanted = set(org_id(query)[4:].split("-"))
    return wanted <= set(org_id(company)[4:].split("-"))


def events(notices) -> list[dict]:
    return [n.event() for n in notices]
