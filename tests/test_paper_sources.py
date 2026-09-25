"""arXiv, OpenReview and OpenAlex parsers against fixtures; no network."""

import importlib.util
from datetime import date
from pathlib import Path

import pytest

from app.sources import arxiv, openalex, openreview
from app.sources.http import content_hash
from app.timeline import TimelineEvent

FIXTURES = Path(__file__).parent / "fixtures" / "sources"
OR_NOTES = "https://api2.openreview.net/notes?"
OA_WORKS = ("https://api.openalex.org/works?filter=authorships.author.id%3AA5000000001%2Cfrom_publication_date%3A2024-07-01"
            "&per-page=200&sort=publication_date%3Adesc&select=id%2Ctitle%2Cpublication_date%2Cdoi%2Cauthorships")


def fake_fetch(pages: dict):
    """URL -> fixture file name; any other URL fails the test."""
    def fetch(url):
        assert url in pages, f"unexpected fetch {url}"
        return (FIXTURES / pages[url]).read_bytes()
    return fetch


@pytest.fixture(autouse=True)
def no_mailto(monkeypatch):
    monkeypatch.delenv("OPENALEX_MAILTO", raising=False)


def valid(events):
    for e in events:
        TimelineEvent.model_validate(e)
    return events


def test_arxiv_versions_and_affiliation():
    fetch = fake_fetch({arxiv.listing_url("Jane Doe"): "arxiv_listing.xml",
                        "https://arxiv.org/abs/2501.01234": "arxiv_abs_2501.01234.html"})
    events = valid(arxiv.events("Jane Doe", subject_id="A5000000001", since="2025-01-01", fetch=fetch))
    # The 2024 paper has no version on or after `since`, so its page is never fetched.
    assert [(e["event_type"], e["event_date"]) for e in events] == [
        ("paper_v1", "2025-01-05"), ("paper_revised", "2025-03-10"), ("affiliation_seen", "2025-01-05")]
    v2 = events[1]
    assert v2["source_url"] == "https://arxiv.org/abs/2501.01234v2"
    assert v2["observed_at"] == "2025-03-10T09:15:00+00:00"
    assert "[v2] Mon, 10 Mar 2025 09:15:00 UTC" in v2["quote"]
    assert v2["source_version_hash"] == content_hash((FIXTURES / "arxiv_listing.xml").read_bytes())
    assert "Action-Conditioned World Models for Long Horizons" in events[0]["quote"]
    assert events[2]["quote"] == "Jane Doe: Example University on 2501.01234"
    assert {e["subject_id"] for e in events} == {"A5000000001"} and {e["tier"] for e in events} == {1}


def test_arxiv_falls_back_to_api_dates_without_abs_page():
    fetch = fake_fetch({arxiv.listing_url("Jane Doe"): "arxiv_listing.xml"})
    events = arxiv.events("Jane Doe", with_versions=False, fetch=fetch)
    assert [(e["event_type"], e["event_date"]) for e in events][:2] == [
        ("paper_v1", "2025-01-05"), ("paper_revised", "2025-03-10")]
    assert events[0]["subject_id"] == "arxiv:Jane Doe" and events[-1]["event_date"] == "2024-12-01"


def test_openreview_accepted_only_dated_by_decision():
    fetch = fake_fetch({OR_NOTES + "content.authorids=~Jane_Doe1&limit=1000": "openreview_submissions.json",
                        OR_NOTES + "forum=sub1&limit=1000": "openreview_forum_sub1.json",
                        OR_NOTES + "forum=sub2&limit=1000": "openreview_forum_sub2.json"})
    events = valid(openreview.events("~Jane_Doe1", subject_id="A5000000001", fetch=fetch))
    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "paper_accepted" and e["event_date"] == "2025-01-22"
    assert e["observed_at"] == "2025-01-22T00:00:00+00:00"
    assert e["quote"] == "Action-Conditioned World Models for Long Horizons: Accept (Poster) (ICLR 2025 Poster)"
    assert e["source_url"] == "https://openreview.net/forum?id=sub1"
    assert e["source_version_hash"] == content_hash((FIXTURES / "openreview_forum_sub1.json").read_bytes())


def test_openalex_coauthors_affiliations_and_history(monkeypatch):
    monkeypatch.setenv("OPENALEX_MAILTO", "ops@example.org")
    fetch = fake_fetch({OA_WORKS + "&mailto=ops%40example.org": "openalex_works.json",
                        "https://api.openalex.org/authors/A5000000001?mailto=ops%40example.org": "openalex_author.json"})
    events = valid(openalex.events("https://openalex.org/A5000000001", since="2024-07-01", fetch=fetch))
    by_type = {}
    for e in events:
        by_type.setdefault(e["event_type"], []).append(e)
    link = by_type["coauthor_link"]
    assert [e["quote"] for e in link] == [
        "Alex Roe (A5000000002) on 'Action-Conditioned World Models for Long Horizons' | at Example Lab | unknown"]
    assert link[0]["date_precision"] == "day" and link[0]["source_url"] == "https://doi.org/10.48550/arxiv.2501.01234"
    assert [e["quote"] for e in by_type["affiliation_seen"]] == [
        "Example University on 'Action-Conditioned World Models for Long Horizons'"]
    changes = by_type["affiliation_change"]
    assert [(e["event_date"], e["date_precision"], e["quote"]) for e in changes] == [
        ("2025", "year", "Example Lab: years [2025]"), ("2022", "year", "Example University: years [2022, 2023, 2024]")]
    assert changes[1]["observed_at"] == "2022-12-31T23:59:59+00:00"
    assert changes[0]["source_url"] == "https://openalex.org/A5000000001"
    assert {e["subject_id"] for e in events} == {"A5000000001"}


def test_answer_key_resolves_author_by_affiliation_and_windows_events():
    spec = importlib.util.spec_from_file_location("mts_answer_key", Path(__file__).parents[1] / "scripts/mts_answer_key.py")
    key = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(key)
    fetch = fake_fetch({"https://api.openalex.org/authors?search=Jane+Doe&per-page=5": "openalex_search.json"})
    hit, how = key.resolve_author(fetch, {"name": "Jane Doe", "prior_affiliation": "Example University", "lab": "Example Lab"})
    assert (openalex.short_id(hit["id"]), how) == ("A5000000001", "affiliation_match")
    events = [{"event_date": d} for d in ("2024-07-01", "2024-07-05", "2025-01-01", "2025-01-02", None)]
    assert [e["event_date"] for e in key.window(events, date(2025, 1, 1))] == ["2024-07-05", "2025-01-01"]
    assert key.join_day("2025-06") == date(2025, 6, 1) and key.join_day("2025-06-26") == date(2025, 6, 26)
    row = key.collect(fetch, {"name": "Jane Doe", "join_date": "2025-06", "prior_affiliation": "Example University"}, date(2025, 6, 1))
    assert (row["join_date"], row["join_date_stated"], row["openalex"]["id"]) == ("2025-06-01", "2025-06", "A5000000001")
    assert row["errors"] and row["events"] == []  # works/arXiv URLs are not in the fake fetch
