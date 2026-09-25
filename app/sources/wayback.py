"""Dated page history through the Internet Archive (Wayback Machine).

Two public, unauthenticated endpoints (archive.org/developers/wayback-cdx-server.html):
- CDX index:   https://web.archive.org/cdx/search/cdx?url=<url>&output=json&...
- Raw capture: https://web.archive.org/web/<timestamp>id_/<original>
  (`id_` returns the archived bytes without the Wayback toolbar or link rewriting.)

A capture is a point-in-time observation: the archive timestamp is when the page
was verifiably public with that content, so it is the `observed_at` the timeline
uses for leakage control. Missing captures mean coverage is unknown, never that
nothing changed. Nothing here writes to the store or contacts anyone.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from ..models import iso, parse_time, public_url, utcnow
from ..providers import MAX_BYTES, MAX_TEXT
from .http import USER_AGENT, Throttle, host_throttle

CDX_URL = "https://web.archive.org/cdx/search/cdx"
CAPTURE_URL = "https://web.archive.org/web/{timestamp}id_/{original}"
ARCHIVE_HOST = "web.archive.org"
DEFAULT_CACHE_DIR = Path("data/wayback")  # data/ is gitignored
RETRY_STATUSES = {429, 502, 503, 504}
# Mirrors providers.fetch_document so an archived version hashes like a live read.
STRIP_TAGS = ["script", "style", "noscript", "template", "nav", "footer", "form"]


class WaybackError(Exception):
    """User-safe diagnostic; never contains archive response bodies."""


def cdx_timestamp(value: str | datetime) -> str:
    """14-digit UTC timestamp (YYYYMMDDhhmmss) the archive uses everywhere."""
    moment = parse_time(value) if isinstance(value, str) else value.astimezone(timezone.utc)
    return moment.strftime("%Y%m%d%H%M%S")


def parse_timestamp(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _year(value: str | datetime) -> int:
    return (parse_time(value) if isinstance(value, str) else value).year


def extract_text(body: str, content_type: str) -> tuple[str, str]:
    """Title and plain text, extracted exactly as providers.fetch_document does."""
    if "html" not in content_type:
        return "", re.sub(r"\s+", " ", body).strip()
    soup = BeautifulSoup(body, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(STRIP_TAGS):
        tag.decompose()
    return title, re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


@dataclass(frozen=True)
class Capture:
    url: str  # the URL asked about; `original` is the variant the archive stored
    timestamp: str
    original: str
    digest: str
    mimetype: str = ""

    @property
    def captured_at(self) -> str:
        return iso(parse_timestamp(self.timestamp))

    @property
    def source_url(self) -> str:
        return CAPTURE_URL.format(timestamp=self.timestamp, original=self.original)


@dataclass(frozen=True)
class PageVersion:
    """Store with timeline.source_version(store, v.url, v.text, v.observed_at)."""

    url: str
    capture_at: str
    # Same as capture_at on purpose: the archive timestamp is the point-in-time
    # observation of this content, not the moment we pulled it from the archive.
    observed_at: str
    content_hash: str  # sha256 of `text`, as timeline.source_version computes it
    text: str
    source_url: str
    title: str = ""
    truncated: bool = False

    def record(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class VersionDiff:
    added: list[str]
    removed: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


@dataclass
class Coverage:
    """Captures per year for one URL. A zero is a known gap; `error` means unknown."""

    url: str
    captures_per_year: dict[int, int] = field(default_factory=dict)
    first_capture_at: str | None = None
    last_capture_at: str | None = None
    error: str | None = None


def diff_versions(old: PageVersion, new: PageVersion) -> VersionDiff:
    """Added and removed phrases between two versions, word-aligned.

    Text is whitespace-collapsed, so words are the stable unit. A changed
    headline or a new "joined X" sentence comes back as one phrase each.
    """
    a, b = old.text.split(), new.text.split()
    added, removed = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            removed.append(" ".join(a[i1:i2]))
        if tag in ("insert", "replace"):
            added.append(" ".join(b[j1:j2]))
    return VersionDiff(added, removed)


class VersionCache:
    """Fetched captures on disk, keyed by (url, capture timestamp); each is fetched once."""

    def __init__(self, root: Path | str = DEFAULT_CACHE_DIR):
        self.root = Path(root)

    def _path(self, url: str, timestamp: str) -> Path:
        return self.root / hashlib.sha256(url.encode()).hexdigest()[:16] / f"{timestamp}.json"

    def get(self, url: str, timestamp: str) -> PageVersion | None:
        path = self._path(url, timestamp)
        return PageVersion(**json.loads(path.read_text())) if path.exists() else None

    def put(self, version: PageVersion) -> None:
        path = self._path(version.url, cdx_timestamp(version.capture_at))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(version.record(), ensure_ascii=False))
        tmp.replace(path)


class WaybackClient:
    """Polite CDX + capture client. Use as `async with WaybackClient() as wb:`.

    Every client shares the process's web.archive.org throttle, so opening one
    per person does not reset the spacing; tests pass their own `throttle`.
    """

    def __init__(
        self,
        *,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        throttle: Throttle | None = None,
    ):
        self.cache = VersionCache(cache_dir)
        self._throttle = throttle or host_throttle(ARCHIVE_HOST)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _attempt(self, request: httpx.Request, stream: bool) -> httpx.Response:
        await self._throttle.wait()
        try:
            return await self._client.send(request, stream=stream)
        except httpx.HTTPError as e:
            raise WaybackError(f"Wayback request failed: {type(e).__name__}") from None

    async def _send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        response = await self._attempt(request, stream)
        if response.status_code in RETRY_STATUSES:  # one polite retry, then give up
            await response.aclose()
            retry_after = response.headers.get("retry-after", "")
            await self._throttle.sleep(int(retry_after) if retry_after.isdigit() else 5)
            response = await self._attempt(request, stream)
        if response.url.host != ARCHIVE_HOST or response.status_code != 200:
            await response.aclose()
            if response.url.host != ARCHIVE_HOST:
                raise WaybackError("Archive redirected outside web.archive.org.")
            raise WaybackError(f"Wayback returned HTTP {response.status_code}.")
        return response

    async def _cdx(self, url: str, **params: str) -> list[dict]:
        query = {
            "url": public_url(url),
            "output": "json",
            "fl": "timestamp,original,mimetype,statuscode,digest",
            "filter": "statuscode:200",
            **params,
        }
        response = await self._send(self._client.build_request("GET", CDX_URL, params=query))
        try:
            rows = json.loads(response.text) if response.text.strip() else []
        except ValueError:
            rows = None
        # A maintenance page served with HTTP 200 is not an empty index.
        if not isinstance(rows, list) or not all(isinstance(row, list) for row in rows):
            raise WaybackError("CDX returned a non-JSON response; coverage is unknown.")
        header, *body = rows or [[]]
        return [dict(zip(header, row)) for row in body]

    async def list_captures(
        self,
        url: str,
        *,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        limit: int | None = None,
    ) -> list[Capture]:
        """Successful captures, oldest first, one per distinct archive digest.

        The first capture of each digest is kept: that is when the content became
        observable. A later flip back to old content is therefore not a version.
        """
        params = {"collapse": "digest"}  # server collapses adjacent repeats; we finish the job
        if since:
            params["from"] = cdx_timestamp(since)
        if until:
            params["to"] = cdx_timestamp(until)
        if limit:
            params["limit"] = str(limit)
        captures, seen = [], set()
        for row in await self._cdx(url, **params):
            if row["digest"] in seen:
                continue
            seen.add(row["digest"])
            captures.append(Capture(url, row["timestamp"], row["original"], row["digest"], row.get("mimetype", "")))
        return captures

    async def capture_as_of(self, url: str, as_of: str | datetime) -> Capture | None:
        """Latest successful capture at or before as_of, or None when the archive has none."""
        rows = await self._cdx(url, to=cdx_timestamp(as_of), limit="-5")
        if not rows:
            return None
        row = max(rows, key=lambda r: r["timestamp"])
        return Capture(url, row["timestamp"], row["original"], row["digest"], row.get("mimetype", ""))

    async def fetch_version(self, capture: Capture) -> PageVersion:
        """Archived page text for one capture; fetched from the archive at most once."""
        if cached := self.cache.get(capture.url, capture.timestamp):
            return cached
        request = self._client.build_request("GET", capture.source_url)
        response = await self._send(request, stream=True)
        try:
            content_type = response.headers.get("content-type", "").lower()
            if not any(t in content_type for t in ("text/", "xml", "json")):
                raise WaybackError("Capture is not a text/HTML document.")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise WaybackError("Capture exceeds the 2 MB retrieval limit.")
                chunks.append(chunk)
        finally:
            await response.aclose()
        title, text = extract_text(b"".join(chunks).decode("utf-8", errors="replace"), content_type)
        version = PageVersion(
            url=capture.url,
            capture_at=capture.captured_at,
            observed_at=capture.captured_at,
            content_hash=hashlib.sha256(text[:MAX_TEXT].encode()).hexdigest(),
            text=text[:MAX_TEXT],
            source_url=capture.source_url,
            title=title,
            truncated=len(text) > MAX_TEXT,
        )
        self.cache.put(version)
        return version

    async def page_as_of(self, url: str, as_of: str | datetime) -> PageVersion | None:
        capture = await self.capture_as_of(url, as_of)
        return await self.fetch_version(capture) if capture else None

    async def history(
        self,
        url: str,
        *,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        limit: int | None = None,
    ) -> list[PageVersion]:
        """Distinct text versions in order; markup-only churn collapses on content_hash."""
        versions, seen = [], set()
        for capture in await self.list_captures(url, since=since, until=until, limit=limit):
            version = await self.fetch_version(capture)
            if version.content_hash not in seen:
                seen.add(version.content_hash)
                versions.append(version)
        return versions

    async def coverage(
        self,
        urls: list[str],
        *,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
    ) -> list[Coverage]:
        """Successful captures per year, zero-filled across the span so gaps are visible."""
        params = {"fl": "timestamp"}
        if since:
            params["from"] = cdx_timestamp(since)
        if until:
            params["to"] = cdx_timestamp(until)
        end_year = _year(until) if until else utcnow().year
        reports = []
        for url in urls:
            report = Coverage(url)
            try:
                stamps = sorted(row["timestamp"] for row in await self._cdx(url, **params))
            except (WaybackError, ValueError) as e:
                report.error = str(e)
            else:
                if stamps:
                    report.first_capture_at = iso(parse_timestamp(stamps[0]))
                    report.last_capture_at = iso(parse_timestamp(stamps[-1]))
                start = _year(since) if since else int(stamps[0][:4]) if stamps else end_year
                report.captures_per_year = {year: 0 for year in range(start, end_year + 1)}
                for stamp in stamps:
                    report.captures_per_year[int(stamp[:4])] += 1
            reports.append(report)
        return reports
