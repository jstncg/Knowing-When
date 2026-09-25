"""WARN and NSF source parsers against fixtures; no network.

The WARN fixture is real rows from Big Local News's consolidated CSV; the NSF
fixture mirrors the API's JSON (tests/fixtures/sources).
"""

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from app.sources import http, nsf, warn
from app.timeline import TimelineEvent

FIXTURES = Path(__file__).parent / "fixtures" / "sources"
WARN_CSV = (FIXTURES / "bln_warn_integrated_sample.csv").read_bytes()  # real rows from the consolidated file


def warn_fetch(url):
    assert url == warn.SOURCE_URL, f"unexpected fetch {url}"
    return WARN_CSV


def test_org_id_normalizes_legal_names():
    assert warn.org_id("Meta Platforms, Inc.") == "org:meta-platforms"
    assert warn.org_id("The Boeing Company") == "org:boeing"
    assert warn.org_id("AT&T Corp") == "org:at-and-t"
    assert warn.org_id("Inc.") == "org:inc"


def test_consolidated_csv_keeps_current_versions_only():
    notices = warn.load(fetch=warn_fetch)
    assert [n.state for n in notices] == ["ca", "wa", "ny", "tx", "nj"]  # the superseded Meta row is gone
    meta = notices[0]
    assert (meta.company, meta.notice_date, meta.effective_date, meta.headcount, meta.action) == (
        "Meta Platforms, Inc.", "2026-01-16", "2026-03-20", 53, "layoff")
    assert meta.source_url == warn.SOURCE_URL and len(meta.source_version_hash) == 64
    assert notices[2].action == "closure"
    assert [n.state for n in warn.load(["ny", "TX"], fetch=warn_fetch)] == ["ny", "tx"]


def test_observed_at_is_first_seen_except_for_the_bulk_backfill():
    meta, boeing, _, _, jnj = warn.load(fetch=warn_fetch)
    assert meta.observed_at.startswith("2026-02-05 18:30")  # 20 days after the notice date
    assert boeing.observed_at == "2020-11-20T00:00:00+00:00"  # loaded 2022-04-07 with the backfill
    assert (jnj.notice_date, jnj.observed_at[:10]) == ("2024-09-30", "2024-09-30")  # no notice date given


def test_lookup_matches_normalized_names():
    assert warn.matches("boeing", "The Boeing Company")
    assert warn.matches("Johnson & Johnson", "Johnson & Johnson Services, Inc.")
    assert not warn.matches("Meta", "Metal Works LLC")


def test_warn_events_are_valid_tier1_org_events():
    events = warn.events(warn.load(["ca"], fetch=warn_fetch))
    assert all(TimelineEvent.model_validate(e) for e in events)
    e = events[0]
    assert (e["subject_type"], e["subject_id"], e["event_type"], e["tier"], e["extractor"]) == (
        "org", "org:meta-platforms", "warn_notice", 1, "warn_bln_v1")
    assert e["event_date"] == "2026-01-16" and e["date_precision"] == "day"
    assert e["observed_at"].startswith("2026-02-05T18:30")
    assert "effective 2026-03-20" in e["quote"] and "53 employees" in e["quote"]


EMPTY = json.dumps({"response": {"award": []}})


def nsf_fetch(pages):
    """(search role, offset) -> response body; the role is pdPIName or coPDPI."""
    def fetch(url):
        params = httpx.URL(url).params
        role = "pdPIName" if "pdPIName" in params else "coPDPI"
        assert params[role] == '"Jane Q Researcher"'
        return pages[(role, int(params["offset"]))].encode()
    return fetch


def test_nsf_awards_and_events():
    body = (FIXTURES / "nsf_awards_sample.json").read_text()
    found = nsf.awards(pi="Jane Q Researcher", fetch=nsf_fetch({("pdPIName", 1): body, ("coPDPI", 1): EMPTY}))
    assert [a.id for a in found] == ["2403987", "1955123"]
    career = found[0]
    assert (career.awarded, career.start, career.end_expected, career.amount) == (
        "2024-01-22", "2024-02-01", "2029-01-31", 412000)
    assert career.institution == "Massachusetts Institute of Technology"

    events = found[0].events()
    assert all(TimelineEvent.model_validate(e) for e in events)
    assert {(e["subject_type"], e["subject_id"], e["event_type"], e["event_date"]) for e in events} == {
        ("person", "person:jane-q-researcher", "grant_started", "2024-02-01"),
        ("person", "person:jane-q-researcher", "grant_end_expected", "2029-01-31"),
        ("org", "org:massachusetts-institute-of-technology", "grant_started", "2024-02-01"),
        ("org", "org:massachusetts-institute-of-technology", "grant_end_expected", "2029-01-31"),
    }
    e = events[0]
    assert e["observed_at"].startswith("2024-01-22") and e["tier"] == 1 and e["extractor"] == "nsf_awards_v1"
    assert e["source_url"] == "https://www.nsf.gov/awardsearch/showAward?AWD_ID=2403987"
    assert e["quote"].startswith("NSF award 2403987: CAREER") and len(e["quote"]) <= 300


def test_nsf_merges_co_pi_awards_without_duplicates():
    awards = json.loads((FIXTURES / "nsf_awards_sample.json").read_text())["response"]["award"]
    as_co_pi = json.dumps({"response": {"award": [awards[1], dict(awards[0], id="2510001", startDate="03/01/2025",
                                                                   pdPIName="Someone Else")]}})
    body = (FIXTURES / "nsf_awards_sample.json").read_text()
    found = nsf.awards(pi="Jane Q Researcher", fetch=nsf_fetch({("pdPIName", 1): body, ("coPDPI", 1): as_co_pi}))
    assert [a.id for a in found] == ["2510001", "2403987", "1955123"]
    assert found[0].pi == "Someone Else"


def test_nsf_pages_until_a_short_page():
    award = json.loads((FIXTURES / "nsf_awards_sample.json").read_text())["response"]["award"][0]
    full = json.dumps({"response": {"award": [dict(award, id=str(i)) for i in range(nsf.PAGE)]}})
    short = json.dumps({"response": {"award": [dict(award, id="last")]}})
    pages = {("pdPIName", 1): full, ("pdPIName", 26): short, ("coPDPI", 1): EMPTY}
    assert len(nsf.awards(pi="Jane Q Researcher", fetch=nsf_fetch(pages))) == nsf.PAGE + 1


def test_nsf_api_errors_are_raised_not_swallowed():
    notice = json.dumps({"response": {"serviceNotification": [{"notificationMessage": "Invalid parameter"}]}})
    with pytest.raises(ValueError, match="Invalid parameter"):
        nsf.awards(pi="Jane Q Researcher", fetch=nsf_fetch({("pdPIName", 1): notice}))


def test_nsf_requires_a_subject():
    with pytest.raises(ValueError):
        nsf.awards()


def test_fetcher_caches_on_disk_and_replays_without_network(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url)
        assert "GITimingEngine/" in request.headers["user-agent"]
        return httpx.Response(200, content=b"body", headers={"content-type": "text/csv"})

    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": http.USER_AGENT})
    fetch = http.Fetcher(tmp_path, client=client)
    assert fetch("https://example.gov/warn.csv") == b"body"
    assert fetch("https://example.gov/warn.csv") == b"body"
    assert len(calls) == 1
    meta = fetch.meta("https://example.gov/warn.csv")
    assert meta["content_hash"] == http.content_hash(b"body") and meta["content_type"] == "text/csv"
    assert http.Fetcher(tmp_path, refresh=True, client=client)("https://example.gov/warn.csv") == b"body"
    assert len(calls) == 2


def test_fetcher_retries_once_on_a_server_error(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "RETRY_AFTER_SECONDS", 0)
    monkeypatch.setattr(http, "MIN_INTERVAL_SECONDS", 0)
    statuses = iter([500, 200])
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(next(statuses), content=b"ok")))
    assert http.Fetcher(tmp_path, client=client)("https://grid.example.gov/grid.aspx") == b"ok"
    forbidden = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    with pytest.raises(httpx.HTTPStatusError):
        http.Fetcher(tmp_path, client=forbidden)("https://grid.example.gov/other")


def test_fetcher_waits_as_long_as_retry_after_asks_and_gives_up_on_a_spent_day(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "RETRY_AFTER_SECONDS", 1)
    slept = []
    monkeypatch.setattr(http.time, "sleep", slept.append)
    answers = iter([httpx.Response(429, headers={"retry-after": "34"}),
                    httpx.Response(200, content=b"ok", headers={"x-ratelimit-remaining": "690"})])
    fetch = http.Fetcher(tmp_path, client=httpx.Client(transport=httpx.MockTransport(lambda r: next(answers))))
    assert fetch("https://limits.example.org/works?cursor=x") == b"ok"
    assert 34 in slept  # what the server asked, not the 1 s the fetcher would have waited
    assert fetch.meta("https://limits.example.org/works?cursor=x")["ratelimit_remaining"] == 690
    calls = []
    spent = httpx.Client(transport=httpx.MockTransport(
        lambda r: calls.append(r) or httpx.Response(429, headers={"retry-after": "3600"})))
    slept.clear()
    with pytest.raises(httpx.HTTPStatusError):
        http.Fetcher(tmp_path, client=spent)("https://limits.example.org/works?cursor=y")
    assert len(calls) == 1 and all(s < 3600 for s in slept)  # an hour's wait is a spent budget: no retry
    assert http.retry_after(httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0
    later = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
    assert 55 <= http.retry_after(httpx.Response(429, headers={"retry-after": later})) <= 60
    assert http.retry_after(httpx.Response(429, headers={"retry-after": "soon"})) is None
    assert http.retry_after(httpx.Response(429)) is None
    assert http.HOST_INTERVAL_SECONDS["api.openalex.org"] >= 1  # the paged search 0.15 s apart drew 429s


def test_one_throttle_per_service_across_clients():
    assert http.host_throttle("arxiv.org") is http.host_throttle("export.arxiv.org")
    assert http.host_throttle("efts.sec.gov") is http.host_throttle("data.sec.gov") is http.host_throttle("www.sec.gov")
    assert http.stated_throttle("arxiv.org") is http.host_throttle("export.arxiv.org")
    assert http.stated_throttle("example.com") is None
