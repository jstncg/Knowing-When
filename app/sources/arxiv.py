"""arXiv: one event per paper version, plus affiliations when the API states them.

The export API lists an author's papers with the v1 (`published`) and latest
(`updated`) timestamps; the abstract page's submission history carries every
version. Author name matching is arXiv's own, so a common name may return
other people's papers: the caller passes the subject_id it has verified.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlencode

from .http import Fetcher
from .records import fetch_text, person_event

API = "https://export.arxiv.org/api/query?"
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ID_RE = re.compile(r"/abs/(?P<paper>\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})v(?P<version>\d+)$")
HISTORY_RE = re.compile(r"\[v(\d+)\]\s*([A-Za-z]{3}, \d{1,2} [A-Za-z]{3} \d{4} \d{2}:\d{2}:\d{2}) UTC")


def listing_url(name: str, max_results: int = 100) -> str:
    return API + urlencode({"search_query": f'au:"{name}"', "max_results": max_results,
                            "sortBy": "submittedDate", "sortOrder": "descending"})


def author_papers(fetch, name: str) -> tuple[list[dict], str]:
    """Papers listing `name` as an author, newest first, with the listing's version hash."""
    text, version_hash = fetch_text(fetch, listing_url(name))
    papers = []
    for entry in ET.fromstring(text).findall("a:entry", NS):
        match = ID_RE.search(entry.findtext("a:id", "", NS))
        if not match:
            continue
        papers.append({
            "arxiv_id": match["paper"], "latest_version": int(match["version"]),
            "title": " ".join(entry.findtext("a:title", "", NS).split()),
            "published": entry.findtext("a:published", "", NS),
            "updated": entry.findtext("a:updated", "", NS),
            "authors": [{"name": a.findtext("a:name", "", NS),
                         "affiliation": a.findtext("arxiv:affiliation", "", NS)}
                        for a in entry.findall("a:author", NS)],
        })
    return papers, version_hash


def versions(fetch, arxiv_id: str) -> list[dict]:
    """Every version's submission timestamp from the abstract page."""
    text, _ = fetch_text(fetch, f"https://arxiv.org/abs/{arxiv_id}")
    section = text.split("Submission history", 1)
    history = re.sub(r"<[^>]+>", " ", section[1]) if len(section) == 2 else ""
    out = []
    for number, stamp in HISTORY_RE.findall(history):
        when = datetime.strptime(stamp, "%a, %d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc)
        out.append({"version": int(number), "submitted": when.isoformat(), "quote": f"[v{number}] {stamp} UTC"})
    return out


def events(name: str, subject_id: str | None = None, since: str | None = None,
           with_versions: bool = True, fetch=None) -> list[dict]:
    """paper_v1 / paper_revised per version and affiliation_seen per stated affiliation.

    `since` (ISO date) skips papers with no version on or after it, which
    avoids one abstract-page fetch per old paper of a prolific author.
    """
    fetch = fetch or Fetcher()
    subject = subject_id or f"arxiv:{name}"
    papers, version_hash = author_papers(fetch, name)
    out = []
    for paper in papers:
        if since and paper["updated"][:10] < since:
            continue
        abs_url = f"https://arxiv.org/abs/{paper['arxiv_id']}"
        history = versions(fetch, paper["arxiv_id"]) if with_versions and paper["latest_version"] > 1 else []
        if not history:
            history = [{"version": 1, "submitted": paper["published"], "quote": f"published {paper['published']}"}]
            if paper["latest_version"] > 1:
                history.append({"version": paper["latest_version"], "submitted": paper["updated"],
                                "quote": f"v{paper['latest_version']} updated {paper['updated']}"})
        for v in history:
            out.append(person_event(subject, "paper_v1" if v["version"] == 1 else "paper_revised",
                                    v["submitted"][:10], "day", v["submitted"], f"{abs_url}v{v['version']}",
                                    version_hash, f"{paper['title']} ({paper['arxiv_id']}v{v['version']}): {v['quote']}",
                                    "arxiv_api"))
        for author in paper["authors"]:
            if author["affiliation"] and author["name"].lower() == name.lower():
                out.append(person_event(subject, "affiliation_seen", paper["published"][:10], "day",
                                        paper["published"], abs_url, version_hash,
                                        f"{author['name']}: {author['affiliation']} on {paper['arxiv_id']}", "arxiv_api"))
    return out
