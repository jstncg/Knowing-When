"""NSF award start and expected end dates, as dated person and org events.

The NSF Awards API (api.nsf.gov/services/v1/awards.json) is free, keyless
and returns every active and past award with its start date and expected
end date. A grant's expiration is a hard funding window: the PI and the
postdocs and students it pays either find new money or move. Because the
date is set years in advance, it is one of the few timing signals for
academics that is knowable long before the person says anything.

Coverage: NSF only. NIH, DOE, DARPA, industry gifts, foundation grants and
non-US funders are invisible here, as are no-cost extensions (the expected
end date moves without a new award) and who besides the PI and co-PIs is
paid. A name is looked up both as PI and as co-PI, with NSF's own exact
name search, so common names return more than one person; check the
institution. Events name the person searched for, not every co-PI.
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

from ..timeline import TimelineEvent
from .http import Fetcher, content_hash
from .warn import org_id

API = "https://api.nsf.gov/services/v1/awards.json"
AWARD_PAGE = "https://www.nsf.gov/awardsearch/showAward?AWD_ID="
FIELDS = "id,title,date,startDate,expDate,pdPIName,awardeeName,awardeeCity,awardeeStateCode,fundsObligatedAmt,fundProgramName"
PAGE = 25  # the API's maximum rpp
MAX_PAGES = 40


def parse_date(value) -> str | None:
    """ISO day from the API's 'MM/DD/YYYY' (or an ISO day), else None."""
    text = str(value or "")
    if m := re.search(r"(\d{4})-(\d{2})-(\d{2})", text):
        y, mo, d = (int(g) for g in m.groups())
    elif m := re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text):
        mo, d, y = (int(g) for g in m.groups())
    else:
        return None
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def person_id(name: str) -> str:
    return "person:" + "-".join(re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split())


@dataclass(frozen=True)
class Award:
    id: str
    title: str
    pi: str               # the award's PI as NSF lists it, even when searched as co-PI
    institution: str
    awarded: str | None   # ISO day NSF dated the award (when it became public)
    start: str | None
    end_expected: str | None
    amount: int | None
    program: str
    api_url: str
    source_version_hash: str

    @property
    def line(self) -> str:
        parts = [f"NSF award {self.id}: {self.title}", f"PI {self.pi}", self.institution,
                 f"{self.start} to {self.end_expected}"]
        if self.amount is not None:
            parts.append(f"${self.amount:,}")
        if self.program:
            parts.append(self.program)
        return " | ".join(p for p in parts if p)[:300]

    def events(self) -> list[dict]:
        # observed_at is the award date: NSF publishes an award when it is
        # made, before the start date, so both dates were knowable from then.
        observed = f"{self.awarded or self.start}T00:00:00+00:00"
        base = dict(observed_at=observed, source_url=AWARD_PAGE + self.id,
                    source_version_hash=self.source_version_hash, quote=self.line,
                    tier=1, extractor="nsf_awards_v1", date_precision="day")
        subjects = [("person", person_id(self.pi)), ("org", org_id(self.institution))]
        dates = [("grant_started", self.start), ("grant_end_expected", self.end_expected)]
        return [
            TimelineEvent(subject_type=st, subject_id=sid, event_type=et, event_date=d, **base).model_dump()
            for st, sid in subjects for et, d in dates if d
        ]


def _award(raw: dict, api_url: str, version_hash: str) -> Award:
    amount = raw.get("fundsObligatedAmt")
    return Award(
        id=str(raw["id"]), title=" ".join(raw.get("title", "").split()),
        pi=raw.get("pdPIName", ""), institution=raw.get("awardeeName", ""),
        awarded=parse_date(raw.get("date")), start=parse_date(raw.get("startDate")),
        end_expected=parse_date(raw.get("expDate")),
        amount=int(float(amount)) if amount not in (None, "") else None,
        program=raw.get("fundProgramName", ""), api_url=api_url, source_version_hash=version_hash,
    )


def _search(fetch, query: dict) -> list[Award]:
    found = []
    for page in range(MAX_PAGES):
        url = f"{API}?{urlencode({**query, 'printFields': FIELDS, 'rpp': PAGE, 'offset': page * PAGE + 1})}"
        body = fetch(url)
        response = json.loads(body).get("response", {})
        if notice := response.get("serviceNotification"):
            raise ValueError(f"NSF API: {notice}")
        batch = response.get("award", [])
        found += [_award(a, url, content_hash(body)) for a in batch]
        if len(batch) < PAGE:
            break
    return found


def awards(pi: str | None = None, institution: str | None = None, fetch=None) -> list[Award]:
    """Awards naming a person as PI or co-PI, and/or an institution; newest start first."""
    if not (pi or institution):
        raise ValueError("Give a PI name or an institution")
    fetch = fetch or Fetcher()
    org = {"awardeeName": f'"{institution}"'} if institution else {}
    queries = [{**org, role: f'"{pi}"'} for role in ("pdPIName", "coPDPI")] if pi else [org]
    found = {a.id: a for query in queries for a in _search(fetch, query)}
    return sorted(found.values(), key=lambda a: a.start or "", reverse=True)
