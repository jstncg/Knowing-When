"""OpenAlex: coauthors and affiliations per work, institution history per author.

Set OPENALEX_MAILTO to join the polite pool; it is never hard-coded.
"""

import json
import os
import re
from urllib.parse import urlencode

import httpx

from ..models import iso
from .http import Fetcher
from .records import fetch_text, person_event

API = "https://api.openalex.org"


def _url(path: str, params: dict) -> str:
    mailto = os.environ.get("OPENALEX_MAILTO")
    return f"{API}/{path}?" + urlencode({**params, "mailto": mailto} if mailto else params)


def short_id(url: str | None) -> str:
    """"https://openalex.org/A123" -> "A123"; "" for a missing id (some authorships have an author with none)."""
    return (url or "").rsplit("/", 1)[-1]


def search_authors(fetch, name: str) -> list[dict]:
    """Top search hits for a display name; the caller decides which is the right person."""
    text, _ = fetch_text(fetch, _url("authors", {"search": name, "per-page": 5}))
    return json.loads(text).get("results", [])


def works(fetch, author_id: str, since: str | None = None) -> tuple[list[dict], str]:
    """Newest 200 works, optionally only those published on or after `since` (ISO date)."""
    text, version_hash = fetch_text(fetch, _url("works", {
        "filter": f"authorships.author.id:{author_id}" + (f",from_publication_date:{since}" if since else ""),
        "per-page": 200, "sort": "publication_date:desc", "select": "id,title,publication_date,doi,authorships",
    }))
    return json.loads(text).get("results", []), version_hash


def work(fetch, ref: str) -> dict:
    """One work, with its authorships and where it appeared, by the URL it was stored under: its DOI
    ("https://doi.org/10...", which OpenAlex takes as is) or its OpenAlex page ("https://openalex.org/W123")."""
    key = short_id(ref) if ref.startswith("https://openalex.org/") else ref
    text, _ = fetch_text(fetch, _url(f"works/{key}", {
        "select": "id,title,doi,publication_date,primary_location,authorships,abstract_inverted_index"}))
    return json.loads(text)


def abstract(work: dict) -> str:
    """A work's abstract as text, from the word-to-positions index OpenAlex keeps it as; "" when it has none."""
    index = work.get("abstract_inverted_index") or {}
    words = sorted((at, word) for word, places in index.items() for at in places)
    return " ".join(word for _, word in words)


def versions(fetch, title: str, budget: "Budget | None" = None) -> list[dict] | None:
    """The works OpenAlex finds for a title, its other versions among them (a preprint, a proceedings chapter), which
    OpenAlex often keeps as separate works: the caller decides which are the same paper. None, without asking, when
    ``budget`` allows no more pages (it then says why)."""
    if budget is not None and not budget.allows():
        return None
    words = " ".join(re.sub(r"[^\w]+", " ", title).lower().split())  # no punctuation, and no AND, OR or NOT to obey
    url = _url("works", {"search": words, "per-page": 10, "select": "id,title,publication_date,primary_location,authorships"})
    text, _ = fetch_text(fetch, url)
    if budget is not None:
        budget.note(_left(fetch, url))
    return json.loads(text).get("results", [])


MAX_CREDITS = 500  # the most one run spends of the 1,000 credits a day OpenAlex gives without a key
RESERVE = 100  # credits a run leaves for the rest of the day's OpenAlex reads
# What a search page cost on the Mac, 2026-09-24 (its response headers): a run counts this until it has measured its own.
CREDITS_A_PAGE = 10
PER_PAGE = 200


class Budget:
    """OpenAlex's daily credits for one run, as its responses report them (x-ratelimit-remaining, which the
    Fetcher keeps in each response's meta, so it needs a Fetcher that refreshes: a cached page's count is old).
    What a page costs is read from the drop between pages, and taken as CREDITS_A_PAGE until two pages have
    reported. ``allows`` says whether one more page stays under ``most`` for the run and leaves ``keep`` for the
    day; once it says no, or OpenAlex refuses a page, ``stopped`` says why and no page goes out after it."""

    def __init__(self, most=MAX_CREDITS, keep=RESERVE):
        self.most, self.keep = most, keep
        self.pages, self.stopped = 0, ""
        self.first = self.left = None  # credits left after the first and the latest page that reported them
        self.first_at = self.last_at = 0  # which pages those were

    def measured(self):
        """Credits per page as measured so far (the first page's cost is taken to be the same); None until two
        pages have reported."""
        span = self.last_at - self.first_at
        return max(round((self.first - self.left) / span), 0) if span else None

    def page_cost(self):
        cost = self.measured()
        return CREDITS_A_PAGE if cost is None else cost

    def spent(self):
        return self.page_cost() * self.pages

    def allows(self):
        cost = self.page_cost()
        if not self.stopped and self.left is not None and self.left - cost < self.keep:
            self.stopped = f"{self.left} credits left today, and {self.keep} are kept for the rest of the day"
        if not self.stopped and self.spent() + cost > self.most:
            self.stopped = f"this run's {self.most} credits are spent"
        return not self.stopped

    def note(self, left):
        """One more page, and the credits OpenAlex said were left after it (None when it didn't say). A count
        that went up is a new day: the measuring starts again from it."""
        self.pages += 1
        if left is not None:
            if self.first is None or left > self.left:
                self.first, self.first_at = left, self.pages
            self.left, self.last_at = left, self.pages

    def said(self):
        """One line on what the run cost."""
        cost = self.measured()
        cost = (f"about {cost} credit{'' if cost == 1 else 's'} a page, {self.spent()} in all; {self.left} left today"
                if cost is not None else f"{self.left} credits left today" if self.left is not None
                else "credits not reported")
        return (f"OpenAlex: {self.pages} search page{'' if self.pages == 1 else 's'}, {cost}."
                + (f" Stopped early: {self.stopped}. What was fetched is kept." if self.stopped else ""))


def _left(fetch, url):
    """The credits OpenAlex said were left after ``url``, from the Fetcher's meta; None when unknown."""
    meta = getattr(fetch, "meta", None)
    return (meta(url) if meta else {}).get("ratelimit_remaining")


def company_works(fetch, phrase: str, since: str, pages: int = 5, budget: Budget | None = None) -> list[dict]:
    """Works published on or after `since` whose title or abstract has `phrase`, with at least one author at a company.

    Up to `pages` pages of 200, newest first, while ``budget`` allows (``companies_works``). Each authorship's
    institutions carry their `type` ("company", "education", ...).
    """
    return [work for _, work in companies_works(fetch, (phrase,), since, pages, budget)]


def companies_works(fetch, phrases, since: str, pages: int = 5, budget: Budget | None = None) -> list[tuple[str, dict]]:
    """``company_works`` for each of ``phrases``, as (phrase, work), a page at a time across them: every phrase's
    newest page first, then every phrase's second, so a run the budget ends early has each phrase's newest works.
    A page ``budget`` doesn't allow, a rate limit that outlasts the Fetcher's retries or a server error ends the
    search with what came before, and the budget says why; so does a request OpenAlex never answers. With no
    ``budget`` given, a refused or unanswered page stops the run, as it always did."""
    told, budget = budget is not None, budget if budget is not None else Budget()
    out, cursors = [], {phrase: "*" for phrase in phrases}
    for _ in range(pages):
        for phrase, cursor in list(cursors.items()):
            if not budget.allows():
                return out
            url = _url("works", {
                "filter": f'title_and_abstract.search:"{phrase}",from_publication_date:{since},authorships.institutions.type:company',
                "sort": "publication_date:desc", "per-page": PER_PAGE, "cursor": cursor,
                "select": "id,title,publication_date,doi,primary_location,authorships"})
            try:
                text, _ = fetch_text(fetch, url)
            except httpx.HTTPStatusError as e:
                if not told or e.response.status_code != 429 and e.response.status_code < 500:
                    raise
                budget.stopped = f"OpenAlex answered {e.response.status_code} on \"{phrase}\""
                return out
            except httpx.TransportError as e:
                if not told:
                    raise
                budget.stopped = f"OpenAlex didn't answer on \"{phrase}\" ({type(e).__name__})"
                return out
            budget.note(_left(fetch, url))
            data = json.loads(text)
            results = data.get("results", [])
            out += [(phrase, work) for work in results]
            cursors[phrase] = data.get("meta", {}).get("next_cursor")
            if not cursors[phrase] or len(results) < PER_PAGE:  # a short page is the last: no credits on an empty one
                del cursors[phrase]
    return out


def coauthor_quote(person: dict, title: str, authorship: dict) -> str:
    """"Name (id) on 'title' | at Inst A; Inst B | last": where the coauthor was on this work, and their position.

    The title gives way when the quote would pass the timeline's 300 characters,
    so the institutions and position, which the detectors read, always survive.
    """
    head = f"{person['display_name']} ({short_id(person['id'])}) on '"
    places = "; ".join(i["display_name"] for i in authorship.get("institutions", []) if i.get("display_name"))
    tail = f"' | at {places} | {authorship.get('author_position') or 'unknown'}"
    return head + (title or "")[:max(20, 300 - len(head) - len(tail))] + tail


def events(author_id: str, subject_id: str | None = None, since: str | None = None, fetch=None) -> list[dict]:
    """coauthor_link and affiliation_seen per work (day), affiliation_change per institution (year)."""
    fetch = fetch or Fetcher()
    author_id = short_id(author_id)
    subject = subject_id or author_id
    out = []
    listing, version_hash = works(fetch, author_id, since)
    for work in listing:
        day = work.get("publication_date")
        if not day:
            continue
        url, observed = work.get("doi") or work["id"], day + "T00:00:00+00:00"
        for authorship in work.get("authorships", []):
            person = authorship.get("author") or {}
            if short_id(person.get("id")) == author_id:
                for inst in authorship.get("institutions", []):
                    out.append(person_event(subject, "affiliation_seen", day, "day", observed, url, version_hash,
                                            f"{inst['display_name']} on '{work['title']}'", "openalex_works"))
            elif person.get("id"):
                out.append(person_event(subject, "coauthor_link", day, "day", observed, url, version_hash,
                                        coauthor_quote(person, work["title"], authorship), "openalex_works"))
    text, version_hash = fetch_text(fetch, _url(f"authors/{author_id}", {}))
    profile = json.loads(text)
    for entry in profile.get("affiliations", []):
        years = sorted(entry.get("years", []))
        if not years:
            continue
        # A year-precision record is only surely public once that year has ended.
        first = str(years[0])
        out.append(person_event(subject, "affiliation_change", first, "year",
                                min(f"{first}-12-31T23:59:59+00:00", iso()), profile.get("id", API), version_hash,
                                f"{entry['institution']['display_name']}: years {years}", "openalex_author"))
    return out


def resolve_dois(fetch, dois) -> list[dict]:
    """OpenAlex works for DOIs (arXiv DOIs are 10.48550/arXiv.<id>), with what each one cites."""
    text, _ = fetch_text(fetch, _url("works", {
        "filter": "doi:" + "|".join(dois), "per-page": 50,
        "select": "id,title,publication_date,doi,referenced_works",
    }))
    return json.loads(text).get("results", [])


def gi_citation_events(author_id: str, gi_works: list[dict], subject_id: str | None = None, fetch=None) -> list[dict]:
    """gi_citation both ways: the author's works citing GI's, and GI's works citing the author's.

    Dated by the citing work's publication date, the day the citation was public.
    """
    fetch = fetch or Fetcher()
    author_id = short_id(author_id)
    subject = subject_id or author_id
    gi = {short_id(w["id"]): w for w in gi_works}
    out = []
    if not gi:
        return out
    text, version_hash = fetch_text(fetch, _url("works", {
        "filter": f"authorships.author.id:{author_id},cites:{'|'.join(gi)}", "per-page": 200,
        "select": "id,title,publication_date,doi,referenced_works"}))
    for work in json.loads(text).get("results", []):
        if day := work.get("publication_date"):
            cited = [gi[short_id(r)]["title"] for r in work.get("referenced_works", []) if short_id(r) in gi]
            out.append(person_event(subject, "gi_citation", day, "day", day + "T00:00:00+00:00",
                                    work.get("doi") or work["id"], version_hash,
                                    f"'{work['title']}' cites the GI-listed '{'; '.join(cited)}'", "openalex_citations"))
    referenced = sorted({short_id(r) for w in gi.values() for r in w.get("referenced_works", [])})
    if not referenced:
        return out
    text, version_hash = fetch_text(fetch, _url("works", {
        "filter": f"authorships.author.id:{author_id},openalex:{'|'.join(referenced[:100])}", "per-page": 100,
        "select": "id,title"}))
    for work in json.loads(text).get("results", []):
        for citing in gi.values():
            day = citing.get("publication_date")
            if day and work["id"] in citing.get("referenced_works", []):
                out.append(person_event(subject, "gi_citation", day, "day", day + "T00:00:00+00:00",
                                        citing.get("doi") or citing["id"], version_hash,
                                        f"GI-listed '{citing['title']}' cites '{work['title']}'", "openalex_citations"))
    return out
