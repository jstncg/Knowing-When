"""What engineers build in public on GitHub: repos they create, repos they star, and recent public activity.

Free REST API (60 requests an hour without GITHUB_TOKEN, 5,000 with one); ``profile`` and ``search_owners`` serve
the watchlist's admit step (app/admit.py). Per person:
  /users/{login}/repos        their own repos, newest first      -> github_repo at created_at, or when
                                                                   a PublicEvent made it public
  /users/{login}/starred      with starred_at                    -> github_star at starred_at
  /users/{login}/events/public  the last 90 days, at most 300    -> github_activity at created_at
Forks, stars of their own repos, and the events that repeat a repo or a star, are left out.
observed_at is when the act became public, as for posts. Quotes follow the post reader's shape:
the person's own words first, then "In reply to ..." for a comment on someone else's issue.
A repo created private and opened later is dated by its creation unless the opening is in the
last 90 days of events, and a repo's description is today's, so a replay of past windows reads
stars only (detectors.REPLAY_TYPES); a star's quote is the repo's name, never today's description.
"""

import httpx

from ..detectors import automated_release
from ..models import parse_time
from .social import in_window, post_event

API = "https://api.github.com"
PER_PAGE = 100
EVENT_LIMIT = 300  # GitHub serves at most 300 public events
TEXT_LIMIT = 300
EXTRACTOR = "github_v2"  # github_v1 quoted a star with today's description (detectors.RETIRED_SOURCES)


def _headers(token):
    return {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {token}"} if token else {})}


def profile(login, token="", http=None):
    """What a user says about themselves on their GitHub profile, in two free calls: {name, bio, blog, company,
    twitter_username, type, social}, type "User" or "Organization", social the URLs of the accounts they linked."""
    http = http or httpx.Client(timeout=30)
    out = []
    for path in (f"/users/{login}", f"/users/{login}/social_accounts"):
        response = http.get(API + path, headers=_headers(token))
        response.raise_for_status()
        out.append(response.json())
    user, social = (o if isinstance(o, t) else t() for o, t in zip(out, (dict, list)))
    return {k: str(user.get(k) or "") for k in ("name", "bio", "blog", "company", "twitter_username", "type")} | \
        {"social": [str(a["url"]) for a in social if isinstance(a, dict) and a.get("url")]}


def search_owners(query, since, token="", http=None, min_stars=10, limit=50):
    """People (never organizations) whose own repos match ``query`` and were pushed to since ``since``, most starred
    first: [{github, evidence_urls, source}], one per person. One free call to GitHub's search (10 a minute without a
    token, 30 with)."""
    http = http or httpx.Client(timeout=30)
    response = http.get(API + "/search/repositories", headers=_headers(token), params={
        "q": f"{query} in:name,description,readme stars:>={min_stars} pushed:>={since} fork:false",
        "sort": "stars", "per_page": limit})
    response.raise_for_status()
    found = {}
    for repo in response.json().get("items") or []:
        if (owner := repo.get("owner") or {}).get("type") == "User":
            found.setdefault(owner["login"], []).append(repo["html_url"])
    return [{"github": login, "evidence_urls": urls, "source": f"GitHub search: {query}"} for login, urls in found.items()]


def fetch(login, per_person, token="", http=None, starred=True):
    """The three lists for one login, each capped at per_person items (events at 300); ``starred=False`` skips the
    stars (someone else's work) for a reader that never counts them.

    Direct httpx, not sources.http.Fetcher: its disk cache never expires, which a watch
    must not read from, and it cannot send the per-request Accept header starred_at needs."""
    headers = _headers(token)
    http = http or httpx.Client(timeout=30)

    def pages(path, limit, accept=None, params=None):
        out = []
        for page in range(1, -(-limit // PER_PAGE) + 1):
            response = http.get(API + path, params={"per_page": PER_PAGE, "page": page, **(params or {})},
                                headers=headers | ({"Accept": accept} if accept else {}))
            response.raise_for_status()
            batch = response.json()
            out += batch
            if len(batch) < PER_PAGE or len(out) >= limit:
                break
        return out[:limit]

    return {"repos": pages(f"/users/{login}/repos", per_person, params={"sort": "created"}),
            "starred": pages(f"/users/{login}/starred", per_person, accept="application/vnd.github.star+json")
            if starred else [],
            "events": pages(f"/users/{login}/events/public", EVENT_LIMIT)}


def _about(repo):
    return repo["full_name"] + (f": {repo['description']}" if repo.get("description") else "") + \
        (f" ({repo['language']})" if repo.get("language") else "")


def _activity(event):
    """(quote, url) for the public events that say what the person worked on; None for the rest."""
    kind, payload, repo = event["type"], event.get("payload") or {}, event["repo"]["name"]
    url = f"https://github.com/{repo}"
    if kind == "PushEvent":
        messages = [c["message"].splitlines()[0] for c in payload.get("commits") or [] if c.get("message")]
        return f"Pushed to {repo}" + (f": {'; '.join(messages)[:TEXT_LIMIT]}" if messages else ""), url
    if kind == "PullRequestEvent" and (pr := payload.get("pull_request")):
        return f"{payload.get('action', 'updated').capitalize()} pull request on {repo}: {pr.get('title', '')}", \
            pr.get("html_url") or url
    if kind == "IssuesEvent" and (issue := payload.get("issue")):
        return f"{payload.get('action', 'updated').capitalize()} issue on {repo}: {issue.get('title', '')}", \
            issue.get("html_url") or url
    if kind in ("IssueCommentEvent", "PullRequestReviewCommentEvent") and (comment := payload.get("comment")):
        about = (payload.get("issue") or payload.get("pull_request") or {}).get("title", "")
        return f"{(comment.get('body') or '').strip()[:TEXT_LIMIT]}\n\nIn reply to {repo}: {about}", \
            comment.get("html_url") or url
    if kind == "ReleaseEvent" and (release := payload.get("release")):
        said = f"Released {release.get('tag_name', '')} of {repo}: {release.get('name') or ''}".rstrip(": ")
        return None if automated_release(said) else (said, release.get("html_url") or url)  # a build, not a launch
    if kind == "ForkEvent":
        return f"Forked {repo}", url
    return None  # CreateEvent and WatchEvent repeat repos and stars; the rest say little


def events(person, raw):
    """github_repo, github_star and github_activity events for one person inside their window."""
    opened = {e["repo"]["name"]: e["created_at"] for e in raw.get("events", []) if e.get("type") == "PublicEvent"}
    found = [("github_repo", opened.get(repo["full_name"], repo["created_at"]), repo["html_url"], "Created " + _about(repo))
             for repo in raw.get("repos", []) if not repo.get("fork")]
    found += [("github_star", star["starred_at"], star["repo"]["html_url"], "Starred " + star["repo"]["full_name"])
              for star in raw.get("starred", []) if "starred_at" in star
              and star["repo"]["full_name"].split("/")[0].lower() != person.github.lower()]
    found += [("github_activity", event["created_at"], said[1], said[0])
              for event in raw.get("events", []) if (said := _activity(event))]
    return [post_event(person, kind, when, url, quote, EXTRACTOR)
            for kind, at, url, quote in found if in_window(when := parse_time(at), person)]
