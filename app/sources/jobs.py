"""Public job boards: Greenhouse, Lever and Ashby, which companies publish for anyone to read.

Each board has a free JSON endpoint keyed by the company's board slug (the part of
its careers URL after the host). The response shapes below are from those
boards' public docs as remembered, not yet checked against a live response: the
cloud blocks all three hosts, so the first Mac run is the check.
"""

import html
import json
import re
from datetime import datetime, timezone

from .records import fetch_text

BOARDS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def _plain(text):
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(text or "")).split())


def _day(value):
    if isinstance(value, (int, float)):  # Lever: milliseconds since the epoch
        return datetime.fromtimestamp(value / 1000, timezone.utc).date().isoformat()
    return (value or "")[:10]


def postings(fetch, board, slug):
    """Open postings on one board, each {title, url, posted, location, text}. ``posted`` is the day the board
    says it was first published ("" when it gives none: an edit date is not a publication date)."""
    text, _ = fetch_text(fetch, BOARDS[board].format(slug=slug))
    data = json.loads(text)
    if board == "greenhouse":
        return [{"title": j["title"], "url": j["absolute_url"], "posted": _day(j.get("first_published")),
                 "location": (j.get("location") or {}).get("name", ""), "text": _plain(j.get("content"))}
                for j in data.get("jobs", [])]
    if board == "lever":
        return [{"title": j["text"], "url": j["hostedUrl"], "posted": _day(j.get("createdAt")),
                 "location": (j.get("categories") or {}).get("location", ""),
                 "text": _plain(" ".join([j.get("descriptionPlain") or "", *((part or {}).get("content") or "" for part in j.get("lists") or []),
                                          j.get("additionalPlain") or ""]))}
                for j in data]
    return [{"title": j["title"], "url": j["jobUrl"], "posted": _day(j.get("publishedAt")), "location": j.get("location", ""),
             "text": _plain(j.get("descriptionPlain") or j.get("descriptionHtml"))}
            for j in data.get("jobs", []) if j.get("isListed", True)]
