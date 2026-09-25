"""Polite fetching for public data sources, with raw responses cached on disk.

Every fetched body lands under a gitignored directory keyed by the URL hash,
so a parser can be re-run (or a fixture recorded) without another request.
Requests to one host are spaced out, and the User-Agent says who we are in
the "Mozilla/5.0 (compatible; ...)" form crawlers use: some WAF rules choke
on anything that does not start with Mozilla.
"""

import asyncio
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from ..models import iso

USER_AGENT = "Mozilla/5.0 (compatible; GITimingEngine/0.1; +https://github.com/jstncg/Knowing-When)"
CACHE_DIR = Path(os.getenv("SOURCE_CACHE_DIR", "data/source_cache"))
MIN_INTERVAL_SECONDS = 2.0
# Hosts with a stated limit: arXiv asks for 3s between requests; SEC allows 10/s per organization; the Wayback
# Machine asks for gentle use. OpenAlex now meters a daily budget of credits and answered a paged search 0.15 s
# apart with 429s (Justin's Mac, 2026-09-24), so one request a second.
HOST_INTERVAL_SECONDS = {"export.arxiv.org": 5.0, "api.openalex.org": 1.0, "www.sec.gov": 0.2,
                         "web.archive.org": 1.5}
RETRY_AFTER_SECONDS = 10.0
RETRIES = 3  # pauses of 10, 20, 40 s, or longer when the server's Retry-After says so
MAX_RETRY_WAIT = 120.0  # a Retry-After longer than this (a spent daily budget) ends the retries at once
# Limits apply to the service, not each hostname: arXiv listings and abstract pages share
# one budget, as do SEC's archive, submissions and full-text search hosts.
HOST_ALIASES = {"arxiv.org": "export.arxiv.org", "data.sec.gov": "www.sec.gov", "efts.sec.gov": "www.sec.gov"}


class Throttle:
    """Keeps min_interval seconds between requests, for sync and async callers alike.

    Each caller reserves its slot under the lock, then sleeps outside it.
    """

    def __init__(self, min_interval: float, clock=time.monotonic, sleep=asyncio.sleep):
        self.min_interval, self.clock, self.sleep = min_interval, clock, sleep
        self._lock = threading.Lock()
        self._next = None

    def _reserve(self) -> float:
        with self._lock:
            now = self.clock()
            start = now if self._next is None else max(now, self._next)
            self._next = start + self.min_interval
            return start - now

    def wait_sync(self) -> None:
        if delay := self._reserve():
            time.sleep(delay)

    async def wait(self) -> None:
        if delay := self._reserve():
            await self.sleep(delay)


_hosts: dict[str, Throttle] = {}
_hosts_lock = threading.Lock()


def host_throttle(host: str) -> Throttle:
    """One throttle per service for the whole process, shared by every client that calls it."""
    host = HOST_ALIASES.get(host, host)
    with _hosts_lock:
        return _hosts.setdefault(host, Throttle(HOST_INTERVAL_SECONDS.get(host, MIN_INTERVAL_SECONDS)))


def stated_throttle(host: str) -> Throttle | None:
    """The shared throttle of a host that publishes a rate limit; None for any other host."""
    return host_throttle(host) if HOST_ALIASES.get(host, host) in HOST_INTERVAL_SECONDS else None


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def retry_after(response) -> float | None:
    """The seconds a response's Retry-After asks for, as a number or an HTTP date; None when it gives none."""
    value = (response.headers.get("retry-after") or "").strip()
    if value.isdigit():
        return float(value)
    try:
        return max((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return None


def content_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def decode(body: bytes) -> str:
    """State files are UTF-8 (often with a BOM) or Windows-1252; never crash on an accent."""
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


def cache_path(url: str, cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / hashlib.sha256(url.encode()).hexdigest()[:24]


class Fetcher:
    """Callable that returns a response body, from cache when it has one.

    `fetch(url)` returns bytes. `fetch.meta(url)` returns the stored
    metadata (fetched_at, content_type, content_hash) so callers can cite
    exactly which version of a page they parsed.
    """

    def __init__(self, cache_dir: Path = CACHE_DIR, *, refresh=False, client=None):
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                     "Accept-Language": "en-US,en;q=0.9"},
            timeout=60, follow_redirects=True,
        )

    def __call__(self, url: str) -> bytes:
        path = cache_path(url, self.cache_dir)
        if not self.refresh and path.with_suffix(".json").exists():
            return path.read_bytes()
        response = self._request(url)
        body = response.content
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.with_suffix(".json").write_text(json.dumps({
            "url": url, "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "content_hash": content_hash(body), "fetched_at": iso(),
            "ratelimit_remaining": _number(response.headers.get("x-ratelimit-remaining")),  # OpenAlex's credits left
        }, indent=1))
        return body

    def _request(self, url):
        """Retry a server error or rate limit with growing pauses, never shorter than the server's Retry-After;
        client errors are final, and so is a Retry-After past MAX_RETRY_WAIT."""
        for attempt in range(RETRIES + 1):
            response = self._send(url)
            # 406 is how arXiv's API sheds load; treat it like a rate limit.
            if not (response.status_code >= 500 or response.status_code in (406, 429)) or attempt == RETRIES:
                break
            asked = retry_after(response)
            if asked is not None and asked > MAX_RETRY_WAIT:
                break
            time.sleep(max(asked or 0, RETRY_AFTER_SECONDS * 2 ** attempt))
        response.raise_for_status()
        return response

    def _send(self, url):
        host_throttle(httpx.URL(url).host or "").wait_sync()
        return self.client.get(url)

    def meta(self, url: str) -> dict:
        path = cache_path(url, self.cache_dir).with_suffix(".json")
        return json.loads(path.read_text()) if path.exists() else {}
