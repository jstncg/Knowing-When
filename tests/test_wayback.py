"""Wayback source: CDX listing, as-of fetch, on-disk cache, diffs and coverage.

Fixtures are hand-written in the documented CDX JSON and raw-capture formats;
no test touches the network.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import providers
from app.sources import wayback as w

FIXTURES = Path(__file__).parent / "fixtures" / "wayback"
URL = "https://lab.example.edu/people/alex"
CDX_ROWS = json.loads((FIXTURES / "cdx_lab.json").read_text())
CAPTURES = {
    "AAAA1111": "<html><head><title>Alex</title></head><body><p>PhD student at Example Lab.</p></body></html>",
    "BBBB2222": (FIXTURES / "capture_2021.html").read_text(),
    "CCCC3333": (FIXTURES / "capture_2023.html").read_text(),
    # Markup-only churn: same text as 2023 with different whitespace and attributes.
    "DDDD4444": (FIXTURES / "capture_2023.html").read_text().replace("<p>", "<p class='x'>\n  "),
}


def run(coro):
    return asyncio.run(coro)


class FakeArchive:
    """Enough of the CDX server and raw-capture endpoint to exercise the client."""

    def __init__(self, rows=CDX_ROWS):
        self.rows = rows
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []  # queued overrides, popped first

    def transport(self):
        return httpx.MockTransport(self)

    def __call__(self, request):
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        if request.url.path.startswith("/cdx/search/cdx"):
            return self.cdx(request.url.params)
        timestamp = request.url.path.split("/")[2].removesuffix("id_")
        digest = next(r[4] for r in self.rows[1:] if r[0] == timestamp)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=CAPTURES[digest])

    def cdx(self, params):
        header, *body = self.rows
        body = body if params["url"] == URL else []
        body = [r for r in body if r[0] >= params.get("from", "0") and r[0] <= params.get("to", "9").ljust(14, "9")]
        if params.get("collapse") == "digest":
            body = [r for i, r in enumerate(body) if i == 0 or r[4] != body[i - 1][4]]
        if limit := int(params.get("limit", 0)):
            body = body[:limit] if limit > 0 else body[limit:]
        fields = params["fl"].split(",")
        index = [header.index(f) for f in fields]
        rows = [fields] + [[r[i] for i in index] for r in body]
        return httpx.Response(200, text=json.dumps(rows) if body else "")


@pytest.fixture()
def archive():
    return FakeArchive()


@pytest.fixture()
def client(archive, tmp_path):
    async def no_sleep(_):
        pass

    return w.WaybackClient(cache_dir=tmp_path / "cache", transport=archive.transport(), throttle=w.Throttle(1.5, sleep=no_sleep))


def test_list_captures_keeps_first_of_each_digest(client, archive):
    captures = run(client.list_captures(URL))
    assert [c.timestamp for c in captures] == ["20190315120000", "20210602101500", "20230108153000", "20250220110000"]
    assert captures[0].original == "http://lab.example.edu/people/alex"
    assert captures[0].source_url == "https://web.archive.org/web/20190315120000id_/http://lab.example.edu/people/alex"
    assert captures[0].captured_at == "2019-03-15T12:00:00+00:00"
    assert archive.requests[0].headers["user-agent"] == w.USER_AGENT
    assert archive.requests[0].url.params["filter"] == "statuscode:200"


def test_list_captures_date_range(client, archive):
    captures = run(client.list_captures(URL, since="2021-01-01T00:00:00Z", until="2023-12-31T23:59:59Z"))
    assert [c.timestamp for c in captures] == ["20210602101500", "20230108153000"]
    params = archive.requests[0].url.params
    assert (params["from"], params["to"]) == ("20210101000000", "20231231235959")


def test_capture_as_of_is_latest_at_or_before(client):
    assert run(client.capture_as_of(URL, "2023-04-12T09:00:00Z")).timestamp == "20230412090000"
    assert run(client.capture_as_of(URL, "2022-06-01T00:00:00Z")).timestamp == "20211120000000"
    assert run(client.capture_as_of(URL, "2019-01-01T00:00:00Z")) is None


def test_page_as_of_matches_fetch_document_extraction(client, monkeypatch):
    version = run(client.page_as_of(URL, "2022-01-01T00:00:00Z"))
    assert version.title == "Alex Example - Example Lab"
    assert "Postdoctoral researcher" in version.text
    assert "not content" not in version.text and "Home People" not in version.text
    assert version.capture_at == version.observed_at == "2021-11-20T00:00:00+00:00"
    assert version.source_url.startswith("https://web.archive.org/web/20211120000000id_/")

    async def resolver(url):
        return "93.184.216.34"

    monkeypatch.setattr(providers, "_resolve_public", resolver)
    real = httpx.AsyncClient
    handler = lambda req: httpx.Response(200, headers={"content-type": "text/html"}, text=CAPTURES["BBBB2222"])
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    live = run(providers.fetch_document(URL))
    assert live["text"] == version.text
    assert live["content_hash"] == version.content_hash


def test_record_matches_source_version_shape(client):
    record = run(client.page_as_of(URL, "2024-01-01T00:00:00Z")).record()
    assert {"url", "content_hash", "text", "observed_at"} <= record.keys()
    assert record["url"] == URL
    assert record["observed_at"] == record["capture_at"]


def test_fetch_version_hits_archive_once_per_capture(client, archive, tmp_path):
    capture = run(client.list_captures(URL))[1]
    first = run(client.fetch_version(capture))
    second = run(client.fetch_version(capture))
    assert first == second
    assert sum(1 for r in archive.requests if "id_" in r.url.path) == 1
    fresh = w.WaybackClient(cache_dir=tmp_path / "cache", transport=archive.transport(), throttle=w.Throttle(0))
    assert run(fresh.fetch_version(capture)) == first
    assert sum(1 for r in archive.requests if "id_" in r.url.path) == 1
    assert (tmp_path / "cache").glob("*/20210602101500.json")


def test_history_collapses_markup_only_churn(client):
    versions = run(client.history(URL, since="2021-01-01T00:00:00Z"))
    assert [v.capture_at[:10] for v in versions] == ["2021-06-02", "2023-01-08"]
    diff = w.diff_versions(versions[0], versions[1])
    assert diff.changed
    assert any("Joined Acme Robotics as a staff scientist in January 2023." in a for a in diff.added)
    assert diff.removed == ["Postdoctoral researcher"] and "Research scientist" in diff.added
    assert not w.diff_versions(versions[1], versions[1]).changed


def test_coverage_zero_fills_years_and_marks_unknown(archive, tmp_path):
    archive.responses.append(httpx.Response(503))
    archive.responses.append(httpx.Response(503))

    async def no_sleep(_):
        pass

    client = w.WaybackClient(cache_dir=tmp_path, transport=archive.transport(), throttle=w.Throttle(1.5, sleep=no_sleep))
    down, known = run(client.coverage(["https://down.example.org/", URL], until="2025-12-31T00:00:00Z"))
    assert down.error and down.captures_per_year == {} and down.first_capture_at is None
    assert known.captures_per_year == {2019: 2, 2020: 0, 2021: 2, 2022: 0, 2023: 3, 2024: 0, 2025: 1}
    assert known.first_capture_at == "2019-03-15T12:00:00+00:00"
    assert known.last_capture_at == "2025-02-20T11:00:00+00:00"
    empty = run(client.coverage(["https://lab.example.edu/nothing"], since="2024-01-01T00:00:00Z", until="2025-06-01T00:00:00Z"))[0]
    assert empty.error is None and empty.captures_per_year == {2024: 0, 2025: 0}


def test_retries_once_on_429_then_fails(archive, tmp_path):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    client = w.WaybackClient(cache_dir=tmp_path, transport=archive.transport(), throttle=w.Throttle(0, sleep=sleep))
    archive.responses.append(httpx.Response(429, headers={"retry-after": "7"}))
    assert len(run(client.list_captures(URL))) == 4
    assert slept == [7]
    archive.responses += [httpx.Response(429), httpx.Response(429)]
    with pytest.raises(w.WaybackError, match="HTTP 429"):
        run(client.list_captures(URL))


def test_cdx_non_json_body_is_unknown_coverage(archive, tmp_path):
    async def no_sleep(_):
        pass

    client = w.WaybackClient(cache_dir=tmp_path, transport=archive.transport(), throttle=w.Throttle(0, sleep=no_sleep))
    offline = "<html><title>Internet Archive: Temporarily Offline</title></html>"
    archive.responses.append(httpx.Response(200, headers={"content-type": "text/html"}, text=offline))
    with pytest.raises(w.WaybackError, match="non-JSON"):
        run(client.list_captures(URL))
    archive.responses.append(httpx.Response(200, text='{"error": "maintenance"}'))
    report = run(client.coverage([URL]))[0]
    assert report.error and report.captures_per_year == {} and report.first_capture_at is None
    assert run(client.coverage([URL]))[0].captures_per_year[2019] == 2  # archive back, same client


def test_rejects_non_text_and_offsite_redirect(archive, tmp_path):
    async def no_sleep(_):
        pass

    client = w.WaybackClient(cache_dir=tmp_path, transport=archive.transport(), throttle=w.Throttle(1.5, sleep=no_sleep))
    capture = run(client.list_captures(URL))[0]
    archive.responses.append(httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF"))
    with pytest.raises(w.WaybackError, match="text/HTML"):
        run(client.fetch_version(capture))
    archive.responses.append(httpx.Response(302, headers={"location": "https://evil.example.net/"}))
    archive.responses.append(httpx.Response(200, text="<p>elsewhere</p>"))
    with pytest.raises(w.WaybackError, match="outside"):
        run(client.fetch_version(capture))
    assert client.cache.get(URL, capture.timestamp) is None


def test_throttle_spaces_requests():
    clock, slept = [0.0], []

    async def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    throttle = w.Throttle(1.5, clock=lambda: clock[0], sleep=sleep)
    run(throttle.wait())
    clock[0] += 0.4
    run(throttle.wait())
    run(throttle.wait())
    assert slept == [pytest.approx(1.1), pytest.approx(1.5)]


def test_timestamps_round_trip():
    assert w.cdx_timestamp("2024-03-01T12:00:00-05:00") == "20240301170000"
    assert w.parse_timestamp("20240301170000").isoformat() == "2024-03-01T17:00:00+00:00"
