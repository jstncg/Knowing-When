"""Journey builder: fill one person's timeline from every source we have.

build_journey(ctx, as_of, budget, run=...) runs each registered source adapter
for the person, admits a record only when two identifiers agree, appends the
events with timeline.add_event (content-keyed, so a rerun adds nothing), and
saves the PersonContext with the related subjects the detectors must see:
employer orgs (WARN names, SEC CIK) and "gi" (detectors.view adds fellow
SEC officers itself).

Adapters import their source lazily, so a missing package reports
`unavailable` instead of breaking the others. Each reports coverage:
  ok           ran and found events for this person
  empty        ran and found nothing
  error        failed for a reason worth reading
  blocked      the site or network refused us (403, proxy, unreachable)
  unavailable  a prerequisite is missing (module, key, anchor, employer)
  skipped      the per-person spend cap would be exceeded (paid sources are off by default)

Identity. One live run showed LinkedIn profile pages carrying other
people's posts, so a name alone never admits a record. An anchor (OpenAlex
author id, LinkedIn URL, homepage, SEC CIK) counts for the identifiers it was
admitted on (anchor_basis, e.g. "name+affiliation"); a record found by name
search needs a second identifier: a known affiliation, a known coauthor, the
person's homepage domain, or a verified OpenAlex author listing the same paper.
Feed pages are tier 2 at most and never first-person evidence (app.bridges).
"""

import hashlib
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import date, timedelta
from typing import Callable

import httpx
from pydantic import BaseModel, Field

from . import bridges, detectors, readiness, role_compiler
from .bridges import name_in, name_matches
from .detectors import COAUTHOR_ID, year_rounded
from .engine import digest
from .models import iso, parse_time
from .sources.warn import org_id
from .timeline import TimelineEvent, add_event, event_key

LOOKBACK = timedelta(days=4 * 365)  # tenure detectors need role starts years back
DEFAULT_CAP = 0  # paid calls per person (Exa search, Crustdata company credit); free sources first
MAX_VERSIONS = 24  # archived versions read per page, spread across the window
# Papers GI lists or its researchers wrote (first: linked from GI's site; the
# other two are DIAMOND and IRIS by researchers GI names). The first live run
# prints the titles OpenAlex resolves, so a wrong id shows at once.
GI_PAPERS = ("2607.05352", "2405.12399", "2209.00588")
CRUSTDATA_COMPANY_PATH = os.getenv("CRUSTDATA_COMPANY_PATH", "/screener/company")
STATUSES = ("ok", "empty", "error", "blocked", "unavailable", "skipped")
ARXIV_SUFFIX = re.compile(r"arxiv\.(\S+)$")  # an arXiv DOI or URL, lowercased, ends in its id


# ------------------------------------------------------------------- person

class PersonContext(BaseModel):
    subject_id: str = Field(min_length=1)  # must equal the answer key's / store's subject id
    name: str = Field(min_length=2)
    role: str = ""
    employer: str = ""                                    # as of the build, never a later employer
    employer_since: str = ""                              # joined it: "YYYY-MM", or the day a year-only start surely began
    affiliations: list[str] = Field(default_factory=list)  # institutions and labs known for the person
    anchors: dict[str, str] = Field(default_factory=dict)  # openalex, openreview, linkedin, homepage, sec_cik
    anchor_basis: dict[str, str] = Field(default_factory=dict)  # "name+affiliation", "name+filing", ...
    pages: list[str] = Field(default_factory=list)         # lab or team pages that should list the person
    candidate_ids: list[str] = Field(default_factory=list)
    coauthors: list[str] = Field(default_factory=list)     # filled from a verified OpenAlex author
    related: list[str] = Field(default_factory=list)       # filled by build_journey


def save_context(store, ctx):
    return store.put("person-context:" + ctx.subject_id, "person_context", ctx.model_dump())


def load_context(store, subject_id):
    record = store.get("person-context:" + subject_id)
    return PersonContext.model_validate(record) if record else None


def related(store, subject_id, ctx=None):
    """Subjects whose events belong in this person's view: "gi", employer ids, coauthors in a key."""
    ctx = ctx or load_context(store, subject_id)
    return list(dict.fromkeys(["gi", *(ctx.related if ctx else [])]))


def _joined(since):
    """The first day they surely worked there, from a "YYYY-MM-DD", "YYYY-MM" or "YYYY" start (a year alone: its last
    day). None when there is no start."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since or ""):
        return date.fromisoformat(since)
    if re.fullmatch(r"\d{4}-\d{2}", since or ""):
        return date(int(since[:4]), int(since[5:]), 1)
    return date(int(since), 12, 31) if re.fullmatch(r"\d{4}", since or "") else None


def view(store, subject_id, as_of):
    """The person's timeline plus their related subjects' as of a date. With a known start at their employer, an
    employer's event (any org's but GI's) dated before it is left out: a deal or a layoff before they joined is not
    theirs."""
    ctx = load_context(store, subject_id)
    events = detectors.view(store, subject_id, as_of, related(store, subject_id, ctx))
    if not (start := _joined(ctx.employer_since) if ctx else None):
        return events
    return [e for e in events if not (e["subject_type"] == "org" and e["subject_id"] != "gi"
                                      and (detectors.day_of(e) or detectors.observed_day(e)) < start)]


def detect(store, subject_id, as_of, events=None, role=None):
    """Activations over the person's view, one per underlying event, for the detectors ``role`` watches
    (role_compiler.watched): the replay's `run=` and assess's input."""
    events = view(store, subject_id, as_of) if events is None else events
    activations = readiness.for_role(detectors.detect(events, subject_id, as_of), role_compiler.watched(role))
    return readiness.dedupe(activations, events)


BORROWED = "None of this is from their own timeline: it rests on their employer's and colleagues' records."


LIVE_POLICY = Path(__file__).resolve().parents[1] / "config" / "live-policy.json"


@lru_cache(maxsize=1)
def notes_only():
    """The early signs live calls keep to a note about their work, never a pitch (config/live-policy.json): the
    signs the first noise test did not keep. Read once per process, so an edit needs a restart of the app. The
    replay and the scorecard never read it."""
    return frozenset(json.loads(LIVE_POLICY.read_text())["notes_only"]) if LIVE_POLICY.exists() else frozenset()


def assess(store, subject_id, as_of, role=None):
    """Readiness for a live decision on one role: detect, then readiness.score, as every replay scores it.

    None when nothing but GI's own records is in view; the decision then falls
    back to the reviewed model judgment alone. A person with no events of their
    own is still scored from their employer's and colleagues' records, and the
    explanation says so.
    """
    events = view(store, subject_id, as_of)
    if all(e["subject_id"] == "gi" for e in events):
        return None
    scored = readiness.score(detect(store, subject_id, as_of, events, role), as_of, notes_only=notes_only())
    if not any(e["subject_id"] == subject_id for e in events):
        scored = scored.model_copy(update={"explanation": f"{scored.explanation} {BORROWED}"})
    return scored


def employer_on(ctx, as_of):
    """Where they worked on a day, as far as their context knows: its employer from the day they surely started there
    (``_joined``), or with no known start; else "", so a replayed morning never names a later employer."""
    start = _joined(ctx.employer_since) if ctx else None
    return ctx.employer if ctx and (not start or start <= parse_time(as_of).date()) else ""


# A LinkedIn "company" that is no employer with news of its own.
NOT_AN_EMPLOYER = re.compile(r"^\s*(?:stealth|self[- ]employed|freelance|independent|confidential|various|none)\b", re.I)


def save_employers(store, rows, read_on):
    """Each person's employer and when they joined it, from a LinkedIn profile read on ``read_on`` (the rows of
    linkedin_profiles.roles), into their context, so their employer's own events reach their view (``view``). A start
    that gives only the year counts from the latest day it can mean: the year's end, or the read day when that is
    sooner. A read that returned no job list changes nothing; a profile with no current job, or only a stealth or
    freelance one, clears it. A new employer drops the old one's org ids and SEC CIKs. Returns (the people with an
    employer now, the people the store has no context for)."""
    from .sources.linkedin_profiles import main_job

    employed, missing = [], []
    for subject_id, row in rows.items():
        if row.get("status") not in ("current", "no_current_role"):
            continue
        if not (ctx := load_context(store, subject_id)):
            missing.append(subject_id)
            continue
        job = main_job(row) or {}
        employer = "" if NOT_AN_EMPLOYER.match(job.get("company") or "") else (job.get("company") or "").strip()
        since = (job.get("joined") or job.get("started") or "") if employer else ""
        if re.fullmatch(r"\d{4}", since):
            since = min(f"{since}-12-31", read_on)
        moved = org_core(employer) != org_core(ctx.employer)
        kept = [r for r in ctx.related if not (moved and (r.startswith("org:") or r.isdigit()))]
        save_context(store, ctx.model_copy(update={
            "employer": employer, "employer_since": since,
            "related": sorted({*kept, *([org_id(employer)] if org_core(employer) else [])})}))
        if org_core(employer):
            employed.append(subject_id)
    return employed, missing


# ----------------------------------------------------------------- identity

def _words(text):
    return set(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def org_core(name):
    """Employer core words: legal suffixes, SEC state tags (/DE/) and punctuation removed."""
    name = re.sub(r"/[A-Za-z]{2}/?", " ", name or "")
    core = org_id(name)[4:]
    return set(core.split("-")) - {""}


def same_org(a, b):
    return bool(org_core(a)) and org_core(a) == org_core(b)


def _aliases(affiliation):
    """"MIT (CSAIL)" -> {"mit"}, {"csail"}: the name and each parenthetical, as core-word sets."""
    parts = [re.sub(r"\([^)]*\)", " ", affiliation), *re.findall(r"\(([^)]*)\)", affiliation)]
    return [c for p in parts if (c := org_core(p))]


def _affiliated(ctx, text):
    words = _words(text)
    return any(alias <= words for a in (ctx.employer, *ctx.affiliations) if a for alias in _aliases(a))


def _host(url):
    return re.sub(r"^www\.", "", (url or "").split("/")[2].lower()) if "//" in (url or "") else ""


def agreeing(ctx, *, anchor=None, name=None, affiliations=(), text="", url="", coauthors=()):
    """Identifiers on a record that agree with the person. A record joins at two."""
    ids = set()
    if anchor and anchor[1] and ctx.anchors.get(anchor[0]) == anchor[1]:
        ids |= set(ctx.anchor_basis.get(anchor[0], "").split("+")) - {""}
    if (name and name_matches(name, ctx.name)) or (text and name_in(text, ctx.name)):
        ids.add("name")
    if any(_affiliated(ctx, a) for a in affiliations if a) or (text and _affiliated(ctx, text)):
        ids.add("affiliation")
    if url and (home := ctx.anchors.get("homepage")) and _host(url) == _host(home):
        ids.add("homepage")
    if url and ctx.anchors.get("linkedin"):
        from .crustdata import linkedin_url

        if linkedin_url(url) == ctx.anchors["linkedin"]:
            ids.add("linkedin")
    if any(name_matches(a, b) for a in coauthors for b in ctx.coauthors):
        ids.add("coauthor")
    return sorted(ids)


def verified(ctx, key):
    return len(agreeing(ctx, anchor=(key, ctx.anchors.get(key)))) >= 2


# -------------------------------------------------------------- org resolver

class OrgResolver:
    """Employer name -> SEC CIK, from a hand mapping first, then SEC's company list.

    A CIK is returned only for exactly one registrant whose core name equals the
    employer's; "Apple" never resolves to "Apple Hospitality REIT".
    """

    def __init__(self, mapping=None, sec_titles: Callable[[], dict] | None = None):
        self.mapping = {frozenset(org_core(k)): v for k, v in (mapping or {}).items()}
        self._sec_titles, self._titles = sec_titles, None

    def cik(self, name):
        if not org_core(name):
            return None, "no employer"
        if hit := self.mapping.get(frozenset(org_core(name))):
            return str(hit["cik"] if isinstance(hit, dict) else hit).zfill(10), "mapping"
        if self._titles is None:
            self._titles = self._sec_titles() if self._sec_titles else {}
        found = {cik for cik, title in self._titles.items() if same_org(title, name)}
        if len(found) == 1:
            return str(found.pop()).zfill(10), "sec_name"
        return None, "ambiguous" if found else "not an SEC registrant"


def sec_company_titles(client):
    data = client.get_json("https://www.sec.gov/files/company_tickers.json")
    return {str(r["cik_str"]): r["title"] for r in data.values()}


# ------------------------------------------------------------------ running

class SourceStatus(Exception):
    status = "error"


class Unavailable(SourceStatus):
    status = "unavailable"


class Skipped(SourceStatus):
    status = "skipped"


BLOCKED = re.compile(r"\b40[37]\b|CONNECT tunnel failed|Forbidden|ProxyError|ConnectError")


def classify(err):
    code = getattr(getattr(err, "response", None), "status_code", None)
    if code in (401, 403, 407, 451) or isinstance(err, (httpx.ProxyError, httpx.ConnectError)):
        return "blocked"
    return "blocked" if BLOCKED.search(f"{type(err).__name__}: {err}") else "error"


class Budget:
    """Per-person cap on paid calls (Exa searches, Crustdata credits)."""

    def __init__(self, cap=DEFAULT_CAP):
        self.cap, self.spent = cap, 0

    def take(self, n=1):
        if self.spent + n > self.cap:
            raise Skipped(f"per-person cap of {self.cap} paid calls reached")
        self.spent += n


class Run:
    """What one build shares across people: clients, caches, the resolver, credit caps.

    Every client is injectable; tests pass fakes and nothing touches the network.
    source_store is the pilot store holding the GI clock and Crustdata history
    when timelines are written elsewhere (the private past-joiner database).
    """

    def __init__(self, store, *, replay, settings=None, source_store=None, fetch=None, sec_client=None,
                 wayback=None, exa=None, crustdata=None, resolver=None, crustdata_credits=0):
        self.store, self.source_store = store, source_store or store
        self.replay = replay  # past joiners' cases: a paper placing them at the new lab is the outcome, so none after
        self.settings = settings or {}
        self._fetch, self._sec = fetch, sec_client
        self.wayback, self.exa, self._crustdata = wayback, exa, crustdata
        self.resolver = resolver
        self.crustdata_credits = crustdata_credits
        self.memo = {}

    @property
    def fetch(self):
        if self._fetch is None:
            from .sources.http import Fetcher
            self._fetch = Fetcher()
        return self._fetch

    @property
    def sec_client(self):
        if self._sec is None:
            from .sources.sec import EdgarClient
            self._sec = EdgarClient()
        return self._sec

    @property
    def crustdata(self):
        if self._crustdata is None:
            from .crustdata import Client
            self._crustdata = Client(self.settings.get("crustdata_key"))
        return self._crustdata

    def org_resolver(self):
        if self.resolver is None:
            self.resolver = OrgResolver(sec_titles=lambda: sec_company_titles(self.sec_client))
        return self.resolver

    def shared(self, key, load):
        """One load per build for data every person reads (WARN files, the GI clock, GI papers)."""
        if key not in self.memo:
            self.memo[key] = load()
        return self.memo[key]


@dataclass
class Outcome:
    events: list[dict] = field(default_factory=list)
    rejected: int = 0
    related: set[str] = field(default_factory=set)
    detail: dict | str | None = None
    status: str | None = None  # "error" keeps a partial result's source asked again next run; else ok or empty


@dataclass(frozen=True)
class Adapter:
    id: str
    fetch: Callable


ADAPTERS: dict[str, Adapter] = {}


def adapter(adapter_id):
    def register(fn):
        ADAPTERS[adapter_id] = Adapter(adapter_id, fn)
        return fn
    return register


def _since(as_of):
    return (parse_time(as_of) - LOOKBACK).date().isoformat()


def _resubject(events, subject):
    return [{**e, "subject_id": subject} for e in events]


def _anchor(ctx, key, label):
    value = ctx.anchors.get(key)
    if not value:
        raise Unavailable(f"no {label}")
    if not verified(ctx, key):
        raise Unavailable(f"{label} not verified on two identifiers (basis: {ctx.anchor_basis.get(key) or 'none'})")
    return value


# ----------------------------------------------------------------- adapters
# Registration order is run order: OpenAlex feeds arXiv's corroboration.

@adapter("gi_clock")
async def _gi_clock(ctx, as_of, run, budget):
    from . import company_context

    def load():
        company_context.backfill_timeline(run.source_store)
        return [e for e in run.source_store.all("timeline_event") if e["subject_id"] == "gi"]

    events = run.shared("gi_clock", load)  # re-adding is a no-op when both stores are one
    return Outcome(events, related={"gi"} if events else set())


@adapter("crustdata_person")
async def _crustdata_person(ctx, as_of, run, budget):
    from . import crustdata

    if not ctx.candidate_ids:
        raise Unavailable("not a live candidate, so no Crustdata watch")
    wanted = set(ctx.candidate_ids)
    events = [e for o in run.source_store.all("crustdata_observation") if wanted & set(o.get("candidate_ids", []))
              for e in crustdata.to_timeline(run.source_store, o) if e["subject_id"] == ctx.subject_id]
    return Outcome(events)


def _openalex_works(ctx, as_of, run):
    """A verified OpenAlex author's events and works since the lookback, fetched once per person and day in a build
    (and disk-cached), for the openalex adapter and for the papers the arXiv adapter takes from them."""
    from .sources import openalex

    key = ("openalex_works", ctx.subject_id, as_of)
    if key not in run.memo:
        author, since = _anchor(ctx, "openalex", "OpenAlex author id"), _since(as_of)
        run.memo[key] = {"events": openalex.events(author, ctx.subject_id, since, run.fetch),
                         "works": openalex.works(run.fetch, openalex.short_id(author), since)[0]}
    return run.memo[key]


@adapter("openalex")
async def _openalex(ctx, as_of, run, budget):
    from .sources import openalex

    author = _anchor(ctx, "openalex", "OpenAlex author id")
    found = _openalex_works(ctx, as_of, run)
    coauthors = {a["author"]["display_name"] for w in found["works"] for a in w.get("authorships", [])
                 if (a.get("author") or {}).get("display_name")
                 and openalex.short_id(a["author"].get("id")) != openalex.short_id(author)}
    run.memo[(ctx.subject_id, "coauthors")] = sorted(coauthors)
    return Outcome(found["events"])


def _title_key(title):
    return " ".join(sorted(_words(title)))


def _arxiv_paper(url):
    """The arXiv id an abstract URL or an arXiv DOI names, without its version; None for any other URL."""
    url = (url or "").lower()
    if "/abs/" in url:
        return re.sub(r"v\d+$", "", url.split("/abs/")[-1])
    return m.group(1) if (m := ARXIV_SUFFIX.search(url)) else None


ARXIV_QUOTE = re.compile(r"(.*) \((\S+?)v\d+\): ")  # arxiv.events: "<title> (<id>v<n>): ..."


def _papers_on_file(store, subject_id):
    """The person's stored papers: arXiv ids from arXiv (-> event id) and from OpenAlex works (-> event id), the
    OpenAlex papers' URLs, and arXiv papers' title keys, so no run counts a paper, or its journal version, twice."""
    on_file = {"arxiv_api": {}, "openalex_paper_v1": {}, "urls": set(), "titles": set()}
    for e in store.all("timeline_event"):
        if e["subject_id"] != subject_id or e["event_type"] != "paper_v1" or e["extractor"] not in on_file:
            continue
        if paper := _arxiv_paper(e["source_url"]):
            on_file[e["extractor"]][paper] = e["id"]
        if e["extractor"] == "openalex_paper_v1":
            on_file["urls"].add(e["source_url"])
        elif m := ARXIV_QUOTE.match(e["quote"]):
            on_file["titles"].add(_title_key(m.group(1)))
    return on_file


def _new_papers(listed, arxiv_ids, titles, on_file, replay):
    """OpenAlex's papers for the works arXiv did not give, less any already on file."""
    fill, _ = openalex_papers(listed, arxiv_ids | set(on_file["arxiv_api"]), titles | on_file["titles"], stop_at_move=replay)
    return [e for e in fill if e["source_url"] not in on_file["urls"]]


@adapter("arxiv")
async def _arxiv(ctx, as_of, run, budget):
    """arXiv is searched by name, so each paper needs a second identifier to join.

    A verified OpenAlex author's works give the papers arXiv does not list, and all of them while arXiv's API
    refuses the search (it answers 406 under load); the refusal still reads as an error, so arXiv is asked again
    next run. A paper arXiv gives later replaces the one OpenAlex gave, so it is never counted twice."""
    from .sources import arxiv

    since = _since(as_of)
    found = {"events": [], "works": []}
    if verified(ctx, "openalex"):
        try:
            found = _openalex_works(ctx, as_of, run)
        except Exception:  # the openalex adapter reports its own failure; arXiv goes on without it
            pass
    listed = found["events"]
    titles_listed = {_title_key(w.get("title")) for w in found["works"]}
    on_arxiv = {paper for w in found["works"] if (paper := _arxiv_paper(w.get("doi")))}
    on_file = _papers_on_file(run.store, ctx.subject_id)
    try:
        papers, _ = arxiv.author_papers(run.fetch, ctx.name)
    except httpx.HTTPStatusError as e:
        if not listed or classify(e) == "blocked":
            raise
        fill = _new_papers(listed, set(), set(), on_file, run.replay)
        return Outcome(fill, status="error", detail=f"arXiv refused the search ({e.response.status_code}), so it is "
                                                    f"asked again next run; papers from OpenAlex meanwhile: {len(fill)}")
    keep, rejected = set(), 0
    for paper in papers:
        if paper["updated"][:10] < since:
            continue
        me = next((a for a in paper["authors"] if name_matches(a["name"], ctx.name)), None)
        ids = set(agreeing(ctx, name=me["name"], affiliations=[me["affiliation"]],
                           coauthors=[a["name"] for a in paper["authors"] if a is not me])) if me else set()
        if paper["arxiv_id"].lower() in on_arxiv or _title_key(paper["title"]) in titles_listed:
            ids.add("openalex")
        if len(ids) >= 2:
            keep.add(paper["arxiv_id"])
        else:
            rejected += 1
    keep = {paper.lower() for paper in keep}
    events = [e for e in arxiv.events(ctx.name, ctx.subject_id, since, fetch=run.fetch) if _arxiv_paper(e["source_url"]) in keep]
    for e in events:
        if e["event_type"] == "paper_v1" and (old := on_file["openalex_paper_v1"].get(_arxiv_paper(e["source_url"]))):
            e["supersedes"] = old
    titles = {_title_key(p["title"]) for p in papers if p["arxiv_id"].lower() in keep}
    return Outcome(events + _new_papers(listed, keep, titles, on_file, run.replay), rejected)


@adapter("openreview")
async def _openreview(ctx, as_of, run, budget):
    from .sources import openreview

    return Outcome(openreview.events(_anchor(ctx, "openreview", "OpenReview profile id"), ctx.subject_id, run.fetch))


@adapter("nsf")
async def _nsf(ctx, as_of, run, budget):
    """NSF matches the exact name; the award's institution must be one the person is known at."""
    from .sources import nsf

    since, events, rejected = _since(as_of), [], 0
    for award in nsf.awards(pi=ctx.name, fetch=run.fetch):
        if (award.end_expected or award.start or "") < since:
            continue
        if len(agreeing(ctx, name=ctx.name, affiliations=[award.institution])) < 2:
            rejected += 1
            continue
        events += _resubject([e for e in award.events() if e["subject_type"] == "person"], ctx.subject_id)
    return Outcome(events, rejected)


@adapter("openalex_citations")
async def _openalex_citations(ctx, as_of, run, budget):
    from .sources import openalex

    author = _anchor(ctx, "openalex", "OpenAlex author id")
    gi_works = run.shared("gi_works", lambda: openalex.resolve_dois(run.fetch, [f"10.48550/arxiv.{p}" for p in GI_PAPERS]))
    if not gi_works:
        raise SourceStatus("GI papers did not resolve in OpenAlex")
    return Outcome(openalex.gi_citation_events(author, gi_works, ctx.subject_id, run.fetch),
                   detail="GI works: " + "; ".join(w.get("title", "") for w in gi_works))


@adapter("sec")
async def _sec(ctx, as_of, run, budget):
    """Employer 8-K/10-K events under the CIK; the person's own officer events join on name + CIK."""
    from .sources import sec

    cik, basis = ctx.anchors.get("sec_cik"), "anchor"
    if not cik:
        cik, basis = run.org_resolver().cik(ctx.employer)
        if not cik:
            raise Unavailable(f"employer {basis}")
    cik = sec.pad_cik(cik)
    since = _since(as_of)
    records = run.shared(("sec", cik, since), lambda: [e.model_dump() for e in sec.org_events(run.sec_client, cik, since)])
    # Fellow officers reach the view through detectors.colleagues; the CIK carries the company's own events.
    events = [{**e, "subject_id": ctx.subject_id}
              if e["subject_type"] == "person" and name_matches(e.get("person_name", ""), ctx.name) else e
              for e in records]
    return Outcome(events, related={cik}, detail={"cik": cik, "resolved_by": basis})


@adapter("warn")
async def _warn(ctx, as_of, run, budget):
    """warn.load once per build (the consolidated feed), then this employer's notices."""
    from .sources import warn

    if not ctx.employer:
        raise Unavailable("no employer")
    notices = run.shared("warn", lambda: warn.load(fetch=run.fetch))
    since = _since(as_of)
    mine = [n for n in notices if same_org(ctx.employer, n.company) and n.notice_date >= since]
    return Outcome(warn.events(mine), related={warn.org_id(n.company) for n in mine})


@adapter("news")
async def _news(ctx, as_of, run, budget):
    """Their employer being bought or laying people off, from news headlines (sources.news.employer), as the
    employer's own events dated when each was published.

    A deal called off supersedes each deal headline of the two years before it (news.DEAL_DAYS), on file or new, from
    the day it was published: before then the deal stands, whenever it was read. Live only: the feed answers with
    today's headlines, not what a past day saw, so a build for a past day keeps only what was published by then,
    call-offs included."""
    from .sources import news

    if run.replay:
        raise Unavailable("live watchlist only")
    if not org_core(ctx.employer):
        raise Unavailable("no employer")
    org = org_id(ctx.employer)
    rows = run.shared(("news", org), lambda: news.employer(run.fetch, ctx.employer))
    since = _since(as_of)
    events = [{"subject_type": "org", "subject_id": org, "event_type": r["event_type"], "event_date": r["day"],
               "date_precision": "day", "observed_at": r["at"], "source_url": r["source_url"], "quote": r["quote"],
               "source_version_hash": hashlib.sha256(r["quote"].encode()).hexdigest(), "tier": 2,
               "extractor": "google_news_v1"} for r in rows if since <= r["day"] and r["at"] <= as_of]
    deals = {event_key(TimelineEvent.model_validate(e).model_dump()): e for e in events if e["event_type"] == "acquisition"}
    deals.update({e["id"]: e for e in run.store.all("timeline_event") if e["subject_id"] == org
                  and e["event_type"] == "acquisition" and e["extractor"] == "google_news_v1"})
    kept = [e for e in events if e["event_type"] != "acquisition_called_off"]
    for off in (e for e in events if e["event_type"] == "acquisition_called_off"):
        kept += [{**off, "supersedes": key} for key, deal in deals.items()
                 if timedelta(0) <= parse_time(off["observed_at"]) - parse_time(deal["observed_at"])
                 <= timedelta(days=news.DEAL_DAYS)]
    return Outcome(kept, related={org})


async def employer_sources(store, subject_ids, as_of, fetch=None):
    """The free employer sources (news, WARN) for each watched person with an employer: what the daily run and the
    profile read call. Read afresh each time, since a profile read can change the employer after the day's run.
    {subject id: build report}."""
    run = Run(store, replay=False, fetch=fetch)
    return {subject_id: await build_journey(ctx, as_of, run=run, only={"news", "warn"}, refresh=True)
            for subject_id in subject_ids
            if (ctx := load_context(store, subject_id)) and org_core(ctx.employer)}


@adapter("crustdata_company")
async def _crustdata_company(ctx, as_of, run, budget):
    from . import crustdata

    if not run.settings.get("crustdata_key"):
        raise Unavailable("no Crustdata key")
    if not ctx.employer:
        raise Unavailable("no employer")

    key = ("crustdata_company", frozenset(org_core(ctx.employer)))
    if key not in run.memo:
        if run.crustdata_credits < 1:
            raise Skipped("run's Crustdata credit cap reached")
        budget.take()
        run.crustdata_credits -= 1
        try:
            data = await run.crustdata.request("GET", CRUSTDATA_COMPANY_PATH, params={"company_name": ctx.employer})
        except crustdata.CrustdataError as e:
            run.memo[key] = e
            raise
        rows = data if isinstance(data, list) else data.get("results", [data]) if isinstance(data, dict) else []
        run.memo[key] = next((r for r in rows if isinstance(r, dict) and same_org(r.get("company_name", ""), ctx.employer)), None)
    record = run.memo[key]
    if isinstance(record, Exception):
        raise record
    if not record:
        return Outcome(detail="no company record matched the employer name")
    version = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    url = f"{crustdata.BASE}{CRUSTDATA_COMPANY_PATH}?company_name={ctx.employer.replace(' ', '+')}"
    events = bridges.company_events(ctx.subject_id, org_id(ctx.employer), ctx.employer, record,
                                    source_url=url, version=version)
    return Outcome(events, related={org_id(ctx.employer)})


@adapter("wayback")
async def _wayback(ctx, as_of, run, budget):
    """The homepage (every change is the person's) and roster pages (only lines naming them)."""
    from .sources.wayback import WaybackClient

    home = ctx.anchors.get("homepage")
    pages = ([(home, verified(ctx, "homepage"))] if home else []) + [(p, False) for p in ctx.pages]
    if not pages:
        raise Unavailable("no homepage or roster page")
    events = []
    async with (run.wayback or WaybackClient)() as client:
        for url, own in pages:
            captures = await client.list_captures(url, since=_since(as_of), until=as_of)
            step = max(1, -(-len(captures) // MAX_VERSIONS))
            chosen = captures[::step] + ([captures[-1]] if captures and (len(captures) - 1) % step else [])
            versions, seen = [], set()
            for capture in chosen:
                version = await client.fetch_version(capture)
                if version.content_hash not in seen:
                    seen.add(version.content_hash)
                    versions.append(version)
            events += bridges.page_history_events(ctx.subject_id, ctx.name, versions, own_page=own)
    return Outcome(events)


async def _exa_search(query, settings):
    from . import providers

    return await providers._search(query, {"id": "journey"}, settings, providers.Budget({"max_calls_per_run": 1}))


@adapter("exa")
async def _exa(ctx, as_of, run, budget):
    """One search for talks and mentions; a hit joins on the full name plus a second identifier."""
    if not run.settings.get("exa_key"):
        raise Unavailable("no Exa key")
    budget.take()
    query = " ".join(filter(None, (f'"{ctx.name}"', ctx.employer or (ctx.affiliations or [""])[0],
                                   "talk OR keynote OR seminar OR interview")))
    docs = await (run.exa or _exa_search)(query, run.settings)
    kept = [d for d in docs if len(agreeing(ctx, text=f"{d.get('title', '')} {d.get('text', '')}", url=d["url"],
                                            coauthors=[c for c in ctx.coauthors if name_in(d.get("text", ""), c)])) >= 2]
    own = {_host(ctx.anchors["homepage"])} if ctx.anchors.get("homepage") and verified(ctx, "homepage") else set()
    return Outcome(bridges.web_mention_events(ctx.subject_id, ctx.name, kept, own_domains=own), len(docs) - len(kept))


# -------------------------------------------------------------------- build

async def build_journey(ctx, as_of, budget=None, *, run, only=None, refresh=False):
    """Fill one person's timeline as far as as_of reaches; idempotent per (person, source, as_of day).

    A source that already came back ok or empty for this person and day is not
    asked again (no second spend) unless refresh=True; errors are retried.
    """
    budget = budget or Budget()
    as_of = iso(parse_time(as_of))
    # The employer's own id always joins, so its later WARN or company events reach the person.
    related_subjects = set(ctx.related) | ({org_id(ctx.employer)} if org_core(ctx.employer) else set())
    report = {"subject_id": ctx.subject_id, "name": ctx.name, "as_of": as_of, "coverage": {}}
    for adapter_id, source in ADAPTERS.items():
        if only and adapter_id not in only:
            continue
        key = "journey-run:" + digest([ctx.subject_id, adapter_id, as_of[:10]])
        prior = run.store.get(key)
        if prior and prior["status"] in ("ok", "empty") and not refresh:
            report["coverage"][adapter_id] = {k: prior.get(k) for k in ("status", "events", "rejected", "detail")} | {"cached": True}
            related_subjects |= set(prior.get("related", []))
            continue
        try:
            outcome = await source.fetch(ctx, as_of, run, budget)
        except ModuleNotFoundError as e:
            entry = {"status": "unavailable", "detail": f"{e.name} is not installed"}
        except SourceStatus as e:
            entry = {"status": e.status, "detail": str(e)[:300]}
        except Exception as e:  # one failing source must not hide the others
            entry = {"status": classify(e), "detail": f"{type(e).__name__}: {e}"[:300]}
        else:
            stored, invalid = 0, 0
            for event in outcome.events:
                try:
                    add_event(run.store, event)
                    stored += 1
                except ValueError:
                    invalid += 1
            entry = {"status": outcome.status or ("ok" if stored else "empty"), "events": stored, "rejected": outcome.rejected,
                     "related": sorted(outcome.related)}
            if invalid:
                entry["invalid"] = invalid
            if outcome.detail:
                entry["detail"] = outcome.detail
            related_subjects |= outcome.related
        run.store.put(key, "journey_run", {"subject_id": ctx.subject_id, "adapter": adapter_id, "as_of": as_of,
                                           "at": iso(), **entry})
        report["coverage"][adapter_id] = entry
    related_subjects.discard(ctx.subject_id)
    coauthors = run.memo.get((ctx.subject_id, "coauthors"), ctx.coauthors)
    ctx = ctx.model_copy(update={"related": sorted(related_subjects), "coauthors": coauthors})
    save_context(run.store, ctx)
    report["related"], report["spent"] = ctx.related, budget.spent
    return report


# ----------------------------------------------------------- who to build

def reference_contexts(path, store):
    """(context, as_of) per entry of a private JSON list of PersonContext fields.

    An entry may name a `candidate_id` in the store instead of a subject_id, so a
    live candidate's timeline and moment plans share the person id. `as_of`
    defaults to now.
    """
    out = []
    for row in json.loads(Path(path).read_text()):
        row = dict(row)
        as_of = row.pop("as_of", None) or iso()
        if cid := row.pop("candidate_id", None):
            candidate = store.get(cid)
            if not candidate:
                raise ValueError(f"{cid} is not in the store")
            row.setdefault("subject_id", candidate["person_id"])
            row.setdefault("name", candidate["name"])
            row.setdefault("role", candidate["role_id"])
            row["candidate_ids"] = [cid]
        out.append((PersonContext.model_validate(row), as_of))
    return out


GENERIC_ORG_WORDS = {"university", "institute", "college", "school", "lab", "labs", "laboratory", "laboratories",
                     "research", "center", "centre", "department", "dept", "of", "the", "and", "for", "at", "in", "de"}


def _employer_names(text):
    """The employers a roster string names, parentheticals dropped: "Tidewell (co-founder); formerly
    Northgate Institute / Lakemont University" -> Tidewell, Northgate Institute, Lakemont University."""
    parts = re.split(r"[;/]", re.sub(r"\([^)]*\)", " ", text or ""))
    return [re.sub(r"^(formerly|earlier|previously|also)\s+", "", p.strip(), flags=re.I) for p in parts if p.strip()]


def _acronyms(words):
    """Acronyms of each run of two or more consecutive words: university of california berkeley -> uc, ucb, cb."""
    letters = [w[0] for w in words if w not in ("of", "the", "and", "for", "at", "in", "de")]
    return {"".join(letters[i:j]) for i in range(len(letters)) for j in range(i + 2, len(letters) + 1)}


def employer_confirmed(employer, institutions):
    """The author's own institutions name the employer: each distinctive word of one of its names
    appears in them, or a short one is the acronym of consecutive words (MIT, UC Berkeley)."""
    wordlists = [re.sub(r"[^a-z0-9 ]+", " ", i.lower()).split() for i in institutions]
    seen = {w for words in wordlists for w in words}
    short = set().union(*(_acronyms(words) for words in wordlists)) if wordlists else set()
    for name in _employer_names(employer):
        distinct = org_core(name) - GENERIC_ORG_WORDS
        if distinct and all(w in seen or (2 <= len(w) <= 4 and w in short) for w in distinct):
            return True
    return False


def author_institutions(row):
    """Institutions the answer-key row's OpenAlex author states for themself: profile and own works."""
    names = list((row.get("openalex") or {}).get("affiliations") or [])
    for e in row.get("events", []):
        if e["extractor"] == "openalex_works" and e["event_type"] == "affiliation_seen":
            names.append(e["quote"].rsplit(" on '", 1)[0])
        elif e["extractor"].startswith("openalex") and e["event_type"] == "affiliation_change":
            names.append(e["quote"].split(": years", 1)[0])
    return names


def mts_contexts(directory):
    """(context, as_of, row) per MTS answer-key row, keyed by OpenAlex author id (or arXiv name).

    The OpenAlex author counts as the person (name plus affiliation) only when
    their own institutions name the person's employer before the move, from the
    roster. The key script's pick also matched the destination lab and generic
    words like "university", so its label is not trusted either way.
    """
    out = []
    for name in ("mts-joiners", "mts-stayers"):
        path = Path(directory) / f"{name}.json"
        if not path.exists():
            continue
        for row in json.loads(path.read_text())["people"]:
            oa = row.get("openalex")
            known = [a for a in (row.get("prior_affiliation"), row.get("affiliation")) if a]
            ctx = PersonContext(
                subject_id=oa["id"] if oa else f"arxiv:{row['name']}", name=row["name"], role="mts",
                employer=known[0] if known else "", affiliations=known,
                anchors={"openalex": oa["id"]} if oa else {},
                anchor_basis={"openalex": "name+affiliation" if employer_confirmed(
                    row.get("prior_affiliation") or row.get("affiliation"), author_institutions(row)) else "name"} if oa else {},
                pages=row.get("pages", []), coauthors=[])
            if row.get("homepage"):
                ctx.anchors["homepage"], ctx.anchor_basis["homepage"] = row["homepage"], "name+answer_key"
            out.append((ctx, row.get("join_date") or row["window_end"], row))
    return out


# ------------------------------------------------ answer keys, offline

ARXIV_ID = re.compile(r"/abs/([^/]+?)(?:v\d+)?$")
QUOTED_TITLE = re.compile(r" on '(.+?)'(?: \| at [^|]* \| \w+)?$")


def _arxiv_id(event):
    m = ARXIV_ID.search(event.get("source_url", ""))
    return m.group(1).lower() if m else None


def mts_row_events(ctx, row, trust_top_hit=False):
    """The events mts_answer_key.py already pulled, kept only where two identifiers agree.

    OpenAlex events stand when the author id was matched on name and
    affiliation. arXiv was searched by name, so a paper stays only when the
    verified OpenAlex author lists it (same arXiv DOI or title) or its stated
    affiliation is one the person is known at. Returns (kept, rejected events
    by reason).
    """
    events = row.get("events", [])
    openalex_ok = verified(ctx, "openalex") or (trust_top_hit and "openalex" in ctx.anchors)
    openalex_events = [e for e in events if e["extractor"].startswith("openalex")]
    works = openalex_events if openalex_ok else []
    listed = {m.group(1).lower() for e in works if (m := ARXIV_SUFFIX.search(e.get("source_url", "").lower()))}
    titles = {_title_key(m.group(1)) for e in works if (m := QUOTED_TITLE.search(e["quote"]))}
    papers = {}
    for e in events:
        if e["extractor"] == "arxiv_api" and (paper := _arxiv_id(e)):
            papers.setdefault(paper, []).append(e)
    corroborated, arxiv_titles = set(), set()
    for paper, group in papers.items():
        title = next((e["quote"].split(f" ({paper}", 1)[0] for e in group if e["event_type"] != "affiliation_seen"), "")
        stated = [e["quote"].split(": ", 1)[-1] for e in group if e["event_type"] == "affiliation_seen"]
        if paper in listed or _title_key(title) in titles or any(_affiliated(ctx, a) for a in stated):
            corroborated.add(paper)
            arxiv_titles.add(_title_key(title))
    arxiv_kept = [e for e in events if e["extractor"] == "arxiv_api" and _arxiv_id(e) in corroborated]
    derived, rejected = openalex_papers(works, corroborated, arxiv_titles)
    if not openalex_ok and openalex_events:
        rejected["openalex: author matched on name only"] = len(openalex_events)
    if dropped := sum(len(g) for p, g in papers.items() if p not in corroborated):
        rejected["arxiv: paper not listed by a verified OpenAlex author and no known affiliation"] = dropped
    return works + arxiv_kept + derived, rejected


def openalex_papers(works, arxiv_ids, arxiv_titles, stop_at_move=True):
    """A paper_v1 per verified OpenAlex work, so a person arXiv returned nothing for still has papers.

    Each is dated and observed on the work's publication day. A work already
    kept from arXiv (same arXiv DOI or title) is skipped, as is a later work
    with an earlier one's title (a journal version of a preprint). A January 1
    date may stand for the year alone, so that work gives no paper. As in
    detectors.colleague_moves, the first work placing the person somewhere new
    is a replay's outcome, so its papers stop there (stop_at_move); a live
    person's papers go on. Returns (events, works skipped by reason).
    """
    groups = defaultdict(list)
    for e in works:
        if e["extractor"] == "openalex_works" and e.get("date_precision") == "day":
            groups[e["source_url"]].append(e)
    home, seen, out, rejected = set(), set(arxiv_titles) - {""}, [], Counter()
    for url, group in sorted(groups.items(), key=lambda g: (g[1][0]["event_date"], g[0])):
        own = {e["quote"].rsplit(" on '", 1)[0] for e in group if e["event_type"] == "affiliation_seen"}
        if stop_at_move and home and own and not own & home:
            break  # the person has moved; everything from here is the outcome
        first = group[0]
        if year_rounded(first):
            rejected["openalex: work dated January 1, which may stand for the year alone (no paper event)"] += 1
            continue
        home |= own
        title = max((m.group(1) for e in group if (m := QUOTED_TITLE.search(e["quote"]))), key=len, default="")
        paper = m.group(1) if (m := ARXIV_SUFFIX.search(url.lower())) else None
        key = _title_key(title)
        if paper in arxiv_ids or key in seen:
            continue
        seen |= {key} - {""}
        day = first["event_date"]
        out.append({"subject_type": "person", "subject_id": first["subject_id"], "event_type": "paper_v1",
                    "event_date": day, "date_precision": "day", "observed_at": first["observed_at"],
                    "source_url": url, "source_version_hash": first["source_version_hash"],
                    "quote": f"{title or url}: published {day} (OpenAlex)"[:300], "tier": 1,
                    "extractor": "openalex_paper_v1"})
    return out, rejected


def no_event_reason(ctx, row, rejected):
    """Why an answer-key person ended up with an empty timeline."""
    if row.get("errors") and not row.get("events"):
        return "fetch failed: " + "; ".join(sorted({e.split(":", 1)[0] for e in row["errors"]}))
    if "openalex" not in ctx.anchors:
        return "no OpenAlex author found; " + ("arXiv papers had no known affiliation" if rejected else "no arXiv papers in the window")
    if not verified(ctx, "openalex"):
        return "OpenAlex author matched on name only" + ("" if rejected else ", and it had no works in the window")
    return "verified OpenAlex author with no works in the window" + ("; arXiv papers uncorroborated" if rejected else "")


def load_answer_keys(store, directory, *, trust_top_hit=False, name=None):
    """The MTS answer key (people who joined a frontier lab, and comparable stayers) into a store
    from files already on disk; nothing is fetched. Saves each person's context with related
    subjects: coauthors who are also key people. `name` (a case-insensitive substring) loads
    only matching people."""
    report = {"mts": {"people": 0, "events": 0, "papers_from_openalex": 0, "rejected": 0, "no_events": 0, "top_hit_only": 0,
                      "name_only_confirmed": 0, "key_match_unconfirmed": 0,
                      "rejected_by_reason": Counter(), "no_events_by_reason": Counter(), "no_event_people": []}}
    directory, contexts = Path(directory), []

    def add(events):
        stored = 0
        for e in events:
            try:
                add_event(store, e)
                stored += 1
            except ValueError:
                pass
        return stored

    def wanted(rows):
        return [r for r in rows if not name or name.lower() in r[0].name.lower()]

    mts = mts_contexts(directory)
    subjects = {ctx.subject_id for ctx, _, _ in mts}
    for ctx, _, row in wanted(mts):
        kept, rejected = mts_row_events(ctx, row, trust_top_hit)
        coauthors = {m for e in kept if e["event_type"] == "coauthor_link" for m in COAUTHOR_ID.findall(e["quote"])}
        ctx = ctx.model_copy(update={"related": sorted(coauthors & subjects - {ctx.subject_id})})
        stats = report["mts"]
        stats["people"] += 1
        stats["events"] += add(kept)
        stats["papers_from_openalex"] += sum(e["extractor"] == "openalex_paper_v1" for e in kept)
        stats["rejected"] += sum(rejected.values())
        stats["rejected_by_reason"].update(rejected)
        if not kept:
            reason = no_event_reason(ctx, row, rejected)
            stats["no_events"] += 1
            stats["no_events_by_reason"][reason] += 1
            stats["no_event_people"].append(f"{ctx.name}: {reason}")
        stats["top_hit_only"] += bool(ctx.anchors.get("openalex")) and not verified(ctx, "openalex")
        if row.get("openalex"):
            key_matched = row["openalex"].get("match") == "affiliation_match"
            stats["name_only_confirmed"] += not key_matched and verified(ctx, "openalex")
            stats["key_match_unconfirmed"] += key_matched and not verified(ctx, "openalex")
        contexts.append(ctx)

    for ctx in contexts:
        save_context(store, ctx)
    return report
