"""Journey builder: identity, bridges, coverage statuses and idempotence, with fakes only."""

import asyncio
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

from app import bridges, detectors, journey, timeline
from app.sources import arxiv
from app.sources.wayback import PageVersion
from app.sources import warn
from app.sources.warn import WarnNotice

FIXTURES = Path(__file__).parent / "fixtures" / "sources"
AS_OF = "2025-06-01T00:00:00+00:00"


def ctx(**kw):
    base = {"subject_id": "person:jane", "name": "Jane Doe", "employer": "Example University",
            "affiliations": ["Example University"]}
    return journey.PersonContext(**{**base, **kw})


def build(context, run, **kw):
    return asyncio.run(journey.build_journey(context, kw.pop("as_of", AS_OF), kw.pop("budget", None), run=run, **kw))


def events(store, subject="person:jane", as_of="2099-01-01T00:00:00+00:00"):
    return timeline.timeline(store, subject, as_of)


def only(*ids):
    return set(ids)


# ------------------------------------------------------------------ identity

def test_name_matching_tolerates_order_initials_and_middle_names():
    assert bridges.name_matches("Mei Lin", "Lin Mei")
    assert bridges.name_matches("Jane Q. Doe", "Jane Doe") and bridges.name_matches("J. Doe", "Jane Doe")
    assert not bridges.name_matches("John Doe", "Jane Doe")


def test_a_record_joins_only_on_two_identifiers():
    c = ctx(anchors={"openalex": "A1", "linkedin": "https://www.linkedin.com/in/janedoe"},
            anchor_basis={"openalex": "name", "linkedin": "name+profile_url"})
    assert journey.agreeing(c, name="Jane Doe") == ["name"]
    assert journey.agreeing(c, name="Jane Doe", affiliations=["Dept. of CS, Example University"]) == ["affiliation", "name"]
    assert journey.agreeing(c, text="Jane Doe spoke at MIT", url="https://www.linkedin.com/in/JaneDoe/") == ["linkedin", "name"]
    # A top-hit OpenAlex id was admitted on the name alone: it is one identifier, not two.
    assert not journey.verified(c, "openalex") and journey.verified(c, "linkedin")


def test_org_resolver_needs_one_exact_registrant():
    titles = {"320193": "Apple Inc.", "1418121": "Apple Hospitality REIT, Inc.", "1": "Acme Corp /DE/", "2": "ACME Corporation"}
    resolver = journey.OrgResolver({"Example Labs": {"cik": 42}}, sec_titles=lambda: titles)
    assert resolver.cik("Apple") == ("0000320193", "sec_name")
    assert resolver.cik("Example Labs, Inc.") == ("0000000042", "mapping")
    assert resolver.cik("Acme") == (None, "ambiguous")
    assert resolver.cik("Nowhere Robotics") == (None, "not an SEC registrant")
    assert journey.same_org("ACME CORP /DE/", "Acme Corporation")


# ------------------------------------------------------------------- bridges

def version(url, when, text):
    import hashlib
    return PageVersion(url=url, capture_at=when, observed_at=when, content_hash=hashlib.sha256(text.encode()).hexdigest(),
                       text=text, source_url=f"https://web.archive.org/web/{when[:10].replace('-', '')}000000id_/{url}")


def test_wayback_homepage_changes_talks_and_availability():
    home = "https://janedoe.example.org/"
    versions = [version(home, "2025-01-10T00:00:00+00:00", "Jane Doe. I work on world models."),
                version(home, "2025-03-02T00:00:00+00:00", "Jane Doe. I work on world models. I will give an invited talk at "
                        "ICLR on April 25, 2025. I am on the job market this fall.")]
    out = bridges.page_history_events("person:jane", "Jane Doe", versions, own_page=True)
    kinds = {e["event_type"]: e for e in out}
    assert set(kinds) == {"page_changed", "paper_talk", "self_stated_availability"}
    assert kinds["paper_talk"]["event_date"] == "2025-04-25" and kinds["paper_talk"]["observed_at"].startswith("2025-03-02")
    assert {e["tier"] for e in out} == {1}


def test_wayback_roster_drop_is_a_dated_departure_and_feeds_say_nothing_first_person():
    roster = "https://lab.example.edu/people"
    versions = [version(roster, "2025-02-01T00:00:00+00:00", "Members: Jane Doe. Alex Roe."),
                version(roster, "2025-03-01T00:00:00+00:00", "Members: Alex Roe. Sam Poe.")]
    out = bridges.page_history_events("person:jane", "Jane Doe", versions, own_page=False)
    assert [(e["event_type"], e["event_date"], e["date_precision"]) for e in out] == [("job_ended", "2025-03", "month")]
    feed = "https://www.linkedin.com/in/janedoe"
    posts = [version(feed, "2025-02-01T00:00:00+00:00", "Jane Doe."),
             version(feed, "2025-03-01T00:00:00+00:00", "Jane Doe. I'm on the job market! Keynote at NeurIPS on December 3, 2025.")]
    out = bridges.page_history_events("person:jane", "Jane Doe", posts, own_page=True)
    assert [(e["event_type"], e["tier"]) for e in out] == [("page_changed", 2)]


def test_crustdata_company_rounds_hold_and_headcount_drops():
    record = {"company_name": "Acme", "last_funding_round_type": "series_c", "last_funding_date": "2025-04-02",
              "funding_and_investment": {"funding_milestones_timeseries": [
                  {"date": "2024-01-10", "funding_milestone_amount_usd": 20000000}]},
              "headcount": {"linkedin_headcount_timeseries": [
                  {"date": "2024-10-01", "employee_count": 200}, {"date": "2025-01-01", "employee_count": 196},
                  {"date": "2025-02-01", "employee_count": 170}, {"date": "2025-03-01", "employee_count": 160}]}}
    out = bridges.company_events("person:jane", "org:acme", "Acme", record,
                                 source_url="https://api.crustdata.com/screener/company", version="v")
    assert [(e["subject_id"], e["event_type"], e["event_date"], e["tier"]) for e in out] == [
        ("person:jane", "equity_refresh", "2024-01-10", 3), ("person:jane", "equity_refresh", "2025-04-02", 3),
        ("org:acme", "headcount_change", "2025-02-01", 1)]


# ------------------------------------------------------------------ adapters

class Pages:
    """URL substring -> body; any other request fails the test."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, **_):
        self.calls.append(url)
        for key, body in self.pages.items():
            if key in url:
                return body if isinstance(body, bytes) else json.dumps(body).encode()
        raise AssertionError(f"unexpected fetch {url}")


def test_arxiv_by_name_keeps_only_corroborated_papers(store):
    fetch = Pages({arxiv.listing_url("Jane Doe"): (FIXTURES / "arxiv_listing.xml").read_bytes(),
                   "arxiv.org/abs/2501.01234": (FIXTURES / "arxiv_abs_2501.01234.html").read_bytes()})
    report = build(ctx(), journey.Run(store, replay=False, fetch=fetch), only=only("arxiv"))
    # 2501.01234 states Example University; the 2024 note names only "Jane Doe", so it could be anyone's.
    assert report["coverage"]["arxiv"] == {"status": "ok", "events": 3, "rejected": 1, "related": []}
    assert {e["source_url"].split("/abs/")[1][:10] for e in events(store)} == {"2501.01234"}


def openalex_work(day, doi, place, title=None):
    jane = {"id": "https://openalex.org/A5000000001", "display_name": "Jane Doe"}
    return {"id": "https://openalex.org/W" + day, "title": title or f"A paper from {place} on {day}", "publication_date": day,
            "doi": doi, "authorships": [{"author": jane, "institutions": [{"display_name": place}], "author_position": "first"}]}


def paper_pages(*more):
    """Jane's OpenAlex author (A5000000001) and works, and arXiv's listing and abstract page.

    Beside the fixture's arXiv paper (2501.01234): a coauthor with no id, a journal paper arXiv never lists,
    a paper from a new lab after both, and any ``more`` works."""
    works = json.loads((FIXTURES / "openalex_works.json").read_text())
    works["results"][0]["authorships"].append({"author": {"id": None, "display_name": "Unlinked Coauthor"},
                                               "institutions": [], "author_position": "last"})
    works["results"] += [openalex_work("2025-02-10", "https://doi.org/10.1/journal", "Example University"),
                         openalex_work("2025-03-03", "https://doi.org/10.1/lab", "Frontier Lab"), *more]
    return Pages({"api.openalex.org/works?": works, "api.openalex.org/authors/A5000000001": (FIXTURES / "openalex_author.json").read_bytes(),
                  arxiv.listing_url("Jane Doe"): (FIXTURES / "arxiv_listing.xml").read_bytes(),
                  "arxiv.org/abs/2501.01234": (FIXTURES / "arxiv_abs_2501.01234.html").read_bytes()})


def papers(store):
    return [(e["source_url"], e["event_date"], e["extractor"]) for e in events(store) if e["event_type"] == "paper_v1"]


def refusing(pages, code=406):
    def fetch(url, **kw):
        if "export.arxiv.org" not in url:
            return pages(url, **kw)
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(f"{code} refused", request=request, response=httpx.Response(code, request=request))
    return fetch


JANE_OA = {"anchors": {"openalex": "A5000000001"}, "anchor_basis": {"openalex": "name+affiliation"}}
LATER = [("https://doi.org/10.1/journal", "2025-02-10", "openalex_paper_v1"),
         ("https://doi.org/10.1/lab", "2025-03-03", "openalex_paper_v1")]


def test_openalex_works_give_the_papers_arxiv_does_not_and_an_author_with_no_id_is_skipped(store):
    report = build(ctx(**JANE_OA), journey.Run(store, replay=False, fetch=paper_pages()), only=only("openalex", "arxiv"))
    assert report["coverage"]["openalex"]["status"] == "ok"  # the coauthor with no id is skipped, not a crash
    # arXiv's paper comes from arXiv alone, never twice; the works arXiv does not list come from OpenAlex, the new
    # lab's included: a live person's papers go on after a move.
    assert papers(store) == [("https://arxiv.org/abs/2501.01234v1", "2025-01-05", "arxiv_api"), *LATER]


def test_a_replay_takes_no_paper_from_after_the_move(store):
    build(ctx(**JANE_OA), journey.Run(store, replay=True, fetch=paper_pages()), only=only("openalex", "arxiv"))
    assert papers(store) == [("https://arxiv.org/abs/2501.01234v1", "2025-01-05", "arxiv_api"), LATER[0]]


def test_while_arxiv_refuses_openalex_gives_the_papers_and_arxiv_replaces_its_own_later(store):
    c, pages = ctx(**JANE_OA), paper_pages()
    report = build(c, journey.Run(store, replay=False, fetch=refusing(pages)), only=only("openalex", "arxiv"))
    assert report["coverage"]["arxiv"] == {  # still an error, so the next run asks arXiv again
        "status": "error", "events": 3, "rejected": 0, "related": [],
        "detail": "arXiv refused the search (406), so it is asked again next run; papers from OpenAlex meanwhile: 3"}
    assert papers(store) == [("https://doi.org/10.48550/arxiv.2501.01234", "2025-01-05", "openalex_paper_v1"), *LATER]
    # A second refusal with a changed OpenAlex listing (a new work) adds that work and no second copy of the rest.
    newer = openalex_work("2025-04-02", "https://doi.org/10.1/newer", "Example University")
    build(c, journey.Run(store, replay=False, fetch=refusing(paper_pages(newer))), only=only("openalex", "arxiv"))
    assert papers(store) == [("https://doi.org/10.48550/arxiv.2501.01234", "2025-01-05", "openalex_paper_v1"), *LATER,
                             ("https://doi.org/10.1/newer", "2025-04-02", "openalex_paper_v1")]
    # arXiv answers on the next run. The openalex adapter is saved as done, so arXiv reads the works itself (a work
    # only this run's OpenAlex lists shows it), and its paper replaces OpenAlex's rather than joining it.
    newest = openalex_work("2025-05-05", "https://doi.org/10.1/newest", "Example University")
    report = build(c, journey.Run(store, replay=False, fetch=paper_pages(newer, newest)), only=only("openalex", "arxiv"))
    assert report["coverage"]["openalex"].get("cached") and report["coverage"]["arxiv"]["status"] == "ok"
    assert papers(store) == [("https://arxiv.org/abs/2501.01234v1", "2025-01-05", "arxiv_api"), *LATER,
                             ("https://doi.org/10.1/newer", "2025-04-02", "openalex_paper_v1"),
                             ("https://doi.org/10.1/newest", "2025-05-05", "openalex_paper_v1")]
    # Without a verified OpenAlex author there is nothing to fall back on, so the refusal stays an error; arXiv
    # blocking us is never a refusal to wait out.
    sam = ctx(subject_id="person:sam", name="Sam Poe")
    assert build(sam, journey.Run(store, replay=False, fetch=refusing(pages)), only=only("arxiv"))["coverage"]["arxiv"]["status"] == "error"
    blocked = build(c, journey.Run(store, replay=False, fetch=refusing(pages, 403)), only=only("arxiv"), as_of="2025-06-02T00:00:00+00:00")
    assert blocked["coverage"]["arxiv"]["status"] == "blocked"


def test_while_arxiv_refuses_a_paper_arxiv_gave_is_not_counted_again_nor_its_journal_version(store):
    c = ctx(**JANE_OA)
    build(c, journey.Run(store, replay=False, fetch=paper_pages()), only=only("openalex", "arxiv"))
    icml = openalex_work("2025-04-20", "https://doi.org/10.1/icml", "Example University",
                         title="Action-Conditioned World Models for Long Horizons")
    build(c, journey.Run(store, replay=False, fetch=refusing(paper_pages(icml))), only=only("openalex", "arxiv"),
          as_of="2025-06-02T00:00:00+00:00")
    assert papers(store) == [("https://arxiv.org/abs/2501.01234v1", "2025-01-05", "arxiv_api"), *LATER]
    assert journey._arxiv_paper("https://arxiv.org/abs/math.AG/0601001v2") == "math.ag/0601001"
    assert journey._arxiv_paper("https://doi.org/10.48550/arXiv.2501.01234") == "2501.01234"


def test_openalex_citations_both_directions(store):
    gi = {"id": "https://openalex.org/W9", "title": "GI world model", "publication_date": "2025-05-01",
          "doi": "https://doi.org/10.48550/arxiv.2607.05352", "referenced_works": ["https://openalex.org/W1"]}
    fetch = Pages({"doi%3A10.48550": {"results": [gi]},
                   "cites%3AW9": {"results": [{"id": "https://openalex.org/W5", "title": "Jane builds on GI",
                                               "publication_date": "2025-05-20", "referenced_works": [gi["id"]]}]},
                   "openalex%3AW1": {"results": [{"id": "https://openalex.org/W1", "title": "Jane's old paper"}]}})
    c = ctx(anchors={"openalex": "A1"}, anchor_basis={"openalex": "name+affiliation"})
    report = build(c, journey.Run(store, replay=False, fetch=fetch), only=only("openalex_citations"))
    assert report["coverage"]["openalex_citations"]["status"] == "ok"
    assert sorted((e["event_type"], e["event_date"]) for e in events(store)) == [
        ("gi_citation", "2025-05-01"), ("gi_citation", "2025-05-20")]


def test_exa_admits_the_person_not_the_namesake_and_feeds_stay_tier_2(store):
    docs = [
        {"url": "https://conf.example.org/speakers", "title": "Speakers", "published_at": "2025-03-01T00:00:00Z",
         "content_hash": "h1", "text": "Jane Doe (Example University) gives a keynote at WMW on May 12, 2025."},
        {"url": "https://news.example.com/jane", "title": "Jane Doe opens a bakery", "published_at": "2025-03-02T00:00:00Z",
         "content_hash": "h2", "text": "Jane Doe opens a bakery downtown."},
        {"url": "https://www.linkedin.com/in/janedoe", "title": "Jane Doe - Example University", "published_at": None,
         "content_hash": "h3", "text": "Jane Doe. I'm on the job market. General Intuition is hiring."},
    ]

    async def exa(query, settings):
        assert '"Jane Doe"' in query
        return docs

    run = journey.Run(store, replay=False, settings={"exa_key": "k"}, exa=exa)
    report = build(ctx(), run, budget=journey.Budget(1), only=only("exa"))
    assert report["coverage"]["exa"]["rejected"] == 1 and report["spent"] == 1
    got = sorted((e["event_type"], e["tier"], e["source_url"]) for e in events(store))
    assert got == [("paper_talk", 2, docs[0]["url"]), ("web_mention", 2, docs[0]["url"]), ("web_mention", 2, docs[2]["url"])]


def test_statuses_idempotence_and_spend_cap(store, monkeypatch):
    calls = []

    async def exa(query, settings):
        calls.append(query)
        return []

    async def refused(ctx_, as_of, run, budget):
        raise httpx.ProxyError("CONNECT tunnel failed, response 403")

    async def broken(ctx_, as_of, run, budget):
        raise ValueError("unexpected shape")

    async def missing(ctx_, as_of, run, budget):
        import app.sources.nowhere  # noqa: F401

    monkeypatch.setitem(journey.ADAPTERS, "refused", journey.Adapter("refused", refused))
    monkeypatch.setitem(journey.ADAPTERS, "broken", journey.Adapter("broken", broken))
    monkeypatch.setitem(journey.ADAPTERS, "missing", journey.Adapter("missing", missing))
    run = journey.Run(store, replay=False, settings={"exa_key": "k"}, exa=exa)
    picked = only("exa", "refused", "broken", "missing", "openalex", "crustdata_person")
    first = build(ctx(), run, budget=journey.Budget(1), only=picked)
    status = {a: c["status"] for a, c in first["coverage"].items()}
    assert status == {"exa": "empty", "refused": "blocked", "broken": "error", "missing": "unavailable",
                      "openalex": "unavailable", "crustdata_person": "unavailable"}
    again = build(ctx(), run, budget=journey.Budget(1), only=picked)
    assert again["coverage"]["exa"]["cached"] and len(calls) == 1 and again["spent"] == 0
    assert again["coverage"]["refused"]["status"] == "blocked"  # failures are retried
    capped = build(ctx(subject_id="person:other"), run, only=only("exa"))  # paid sources are off by default
    assert capped["coverage"]["exa"]["status"] == "skipped" and len(calls) == 1


def test_employer_events_reach_the_person_through_related(store, monkeypatch):
    notice = WarnNotice(state="ca", company="Example University Inc.", notice_date="2025-04-01", effective_date=None,
                        headcount=120, action="layoff", location="Oakland", source_url="https://edd.ca.gov/x.xlsx",
                        source_version_hash="w1", observed_at="2025-04-03T00:00:00+00:00")
    other = WarnNotice(state="ca", company="Example University Hospital", notice_date="2025-04-02", effective_date=None,
                       headcount=90, action="layoff", location="", source_url="https://edd.ca.gov/x.xlsx",
                       source_version_hash="w1", observed_at="2025-04-03T00:00:00+00:00")
    monkeypatch.setattr(warn, "load", lambda fetch=None: [notice, other])
    report = build(ctx(), journey.Run(store, replay=False, fetch=object()), only=only("warn"))
    assert report["coverage"]["warn"] == {"status": "ok", "events": 1, "rejected": 0, "related": ["org:example-university"]}
    assert report["related"] == ["org:example-university"]
    fired = {a["detector_id"] for a in journey.detect(store, "person:jane", "2025-05-01T00:00:00+00:00")}
    assert "warn_notice" in fired
    own = timeline.timeline(store, "person:jane", "2025-05-01T00:00:00+00:00")
    assert not detectors.detect(own, "person:jane", "2025-05-01T00:00:00+00:00")  # the one-subject view is blind


@pytest.fixture
def fake_sec(monkeypatch):
    def officer(name, kind, day, company="ACME CORP"):
        return timeline.TimelineEvent(subject_type="person", subject_id=f"{name.lower().replace(' ', '-')}|acme-corp",
                                      event_type=kind, event_date=day, date_precision="day",
                                      observed_at=f"{day}T20:00:00+00:00", source_url="https://www.sec.gov/a.htm",
                                      source_version_hash="s1", quote=f"{name} {kind}", tier=1, extractor="sec_8k_v1")

    class Officer(timeline.TimelineEvent):
        person_name: str

    records = [
        timeline.TimelineEvent(subject_type="org", subject_id="0000000007", event_type="acquisition_closed",
                               event_date="2024-03-01", date_precision="day", observed_at="2024-03-04T20:00:00+00:00",
                               source_url="https://www.sec.gov/b.htm", source_version_hash="s2", quote="closed", tier=1,
                               extractor="sec_8k_v1"),
        Officer(**officer("Pat Chen", "officer_departure", "2025-02-01").model_dump(), person_name="Pat Chen"),
        Officer(**officer("Jane Doe", "officer_appointment", "2023-01-05").model_dump(), person_name="Jane Doe"),
    ]
    module = types.SimpleNamespace(pad_cik=lambda c: str(int(c)).zfill(10), org_events=lambda client, cik, since: records,
                                   person_id=lambda name, company: f"{name}|{company}".lower().replace(" ", "-"))
    monkeypatch.setitem(sys.modules, "app.sources.sec", module)
    import app.sources  # once the real module is imported, `from .sources import sec` reads this attribute
    monkeypatch.setattr(app.sources, "sec", module, raising=False)
    return module


def test_sec_org_events_own_officer_record_and_fellow_officers(store, fake_sec):
    c = ctx(subject_id="jane-doe|acme-corp", employer="ACME CORP", anchors={"sec_cik": "7"}, anchor_basis={"sec_cik": "name+filing"})
    report = build(c, journey.Run(store, replay=False, sec_client=object()), only=only("sec"))
    assert report["related"] == ["0000000007", "org:acme"]
    assert [e["event_type"] for e in events(store, "jane-doe|acme-corp")] == ["officer_appointment"]
    fired = {a["detector_id"] for a in journey.detect(store, "jane-doe|acme-corp", "2025-03-01T00:00:00+00:00")}
    assert {"exec_departure", "retention_cliff"} <= fired


def test_answer_key_contexts_are_keyed_by_openalex_author(tmp_path):
    (tmp_path / "mts-joiners.json").write_text(json.dumps({"people": [
        {"name": "Jane Doe", "prior_affiliation": "MIT (CSAIL)", "join_date": "2025-06-01", "window_end": "2025-06-01",
         "openalex": {"id": "A1", "match": "affiliation_match", "affiliations": ["Massachusetts Institute of Technology"]}}]}))
    (mts, as_of, _), = journey.mts_contexts(tmp_path)
    assert (mts.subject_id, as_of, journey.verified(mts, "openalex")) == ("A1", "2025-06-01", True)
    assert journey.agreeing(mts, text="Jane Doe, CSAIL") == ["affiliation", "name"]


def test_openalex_identity_needs_the_pre_move_employer_among_the_authors_own_institutions(tmp_path):
    confirmed = journey.employer_confirmed
    assert confirmed("MIT (CSAIL)", ["Massachusetts Institute of Technology"])
    assert confirmed("UC Berkeley (assistant professor)", ["University of California, Berkeley"])
    assert confirmed("Google DeepMind", ["Google (United States)", "DeepMind (United Kingdom)"])
    assert confirmed("Tidewell (co-founder); formerly Northgate Institute / University of Washington",
                     ["University of Washington"])
    # A shared generic word or a parenthetical place is a namesake's, not the person's.
    assert not confirmed("Example University", ["University of Tokyo"])
    assert not confirmed("OpenAI (Zurich)", ["University of Zurich"])
    # The key script accepted the destination lab; the context does not.
    (tmp_path / "mts-joiners.json").write_text(json.dumps({"people": [
        {"name": "Jane Doe", "prior_affiliation": "Example University", "lab": "Frontier Lab", "join_date": "2025-06-01",
         "window_end": "2025-06-01", "openalex": {"id": "A1", "match": "affiliation_match", "affiliations": ["Frontier Lab"]}},
        {"name": "Sam Poe", "prior_affiliation": "Example University", "lab": "Frontier Lab", "join_date": "2025-06-01",
         "window_end": "2025-06-01", "openalex": {"id": "A2", "match": "top_hit", "affiliations": []},
         "events": [openalex_event("A2", "affiliation_seen", "2025-01-10", "Example University on 'A paper'",
                                   "https://doi.org/10.1/a")]}]}))
    (jane, _, _), (sam, _, _) = journey.mts_contexts(tmp_path)
    assert not journey.verified(jane, "openalex") and journey.verified(sam, "openalex")


def test_reference_people_can_be_live_candidates(tmp_path, store):
    store.put("candidate:c1", "candidate", {"person_id": "person:live:abc", "name": "Jane Doe", "role_id": "controller"})
    path = tmp_path / "people.json"
    path.write_text(json.dumps([{"candidate_id": "candidate:c1", "employer": "Example University", "as_of": "2026-09-01"},
                                {"subject_id": "person:sam", "name": "Sam Lee"}]))
    (jane, as_of), (sam, _) = journey.reference_contexts(path, store)
    assert (jane.subject_id, jane.candidate_ids, jane.role, as_of) == ("person:live:abc", ["candidate:c1"], "controller", "2026-09-01")
    build(jane, journey.Run(store, replay=False), only=only("openalex"))
    assert journey.related(store, "person:live:abc") == ["gi", "org:example-university"]
    assert journey.related(store, "person:sam") == ["gi"]


# ------------------------------------------------ answer keys, offline

def paper_events(subject, arxiv_id, title, day, affiliation=None, author="Jane Doe"):
    url = f"https://arxiv.org/abs/{arxiv_id}"
    out = [{"subject_type": "person", "subject_id": subject, "event_type": "paper_v1", "event_date": day,
            "date_precision": "day", "observed_at": f"{day}T12:00:00+00:00", "source_url": url + "v1",
            "source_version_hash": "a1", "quote": f"{title} ({arxiv_id}v1): published {day}", "tier": 1,
            "extractor": "arxiv_api", "author": "", "supersedes": None}]
    if affiliation:
        out.append({**out[0], "event_type": "affiliation_seen", "source_url": url,
                    "quote": f"{author}: {affiliation} on {arxiv_id}"})
    return out


def openalex_event(subject, event_type, day, quote, doi):
    return {"subject_type": "person", "subject_id": subject, "event_type": event_type, "event_date": day,
            "date_precision": "day", "observed_at": f"{day}T00:00:00+00:00", "source_url": doi,
            "source_version_hash": "o1", "quote": quote, "tier": 1, "extractor": "openalex_works",
            "author": "", "supersedes": None}


def mts_row(name, subject, match, events, **extra):
    return {"name": name, "source_url": "https://example.org/announcement", "window_end": "2025-06-01",
            "openalex": {"id": subject, "match": match} if match else None, "events": events, **extra}


@pytest.fixture
def keys(tmp_path):
    jane = [
        openalex_event("A1", "coauthor_link", "2025-02-01", "Sam Poe (A2) on 'World models at scale'",
                       "https://doi.org/10.48550/arxiv.2502.00001"),
        openalex_event("A1", "affiliation_change", "2019", "Example University: years [2019]", "https://openalex.org/A1"),
        *paper_events("A1", "2502.00001", "World models at scale", "2025-02-01"),          # listed by OpenAlex
        *paper_events("A1", "2503.00002", "Planning in latent space", "2025-03-01", "Example University"),  # affiliation
        *paper_events("A1", "2504.00003", "Protein folding", "2025-04-01", "Other Institute"),  # a namesake
    ]
    jane[1].update(event_date="2019", date_precision="year")
    joiners = {"people": [mts_row("Jane Doe", "A1", "affiliation_match", jane, prior_affiliation="Example University",
                                  join_date="2025-06-01", lab="Frontier Lab")]}
    sam = [openalex_event("A2", "coauthor_link", "2025-02-01", "Jane Doe (A1) on 'World models at scale'",
                          "https://doi.org/10.48550/arxiv.2502.00001")]
    stayers = {"people": [mts_row("Sam Poe", "A2", "top_hit", sam, affiliation="Example University")]}
    (tmp_path / "mts-joiners.json").write_text(json.dumps(joiners))
    (tmp_path / "mts-stayers.json").write_text(json.dumps(stayers))
    return tmp_path


def test_mts_rows_keep_only_corroborated_events_and_link_coauthors(store, keys):
    report = journey.load_answer_keys(store, keys)
    stats = report["mts"]
    assert {k: stats[k] for k in ("people", "events", "rejected", "no_events", "top_hit_only")} == {
        "people": 2, "events": 4, "rejected": 3, "no_events": 1, "top_hit_only": 1}
    assert stats["rejected_by_reason"] == {
        "arxiv: paper not listed by a verified OpenAlex author and no known affiliation": 2,
        "openalex: author matched on name only": 1}
    assert stats["no_event_people"] == ["Sam Poe: OpenAlex author matched on name only"]
    kept = {e["source_url"] for e in events(store, "A1")}
    assert "https://arxiv.org/abs/2504.00003v1" not in kept
    assert {"https://arxiv.org/abs/2502.00001v1", "https://arxiv.org/abs/2503.00002v1"} <= kept
    # Sam's OpenAlex id was a top hit on the name alone, so nothing of his joins until --trust-top-hit.
    assert events(store, "A2") == [] and journey.related(store, "A1") == ["gi", "A2"]
    journey.load_answer_keys(store, keys, trust_top_hit=True)
    assert sorted((e["event_type"], e["extractor"]) for e in events(store, "A2")) == [
        ("coauthor_link", "openalex_works"), ("paper_v1", "openalex_paper_v1")]  # Sam has no arXiv rows


def test_openalex_works_give_papers_when_arxiv_has_none(store):
    """One paper_v1 per verified work on its publication day, deduped against arXiv and journal versions.

    Stops at the work placing the person at the new lab (the outcome) and skips a January 1 date.
    """
    def work(day, title, doi, place="Example University"):
        return [openalex_event("A1", "affiliation_seen", day, f"{place} on '{title}'", doi),
                openalex_event("A1", "coauthor_link", day, f"Sam Poe (A2) on '{title}' | at {place} | last", doi)]

    events_in_row = [
        *work("2024-09-10", "Scaling laws for agents", "https://doi.org/10.1/a"),
        *work("2024-11-01", "Scaling Laws for Agents", "https://doi.org/10.2/journal"),   # journal version
        *work("2025-01-01", "A year-only work", "https://doi.org/10.1/y"),
        *work("2025-02-01", "World models at scale", "https://doi.org/10.48550/arxiv.2502.00001"),
        *paper_events("A1", "2502.00001", "World models at scale", "2025-02-01"),          # on arXiv too
        *work("2025-04-01", "First paper at the lab", "https://doi.org/10.1/lab", place="Frontier Lab"),
        *work("2025-05-01", "Back home", "https://doi.org/10.1/later"),
    ]
    row = mts_row("Jane Doe", "A1", "affiliation_match", events_in_row, prior_affiliation="Example University")
    context = journey.PersonContext(subject_id="A1", name="Jane Doe", affiliations=["Example University"],
                                    anchors={"openalex": "A1"}, anchor_basis={"openalex": "name+affiliation"})
    kept, rejected = journey.mts_row_events(context, row)
    derived = [e for e in kept if e["extractor"] == "openalex_paper_v1"]
    assert [(e["source_url"], e["observed_at"][:10]) for e in derived] == [("https://doi.org/10.1/a", "2024-09-10")]
    assert rejected == {"openalex: work dated January 1, which may stand for the year alone (no paper event)": 1}
    # A live person has no outcome to stop at: their papers after a move still count.
    live, _ = journey.openalex_papers(events_in_row, set(), set(), stop_at_move=False)
    assert [e["source_url"] for e in live] == ["https://doi.org/10.1/a", "https://doi.org/10.48550/arxiv.2502.00001",
                                               "https://doi.org/10.1/lab", "https://doi.org/10.1/later"]
    # Name-only OpenAlex authors give no papers either.
    unverified = context.model_copy(update={"anchor_basis": {"openalex": "name"}})
    assert not [e for e in journey.mts_row_events(unverified, row)[0] if e["extractor"] == "openalex_paper_v1"]

    for e in kept:
        timeline.add_event(store, e)

    def papers(as_of):
        found = detectors.detect(timeline.timeline(store, "A1", as_of), "A1", as_of)
        return [a for a in found if a["detector_id"] == "paper_v1" and a["window_open"].startswith("2024-09-10")]

    assert papers("2024-09-20T00:00:00+00:00") and not papers("2024-09-09T00:00:00+00:00")


def test_mts_answer_keys_load_every_person_with_a_context(store, keys):
    report = journey.load_answer_keys(store, keys)
    assert report["mts"]["people"] == 2
    assert journey.load_context(store, "A1") and journey.load_context(store, "A2")
