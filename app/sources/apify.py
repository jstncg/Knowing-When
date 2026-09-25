"""What people post, reply and comment on X and LinkedIn, through Apify actors.

Three pay-per-result actors, one account and token (APIFY_TOKEN):
  X_SEARCH           apidojo/tweet-scraper, X's own search syntax. The search
                     "from:handle since:D until:D" returns the person's tweets
                     and replies; $0.40 per 1,000, each search billed at least 50.
                     A search with until: can come back as {"noResults": true}
                     rows although the window has tweets (seen on 2026-09-23:
                     two accounts with 4 and 5 tweets in the window came back
                     empty, and the same search without until: found them), so
                     people it left empty or thin are re-asked without it
                     (open_runs); the parser cuts at until and drops repeats.
                     A daily pull asks for everyone at once (x_together_inputs).
  LINKEDIN_POSTS     harvestapi/linkedin-profile-posts, a profile's own posts
                     and quote posts; about $2 per 1,000; no login or cookie.
  LINKEDIN_COMMENTS  harvestapi/linkedin-profile-comments, the comments a
                     profile left on other people's posts; $2 per 1,000.

Each parser is a pure function from an actor's items to TimelineEvent dicts
(x_post, x_reply, linkedin_post, linkedin_comment) for the people asked
about. An item is admitted only when its author is the handle or profile the
person was asked under, so a retweet, a repost or another person's reply never
lands on their timeline. observed_at is the publish time: a post counts from
when it became public, which keeps a historical window replayable.
"""

import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

import httpx

from ..models import parse_time
from .social import in_window, linkedin_url, post_event, responding

API = "https://api.apify.com/v2"
X_SEARCH = "apidojo~tweet-scraper"
LINKEDIN_POSTS = "harvestapi~linkedin-profile-posts"
LINKEDIN_COMMENTS = "harvestapi~linkedin-profile-comments"
LINKEDIN_SEARCH = "harvestapi~linkedin-post-search"
# List prices per item, read on apify.com on 2026-09-23 (post search assumed to match its sibling actors).
PRICE = {X_SEARCH: 0.0004, LINKEDIN_POSTS: 0.002, LINKEDIN_COMMENTS: 0.002, LINKEDIN_SEARCH: 0.002}
# Newest first, an open-ended re-ask reads everything after the window before reaching it.
OPEN_CAP_FACTOR = 4
X_MIN_PER_SEARCH = 50
X_QUERY_CHARS = 400  # a combined search's length, kept under the 512 characters X's search API allows
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


class ApifyError(RuntimeError):
    pass


# ------------------------------------------------------------------ client

class Client:
    """Starts an actor run, waits for it, and returns its dataset items.

    max_usd caps what one run may charge (Apify's maxTotalChargeUsd); the
    caller decides it, so no run spends without a stated limit.
    """

    def __init__(self, token, http=None, poll_seconds=10):
        if not token:
            raise ApifyError("APIFY_TOKEN is not set")
        self.http = http or httpx.AsyncClient(timeout=120, headers={"Authorization": f"Bearer {token}"})
        self.poll_seconds = poll_seconds
        self.undeleted = []  # datasets run(keep=False) could not delete
        self.charged = []  # what each finished run's own record says it cost; kept to check prices, never capped on

    async def _call(self, method, path, **kwargs):
        response = await self.http.request(method, API + path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def actor(self, actor):
        """(the actor's record, its latest build): its pricing and input schema, read free; nothing runs."""
        data = (await self._call("GET", f"/acts/{actor}"))["data"]
        build_id = ((data.get("taggedBuilds") or {}).get("latest") or {}).get("buildId")
        return data, (await self._call("GET", f"/actor-builds/{build_id}"))["data"] if build_id else {}

    async def run(self, actor, actor_input, *, max_items, max_usd, keep=True):
        """The run's items. ``keep=False`` deletes its dataset on Apify once read, so what was read is left nowhere
        but here; a delete that fails does not lose the items, and its dataset id goes on ``undeleted``."""
        started = await self._call("POST", f"/acts/{actor}/runs", json=actor_input,
                                   params={"maxItems": max_items, "maxTotalChargeUsd": max_usd, "waitForFinish": 60})
        run = started["data"]
        try:
            while run["status"] not in TERMINAL:
                await asyncio.sleep(self.poll_seconds)
                run = (await self._call("GET", f"/actor-runs/{run['id']}", params={"waitForFinish": 60}))["data"]
        finally:  # a run lost while polling may still be charging: it goes in as last seen
            self.charged.append({"actor": actor, "run": run.get("id"), "status": run.get("status"),
                                 "usd": run.get("usageTotalUsd"), "events": run.get("chargedEventCounts") or {}})
        if run["status"] != "SUCCEEDED":
            raise ApifyError(f"{actor} run {run['id']} ended {run['status']}")
        items = await self._call("GET", f"/datasets/{run['defaultDatasetId']}/items",
                                 params={"clean": "true", "format": "json"})
        if not keep:
            try:
                (await self.http.request("DELETE", f"{API}/datasets/{run['defaultDatasetId']}")).raise_for_status()
            except httpx.HTTPError:
                self.undeleted.append(run["defaultDatasetId"])
        return items


# ------------------------------------------------------------------ inputs

def x_input(person, per_person):
    """One search per person, so a prolific poster cannot use up everyone else's items."""
    window = (f" since:{person.since}" if person.since else "") + (f" until:{person.until}" if person.until else "")
    return {"searchTerms": [f"from:{person.x_handle}{window}"], "sort": "Latest", "maxItems": per_person}


def linkedin_posts_input(people, per_person):
    """The earliest since across the group; each person's own window is cut again when parsed."""
    since = min((p.since for p in people if p.since), default="")
    return {"targetUrls": [p.linkedin_url for p in people], "maxPosts": per_person,
            "includeReposts": False, "includeQuotePosts": True} | ({"postedLimitDate": since} if since else {})


def linkedin_comments_input(people, per_person, posted_limit="any"):
    """posted_limit is the actor's relative filter (24h, week, month, ..., any). It cannot reach a past
    window, so people with an until date are not asked (see runs)."""
    return {"profiles": [p.linkedin_url for p in people], "maxItems": per_person, "postedLimit": posted_limit}


def runs(people, per_person, comments_limit=None):
    """(actor, input, item cap, worst-case $) per run: X once per person, LinkedIn posts once, and LinkedIn
    comments once when comments_limit is given. Comments go back from today only, so people with a past
    window (an until date) are left out of that run."""
    out = [(X_SEARCH, x_input(p, per_person), per_person, PRICE[X_SEARCH] * max(per_person, X_MIN_PER_SEARCH))
           for p in people if p.x_handle]
    if li := [p for p in people if p.linkedin_url]:
        out.append((LINKEDIN_POSTS, linkedin_posts_input(li, per_person), per_person * len(li),
                    PRICE[LINKEDIN_POSTS] * per_person * len(li)))
    if comments_limit and (now := [p for p in people if p.linkedin_url and not p.until]):
        out.append((LINKEDIN_COMMENTS, linkedin_comments_input(now, per_person, comments_limit), per_person * len(now),
                    PRICE[LINKEDIN_COMMENTS] * per_person * len(now)))
    return out


def x_together_inputs(people, since, cap):
    """Everyone's recent tweets in as few searches as fit X_QUERY_CHARS: "(from:a OR from:b ...) since:D", newest
    first, cap items each shared by everyone in it. Each search is billed at least X_MIN_PER_SEARCH, so one search
    for the whole watchlist costs what one person's does; x_events routes each tweet to its author."""
    groups = []
    for handle in sorted({p.x_handle.lower() for p in people if p.x_handle}):
        if groups and len(_either(groups[-1] + [handle], since)) <= X_QUERY_CHARS:
            groups[-1].append(handle)
        else:
            groups.append([handle])
    return [{"searchTerms": [_either(g, since)], "sort": "Latest", "maxItems": cap} for g in groups]


def _either(handles, since):
    return "(" + " OR ".join(f"from:{h}" for h in handles) + f") since:{since}"


def together_runs(people, since, x_cap, linkedin_per_person):
    """(actor, input, item cap, worst-case $) for a daily pull: the combined X searches (x_together_inputs) and one
    LinkedIn posts run for everyone, both from since."""
    out = [(X_SEARCH, x, x_cap, PRICE[X_SEARCH] * max(x_cap, X_MIN_PER_SEARCH))
           for x in x_together_inputs(people, since, x_cap)]
    if li := [replace(p, since=since) for p in people if p.linkedin_url]:
        out.append((LINKEDIN_POSTS, linkedin_posts_input(li, linkedin_per_person), linkedin_per_person * len(li),
                    PRICE[LINKEDIN_POSTS] * linkedin_per_person * len(li)))
    return out


def search_input(queries, max_posts, posted_limit):
    """LinkedIn post search, newest first; written against the actor's published example (the first run confirms it)."""
    return {"searchQueries": queries, "maxPosts": max_posts, "postedLimit": posted_limit, "sortBy": "date"}


def search_hits(items):
    """Who posted each search hit: name, profile, headline, date, link and the start of the text."""
    hits = []
    for item in items:
        author = item.get("author") or {}
        when, profiles = _posted(item), _profiles(author)
        if profiles and when and item.get("linkedinUrl"):
            hits.append({"name": author.get("name", ""), "linkedin_url": profiles[0],
                         "headline": author.get("info") or author.get("position") or "",
                         "posted_at": when.isoformat(), "post_url": item["linkedinUrl"],
                         "text": (item.get("content") or "")[:500]})
    return hits


def estimate_usd(planned):
    return round(sum(r[3] for r in planned), 2)


# ----------------------------------------------------------------- parsers

def _when(value):
    """An actor's timestamp (ISO, X's "Wed Sep 24 18:06:27 +0000 2025", or epoch ms) as UTC; None if unreadable."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_time(value)
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc)
    except ValueError:
        return None


def x_events(people, items):
    """x_post and x_reply events, routed to people by the tweet's author handle.

    Returns (events, rejected): a count per reason an item was dropped.
    A reply to their own tweet continues a thread, so it is a post.
    """
    by_handle = {p.x_handle.lower(): p for p in people if p.x_handle}
    events, rejected, seen = [], Counter(), set()
    for item in items:
        if item.get("noResults"):
            rejected["no_results"] += 1
            continue
        if (tweet := item.get("id") or item.get("url")) in seen:
            rejected["repeat"] += 1
            continue
        seen.add(tweet)
        person = by_handle.get(((item.get("author") or {}).get("userName") or "").lower())
        text = item.get("fullText") or item.get("text") or ""
        when = _when(item.get("createdAt"))
        if reason := _dropped(person, when, text, item.get("url"),
                              item.get("isRetweet") or text.startswith("RT @")):
            rejected[reason] += 1
            continue
        quoted = (item.get("quote") or {}) if item.get("isQuote") else {}
        to = item.get("inReplyToUsername")
        reply = bool(item.get("isReply")) and (to or "").lower() != person.x_handle.lower()
        if reply:
            quote = responding(text, f"@{to}" if to else "a post", "")  # search results carry no parent text
        elif quoted.get("text"):
            by = (quoted.get("author") or {}).get("userName")
            quote = responding(text, f"@{by}" if by else "a post", quoted["text"])
        else:
            quote = text
        events.append(post_event(person, "x_reply" if reply else "x_post", when, item["url"],
                             quote, "apify_x_v1"))
    return events, rejected


def _dropped(person, when, text, url, shared):
    """Why an item is not an event for the person asked about, or None."""
    if not person:
        return "not_asked_author"
    if shared:
        return "repost"
    if not text or not url or not when:
        return "incomplete"
    return None if in_window(when, person) else "outside_window"


def _profiles(block):
    """The profile URLs an author block names: by public identifier first, since an actor may give an opaque id URL."""
    block = block or {}
    ident = block.get("publicIdentifier")
    return [u for u in (ident and linkedin_url("https://www.linkedin.com/in/" + ident),
                        linkedin_url(block.get("linkedinUrl") or "")) if u]


def _author(by_url, block):
    return next((by_url[u] for u in _profiles(block) if u in by_url), None)


def _posted(item):
    posted = item.get("postedAt") or {}
    return _when(posted.get("date") or posted.get("timestamp")) if isinstance(posted, dict) else _when(posted)


def posted_day(item):
    """The UTC day an X or LinkedIn post item was published; "" when it has no readable time."""
    when = _when(item.get("createdAt")) or _posted(item)
    return when.date().isoformat() if when else ""


def linkedin_post_events(people, items):
    """linkedin_post events for the person whose profile wrote each post; reposts and others' posts are rejected."""
    by_url = {p.linkedin_url: p for p in people if p.linkedin_url}
    events, rejected = [], Counter()
    for item in items:
        person = _author(by_url, item.get("author"))
        when = _posted(item)
        text = item.get("content") or ""
        if reason := _dropped(person, when, text, item.get("linkedinUrl"), item.get("repostedBy")):
            rejected[reason] += 1
            continue
        shared = item.get("repost") or {}  # a quote post: their words on top of someone else's post
        quote = responding(text, (shared.get("author") or {}).get("name") or "a post",
                            shared.get("content") or "") if shared else text
        events.append(post_event(person, "linkedin_post", when, item["linkedinUrl"], quote, "apify_linkedin_v1"))
    return events, rejected


def linkedin_comment_events(people, items):
    """linkedin_comment events: the person's own comment, then the post it was left on when the actor returns it."""
    by_url = {p.linkedin_url: p for p in people if p.linkedin_url}
    events, rejected = [], Counter()
    for item in items:
        person = _author(by_url, item.get("actor"))
        when = _when(item.get("createdAt") or item.get("createdAtTimestamp"))
        text = item.get("commentary") or ""
        if reason := _dropped(person, when, text, item.get("linkedinUrl"), False):
            rejected[reason] += 1
            continue
        post = item.get("post") or {}
        quote = responding(text, (post.get("author") or {}).get("name") or "a post", post.get("content") or "")
        events.append(post_event(person, "linkedin_comment", when, item["linkedinUrl"], quote, "apify_linkedin_v1"))
    return events, rejected


def thin(people, items, per_person=X_MIN_PER_SEARCH):
    """People with an end date whose windowed X search returned fewer than X_MIN_PER_SEARCH of their tweets,
    or fewer than per_person when that is lower (the caller leaves out searches that failed outright,
    which are reported, not re-asked)."""
    counts = Counter(((i.get("author") or {}).get("userName") or "").lower() for i in items if not i.get("noResults"))
    enough = min(per_person, X_MIN_PER_SEARCH)
    return [p for p in people if p.x_handle and p.until and counts[p.x_handle.lower()] < enough]


def short_of_window(people, items, cap):
    """People whose re-ask filled its cap with tweets still after their window: the cap ran out before reaching it."""
    oldest, counts = {}, Counter()
    for item in items:
        handle = ((item.get("author") or {}).get("userName") or "").lower()
        if when := _when(item.get("createdAt")):
            counts[handle] += 1
            if handle not in oldest or when < oldest[handle]:
                oldest[handle] = when
    return [p for p in people if counts[h := p.x_handle.lower()] >= cap and oldest[h].date().isoformat() >= p.until]


def open_runs(people, per_person, budget):
    """Re-ask without until: (the parser cuts at until), up to OPEN_CAP_FACTOR times the usual cap, all within budget."""
    cap = min(OPEN_CAP_FACTOR * per_person, int(budget / (PRICE[X_SEARCH] * max(len(people), 1))))
    if cap < X_MIN_PER_SEARCH:
        return []
    return [(X_SEARCH, x_input(replace(p, until=""), cap), cap, PRICE[X_SEARCH] * cap) for p in people]


def open_worst_usd(people, per_person):
    """What open_runs could add if every windowed X search came back empty."""
    return round(PRICE[X_SEARCH] * OPEN_CAP_FACTOR * per_person * sum(1 for p in people if p.x_handle and p.until), 2)


PARSERS = {X_SEARCH: x_events, LINKEDIN_POSTS: linkedin_post_events, LINKEDIN_COMMENTS: linkedin_comment_events}


async def pull(client, planned, max_usd, concurrency=4):
    """Run the planned runs, a few at a time; returns ({actor: items}, the runs that failed).

    Each run's charge cap is its share of max_usd in proportion to its worst case, so a
    cap below the estimate truncates every run alike. Callers refuse that case up front.
    """
    total = estimate_usd(planned) or 1
    gate = asyncio.Semaphore(concurrency)

    async def one(actor, actor_input, cap, usd):
        async with gate:
            return actor, await client.run(actor, actor_input, max_items=cap,
                                           max_usd=round(max(max_usd * usd / total, 0.05), 2))

    raw, failed = {}, []
    for (actor, actor_input, _, _), result in zip(planned, await asyncio.gather(*(one(*run) for run in planned),
                                                                                return_exceptions=True)):
        if isinstance(result, Exception):  # keep what the other runs paid for
            failed.append({"actor": actor, "input": actor_input, "error": f"{type(result).__name__}: {result}"[:300]})
        else:
            raw.setdefault(result[0], []).extend(result[1])
    return raw, failed


def events(people, raw):
    """Parse every actor's items; returns (events, rejected per actor)."""
    out, rejected = [], {}
    for actor, items in raw.items():
        parsed, rejected[actor] = PARSERS[actor](people, items)
        out += parsed
    return out, rejected
