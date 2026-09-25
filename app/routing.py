"""Routing: when readiness says reach_now, build the ping: the route in, who writes, the channel, confidence, what
would prove the call wrong, and a checked draft.

Everything comes from public professional data and files a person supplies: the GI team roster (who is on the team,
their titles with a source, OpenAlex ids, X handles, GitHub logins, past employers, the roles they write for and
their own work) and each candidate's public contact routes, each with the URL where it was found. No inboxes. Whom
they follow on X comes only from the follows pull (app/sources/x_follows.py).

Route in (ping field 07) names a teammate only when they worked together (confirmed): a teammate coauthored their
public work, or they pushed to or released from a teammate's repo. An answered two-way exchange would also count, but
the teammate's side of a thread is not pulled, so the card lists it as not checked. Otherwise the route line says
"none found" and what was checked, or "not checked" when nothing was. Two other lists sit beside it, never on the
route line:
- leads to check: a teammate was at the same employer or lab in the same years. The card says to ask them.
- warmth: one-way attention, dated and linked. They replied to, quoted or mentioned a teammate on X, commented on a
  teammate's LinkedIn post, opened an issue or pull request on a teammate's repo or commented there, or starred or
  forked one. It can change who writes; the draft never mentions it.

Who writes (Justin, 2026-09-23): the teammate with a confirmed tie; else a writer for the role whom their warmth went
to; else the team file's table (signs_for), by role and, where it matters, seniority. The draft is written as them and
signed with their first name. The channel is per person, not always email: an X DM when they follow the sender, a
public email, the platform they post on most, or their site. The draft is shaped for it: no subject in a DM, a
connection note within LinkedIn's limit.

The draft (ping field 06) is app.outreach's when a writer is passed (paid, fact-checked against its sources). Without
one, or when the run's model calls run out, it is a free template that only copies their words and GI's facts. Either
way app.outreach checks it, and a card whose draft fails a check never goes out. Every draft names the role in one
plain clause; a rapport call leads with the person's own words that opened the moment (readiness.opener).

Confidence and the dated falsifiers (field 05) come from app.ping. The card lists every signal behind the call with
its date, quote and link. It is a Slack Block Kit card whose button opens the draft in the chosen channel. Nothing is
sent from here: a person presses send there. The card itself posts only when a caller passes a webhook and asks to.

Every route carries the contact gate's clearance (contact.check). A card goes out only through send(), which refuses
one the gate holds or asks to check first, one whose draft fails a check, or one past a weekly cap (per role, and
across roles), and records the card in the ledger.
"""

import json
import re
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path as FilePath
from typing import Literal
from urllib.parse import quote, urlencode, urlparse

import httpx
from pydantic import BaseModel, Field, model_validator

from . import contact, happened, journey, outreach, pay, ping, providers, readiness, timeline
from .detectors import FEED, GITHUB_TYPES, LAUNCH_TYPES as LAUNCHES, REPLY_CONTEXT, contacts as talks_to, made_by_hand, \
    READ_TYPES, item, own_words, read_from, their_words, unread
from .happened import Happened
from .models import iso, parse_time
from .role_compiler import role_id as config_role_id
from .sources import x_follows

ROLES = FilePath(__file__).resolve().parents[1] / "config" / "roles.json"
COAUTHOR = re.compile(r"^(?P<name>.+?) \((?P<id>[^()]+)\) on '(?P<title>.+?)'(?: \| at [^|]* \| \w+)?$")
TITLE = outreach.TITLE
REPO = re.compile(r"\b(?P<owner>[A-Za-z0-9-]+)/(?P<name>[A-Za-z0-9_.-]*[A-Za-z0-9_-])")
LEVELS = ("confirmed", "likely")  # a route in; a lead to check
ACTIVE_DAYS = 90  # where they are most active: their own posts in this many days
PLATFORM = {"x_post": "x", "x_reply": "x", "linkedin_post": "linkedin", "linkedin_comment": "linkedin"}
# A title in their own record that makes them senior, for the roles where seniority changes who writes.
# Not "Member of Technical Staff" (a lab's usual title), "Professor Kim's lab" or "lead author".
SENIOR = re.compile(r"\b((assistant |associate |full )?professor (of|at)|as (an? )?(assistant |associate )?professor|"
                    r"principal|staff (research |software |ml |machine learning )?(engineer|scientist|researcher)|"
                    r"head of|director|chief|vp|vice president|co-?founder|founder|senior|"
                    r"(tech|team|research|engineering) lead|lead (engineer|researcher|scientist|designer|developer))\b",
                    re.I)
TITLED = ("job_started", "role_announced", "job_ended", "affiliation_change")
REMEMBERED_YEARS = 3  # work together older than this is a lead to check, not a route (the design's judgment call)
SLACK_URL_MAX = 3000
SLACK_TEXT_MAX = 3000


class Stint(BaseModel):
    org: str
    start: int | None = None  # years; None means unknown
    end: int | None = None


class Teammate(BaseModel):
    name: str = Field(min_length=2)
    title: str = ""
    email: str = ""
    slack_user_id: str = ""
    openalex_ids: list[str] = Field(default_factory=list)
    x_handle: str = ""
    github: str = ""  # login: a repo under it is theirs
    history: list[Stint] = Field(default_factory=list)
    # Role ids they write for, the team's table (Justin, 2026-09-23): "backend", or "mts-research:senior" for
    # senior people only. Never a route in.
    signs_for: list[str] = Field(default_factory=list)
    # Phrases from their own public work, for who is closest to a candidate's.
    works_on: list[str] = Field(default_factory=list)
    about: str = ""  # one sourced sentence on their own work, which a draft they write may say in the first person
    source_url: str = ""  # where the title is from


class ContactRoute(BaseModel):
    kind: Literal["email", "x", "linkedin", "site"]
    value: str = Field(min_length=1)  # address, handle, or URL
    source_url: str = Field(min_length=8)  # the public page where it was found
    x_user_id: str = ""  # numeric id; lets the X button open a filled-in DM
    follows: list[str] | None = None  # on an x route: the team's X handles they follow, from a pull; None: not pulled

    @model_validator(mode="after")
    def _url(self):
        """A LinkedIn or site route is a link the card opens: a bare handle or host becomes one."""
        if self.kind in ("linkedin", "site"):
            value = self.value.strip()
            scheme, _, rest = value.partition("://")
            bare = quote(rest if rest and scheme.lower() in ("http", "https") else value.strip("/@ "), safe="/.-_~%?=&#")
            self.value = f"https://www.linkedin.com/in/{bare}" if self.kind == "linkedin" and "/" not in bare and \
                not rest else f"https://{bare}"
        return self


class Path(BaseModel):
    kind: Literal["coauthor", "commit", "overlap"]
    level: Literal[LEVELS]
    teammate: Teammate
    detail: str  # what ties them, for the card
    tie: str = ""  # the same, said to the teammate who then writes (confirmed paths only)
    latest: str | None = None  # date of the latest shared work or overlapping year
    work: str = ""  # title of the latest shared work
    evidence: list[str] = Field(default_factory=list)  # source URLs or quotes


CHANNELS = {"x_dm": "X direct message", "email": "email", "linkedin": "LinkedIn", "x_request": "X message request",
            "site": "their own site", "none": "none found"}  # a channel in plain words


class Channel(BaseModel):
    kind: Literal["x_dm", "email", "linkedin", "x_request", "site", "none"]
    target: str
    reason: str
    url: str = ""  # opens the draft (or the place to paste it); empty when there is nowhere to go
    check_first: str = ""  # what only the sender can see, to check before sending (a LinkedIn connection)
    source: str = ""  # the page the route was found on (a public email's), for the card


class Route(BaseModel):
    subject_id: str
    name: str
    role: dict
    call: readiness.Readiness
    paths: list[Path]  # confirmed routes in, then leads to check
    warmth: list[dict]  # [{date, what, url, teammate}], newest first: never a route in
    channel: Channel
    # The message shaped for the channel: {to, subject, body, note, after}. note and after (the message once they
    # accept the note) are only for LinkedIn; to is only for email.
    draft: dict
    card: dict
    gate: contact.Clearance  # whether the card may go out: only a clear one is sent
    opener: dict | None = None  # the timeline event the draft quotes, when a reason to write opened the moment
    route_in: dict  # {level: confirmed | none found | not checked, checked, not_checked}
    sender: dict  # {name, title, why, slack_user_id, x_handle, about}: who writes and signs the draft
    outreach: dict  # the message to the person, whatever the channel: {subject, body, note, after, cites, checks, model}
    confidence: dict  # app.ping.confidence
    falsifiers: list[dict]  # app.ping.falsifiers: dated claims the engine checks later
    kind: Literal["early_sign", "just_happened"] | None = None  # happened.kind: what the card is about
    happened: list[Happened] = Field(default_factory=list)  # public moments of the last month, newest first
    signals: list[dict] = Field(default_factory=list)  # routing.signals: what the card lists
    ask: dict | None = None  # a warm intro: the Slack message asking the teammate to reach out, {to, slack_user_id, body}
    # Why the draft never names the role: "undergraduate" or "unconfirmed" (the contact gate), or "acquisition".
    role_free: str = ""
    known: list[dict] = Field(default_factory=list)  # the ledger's "knows" from teammates on the list, newest first
    unlisted: list[dict] = Field(default_factory=list)  # "knows" from a name not on the team list: check who first
    profiles: list[dict] = Field(default_factory=list)  # [{label, url}]: their public profiles the identity check ties
    email: dict | None = None  # {address, source_url}: an address they list publicly; None: no public email found
    message: str = ""  # the circumstance the draft is written for (MESSAGE): the card labels the draft with it


def _first(name):
    return name.split()[0] if name else "there"


def _year(value):
    return int(value[:4]) if value else None


def _history(events):
    """(org, start year, end year, url) the candidate was at, from dated public records."""
    out = []
    for e in events:
        if e["event_type"] == "affiliation_change":
            org, _, years = e["quote"].partition(": years ")
            years = [int(y) for y in re.findall(r"\d{4}", years)]
            if years:
                out.append((org, min(years), max(years), e.get("source_url") or e["quote"]))
        elif e["event_type"] == "affiliation_seen":
            org = TITLE.split(e["quote"], 1)[0]
            out.append((org, _year(e["event_date"]), _year(e["event_date"]), e.get("source_url") or e["quote"]))
    return out


def _worked_with(events, team):
    """(kind, teammate, date, url, what, work): public work they did with a teammate, in the third person. A
    coauthored paper, or a push or release on a teammate's repo, which takes the teammate's say-so."""
    by_openalex = {oid: m for m in team for oid in m.openalex_ids}
    by_login = {m.github.lower(): m for m in team if m.github}
    for e in events:
        day, url, quote = e["event_date"] or "", e.get("source_url") or e["quote"], e["quote"]
        own, _, answered = quote.partition(REPLY_CONTEXT)
        if e["event_type"] == "coauthor_link" and (m := COAUTHOR.match(quote)) and m["id"] in by_openalex:
            yield "coauthor", by_openalex[m["id"]], day, url, "", m["title"]
        elif e["event_type"] == "github_activity" and not answered and own.startswith(("Pushed to ", "Released ")) \
                and (repo := REPO.search(own)) and (mate := by_login.get(repo["owner"].lower())):
            tag = own.removeprefix("Released ").split(f" of {repo[0]}")[0].strip() if own.startswith("Released") else ""
            done = "pushed to" if own.startswith("Pushed") else f"released {tag} of" if tag else "made a release of"
            yield "commit", mate, day, url, f"They {done} {mate.name}'s repo {repo[0]}", repo[0]


def paths(events, team, as_of=None):
    """Routes in, then leads to check: teammates who worked with them (confirmed), strongest first, per teammate and
    kind the latest and a count; then teammates who were at the same employer in the same years, or who worked with
    them more than REMEMBERED_YEARS before ``as_of`` (likely)."""
    found = {}
    for kind, mate, day, url, what, work in _worked_with(events, team):
        path = found.setdefault((kind, mate.name), Path(kind=kind, level="confirmed", teammate=mate, detail=""))
        path.evidence.append(url)
        if not path.latest or day > path.latest:
            path.latest, path.work, path.detail = day, work, what
    for p in found.values():
        count, when = len(p.evidence), p.latest or "undated"
        if p.kind == "coauthor":
            p.detail = f"{p.teammate.name} coauthored {count} public work{'s' if count > 1 else ''} with them, latest '{p.work}' ({when})"
            p.tie = f"You coauthored '{p.work}' with them ({when})."
        else:
            more = f", {count} pushes or releases in all" if count > 1 else ""
            p.tie = p.detail.replace(f"{p.teammate.name}'s repo", "your repo") + f" ({when}{more})."
            p.detail = f"{p.detail} ({when}{more})"
        if as_of and p.latest and p.latest < f"{int(as_of[:4]) - REMEMBERED_YEARS}{as_of[4:10]}":
            p.level = "likely"  # too long ago to count on being remembered: ask first

    stints = _history(events)
    tied = {name for _, name in found}
    for mate in team:
        if mate.name in tied:
            continue
        for stint in mate.history:
            for org, start, end, source in stints:
                if not journey.same_org(org, stint.org):
                    continue
                if None in (stint.start, stint.end) or max(start, stint.start) > min(end, stint.end):
                    continue  # same place at unknown or different times says little
                first, last = max(start, stint.start), min(end, stint.end)
                key = ("overlap", mate.name)
                if key not in found:
                    found[key] = Path(kind="overlap", level="likely", teammate=mate, latest=str(last), evidence=[source],
                                      detail=f"{mate.name} and they were both at {stint.org} in {first}"
                                             + (f"-{last}" if last != first else ""))
    newest = sorted(found.values(), key=lambda p: p.latest or "", reverse=True)
    return sorted(newest, key=lambda p: (LEVELS.index(p.level), p.kind != "coauthor", -len(p.evidence)))


def warmth(events, team):
    """One-way attention to a teammate or their work, dated and linked, newest first: [{date, what, url, teammate}].
    It can change who signs; it is never a route in, and the draft never mentions it."""
    by_handle = {"@" + m.x_handle.lstrip("@").lower(): m for m in team if m.x_handle}
    by_name = {m.name.lower(): m for m in team}
    by_login = {m.github.lower(): m for m in team if m.github}
    out = {}
    for e in events:
        kind, quote = e["event_type"], e["quote"]
        own, _, answered = quote.partition(REPLY_CONTEXT)
        said = []
        if kind in ("x_post", "x_reply"):
            target = answered.split(":", 1)[0].strip().lower()
            for who in sorted(talks_to(e)):
                if mate := by_handle.get(who):
                    verb = "mentioned" if who != target else "replied to" if kind == "x_reply" else "quoted"
                    said.append((mate, f"They {verb} {mate.name} on X"))
        elif kind == "linkedin_comment" and (mate := by_name.get(answered.split(":", 1)[0].strip().lower())):
            said.append((mate, f"They commented on {mate.name}'s LinkedIn post"))
        elif kind in ("github_activity", "github_star") and (answered or not own.startswith(("Pushed to ", "Released "))) \
                and (repo := REPO.search(answered or own)) and (mate := by_login.get(repo["owner"].lower())):
            item = re.match(r"(\w+) (issue|pull request) on ", own)
            verb = ("commented on" if answered else f"{item[1].lower()} an {item[2]} on" if item and item[2] == "issue"
                    else f"{item[1].lower()} a {item[2]} on" if item else own.split()[0].lower())
            said.append((mate, f"They {verb} {mate.name}'s repo {repo[0]}"))
        for mate, what in said:
            url = e.get("source_url") or ""
            out.setdefault((url, what), {"date": e["event_date"] or "", "what": what, "url": url, "teammate": mate.name})
    return sorted(out.values(), key=lambda w: w["date"], reverse=True)


def searched(events, team):
    """(checked, not checked): which route-in sources this call looked at, so "none found" says what it covers."""
    kinds = {e["event_type"] for e in events}
    rows = [
        ("shared papers", any(m.openalex_ids for m in team), "coauthor_link" in kinds),
        ("pushes to teammates' repos", any(m.github for m in team), "github_activity" in kinds),
        ("shared employers", any(m.history for m in team), kinds & {"affiliation_change", "affiliation_seen"}),
    ]
    checked = [what for what, ours, theirs in rows if ours and theirs]
    missing = [f"{what} ({'the team roster lacks it' if not ours else 'nothing pulled for them'})"
               for what, ours, theirs in rows if not (ours and theirs)]
    return checked, missing + ["answered exchanges with a teammate (their replies are not pulled)"]


ROUTE_SOURCES = ("shared papers", "pushes to teammates' repos")  # a shared employer only ever gives a lead


def _compose_email(to, subject, body, sender_email=""):
    """A Gmail compose link, filled in, opened in the sender's own account when their address is known. Falls back
    to an empty body when the link would pass Slack's limit."""
    for text in (body, ""):
        url = "https://mail.google.com/mail/?" + urlencode(
            {**({"authuser": sender_email} if sender_email else {}), "view": "cm", "fs": 1, "to": to, "su": subject,
             "body": text}, quote_via=quote)
        if len(url) <= SLACK_URL_MAX:
            return url
    return ""


def _active(events, as_of):
    """{platform: their own posts there in the last ACTIVE_DAYS}, by when each became public."""
    since = (parse_time(as_of) - timedelta(days=ACTIVE_DAYS)).date().isoformat() if as_of else ""
    return Counter(PLATFORM[e["event_type"]] for e in events
                   if e["event_type"] in PLATFORM and (e.get("observed_at") or "")[:10] >= since)


def _x_url(x, body):
    """A filled-in DM when their numeric X id is known, else their profile."""
    handle = x.value.lstrip("@")
    if x.x_user_id:
        url = f"https://x.com/messages/compose?{urlencode({'recipient_id': x.x_user_id, 'text': body}, quote_via=quote)}"
        if len(url) <= SLACK_URL_MAX:
            return url
    return f"https://x.com/{handle}"


def on_file(contacts, context, follows=None):
    """The routes in the contacts file, plus their X and LinkedIn profiles from the people file (the context's
    anchors, which scripts/posts.py saves) for any the contacts file lacks: a fresh checkout reaches the same people
    without hand-built routes. The profile page is the route's source. ``follows`` is their record from the X follows
    pull (x_follows.of): it fills the X route's follows when it read the same handle and the route has none."""
    have, anchors = {c.kind for c in contacts}, context.anchors if context else {}
    found = [ContactRoute(kind="x", value=url.rstrip("/").rsplit("/", 1)[-1], source_url=url)
             for url in [anchors.get("x")] if url and "x" not in have]
    found += [ContactRoute(kind="linkedin", value=url, source_url=url)
              for url in [anchors.get("linkedin")] if url and "linkedin" not in have]
    read = (follows or {}).get("x_handle", "").lstrip("@").lower()
    return [c.model_copy(update={"follows": follows["follows"]})
            if c.kind == "x" and c.follows is None and read and c.value.lstrip("@").lower() == read else c
            for c in [*contacts, *found]]


# An account's own page on a site of accounts: (label, pattern, the profile's URL). Only a bare account page: a
# repository, a post or a site page (github.com/features, x.com/explore) is not their account.
_END = r"/?(?:[?#].*)?$"
ACCOUNTS = [
    ("X", re.compile(r"^https?://(?:www\.|mobile\.|m\.)?(?:x|twitter)\.com/(?!(?:i|home|search|intent|share|hashtag|explore|"
                     r"settings|notifications|messages|compose|login|signup|tos|privacy|jobs)" + _END + r")"
                     r"([A-Za-z0-9_]{1,50})" + _END, re.I), "https://x.com/{}"),
    ("GitHub", re.compile(r"^https?://(?:www\.)?github\.com/(?!(?:orgs|sponsors|topics|features|settings|about|pricing|"
                          r"marketplace|explore|collections|trending|login|join|enterprise|security|readme|events|site|"
                          r"apps|notifications|pulls|issues|new|organizations|codespaces|copilot|customer-stories)"
                          + _END + r")([A-Za-z0-9-]+)" + _END, re.I), "https://github.com/{}"),
    ("LinkedIn", re.compile(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([^/?#]+)", re.I), "https://www.linkedin.com/in/{}"),
    ("Hugging Face", re.compile(r"^https?://(?:www\.)?huggingface\.co/(?!(?:papers|datasets|spaces|models|blog|docs|learn|"
                                r"pricing|join|login|settings|organizations|collections|posts|chat|tasks|enterprise)"
                                + _END + r")([A-Za-z0-9_.-]+)" + _END, re.I), "https://huggingface.co/{}"),
]
SOCIAL = {"x.com", "twitter.com", "mobile.twitter.com", "m.twitter.com", "github.com", "linkedin.com", "huggingface.co"}
# A researcher profile as linked, only from its profile page: (label, pattern).
LINKED_AS_IS = {"scholar.google.com": ("Google Scholar", re.compile(r"^/citations\?(?:.*&)?user=[\w-]+")),
                "orcid.org": ("ORCID", re.compile(r"^/\d{4}-\d{4}-\d{4}-\d{3}[\dX]/?$")),
                "openreview.net": ("OpenReview", re.compile(r"^/profile\?(?:.*&)?id=~?[\w.-]+"))}


def profile(url):
    """{label, url}: the profile a page is on. An X, GitHub, LinkedIn or Hugging Face account's own page; a Scholar,
    ORCID or OpenReview profile page as linked; else the page as given (their site, a university page, a portfolio),
    named by its address. None for a page on a site of accounts or of profiles that is no one's account or profile
    (a repository, a post, a search, a paper)."""
    for label, pattern, page in ACCOUNTS:
        if m := pattern.match(url or ""):
            return {"label": label, "url": page.format(m[1])}
    parts = urlparse(url or "")
    host = parts.netloc.lower().removeprefix("www.")
    if not host or parts.scheme not in ("http", "https") or host in SOCIAL or host.endswith(".linkedin.com"):
        return None
    if host in LINKED_AS_IS:
        label, pattern = LINKED_AS_IS[host]
        return {"label": label, "url": url} if pattern.match(parts.path + (f"?{parts.query}" if parts.query else "")) \
            else None
    return {"label": (host + parts.path).rstrip("/"), "url": url}


def read_profiles(context):
    """The profile pages the engine reads for them (their context's anchors), as the gate checks identity against."""
    return [u for u in (context.anchors.values() if context else []) if u.startswith(("https://", "http://"))]


def tied(history, as_of, read=()):
    """Their public profiles, as links: the pages their latest identity record (by ``as_of``) ties to them, each as
    the profile it is on. A post or repository the record ties shows as the account it is on, when that account is
    one the engine reads for them (``read``, read_profiles), never as itself and never as someone else's account.
    Only those, so a card never lists a namesake's account; none without a record, or after a later "wrong
    person"."""
    past = [e for e in history if parse_time(e["at"]) <= parse_time(as_of)]
    last = next((e for e in reversed(past) if e["kind"] in ("identity", "wrong_person")), None)
    if not last or last["kind"] != "identity":
        return []
    found = [p for url in last.get("links") or [] for p in
             ([profile(url)] if profile(url) else [profile(own) for own in read if contact.tied([url], [own])]) if p]
    return list({p["url"].rstrip("/").lower(): p for p in found}.values())


def public_email(contacts):
    """{address, source_url}: the address they list publicly (the contacts file's email route, with the page it is
    on), else None. Never a guessed one."""
    email = _routes(contacts).get("email")
    return {"address": email.value, "source_url": email.source_url} if email else None


def _routes(contacts):
    by_kind = {}
    for c in contacts:
        by_kind.setdefault(c.kind, c)
    return by_kind


def channel(who, contacts, events, as_of):
    """How the sender reaches them, and why (Justin, 2026-09-23: not always email).

    An X DM when they follow the sender, since it lands in their inbox; else an email they list publicly; else the
    platform they post on most (X or LinkedIn, X on a tie) that the sender can use (X needs the sender's handle in the
    team file); else their site. LinkedIn connections show only to the sender, so when they have a LinkedIn the card
    asks the sender to check it. The channel is picked before the draft, so ``fill`` can shape the draft for it."""
    by_kind = _routes(contacts)
    x, email, linkedin, site = map(by_kind.get, ("x", "email", "linkedin", "site"))
    me = _first(who["name"])
    handle = who.get("x_handle", "").lstrip("@").lower()
    follows = {h.lstrip("@").lower() for h in (x.follows or [])} if x else set()
    active = _active(events, as_of)
    check_first = (f"Before sending, {me} checks LinkedIn ({linkedin.value}): if they're connected, a LinkedIn "
                   f"message works too, with the same draft.") if linkedin else ""

    if x and handle and handle in follows:
        return Channel(kind="x_dm", target="@" + x.value.lstrip("@"), check_first=check_first,
                       reason=f"They follow {me} on X, so a DM lands in their inbox, not in message requests.")
    if email:
        return Channel(kind="email", target=email.value, check_first=check_first, source=email.source_url,
                       reason=f"They list this address publicly at {email.source_url}.")
    ranked = sorted((p for p in active if by_kind.get(p)), key=lambda p: (-active[p], p != "x"))
    # The sender can write on X only with an account in the team file: else LinkedIn or their site, never X.
    skipped = not handle and bool(x)
    posted = [p for p in ranked if not (skipped and p == "x")]
    where = posted[0] if posted else "x" if x and not skipped else "linkedin" if linkedin else ""
    platform = "X" if where == "x" else "LinkedIn"

    def count(p):
        return f"{active[p]} post{'s' if active[p] != 1 else ''} in {ACTIVE_DAYS} days"
    most = (f"they post most on {platform} ({count(where)})" if posted
            else f"no recent posts to go by, so their {platform} account, the route on file")
    if skipped and linkedin and ranked[:1] == ["x"]:
        most = (f"they post most on X ({count('x')}), but {me} has no X account in the team file, so LinkedIn"
                + (f" ({count('linkedin')})" if posted else ""))
    elif skipped and linkedin and not posted:
        most = f"no recent posts to go by, and {me} has no X account in the team file, so their LinkedIn, the route on file"
    if where == "linkedin":
        return Channel(kind="linkedin", target=linkedin.value,
                       reason=f"No public email, and {most}. If {me} is connected with them, send the message; if not, "
                              f"a connection request with the note.")
    if where == "x":
        why = (f"It lands in their message requests, since they don't follow {me} there. " if x.follows is not None else
               f"It lands in their message requests unless they follow {me}, which isn't pulled yet. ")
        return Channel(kind="x_request", target="@" + x.value.lstrip("@"), check_first=check_first,
                       reason=f"No public email, and {most}. {why}If they take requests only from verified accounts, "
                              f"or none, reply to their post first.")
    no_x = f"{me} has no X account in the team file, " if skipped else ""
    if site:
        return Channel(kind="site", target=site.value, check_first=check_first,
                       reason=f"No public email or LinkedIn, and {no_x or 'no X on file, '}so their own site, the "
                              f"listed way to reach them.")
    if skipped:
        return Channel(kind="none", target="", reason=f"No way in for {me}: X is their only route on file, and {me} "
                                                      f"has no X account in the team file. Find their email, LinkedIn or "
                                                      f"site, or have a teammate with X write.")
    return Channel(kind="none", target="", reason="No public route found. Find one before reaching out.")


def fill(chan, who, contacts, message):
    """(Channel with its button's link, draft): the message shaped for the channel as {to, subject, body, note,
    after}. There is always a subject: the card shows it and an email sends it. The connection note and the message
    for after they accept are filled only for LinkedIn."""
    x, body = _routes(contacts).get("x"), message["body"]
    draft = {"to": "", "subject": message["subject"], "body": body, "note": "", "after": ""}
    if chan.kind == "email":
        url = _compose_email(chan.target, message["subject"], body, who.get("email", ""))
        draft["to"] = chan.target
    elif chan.kind in ("x_dm", "x_request"):
        url = _x_url(x, body)
    elif chan.kind == "linkedin":
        url, draft["note"], draft["after"] = chan.target, message.get("note", ""), message.get("after", "")
    else:
        url = chan.target if chan.kind == "site" else ""
    return chan.model_copy(update={"url": url}), draft


def _work(events, skip=""):
    """The candidate's latest public work title and date in the view, for the opener; not ``skip`` (the work the
    sender did with them, which the draft already names)."""
    titled = [(e["event_date"] or "", m["title"]) for e in events
              if e["event_type"] in ("coauthor_link", "affiliation_seen") and (m := TITLE.search(e["quote"]))
              and m["title"] != skip]
    return max(titled, default=None)


PAPERS = ("paper_accepted", "paper_v1", "publication")  # their own new paper in the call's evidence: what the draft opens with
# The public moments a note can open with (readiness.NEWS_TYPES) when no paper title can be read: what it was.
NEWS_SAW = {"paper_v1": "your paper", "publication": "your paper", "paper_accepted": "your paper",
            "launch_announced": "the announcement", "project_release": "the release", "acquisition": "your post"}
# What each draft is, in a few words: the card labels the draft with it, so different moments get plainly different
# messages (Justin, 2026-09-24).
MESSAGE = {
    "acquisition": "congratulations on the acquisition, no role named",
    "paper": "congratulations on the new paper",
    "accepted": "congratulations on the acceptance",
    "launch": "congratulations on the launch",
    "release": "congratulations on the release",
    "open_to_work": "they said they're open to work, so the role comes first",
    "early_sign": "their own post about their work, before it's news",
    "undergraduate": "about their work only, no role named (an undergraduate)",
    "unconfirmed": "about their work only, no role named until it's confirmed",
    "work": "about their recent work",
}
CONGRATS = {"acquisition": "congrats on the acquisition!", "paper": "congrats on the new paper!",
            "accepted": "congrats on the acceptance!", "launch": "congrats on the launch!",
            "release": "congrats on the new release!"}  # what such a note opens with
# A subject that says what the note is about, never their post's own first words.
SUBJECT = {"release": "Congrats on the new release", "launch": "Congrats on the launch",
           "paper": "Congrats on the new paper", "accepted": "Congrats on the acceptance"}
FACT_MAX = 160  # a role's own GI fact longer than this reads as a pasted job description: the company's instead


def circumstance(role_free, moment=None, news=None, opener=None):
    """What the draft is about, a MESSAGE key: no role for an undergraduate or in deal week; they said they're open
    to work (the public moment behind the reach); else what the draft opens with: their own post (an early sign),
    their public moment (a paper, an acceptance, a launch, or a new version of something they make, which is a
    release, never a launch), or their recent work. route() turns an acquisition or open to work into "work" when
    their post about it names a layoff or something personal."""
    if role_free:
        return role_free if role_free in MESSAGE else "undergraduate"
    if moment is not None and moment.kind == "open_to_work":
        return "open_to_work"
    if opener:
        return "early_sign"
    kind = (news or {}).get("event_type")
    return "accepted" if kind == "paper_accepted" else "paper" if kind in PAPERS else \
        "release" if kind == "project_release" or kind in LAUNCHES and happened.shipped(news["quote"]) else \
        "launch" if kind in LAUNCHES else "work"



def fact(facts, role, message):
    """The GI fact a draft states: the role's own when it is short and does not name the role; else the company's.
    A note on an acquisition says what GI builds (the first company fact), never the role's."""
    company = [f for f in facts if "*" in f["roles"]]
    own = [f for f in facts if "*" not in f["roles"] and f["id"] != "nyc" and len(f["text"]) <= FACT_MAX
           and role["title"].lower() not in f["text"].lower()]
    return ((company if message == "acquisition" else own + company) or facts)[0]["text"]


def _paper(event):
    """(title, where) from a new paper's record: an OpenReview decision "Title: Accept (Venue)", an arXiv
    "Title (2601.00001v1): ...", or OpenAlex "Title: published 2026-08-20 (OpenAlex)"."""
    quote = event["quote"]
    if event["event_type"] == "paper_accepted":  # "Title: Accept (Oral) (NeurIPS 2026)", "Title: Accept: poster (ICLR 2023 poster)"
        m = re.match(r"(?P<title>.+?): (?P<verdict>Accept\b.*)$", quote)
        title, verdict = (m["title"], m["verdict"]) if m else quote.rpartition(": ")[::2]
        venue = re.search(r"\(([^()]+)\)$", verdict)
        venue = re.sub(r"\s+(poster|oral|spotlight|notable[\w %-]*)$", "", venue[1], flags=re.I) if venue else ""
        return title, "" if "/" in venue or ".cc" in venue else venue  # an OpenReview venue id is not a name
    if m := re.match(r"(?P<title>.+?) \(\d{4}\.\d{4,5}v\d+\):", quote):
        return m["title"], "arXiv"
    title = quote.rpartition(": published")[0]
    return ("", "") if not title or title.startswith("http") else (title, "")  # OpenAlex without a title: a URL


def _when(day):
    """A date at the source's precision: "12 September 2026", "September 2026" or "2026"."""
    if len(day) == 10:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%-d %B %Y")
    return datetime.strptime(day[:7], "%Y-%m").strftime("%B %Y") if len(day) >= 7 else day


def _excerpt(quote, limit=160):
    quote = " ".join(quote.split())
    return quote if len(quote) <= limit else quote[:limit].rsplit(" ", 1)[0] + "…"


# Nothing past a list, a link or a cut is quoted; a sentence or line ends before a capital (not "e.g. for"); a long
# sentence can be quoted up to an emoji that a new sentence follows ("for all of you 🥹 And yes, ..."), else up to where
# a clause ends.
STOP_AT = re.compile(r"\s[-–•*]\s|https?://\S+|…|\.\.\.")
SENTENCE = re.compile(r"(?<=[.!?])\s+(?![a-z])|\s*\n\s*")
CLAUSE = re.compile(r"[,;:](?=\s)|\s[—–](?=\s)")
EMOJI_THEN = re.compile(r"(?<=[\u2600-\u27bf\ufe0f\U0001f000-\U0001faff])\s+(?=(?:And|But|So|Yes|Also|Now|Here|This|It|I|We)\b)")
LIST_ITEM = re.compile(r"\s[-–•*]\s")


def _clause(text, ends_at=CLAUSE):
    """The longest start of ``text`` that ends where a clause ends (or ``ends_at``) and runs 3 to outreach.QUOTE_WORDS
    words."""
    ends = [m.start() for m in ends_at.finditer(text) if 3 <= len(outreach.terms(text[:m.start()])) <= outreach.QUOTE_WORDS]
    return text[:ends[-1]].strip() if ends else ""


def _phrase(text):
    """What a draft quotes of their words, 3 to outreach.QUOTE_WORDS words: their first sentence or line, with the
    next one too when both fit; a first sentence too long to quote, up to where a clause ends. Never past a list, a
    link or a cut, never a list's first item, never a sentence their text cuts off (only a clause of it that ends
    before the cut); else "" (the draft names the work without quoting)."""
    text = (text or "").strip()
    if outreach.BULLET.match(text):
        return ""
    stop = STOP_AT.search(text)
    kept = text[:stop.start()] if stop else text
    pieces = [s.strip() for s in SENTENCE.split(kept) if s.strip()]
    whole = pieces if not (stop and stop.group() in ("…", "...")) else pieces[:-1]  # the last runs into the cut
    size = lambda s: len(outreach.terms(s))  # noqa: E731
    if not whole:
        return _clause(pieces[0]) if pieces else ""
    first = whole[0].rstrip(" ,;:-–—")
    if size(first) > outreach.QUOTE_WORDS:
        return _clause(first, EMOJI_THEN) or _clause(first)
    if len(whole) > 1 and first[-1:] in ".!?" and size(first) + size(whole[1]) <= outreach.QUOTE_WORDS:
        return f"{first} {whole[1].rstrip(' ,;:-–—')}"
    return first if size(first) >= 3 else ""


def _parts(text):
    """The first two items of a list their post opens with a line on ("Built X - parser - renderer - ..."), each
    one to five words of theirs: the parts a draft can name. [] for anything else, such as two dashes in a sentence
    ("a renderer - took longer than planned - but it works"): a list has three items or more."""
    head, *items = LIST_ITEM.split((text or "").strip())
    items = [i.strip().rstrip(" ,;:.") for i in items]
    named = [i for i in items[:2] if 1 <= len(outreach.terms(i)) <= 5 and not STOP_AT.search(i) and "\n" not in i]
    return named if head and len(items) >= 3 and len(named) == 2 else []


# A word that says they made something, opening the post's line or a clause of it after a label ("Build log 2:"),
# "I" or "we" (never "Dana built ..."): "built", "shipped", or a name of theirs turned into another ("extended PebbleGrad
# into", never "made NeurIPS", "extended our renderer into WebAssembly", "merged into LangChain"). Then the name it
# gives: a word with a capital inside it after a lowercase letter (RiverGrad, never GPU), ending the clause. A name
# with more after it describes the thing ("ChatGPT plugin", "VoxelRay with NumPy"); a hashtag or a handle is no name.
NAMED = r"[A-Za-z][a-z0-9]+[A-Z][A-Za-z0-9]*"
MADE = re.compile(r"(?:^|[:.!?]\s+)(?:(?i:i|we|just|finally)\s+)*(?:(?i:built|launched|shipped|released|introducing|"
                  rf"announcing|open[- ]sourced|created)\s+|(?i:turned|extended|grew)\s+{NAMED}\s+(?i:into)\s+)"
                  rf"(?P<name>{NAMED})(?=[.,:;!?]|\s*$)")  # the names' case matters: only the words ignore it


def _built(text):
    """(name, parts) for a post that opens on a line and then lists parts: the name the line gives what they made
    (``MADE``: "extended PebbleGrad into RiverGrad." is RiverGrad; "Dana built GridWorld, and I built VoxelRay." is
    none), and the first two items of its list (``_parts``) when each has two words that say something
    (a detail only true of them, as the name swap counts it) and no quote of its own; else ("", [])."""
    parts, head = _parts(text), LIST_ITEM.split((text or "").strip())[0]
    made = MADE.search(head)
    said = all(len([w for w in outreach.terms(p) if not outreach._weak(w)]) >= 2 and not re.search("[\"“”]", p)
               for p in parts)
    return (made["name"], parts) if made and parts and said else ("", [])


def _theirs(event, events):
    """Their own words in what a draft opens with: those of the GitHub item it was read from, when there is one (the
    reader's copy can keep the feed's framing, "(Swift)"; an item with no words of theirs has none), else the event's."""
    return their_words(read_from(event, events))


def _in_quotes(text):
    """Their words in quotation marks, closed with a full stop when they end on a word without one (never after
    an emoji or a link)."""
    said = " ".join(text.split()).rstrip(" ,;:-–—")
    bare = said and (said[-1].isalnum() or said[-1] in ")]'’") and not said.split()[-1].startswith("http")
    return f"\"{said}.\"" if bare else f"\"{said}\""


WHERE = {"x.com": "X", "twitter.com": "X", "linkedin.com": "LinkedIn", "github.com": "GitHub", "arxiv.org": "arXiv"}


def _where(url):
    host = (url or "").split("/")[2].removeprefix("www.") if (url or "").count("/") >= 2 else ""
    return f" on {WHERE[host]}" if host in WHERE else ""


def _saw(what, url):
    """"saw your X post": where it was, never the day (a post's day depends on a time zone we don't know; the card's
    Trigger dates it beside its link); off X and LinkedIn, ``what`` it was ("the release on GitHub")."""
    host = _where(url).removeprefix(" on ")
    place = f"your {host} post" if host in ("X", "LinkedIn") else "your work on GitHub" \
        if host == "GitHub" and what == "your post" else what + (f" on {host}" if host else "")
    return f"saw {place}"


# How a draft names a GitHub item by what it was (FEED's "what" less its repo), with the repo's own name in it.
ON_GITHUB = (("Created", "your {repo} repo on GitHub"), ("Pushed to", "your {repo} commits on GitHub"),
             ("Released", "the {repo} release on GitHub"), ("pull request", "your {repo} pull request on GitHub"),
             ("issue", "your {repo} issue on GitHub"), ("In reply to", "your {repo} comment on GitHub"))


def _on_github(event, events):
    """(what the draft calls it, their words) for a GitHub item of theirs the draft opens with: the repo it is on by
    name ("your tidewire commits on GitHub"), and the item's own words or, for work on a repo of theirs with none (a
    push with no messages: it shares the repo's page), that repo's description. A repo's name is a detail of the
    person who made it or worked on it; the name swap still needs a second one. None off GitHub, or for someone
    else's repo they starred or forked."""
    source = read_from(event, events)
    said, _, context = (source.get("quote") or "").partition(REPLY_CONTEXT)
    if source["event_type"] not in GITHUB_TYPES:
        return None
    if context:
        what, full = "In reply to", context.split(":", 1)[0].strip()
    elif m := FEED.match(said.strip()):
        what, full = m["what"].rsplit(" ", 1)
    else:
        return None
    how = next((h for w, h in ON_GITHUB if w in what), None)
    if not how or "/" not in full:
        return None
    words = their_words(source) or next((their_words(e) for e in events if what == "Pushed to" and e["event_type"] ==
                                         "github_repo" and (m := FEED.match(e["quote"] or ""))
                                         and m["what"] == f"Created {full}"), "")
    return how.format(repo=full.split("/", 1)[1]), words


def _about(url):
    """A subject for a note about their post that isn't the post's own words."""
    host = _where(url).removeprefix(" on ")
    return {"": "Your recent post", "GitHub": "Your work on GitHub", "arXiv": "Your paper"}.get(host, f"Your post on {host}")


def unnamed(facts, role):
    """GI's facts that say nothing about the role: for an undergraduate's draft, which never names it."""
    return [f for f in facts if role["title"].lower() not in f["text"].lower() and f["id"] != "nyc"]


def template(name, role, context, events, opener=None, sender_name=None, tie=None, role_free=False, news=None,
             employer=None, message=None, by_name=True):
    """The free draft to the person, {subject, body, note}, in the model's shape (app.outreach): their own words and
    dates, one short GI fact, and the role, signed by the sender. ``note`` is the same draft cut to a LinkedIn
    connection request.

    ``message`` is what the draft is about (``circumstance``, worked out from the rest when not given). A public
    moment opens with congratulations; an acquisition says what GI builds and leaves the door open, never naming the
    role; open to work leads with the role. ``news`` is their own public moment or new paper behind the call; without
    an opener, the draft opens with it. ``tie`` is the sender's confirmed path to them, and the draft then says what
    they worked on together.

    ``role_free`` is "undergraduate" (per the contact gate: the ask is about their work and the role goes unnamed) or
    "acquisition" (their company was just acquired). ``employer`` is where they work now (happened.employer); None
    means the people file's. ``by_name``: a post that says what they built opens with its name (``_built``); False
    quotes its line instead, which is route's fallback when the named draft fails its checks. A post is never dated
    ("saw your X post"), since its day depends on a time zone we don't know."""
    employer = context.employer if employer is None and context else employer or ""
    work = _work(events, tie.work if tie else "")
    said = news and their_words(news)
    if news and (outreach.LOSS.search(said) if news["event_type"] in PAPERS else outreach.unquotable(said)):
        news = None  # an acquisition post about layoffs, or something personal: never quote it, open with their work
    if news and tie and _paper(news)[0].casefold() == tie.work.casefold():
        news = None  # their paper is the one they wrote with the sender: the draft names it once, as work together
    if opener and outreach.unquotable(opener["quote"].partition(REPLY_CONTEXT)[0]):
        opener = None  # their post is about a layoff, a job or something personal: open with their work instead
    message = message or circumstance(role_free, None, news, opener)
    lead = ""  # what the note opens with before the rest: the congratulations, or the role when they're open to work
    # Open to work: ``opener`` is their post saying so, quoted before the role; the rest is about their work.
    stated, opener = (opener, None) if message == "open_to_work" else (None, opener)
    thing = ""  # the product and version a release names: the congratulations says it
    if opener:
        github = _on_github(opener, events)  # the repo by name, and its description when a push says nothing
        words = github[1] if github else _theirs(opener, events)  # never the feed's framing ("Pushed to owner/repo")
        said, parts = _phrase(words), _parts(words)
        subject = _about(opener.get("source_url"))
        seen = f"saw {github[0]}" if github else _saw("your post", opener.get("source_url"))
        # The parts their list names, in their words: "especially "formula parser" and "lazy recalculation"".
        especially = f", especially \"{parts[0]}\" and \"{parts[1]}\"" if said and parts else ""
        hook = f"{seen}: {_in_quotes(said)} It really caught my eye{especially}." if said else \
            f"{seen}, and it really caught my eye."
        made, listed = ("", []) if github or not by_name else _built(words)
        if made:  # what they built by its name, and the parts their list names in their words: never the post's line
            hook = f"{seen} about {made}, and it really caught my eye, especially \"{listed[0]}\" and \"{listed[1]}\"."
    elif news and news["event_type"] in PAPERS and (paper := _paper(news))[0]:
        (title, venue), day = paper, news["event_date"] or ""
        subject = title
        if news["event_type"] == "paper_accepted":
            seen = f"saw that '{title}' was accepted" + (f" at {venue}" if venue else "")
        else:
            seen = f"saw '{title}'" + (f" on {venue}" if venue else "") + (f" from {_when(day[:7])}" if day else "")
        hook = f"{seen}, and it really caught my eye."
        if message in ("paper", "accepted"):  # the congratulations names the paper: no second line saying it again
            lead = (f"congrats on getting '{title}' accepted" + (f" at {venue}" if venue else "") + "!"
                    if news["event_type"] == "paper_accepted" else f"congrats on '{title}'"
                    + (f" on {venue}" if venue else "") + (f" from {_when(day[:7])}" if day else "") + "!") \
                + " It really caught my eye."  # beside the paper it means, before any line on work together
            hook = ""
    elif news and news["event_type"] in NEWS_SAW:  # a launch, an acquisition, or a paper its record gives no title for
        words = _theirs(news, events)  # a release's name, never "Released v1.0 of owner/repo"
        # What shipped, by the name and version their words give, else a GitHub release's repo and tag.
        thing = happened.shipped(words) or happened.shipped(read_from(news, events)["quote"]) if message == "release" \
            else ""
        said = _phrase(words)
        subject = f"Congrats on {thing}" if thing else SUBJECT.get(message) or _about(news.get("source_url"))
        github = None if thing else _on_github(news, events)  # a release with no version: its repo by name
        seen = f"saw {github[0]}" if github else _saw(NEWS_SAW[news["event_type"]], news.get("source_url"))
        # Quoted when the post says more than the name and version the congratulations already gives.
        more = set(outreach.terms(said)) - set(outreach.terms(thing)) - outreach.STOP
        # A post that says only what shipped needs no "it caught my eye": the congratulations names it.
        hook = f"{seen}: {_in_quotes(said)}" if more else f"{seen}." if thing else f"{seen}, and it really caught my eye."
    elif work:
        day, title = work  # at the source's precision: a year, a month or a day, or undated
        subject, seen = title, f"saw '{title}'" + (f" from {_when(day[:7])}" if day else "")
        hook = f"{seen}, and it really caught my eye."
    elif context and context.role and employer:
        subject, seen = role["title"], f"saw your work at {employer}"
        hook = f"{seen}."
    else:
        subject, seen, hook = role["title"], "", ""
    if message == "acquisition":
        subject = "Congrats on the acquisition"
    elif role_free and subject == role["title"]:
        subject = "Your work"
    together = ""
    if tie and tie.work:
        # "since" only when what the draft names next came after the work together; else just when it was.
        day = (_posted(opener) if opener else (news["event_date"] if news["event_type"] in PAPERS else _posted(news))
               if news else work[0] if work else "") or ""
        later, when = tie.latest and day > tie.latest, f" in {_when(tie.latest[:7])}" if tie.latest else ""
        together = (f"good to see what you've done since we wrote '{tie.work}' together." if later else
                    f"we wrote '{tie.work}' together{when}.") if tie.kind == "coauthor" else \
            (f"good to see what you've done since your work on my repo {tie.work}." if later else
             f"you worked on my repo {tie.work}{when}.")
    gi = outreach.facts(role["id"])
    told = unnamed(gi, role) if role_free else gi  # an undergraduate: a fact about GI, never one naming the role
    where = ", in person in New York" if any(f["id"] == "nyc" for f in gi) else ""
    link = f" ({role['jd_url']})" if role["jd_url"] else ""  # a role dropped in without its post's link
    hiring = f"we're hiring for our {role['title']} role{link}{where}"
    cheer = f"congrats on shipping {thing}!" if thing else CONGRATS.get(message, "")
    if message == "open_to_work":
        said = _phrase(_theirs(stated, events)) if stated else ""
        lead = (f"{_saw('your post', stated.get('source_url'))}: {_in_quotes(said)} "
                f"{_cap(hiring)}." if said else f"{hiring}.")
    else:
        lead = lead or cheer
    opening = [p for p in (lead, together, hook) if p]
    close = ("Not sure what you're looking for next, but if this interests you, I'd love to chat."
             if message == "acquisition" else "I'd love to chat more about it if you're interested." if role_free else
             "If you're open to it, I'd love to chat." if message == "open_to_work" else
             f"{_cap(hiring)}, and I'd love to chat more if you're interested.")
    body = outreach.sign(" ".join(filter(None, [
        f"Hey {_first(name)},", *opening[:1], *map(_cap, opening[1:]), fact(told, role, message), close])),
        sender_name)
    ask = "I'd love to connect." if role_free else \
        f"We're hiring for our {role['title']} role at General Intuition, and I'd love to connect."
    congrats = f"{cheer} " if cheer else ""
    named = hook or (f"{seen} and it really caught my eye." if seen else "")  # the body's hook, which names the part
    note = next((n for n in (f"Hey {_first(name)}, {congrats}{_cap(named) if congrats else named} {ask}" if named else "",
                             f"Hey {_first(name)}, {congrats}{ask if congrats else ask[:1].lower() + ask[1:]}")
                 if n and len(n) <= outreach.NOTE_MAX), "")
    return {"subject": subject, "body": body, "note": note}


def seniority(events):
    """Why they read as senior, from a title in their own public record, newest first; "" when nothing says so."""
    for e in sorted(events, key=lambda e: e["event_date"] or "", reverse=True):
        if e["event_type"] in TITLED and SENIOR.search(e["quote"].partition(REPLY_CONTEXT)[0]):
            return f"their record says \"{_excerpt(e['quote'], 80)}\" ({e['event_date'] or 'undated'})"
    return ""


def _as_sender(mate, why):
    return {"name": mate.name, "title": mate.title, "why": why, "slack_user_id": mate.slack_user_id,
            "x_handle": mate.x_handle, "email": mate.email, "about": mate.about, "source_url": mate.source_url}


def on_x(who, team, role_id, senior=""):
    """When X is the only way in and the sender has no X account: another teammate who writes for the role (or for
    its senior people, when they read as senior) and has one writes instead; None when nobody does."""
    return next((_as_sender(m, f"{_first(who['name'])} has no X account in the team file and X is the only way in, "
                               f"so {_first(m.name)}, who also writes for this role, writes instead")
                 for m in team if m.name != who["name"] and m.x_handle
                 and (role_id in m.signs_for or (senior and f"{role_id}:senior" in m.signs_for))), None)


def sender(paths, warm, team, role_id, work, senior="", said=()):
    """Who writes and signs, and why (Justin, 2026-09-23).

    A teammate who worked with them comes first, since a real tie beats the table. Next is a writer for this role
    whom their warmth went to, then the team file's table (signs_for). For a role with a senior writer (MTS: the CEO),
    senior people get that writer; everyone else gets the writer whose own work (works_on) is closest to theirs, else
    the first writer named. With no writer at all, the default sender (config/gi-facts.json).

    ``work`` is their own words and work (outreach.own_work); ``senior`` is why they read as senior (``seniority``);
    ``said`` is their texts as written (outreach.own_texts), so a shared topic is said back their way ("world
    models")."""
    pick = _as_sender
    if confirmed := [p for p in paths if p.level == "confirmed"]:
        mate = confirmed[0].teammate
        return pick(mate, f"{confirmed[0].detail}: {_first(mate.name)} has worked with them directly")
    writers = [m for m in team if role_id in m.signs_for]
    seniors = [m for m in team if f"{role_id}:senior" in m.signs_for] if senior else []  # only ever to senior people
    by_name = {m.name: m for m in seniors + writers}
    if w := next((w for w in warm if w["teammate"] in by_name), None):
        return pick(by_name[w["teammate"]], f"{w['what']} ({w['date'] or 'undated'}), and {_first(w['teammate'])} "
                                             "writes for this role")
    if seniors:
        return pick(seniors[0], f"they read as senior: {senior}")
    # A phrase only one writer lists says more than one they all list: each counts 1 / how many writers list it.
    def key(phrase):
        return " ".join(outreach.terms(phrase))
    listed = Counter(key(p) for m in writers for p in dict.fromkeys(m.works_on) if key(p))
    shared = [(m, [p for p in m.works_on if key(p) and outreach._has(work, key(p))]) for m in writers]
    if (best := max(shared, key=lambda s: sum(1 / listed[key(p)] for p in s[1]), default=None)) and best[1]:
        return pick(best[0], "closest to their work: both work on "
                             + ", ".join(outreach.as_written(p, *said) for p in best[1][:3]))
    if writers:
        return pick(writers[0], "the team file names them for this role" + (
            ", and no writer's own work matches theirs" if any(m.works_on for m in writers) else ""))
    why = ("the team file names a writer for this role only for senior people" if any(
        f"{role_id}:senior" in m.signs_for for m in team) else "no one in the team file writes for this role")
    return {"name": outreach.sender(), "title": "", "why": f"{why}, so the default sender", "slack_user_id": "",
            "x_handle": "", "email": "", "about": "", "source_url": ""}


def _cap(text):
    return text[:1].upper() + text[1:]


def _esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _posted(event):
    """The day their post (or announcement) went public, never a date it names ("available from November", a launch
    day ahead): the same rule as the card and the paid writer (outreach.said_on)."""
    return outreach.said_on(event)[:10]


def _section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:SLACK_TEXT_MAX]}}


def _public(event):
    """The day it became public: a date the post names ("from November", "since August") is not the day it was
    said (outreach.said_on)."""
    return outreach.said_on(event)


# Reasons whose words date the thing itself ("the role they started in", "the acquisition, closed in"), not the day
# someone said it: the event's own date, when it is not later than the day it was seen.
ACT_DATED = ("tenure_milestone", "retention_cliff")


def _act(event):
    seen, day = (event.get("observed_at") or "")[:10], event.get("event_date") or ""
    return day if day and (not seen or day[:10] <= seen) else seen


WITHHELD = "(not quoted: it touches something personal)"
UNREAD = "(not quoted: nothing in it was read as a sign)"  # detectors.unread: a post whose words may be anything
NO_WORDS = "(not quoted: no words of their own, only the post it answers or quotes)"  # own_quote
NOTES = (WITHHELD, UNREAD, NO_WORDS)  # what shows in a quote's place: not their words, so never in quotation marks


def _shown(event):
    """The quote a card or page shows: one that touches something personal (a post, or a sign read from one) is dated
    and linked, never quoted. Their paper's title is their work, so it is always shown."""
    return WITHHELD if event["event_type"] not in PAPERS and outreach.personal(own_words(event["quote"])) \
        else event["quote"]


def own_quote(event):
    """What a page or card may quote of an item: of a post or GitHub item, the person's own words only
    (detectors.own_words), never the post they replied to or quoted ("In reply to @someone: ..."), which is someone
    else's, and NO_WORDS when they added none; withheld when it touches something personal, as _shown. Anything else
    (a paper's title, a filing, a reader's tag, already in their own words) is shown as _shown shows it."""
    shown = _shown(event)
    if event["event_type"] not in READ_TYPES or shown == WITHHELD:
        return shown
    return own_words(shown).strip() or (NO_WORDS if shown.strip() else "")


def own_claims(claims, events):
    """What would prove a call wrong, as a page shows it. An engine check that quotes a reply (the first 160
    characters of its text, in quotes, as detectors writes them) names it and the post it answers or quotes instead:
    the words the engine matched may be in either, so neither is quoted, and the post they answered is never shown."""
    swaps = {repr(e["quote"][:160]): f"their post of {_replied(e)} and the post it answers or quotes"
             for e in events if e.get("quote") and own_words(e["quote"]) != e["quote"]}
    for old, new in swaps.items():
        claims = [c.replace(old, new) for c in claims]
    return list(claims)


def _replied(event):
    day = parse_time(event["observed_at"])
    return f"{day:%b} {day.day}, {day.year}"


# Where a card's quote of their words may end: after a sentence (not "e.g. for", nor an abbreviation or an initial
# before a capital: "Dr. Smith", "et al. 2024", "U.S. Army"), at a line break, or before an item of a list of two or
# more in a line ("• ", or "1. " opening the text or a line or after a stop, a colon or an ellipsis, then "2. ", "3.
# ", whose own "2." ends no sentence; "Step 1. Then step 2." is a sentence's own count, as are a dash's aside and
# "1) ... and 2)").
ENDS = re.compile(r"[.!?][\"”’')\]]*(?=\s+[^a-z\s])")
ABBREVIATIONS = {"al", "approx", "cf", "co", "corp", "dr", "eq", "fig", "inc", "jr", "ltd", "mr", "mrs", "ms",
                 "no", "ph.d", "prof", "sr", "st", "vs"}
NUMBERED, BULLETS = re.compile(r"(?:(?<=\s)|^)\d{1,2}\.(?=\s)"), re.compile(r"\s(?=•\s)")
CUT = ("…", "...")


def _abbreviation(text, stop):
    """Whether the stop at ``stop`` ends an abbreviation or an initial ("Dr", "al", "e.g", "U.S", "J"), not a
    sentence."""
    word = (text[:stop].split() or [""])[-1].lstrip("(\"“'").lower()
    return word in ABBREVIATIONS or len(word) == 1 and word.isalpha() or bool(re.fullmatch(r"(?:[a-z]\.)+[a-z]", word))


def _numbered(text):
    """The item numbers of the list ``text`` has, "1." to "N." in order ([] when it has no list of two or more): its
    "1." opens the text or a line, or follows a stop, a colon or an ellipsis, and an item after a line break opens a
    line itself (so "\n1. ... \nWe shipped version 2." is no list)."""
    found = list(NUMBERED.finditer(text))
    first = next((i for i, m in enumerate(found) if m.group() == "1." and re.search(r"(?:^|[\n.!?:…])\s*$",
                                                                                    text[:m.start()])), None)
    items = []
    for m in found[first:] if first is not None else []:
        if m.group() == f"{len(items) + 1}." and (not items or "\n" not in text[items[-1].end():m.start()]
                                                  or _opens_line(text, m)):
            items.append(m)
    return items if len(items) >= 2 else []


def _opens_line(text, number):
    return bool(re.search(r"(?:^|\n)[^\S\n]*$", text[:number.start()]))  # after any indent, a no-break space too


def _whole(quote, limit=90, most=160):
    """What a card quotes of their words (never the post they answered): the most of their start that ends where a
    sentence, a line or a list's item does and fits ``limit`` characters (all of it when it fits), else a first such
    stretch of 3 or more words up to ``most``; never cut mid-sentence and never ending on "…" (a cut, or theirs, which
    reads as one); else "" (the line keeps its link)."""
    words = own_words(quote)
    head = words[:4 * most]  # nothing further in can start a quote this short
    numbers = _numbered(head)
    lined = [m for m in NUMBERED.finditer(head) if _opens_line(head, m)]  # a number opening a line ends no sentence
    bullets = [m.start() for m in BULLETS.finditer(head)]
    ends = {m.end() for m in ENDS.finditer(head) if head[m.start()] != "." or not _abbreviation(head, m.start())
            and m.start() not in {n.end() - 1 for n in numbers + lined}} | {m.start() for m in re.finditer(r"\n", head)} | \
        {n.start() for n in numbers} | (set(bullets) if len(bullets) >= 2 else set())
    whole = " ".join(words.split())
    starts = [" ".join(head[:end].split()).rstrip(" ,;:-–—") for end in sorted(ends)] + [whole]
    starts = [s for s in starts if s and not s.endswith(CUT) and (len(s.split()) >= 3 or s == whole)]
    fits = [s for s in starts if len(s) <= limit]
    return fits[-1] if fits else next((s for s in starts[:1] if len(s) <= most), "")


def _quoted(quote, limit=160):
    """A quote in quotation marks (_whole, "" when it could only be cut), or the withheld note without them: it is
    not their words."""
    if quote in NOTES:
        return _esc(quote)
    return f"“{_esc(said)}”" if (said := _whole(quote, limit, max(limit, 160))) else ""


def _told(event, untold):
    """What a signal shows in a quote's place: _shown's, or UNREAD for a post or GitHub item in ``untold``
    (detectors.unread), never for a record that only shares its key. A personal one keeps the withheld note, which
    says more."""
    shown = _shown(event)
    return UNREAD if event["event_type"] in READ_TYPES and item(event) in untold and shown != WITHHELD else shown


def signals(call, by_id, per_reason=2):
    """Each reason behind the call with its newest evidence: the day it became public, what it is in plain
    words, the quote and its link, and whose record it is (theirs, or a colleague's or employer's). The card and
    the app both show these."""
    untold = unread(by_id.values())  # a burst of posting counts posts the reader read nothing off
    return [{"id": e["id"], "date": _act(e) if reason in ACT_DATED else _public(e),
             "what": "something they released" if (release := reason == "launch" and happened.released(e))
             else readiness.label(reason), "quote": (quote := _told(e, untold)),
             "source_url": e.get("source_url") or "", "subject_id": e["subject_id"], "reason": reason,
             "type": e["event_type"], "release": release,
             "shipped": happened.shipped(e["quote"]) if release and quote not in NOTES else ""}
            for reason in call.reasons
            for e in [by_id[i] for i in call.evidence.get(reason, []) if i in by_id][:per_reason]]


def _link(url):
    """A URL inside Slack's <url|text>: a | or > would end it early."""
    return url.replace("|", "%7C").replace(">", "%3E").replace("<", "%3C")


def _signal_lines(listed):
    return "\n".join(f"• {s['date']} · {s['what']}" + (f" · {q}" if (q := _quoted(s["quote"])) else "")
                     + (f" <{_link(s['source_url'])}|source>" if s["source_url"] else "") for s in listed)


# What happened, one clause per reason, in plain words: {who} is their first name in the first clause and "they"
# after it, {whose} likewise. The date is the evidence's public date: "{on}" ("on Aug 20", "in Nov 2025") only where
# the event is the public act itself; else "as of {when}" (true by then) or when it was announced. Fixed words only,
# so the card cannot say anything the evidence does not, and nothing reads as a guess they'll leave.
SAY = {
    "tenure_milestone": "{who} reached a work anniversary in the role they started {on}",
    "exec_departure": "an executive left {whose} employer, as of {when}",
    "pi_departure": "the head of {whose} lab left, as of {when}",
    "team_exodus": "several of {whose} colleagues left, as of {when}",
    "coauthor_departure": "{whose} close coauthor changed jobs, as of {when}",
    "placement_end": "{whose} fixed-term role is ending (announced {on})",
    "own_departure": "{who} left their job or said they're leaving, as of {when}",
    "company_closure": "{whose} employer said {on} that it is closing",
    "retention_cliff": "{whose} employer's acquisition, closed {on}, reached its retention cliff",
    "new_work": "{who} put out new work {on}",
    "launch": "{who} announced a launch {on}",
    "release": "{who} shipped {what} {on}",  # a launch that is a release (happened.released): never "a launch"
    "acquired": "{who} posted {on} that their company was acquired",
    "paper_v1": "{who} posted a new preprint {on}",
    "paper_accepted": "{who} had a paper accepted {on}",
    "paper_talk": "{whose} talk on their paper was announced {on}",
    "rhythm_change": "{whose} publishing pace changed, as of {when}",
    "own_precedent": "what came before {whose} past moves happened again, as of {when}",
    "work_in_progress": "{who} posted work in progress {on}",  # the reader keeps no note of what it matched
    "technical_ask": "{who} asked in public for help GI could give {on}",
    "just_submitted": "{who} said {on} that they'd submitted work that isn't public yet",
    "topic_drift": "{whose} posts turned toward GI's topics, as of {when}",
    "new_field_contact": "{who} started talking with new people in GI's field, as of {when}",
    "posting_burst": "{who} posted far more than usual, as of {when}",
    "similar_path": "{whose} path matched what past hires showed before joining, as of {when}",
    "associate_joined": "{whose} close colleague joined a lab like GI, as of {when}",
    "acquisition_closed": "{whose} employer was acquired, as of {when}",
    "acquisition": "a deal to buy {whose} employer was reported {on}",  # a headline or post, not a filing
    "warn_notice": "{whose} employer filed a layoff notice {on}",
    "layoffs_reported": "layoffs at {whose} employer were reported {on}",
    "auditor_change": "{whose} employer changed auditors {on}",
    "late_filing": "{whose} employer filed its accounts late {on}",
    "grant_end": "{whose} grant is ending (announced {on})",
    "gi_citation": "GI's own work cited {whose} work {on}",
    "gi_attention": "GI engaged with {whose} work {on}",
    "self_stated_availability": "{who} said {on} when they'd like to hear from people",
    "stated_follow_up": "{who} named a date to talk {on}",
    "private_note": "a GI teammate noted {on} that {who} may be open to talking",
}


# One reason alone: why writing this month is natural. Never that they might leave (Justin's standing rule).
NOW = {
    "paper_accepted": "An acceptance is a natural moment to write about the work.",
    "paper_v1": "New work is a natural reason to write while it's fresh.",
    "new_work": "New work is a natural reason to write while it's fresh.",
    "launch": "A launch is a natural moment to write about the work.",
    "release": "A new release is a natural moment to write about the work.",
    "acquired": "An acquisition is a natural moment to congratulate them and ask about the work.",
    "work_in_progress": "Writing while it's still in progress reaches them before it's public and everyone notices.",
    "just_submitted": "Writing before it's public reaches them before everyone else notices.",
    "technical_ask": "It's an open door: GI can help with what they asked, so a note is welcome, not cold.",
    "topic_drift": "Their interests are moving toward GI's, so GI's work will feel relevant to them now.",
    "gi_citation": "Their work already engages with GI's, so a note will feel relevant, not cold.",
    "self_stated_availability": "They said so themselves: this is when they asked to hear from people.",
}
# Their own news: what the card and the Slack ask lead with, before anything that happened around them.
OWN_NEWS = ("paper_accepted", "paper_v1", "new_work", "launch", "paper_talk", "work_in_progress", "just_submitted",
            "technical_ask", "acquired", "gi_citation", "gi_attention")
# Clauses that bring in another person (an executive, a coauthor, a teammate): the next clause names them again
# rather than saying "they", which could mean either.
OTHERS = ("exec_departure", "pi_departure", "team_exodus", "coauthor_departure", "associate_joined", "private_note")
# Reasons worded by what their newest evidence is (its event type, when SAY has words for it): a paper accepted is not a
# preprint, and a headline is not a filing.
BY_TYPE = ("new_work", "acquisition_closed", "warn_notice")

# What would change the call, in plain words, for each falsifier app.moments names: "{first}" is their first name.
# A claim "before the window closes" gets the window's date.
CHANGE = {
    "A promotion or new title before the window closes": "{first} gets promoted",
    "They start a new role elsewhere before the window closes": "{first} starts a new role elsewhere",
    "A new equity or retention grant": "{first} gets a new equity or retention grant",
    "They are promoted into the departed officer's scope": "{first} is promoted into the departed executive's scope",
    "A successor is named and the team holds": "a successor is named and the team holds",
    "A new PI takes the group and they stay": "a new head takes the lab and {first} stays",
    "No further departures from the team within 90 days": "no one else leaves the team within 90 days",
    "They publish with the remaining team at the same affiliation": "{first} keeps publishing with the team that stayed",
    "An extension or a new end date is announced": "the role is extended or gets a new end date",
    "They announce a new role or say they are taking time off": "{first} announces a new role or time off",
    "The closure is called off, or an acquirer keeps the team": "the closure is called off, or a buyer keeps the team",
    "A new grant or retention bonus before the cliff date": "{first} gets a new grant or retention bonus before the cliff",
    "A promotion before the cliff date": "{first} gets promoted before the cliff",
    "They announce a new role or promotion on the back of the work": "{first} announces a new role or promotion on the back of the work",
    "No engagement with the work within the window": "the work gets no engagement before the window closes",
    "The talk is cancelled or given by a coauthor": "the talk is cancelled or a coauthor gives it",
    "Their cadence resumes: a new paper at the old rhythm": "{first} goes back to publishing at their old pace",
    "Integration retention grants for the team": "the team gets retention grants after the acquisition",
    "The layoffs are called off or their unit is not affected": "the layoffs are called off or {first}'s unit isn't affected",
    "The next annual report is filed on time with a clean opinion": "the next annual report is filed on time and clean",
    "The company files and the restatement is clean": "the company files and the restatement is clean",
    "The grant is renewed or a new grant starts": "the grant is renewed or a new one starts",
    "No further engagement with GI's work within 60 days": "{first} doesn't engage with GI's work again within 60 days",
    "The attention was incidental: no follow-up within 30 days": "nothing follows within 30 days",
    "It was a passing post: nothing more on the work within three weeks": "nothing more comes of the work within three weeks",
    "They say the ask is answered or closed": "{first} says the ask is answered",
    "It goes public before we write: then everyone can see it": "the work goes public before anyone writes",
    "The precedent does not hold: their next signals diverge from the past hire's path":
        "{first}'s next signals stop matching the past hire's path",
    "The associate leaves the new lab again, or they were never close": "the colleague leaves the new lab again",
    "Their next posts return to their old topics": "{first}'s next posts go back to their old topics",
    "The exchanges were one-offs: no further contact within a month": "the new conversations were one-offs",
    "Their posting drops back to its usual rate with nothing to show": "{first}'s posting drops back to normal with nothing to show",
    "They restate a later date": "{first} names a later date",
    "A newer typed note from a GI teammate replaces it": "a newer note from a GI teammate replaces it",
}


def _short(day, as_of):
    """A date as a person writes it: "Aug 20" this year, "Aug 20, 2025" otherwise, "Nov 2025", or "2025"."""
    if not day:
        return "undated"
    if len(day) >= 10:
        when = datetime.strptime(day[:10], "%Y-%m-%d")
        return f"{when:%b} {when.day}" + ("" if day[:4] == as_of[:4] else f", {day[:4]}")
    return datetime.strptime(day[:7], "%Y-%m").strftime("%b %Y") if len(day) >= 7 else day


def _on(day, as_of):
    """"on Aug 20", or "in Nov 2025" / "in 2025" at a coarser precision."""
    return f"{'on' if len(day or '') >= 10 else 'in'} {_short(day, as_of)}"


def _month(day):
    """"Nov 2025": when work together happened, at a month's precision at most."""
    return datetime.strptime(day[:7], "%Y-%m").strftime("%b %Y") if day and len(day) >= 7 else day or "undated"


def _join(parts, word="and"):
    """"a and b", "a, b, and c"; "a, and b" when a part has its own comma, so the "and" reads as the list's."""
    parts = [p for p in parts if p]
    comma = len(parts) > 2 or any("," in p for p in parts)
    return "" if not parts else parts[0] if len(parts) == 1 else \
        ", ".join(parts[:-1]) + f"{',' if comma else ''} {word} {parts[-1]}"


def _day(day):
    """The first day a date at any precision covers: "2026-08" is Aug 1, "2025" is Jan 1."""
    day = day[:10]
    return datetime.strptime(day + {4: "-01-01", 7: "-01"}.get(len(day), ""), "%Y-%m-%d").date()


def _ordered(r):
    """The call's reasons with their signals: their own news first (a paper, then GI citing it), then the rest of
    their own record, then what happened around them, each group in the engine's order. The card reads as a note
    about their work."""
    rows = [(reason, [s for s in r.signals if s["reason"] == reason]) for reason in r.call.reasons]
    rows = [(reason, listed) for reason, listed in rows if listed]
    return sorted(rows, key=lambda row: (row[0] not in OWN_NEWS, OWN_NEWS.index(row[0]) if row[0] in OWN_NEWS else 0,
                                         row[1][0]["subject_id"] != r.subject_id))


def _clauses(r, only_theirs=False, named=False):
    """[(reason, clause, its signals)]: what happened, one clause per reason from the fixed words in SAY, each with its
    date. ``named``: every clause names them (a line each); else "they" after the first where it can't mean another
    person. ``only_theirs``: their own news only (OWN_NEWS)."""
    first, out, last = _first(r.name), [], None
    for reason, listed in _ordered(r):
        if only_theirs and reason not in OWN_NEWS:
            continue
        say_name = named or not out or last in OTHERS or reason in OTHERS
        who, whose = (first, f"{first}'s") if say_name else ("they", "their")
        last = reason
        kind = listed[0]["type"] if reason in BY_TYPE and listed[0]["type"] in SAY else _release(reason, listed)
        day = listed[0]["date"]
        out.append((reason, SAY.get(kind, "{who}: " + readiness.label(reason) + ", as of {when}").format(
            who=who, whose=whose, when=_short(day, r.call.as_of), on=_on(day, r.call.as_of),
            what=listed[0].get("shipped") or "a new release"), listed))
    return out


def _release(reason, listed):
    """"release" for a launch whose newest evidence is a release (happened.released, from the post itself even when
    the card withholds its words), else the reason."""
    return "release" if reason == "launch" and listed[0].get("release") else reason


def what_happened(r, only_theirs=False):
    """What happened, one plain sentence from the fixed words in SAY, each with its date: "Rin had a paper accepted on
    Aug 20, GI's own work cited their work on Sep 10, and Rin's close coauthor changed jobs, as of Sep 1.".
    ``only_theirs``: their own news only (OWN_NEWS), for the Slack ask, which leads with their work."""
    text = _join([clause for _, clause, _ in _clauses(r, only_theirs)])
    return text[:1].upper() + text[1:] + "." if text else ""


TRIGGERS_SHOWN = 4


def trigger(r):
    """What changed: a line per reason, their own news first, each with its date, their words and a link to check
    it."""
    lines = []
    for _, clause, listed in _clauses(r, named=True)[:TRIGGERS_SHOWN]:
        s = listed[0]
        link = f" <{_link(s['source_url'])}|{_esc(_host(s['source_url']) or 'source')}>" if s["source_url"] else ""
        quoted = _quoted(s["quote"], 90)
        lines.append(f"• {_esc(_cap(clause))}{': ' + quoted if quoted else ''}{link}")
    return "\n".join(lines)


def dates(text, as_of):
    """Every ISO date in a sentence as a person writes it: "2026-11-01" is "Nov 1"."""
    return re.sub(r"\b\d{4}-\d{2}-\d{2}\b", lambda m: _short(m[0], as_of), text)


def _natural(r):
    """Why this month is a natural time to write: the one reason's own words, or that together they make it one and
    how recent they are. Never that each is enough alone (the engine weighs them together), never that they might
    leave."""
    rows = _ordered(r)
    if not rows:
        return ""
    if len(rows) == 1 and (said := NOW.get(_release(*rows[0]))):
        return said
    oldest = min((listed[0]["date"] for _, listed in rows), key=_day)
    days = (parse_time(r.call.as_of).date() - _day(oldest)).days
    n = len(rows)
    came = "came in the last month" if days <= 31 else "came in the last two months" if days <= 62 else \
        f"came {_on(oldest, r.call.as_of)}" if n == 1 else f"have come since {_short(oldest, r.call.as_of)}"
    if n == 1:
        return f"That's a natural reason to write, and it {came}."
    return f"Together they make this a natural time to write, and {'both' if n == 2 else f'all {_count(n)}'} {came}."


def _count(n):
    return {3: "three", 4: "four", 5: "five"}.get(n, str(n))


def why_now(r):
    """Why now in full, for a page with one trigger: what happened, why that makes this a natural time to write, and
    why this week (``window``). The card shows the last two under Why now, and what happened under Trigger."""
    return " ".join(filter(None, [what_happened(r), _natural(r), window(r)]))


NOUN = {"paper": "paper", "launch": "launch", "release": "release", "acquired": "acquisition", "open_to_work": "post saying they're open",
        "left_job": "move", "company_closed": "news", "two_years": "anniversary"}  # a moment, as "after the ..."


def reach_by(r):
    """The day to reach them by: about a month after the public moment the reach rests on (Just happened), else
    when the first of its windows closes; None when none closes."""
    if moment := _moment(r):
        return (parse_time(r.call.as_of) + timedelta(days=happened.WINDOW_DAYS - moment.day)).date().isoformat()
    return r.call.earliest_close[:10] if r.call.earliest_close else None


def window(r):
    """Why this week and not three months ago or three months from now: none of it was public then, and it stops
    being fresh by the reach-by day."""
    rows, as_of = _ordered(r), r.call.as_of
    if not rows:
        return ""
    oldest = min((listed[0]["date"] for _, listed in rows), key=_day)
    before = ("Three months ago none of this was public" if (parse_time(as_of).date() - _day(oldest)).days <= 90
              else f"None of this was public before {_short(oldest, as_of)}")
    if not (by := reach_by(r)):
        return before + "."
    after = (f"a month after the {NOUN.get('release' if moment.release else moment.kind, 'news')}, it's old news"
             if (moment := _moment(r)) else
             "the first of these stops being fresh")
    return f"{before}, and by {_short(by, as_of)}, {after}: reach them before then."


def _claim(f):
    """A falsifier's claim without its "(check on ...)" and source link."""
    claim = f["claim"].rstrip(".")
    while (bare := re.sub(r"\s*\([^()]*\)$", "", claim)) != claim:
        claim = bare.rstrip(".")
    return claim


def _changes(r):
    """The top one or two things that would change the call, as one plain sentence from the fixed words in CHANGE:
    "Rin gets promoted or starts a new role elsewhere before Oct 19."."""
    first, as_of, parts = _first(r.name), r.call.as_of, []
    for f in r.falsifiers:
        by = _short(f["by"], as_of)
        if f["kind"] == "news":
            parts.append(("has no public news", f"by {by}", True))
        elif f["kind"] == "follow_up":
            parts.append(("nothing more comes of this work", f"by {by}", False))
        elif (claim := _claim(f)) in CHANGE:  # anything else is a fact to check first: see _checking
            text, when = CHANGE[claim], f"before {by}" if claim.endswith(" before the window closes") else ""
            mine = text.startswith("{first} ")
            parts.append((text.removeprefix("{first} ").format(first=first), when, mine))
        if len(parts) == 2:
            break
    if not parts:
        return ""
    (text, when, mine), rest = parts[0], parts[1:]
    out = f"{first} {text}" if mine else text
    if rest:
        text2, when2, mine2 = rest[0]
        if mine and mine2 and when2 == when:  # "Rin gets promoted or starts a new role elsewhere before Oct 19"
            out += f" or {text2}"
        else:
            out += (f" {when}" if when else "") + ", or " + (f"{first} {text2}" if mine2 else text2)
            when = when2
    out += f" {when}" if when else ""
    return out[:1].upper() + out[1:] + "."


def _checking(r):
    """The engine's own fact to check behind the call, when it named one: "Find out why the filing was late and
    whether it lands on them: '...' (url)" reads as its instruction, the quote and link left to the sources."""
    for f in r.falsifiers:
        if f["kind"] == "person" and (claim := _claim(f)) not in CHANGE:
            return re.split(r":\s*['\"“]", claim, maxsplit=1)[0] + "."
    return ""


def _sure(r):
    """What the level rests on: readiness's rule (high at readiness.STRONG on hand-set weights, said without the
    numbers), then the scorecard's rate for the sign when one covers the role. The level is never a measured rate."""
    c, level = r.confidence, r.call.confidence.capitalize()
    rule = f"{level} by the engine's own rules, not a measured rate"
    if not c.get("measured"):
        return f"{rule}. Not yet tested on past hires for this role."
    (a, b), (x, y) = c["before_moment"], c["ordinary"]
    if not c.get("beats_ordinary"):  # the measurement's own warning: not shown to beat an ordinary week yet
        return (f"{rule}. On past people for this role this kind of sign was seen before a public moment {a} of {b} "
                f"times" + (f" and in a quiet stretch {x} of {y} times" if y else "")
                + f": {ping.missed(a, y, c.get('lift'), c.get('p'), card=True)}.")
    return (f"{rule}. On past people for this role, this kind of sign came before {a} of {b} of their public moments"
            + (f" and in only {x} of {y} quiet stretches." if y else "."))


def _host(url):
    return urlparse(url).netloc.removeprefix("www.") if url else ""


def _how(r):
    """How the sender reaches them, one plain line."""
    chan, me, first = r.channel, _esc(_first(r.sender["name"])), _esc(_first(r.name))
    linkedin = ", or use LinkedIn if they're connected" if chan.check_first else ""
    target = _esc(chan.target)
    if chan.kind == "x_dm":
        return f"{me} can DM {first} on X ({target}), since {first} follows {me} there{linkedin}."
    if chan.kind == "email":
        where = f"the address on {_esc(_host(chan.source))}" if chan.source else "their public address"
        return f"{me} can email {first} at {where}{linkedin}."
    if chan.kind == "linkedin":
        most = ", where they post most," if "post most on LinkedIn" in chan.reason else ""
        return f"{me} can message {first} on LinkedIn{most} with a connection note if they aren't connected."
    if chan.kind == "x_request":
        no_x = "" if r.sender.get("x_handle") else f" {me} has no X account on file yet."
        return (f"{me} can message {first} on X ({target}); it lands in their requests, so if those are closed, reply "
                f"to their post first{linkedin}.{no_x}")
    if chan.kind == "site":
        return f"{me} can reach {first} through their site ({target}){linkedin}."
    return f"No public way to reach {first} found yet."


def _lead(p, first):
    """A teammate who may know them: "Omar may know Rin too (both at Acme Research, 2022-2023)"."""
    mate = _esc(_first(p.teammate.name))
    if p.kind == "overlap":
        org, _, years = p.detail.removeprefix(f"{p.teammate.name} and they were both at ").rpartition(" in ")
        return f"{mate} may know {first} too (both at {_esc(org)}, {years})"
    return f"{mate} may know {first} too (worked together, {_month(p.latest or '')})"


def knows(history, team, as_of):
    """(teammates who said they know them, anyone else who said so), each newest first: the ledger's "knows" by
    ``as_of`` and since their latest "wrong_person", which says the earlier ones were about someone else. Only a name
    on the team list counts as a way in; another (a Slack handle not matched to the list, someone who left) is a
    name to check first, never a claim."""
    said = []
    for e in reversed([e for e in history if parse_time(e["at"]) <= parse_time(as_of)]):
        if e["kind"] == "wrong_person":
            break
        if e["kind"] == "knows" and e.get("by"):
            said.append(e)
    names = {m.name for m in team}
    return [e for e in said if e["by"] in names], [e for e in said if e["by"] not in names]


def _said(k, first, as_of):
    """Someone at GI who said on a card that they know them: "Dana Kest knows Rin (worked together, said 3 Sep)"."""
    how = k["note"].split(".")[0].strip() if k.get("note") else ""
    word = how.split(" ", 1)[0]
    how = how[:1].lower() + how[1:] if len(word) > 1 and word[1:].islower() else how  # "Met them"; "NeurIPS", "I" stay
    return f"{_esc(k['by'])} knows {first} ({_esc(how) + ', ' if how else ''}said {_short(k['at'][:10], as_of)})"


def _way_in(r):
    """Route in, three short lines at most: the warm path (who to ask, or a lead to check, or "none found"), who
    writes, and how."""
    first, who = _esc(_first(r.name)), r.sender
    leads = [_lead(p, first) for p in r.paths if p.level == "likely"][:1]
    said = [_said(k, first, r.call.as_of) for k in r.known][:1]
    if r.ask:
        tie = next(p for p in r.paths if p.level == "confirmed" and p.teammate.name == who["name"])
        mate = _esc(_first(who["name"]))
        did = (f"{mate} wrote '{_esc(tie.work)}' with {first}" if tie.kind == "coauthor"
               else f"{first} worked on {mate}'s repo {_esc(tie.work)}")
        lines = [f"Warm path: ask {_esc(who['name'])} for an intro. {did} ({_month(tie.latest or '')}).",
                 *[f"{lead[:1].upper()}{lead[1:]}." for lead in said or leads]]
    else:
        title = _esc(who["title"]) if who.get("title") else ""
        if title and who.get("source_url"):
            title = f"<{_link(who['source_url'])}|{title}>"
        old = any(p.level == "likely" and p.kind != "overlap" for p in r.paths)  # worked together, long ago
        known = (f"No one at GI has worked with {first} in the last {REMEMBERED_YEARS} years. " if old else
                 f"No one at GI has worked with {first} that we can see. " if r.route_in["checked"] else "")
        check = (f"{_esc(r.unlisted[0]['by'])} said on Slack they know {first} but isn't on GI's team list: check who "
                 "that is first." if r.unlisted else "Ask the team first.")
        why = dates(who["why"], r.call.as_of)
        lines = [f"Warm path: {said[0]}. Ask {_esc(_first(r.known[0]['by']))} for an intro first." if said else
                 f"Warm path to check: {leads[0]}. {known}{check}" if leads else
                 f"Warm path: {'none found' if known else f'not checked yet for {first}'}. {known}{check}",
                 f"From {_esc(who['name'])}{f' ({title})' if title else ''}: {_esc(why)}."]
    return "*Route in*\n" + "\n".join(lines + [_how(r)])


def ask_teammate(r, tie):
    """The Slack message asking the teammate with the tie to reach out: their own news in fixed words, the tie, the
    role, the ask, and the draft itself, since the teammate gets this message and not the card."""
    first, mate, draft, chan = _first(r.name), _first(tie.teammate.name), r.draft, r.channel
    together = (f"You and {first} wrote '{tie.work}' together ({_month(tie.latest or '')})" if tie.kind == "coauthor"
                else f"{first} worked on your repo {tie.work} ({_month(tie.latest or '')})")
    news = what_happened(r, only_theirs=True) or what_happened(r)
    if not news.startswith((first, "GI")):
        news = news[:1].lower() + news[1:]  # "Hey Dana, an executive left Rin's employer..."
    where = {"email": f"an email to {draft['to']}, subject “{draft['subject']}”", "x_dm": "an X message",
             "x_request": "an X message", "linkedin": "a LinkedIn message"}.get(chan.kind, "a message")
    ask = (f"We're not raising the role until we've confirmed it fits them: would you be up for writing to {first} "
           f"about their work?" if r.role_free == "unconfirmed" else
           f"They're an undergraduate, so we're not raising the role: would you be up for writing to {first} about "
           f"their work?" if r.gate.rapport_only else
           "Their company was just acquired, so we're not raising the role: would you be up for "
           + (f"congratulating {first} and leaving the door open?" if r.message == "acquisition" else
              f"writing to {first} about their work?") if r.role_free else
           f"We're hiring for our {r.role['title']} role: would you be up for reaching out to {first}?")
    text = (f"Hey {mate}, {news} {together}, so a note from you would land far better than a cold one. {ask} Here's "
            f"a draft you can use or rewrite ({where}):\n\n{draft['body']}")
    if chan.kind == "linkedin" and draft["note"]:
        text += (f"\n\nIf you aren't connected yet, send this connection note instead:\n\n{draft['note']}\n\nand once "
                 f"they accept, this:\n\n{draft['after'] or draft['body']}")
    return text


def _mention(ask):
    """The teammate as a Slack @-mention when the team file has their Slack id, else their first name."""
    return f"<@{ask['slack_user_id']}>" if ask.get("slack_user_id") else _esc(_first(ask["to"]))


WHERE_SENT = {"email": "Email", "x_dm": "X message", "x_request": "X message request", "linkedin": "LinkedIn message",
              "site": "Message through their site", "none": "Message"}


def _draft(r):
    """The draft: what it is (MESSAGE), who sends it where, always a subject and the body; LinkedIn's connection note
    too."""
    chan, draft, me = r.channel, r.draft, _esc(_first(r.sender["name"]))
    head = f"*Draft: {_esc(MESSAGE.get(r.message, MESSAGE['work']))}*\n{WHERE_SENT[chan.kind]} from {me}"
    head += (f" to {_esc(draft['to'])}" if chan.kind == "email" else
             " (no channel yet: see Route in)" if chan.kind == "none" else "")
    head += f"\n*Subject:* {_esc(draft['subject'])}\n"
    if chan.kind == "linkedin" and draft["note"]:
        return (f"{head}Connection note ({len(draft['note'])} of {outreach.NOTE_MAX} characters)\n"
                f"```{_esc(draft['note'])}```\nMessage, once connected\n```{_esc(draft['after'] or draft['body'])}```")
    text = f"{head}```{_esc(draft['body'])}```"
    return text + ("\nNo connection note: connect without one, then send the message." if chan.kind == "linkedin" else "")


HEADER = {"early_sign": "Early sign", "just_happened": "Just happened", None: "Reach now"}


def behind_moment(fresh, call):
    """The public moment of the last month in the call's evidence, or None: what a Just happened reach rests on."""
    behind = {i for ids in call.evidence.values() for i in ids}
    return next((h for h in fresh if h.event_id in behind and h.day <= happened.WINDOW_DAYS), None)


def _moment(r):
    """The public moment of the last month the reach rests on (happened.kind), when it is one."""
    return behind_moment(r.happened, r.call) if r.kind == "just_happened" else None


def _also(r):
    """Other public moments of the last month, not behind the call (a LinkedIn profile's two years in the seat):
    listed, never a reason. One told late in their own post (happened.LATE_DAYS) is older news: left off; a profile's
    moment is known to the month, this month or last, and stays."""
    behind = {i for ids in r.call.evidence.values() for i in ids}
    out = []
    for h in [h for h in r.happened if h.event_id not in behind and (h.note or h.day <= happened.WINDOW_DAYS)][:2]:
        when = h.note or _short(h.date, r.call.as_of)
        link = f" (<{h.source_url.replace('|', '%7C').replace('>', '%3E')}|{_where(h.source_url)[4:] or 'source'}>)" \
            if h.source_url else ""
        out.append(f"{happened.label(h)}, {when}{link}")
    return "Also in the last month: " + "; ".join(out) if out else ""


def _who(r):
    """Their public profiles as links, then their public email or that none was found."""
    links = " · ".join(f"<{_link(p['url'])}|{_esc(p['label'])}>" for p in r.profiles) or \
        "No profiles tied to them yet: confirm who they are first."
    source = r.email and r.email.get("source_url")
    email = (f"Email: {_esc(r.email['address'])}" + (f" (listed on <{_link(source)}|{_esc(_host(source))}>)" if source
                                                      else "") if r.email else "Email: no public email found.")
    return f"{links}\n{email}"


def card(r, profile_url="", bands=None):
    """Slack Block Kit card for a Route, in the brief's order: their name, their public profiles and email; the role
    and when to reach them by; then Trigger (what changed, when, with links), Why now, Confidence (and what would
    prove it wrong), the Draft (what it is, subject and body) and Route in (a warm path or "none found"). ``bands``: the
    posted pay ranges to show (route's)."""
    call, chan = r.call, r.channel
    line = f"*{_esc(r.role['title'])}*"
    if r.kind:
        line += f" · {HEADER[r.kind]}" + (f", day {moment.day} of about {happened.WINDOW_DAYS}"
                                          if (moment := _moment(r)) else "")
    if by := reach_by(r):
        line += f" · reach by {_short(by, call.as_of)}"
    if r.role_free == "unconfirmed":
        line += " · their work only until it's confirmed"
    elif r.gate.rapport_only:
        line += " · their work only: they're an undergraduate"
    elif r.role_free == "acquisition":
        line += " · no role named: their company was just acquired"
    elif call.track == "rapport":
        line += " · lead with their work"
    confidence = ("*Confidence*\n" + _esc(_sure(r))
                  + (f"\n*What would prove it wrong:* {_esc(changes)}" if (changes := _changes(r)) else "")
                  + (f"\n*Worth checking:* {_esc(check)}" if (check := _checking(r)) else ""))
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": r.name[:150]}},
        _section(_who(r)),
        _section(line),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _esc(dates(pay.line(r.role["id"], bands), call.as_of))}]},
        {"type": "divider"},
        _section(f"*Trigger*\n{trigger(r) or 'Nothing of their own to link.'}"),
        *([{"type": "context", "elements": [{"type": "mrkdwn", "text": also}]}] if (also := _also(r)) else []),
        _section(f"*Why now*\n{_esc(' '.join(filter(None, [_natural(r), window(r)])))}"),
        _section(confidence),
        {"type": "divider"},
        _section(_draft(r)),
    ]
    if not r.outreach["checks"]["passes"]:  # a draft that fails a check is never passed off as ready
        blocks.append(_section(f"*Don't send this draft yet:* {_esc('; '.join(r.outreach['checks']['problems']))}."))
    blocks.append(_section(_way_in(r)))
    if r.ask:  # the teammate gets a Slack message, not the card: its opening, with the draft above after it
        lead = r.ask["body"].split("\n\n", 1)[0]
        blocks.append(_section(f"*Message {_mention(r.ask)} on Slack*\n```{_esc(lead)}```\nThen paste the draft above."))
    buttons = []
    if chan.url and not r.ask:  # a warm intro's draft is the teammate's to open, from the Slack message
        me = _first(r.sender["name"])
        buttons.append({"type": "button", "action_id": "open_draft", "style": "primary", "url": chan.url,
                        "text": {"type": "plain_text", "text": (f"Open draft for {me}" if
                                 chan.kind == "email" or "/compose" in chan.url else "Open channel")[:75]}})
    if profile_url and not r.profiles:  # the profiles above are links already
        buttons.append({"type": "button", "action_id": "open_profile", "url": profile_url,
                        "text": {"type": "plain_text", "text": "Profile"}})
    if buttons:
        blocks.append({"type": "actions", "elements": buttons})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                   f"Nothing is sent until a person presses send. As of {_short(call.as_of[:10], call.as_of)}."}]})
    return {"text": f"{HEADER[r.kind]}: {r.name} ({r.role['title']})", "blocks": blocks}


def role_of(context):
    """The opening a person is watched for, from config/roles.json: {id, title, jd_url}; MTS without a role,
    None for a role the config does not have."""
    role_id = config_role_id(context.role) if context and context.role else "mts-research"
    role = next((r for r in json.loads(ROLES.read_text())["roles"] if r["id"] == role_id), None)
    return {k: role[k] for k in ("id", "title", "jd_url")} if role else None


def redraft(found, again):
    """(found, not_tried, failed): the model's try at each clear route whose free draft fails its checks, strongest
    first, before pick.

    ``again(route)`` routes that person again with the model writer (outreach.drafter, which holds the run's cap:
    cached calls are free, and a draft that needs a new call is tried only while the run's spend plus its worst case
    fits). It returns None to keep the route as it was. pick holds a draft that still fails, like any failing draft,
    and likewise one left untried (outreach.OverCap) or one whose call failed (a rate limit, a server error, a refused or
    unreadable answer); the run carries on to the next. ``not_tried`` counts the untried in words ("" when none);
    ``failed`` says what went wrong with each failed call."""
    redone, untried, failed = {}, [], []
    for r in rank([r for r in found if r.gate.state == "clear" and not r.outreach["checks"]["passes"]]):
        try:
            route = again(r)
        except outreach.OverCap as over:
            untried.append(str(over))
            continue
        except providers.ProviderError as error:  # what it spent is in the budget; the free draft's card stays held
            failed.append(f"{r.name}'s model draft failed, so the card stays held: {error}")
            continue
        if route is not None:
            redone[id(r)] = route
    not_tried = f"{len(untried)} model draft{'s' if len(untried) != 1 else ''} not tried: {untried[0]}" if untried else ""
    return [redone.get(id(r), r) for r in found], not_tried, failed


def pick(store, found, as_of, top=None, drafted=False):
    """(kept, quiet): which of one run's routes get a card (scripts/route.py and scripts/replay_drill.py).

    Held and check-first routes stay quiet with the reason. So does a draft that fails a check, since it never goes
    out, unless the paid writer redoes it (``drafted``). The rest are cut to one card per person (their best role),
    then to each role's weekly room (``top`` caps it further), then to the week's room across every role, strongest
    first."""
    quiet = [r for r in found if r.gate.state != "clear"]
    # A draft that fails a check never goes out (send), so unless the paid writer redoes it, it takes no slot.
    failing = [] if drafted else [r for r in found if r.gate.state == "clear" and not r.outreach["checks"]["passes"]]
    quiet += [r.model_copy(update={"gate": contact.Clearance(state="hold", reason=(
        f"The draft fails a check ({'; '.join(r.outreach['checks']['problems'])}): fix it before a card goes out."))})
        for r in failing]
    per_role, carded = {}, {}
    for result in rank([r for r in found if r.gate.state == "clear" and r not in failing]):
        if first := carded.get(key := contact.person_key(result.subject_id)):  # one card per person: their best role
            quiet.append(result.model_copy(update={"gate": contact.Clearance(
                state="hold", reason=f"One card per person: this run cards them for {first}.")}))
            continue
        carded[key] = result.role["id"]
        per_role.setdefault(result.role["id"], []).append(result)
    kept = []
    for role_id, ranked in per_role.items():
        room = contact.room(store, role_id, as_of)
        keep = min(room, top) if top is not None else room
        kept += ranked[:keep]
        why = (f"Past this week's cap of {contact.WEEKLY_CAP} cards for the role." if keep == room
               else f"Not in the top {top} for the role this run.")
        quiet += [r.model_copy(update={"gate": contact.Clearance(state="hold", reason=why)}) for r in ranked[keep:]]
    strongest, total = rank(kept), contact.room_all(store, as_of)  # the week's cap across every role
    kept = strongest[:total]
    why = f"Past this week's cap of {contact.TOTAL_CAP} cards across all roles."
    quiet += [r.model_copy(update={"gate": contact.Clearance(state="hold", reason=why)}) for r in strongest[total:]]
    return kept, quiet


def route(store, subject_id, as_of, team, contacts=(), profile_url="", scorecard=None, cohort=None, writer=None,
          call=None, profiles=None, follows=None, bands=None):
    """A Route when readiness says reach_now, else None.

    ``call`` is journey.assess's, when the caller has it. ``profiles`` is the LinkedIn profile read
    (happened.load_profiles), for the moments the card lists. ``follows`` is the X follows pull (x_follows.load), for
    whether they follow the sender. ``bands`` is the posted pay ranges the card shows (pay.for_mode: none in the
    simulation; None reads GI's own). ``scorecard`` is a scripts/posts.py scorecard report, for confidence. ``cohort``
    is {subject_id: (name, what they wrote)} (outreach.written_by), for the name swap.

    ``writer(name, role, track, items, facts, others, sender=, tie=, note=, role_free=, message=, moments=)`` writes
    the draft with the model (outreach.write) as ``sender`` ({name, about}). It adds a LinkedIn note and its follow-up
    when ``note``, never names the role when ``role_free`` (an undergraduate, per the contact gate, or a note on their
    company's acquisition), and congratulates only ``moments``, what happened to them. Without a writer the draft is
    the free template, checked the same way."""
    context = journey.load_context(store, subject_id)
    call = call or journey.assess(store, subject_id, as_of, context.role if context else None)
    if not call or call.action != "reach_now":
        return None
    name = context.name if context else subject_id
    if not (role := role_of(context)):
        raise ValueError(f"{subject_id}: role {context.role!r} is not in config/roles.json")
    role_id = role["id"]
    team = [Teammate.model_validate(m) for m in team]
    contacts = on_file([ContactRoute.model_validate(c) for c in contacts], context,
                       x_follows.of(follows or {}, subject_id))
    events = timeline.timeline(store, subject_id, as_of)
    view = made_by_hand(journey.view(store, subject_id, as_of))  # a build is never their work in a draft
    by_id = {e["id"]: e for e in view}  # signals include employer and colleague records
    fresh = happened.moments(view, subject_id, as_of, profiles=profiles)
    behind = [by_id[i] for ids in call.evidence.values() for i in ids if i in by_id]
    history = contact.history(store, subject_id)
    read = read_profiles(context)
    gate = contact.check(history, as_of, call, behind, role_id, profiles=read)
    if gate.rapport_only:
        call = call.model_copy(update={"track": "rapport"})
    kind = happened.kind(call, fresh)
    opener = by_id.get(readiness.opener(call))
    quotable = opener if opener and not outreach.unquotable(opener["quote"].partition(REPLY_CONTEXT)[0]) else None
    # With no early sign to quote, the draft opens with their public moment of the last month (readiness.news), else
    # their own new paper behind the call.
    moment = by_id.get(readiness.news(call, view))
    news = moment if moment and moment["subject_id"] == subject_id else \
        next((by_id[i] for reason in call.reasons for i in call.evidence.get(reason, []) if i in by_id
              and by_id[i]["subject_id"] == subject_id and by_id[i]["event_type"] in PAPERS), None)
    # A reach that rests on their own public moment of the last month is about that moment (Just happened), even
    # beside an early sign: the draft opens with it.
    lately = behind_moment(fresh, call) if kind == "just_happened" else None
    if lately and (event := by_id.get(lately.event_id)) and \
            event["subject_id"] == subject_id and event["event_type"] in readiness.NEWS_TYPES:
        news, quotable = event, None
    # In deal week goodwill is thinnest: within a month of their own post that their company was acquired, whatever
    # the reach rests on, the draft asks about their work, never the role.
    deal = next((h for h in fresh if h.kind == "acquired" and h.day <= happened.WINDOW_DAYS), None)
    role_free = ("unconfirmed" if gate.state == "check_first" else "undergraduate") if gate.rapport_only else \
        "acquisition" if deal else ""
    about = circumstance(role_free, lately, news, quotable)
    # Their post about the deal, or saying they're open to work, names a layoff or something personal: no
    # congratulations and never quoted. Deal week's note is then about their work (still no role); open to work's is
    # about what else it would open with (an early sign, else their work).
    moved = {"acquisition": deal, "open_to_work": lately}.get(about)
    if moved and (post := by_id.get(moved.event_id)) and outreach.unquotable(post["quote"].partition(REPLY_CONTEXT)[0]):
        about = "work" if about == "acquisition" else circumstance(role_free, None, news, quotable)
    if about == "open_to_work":  # the draft quotes their post saying so, then the role
        quotable = by_id.get(lately.event_id)

    found, warm = paths(events, team, as_of), warmth(events, team)
    checked, not_checked = searched(events, team)
    senior = seniority(events)
    own = outreach.own_texts(view, subject_id)
    who = sender(found, warm, team, role_id, outreach.text_of(*own), senior, own)
    tie = next((p for p in found if p.level == "confirmed" and p.teammate.name == who["name"]), None)
    picked = channel(who, contacts, events, as_of)  # before the draft, so it is shaped for it
    if picked.kind == "none" and not tie and _routes(contacts).get("x") and (other := on_x(who, team, role_id, senior)):
        who, picked = other, channel(other, contacts, events, as_of)
    linkedin = picked.kind == "linkedin"
    skip = {w["url"] for w in warm}  # warmth never reaches the draft
    cited = outreach.items(view, subject_id, call, skip=skip)  # the writer's: never a post the reader read nothing off
    gi = outreach.facts(role_id)
    if role_free:
        gi = unnamed(gi, role)  # the writer never sees a fact that names the role
    # The name swap is against other people: the same person listed for another role is not one.
    others = [pair for subject, pair in (cohort or {}).items() if contact.person_key(subject) != contact.person_key(subject_id)]
    message, ran_out = None, False
    # What happened, as a MESSAGE key, whatever the draft is about (an undergraduate's note on their accepted paper): a
    # draft congratulates only that.
    moments = {"acquisition"} if about == "acquisition" else set()  # not when their post names a layoff
    moments |= {circumstance(False, None, news, None)}
    if writer:
        try:  # only the sender's name and their own work: why they were picked can hold warmth
            message = writer(name, role, "rapport" if role_free else call.track, cited, gi, others,  # never a pitch
                             sender={"name": who["name"], "about": who["about"]}, tie=tie.tie if tie else "",
                             note=linkedin, role_free=role_free, message=about, moments=moments)
        except providers.CallLimitReached:
            ran_out = True  # the run's cap on model calls: this one gets the free template, checked the same way
    # The free draft quotes only what is behind the call; its checks read every post. One that names what a post says
    # they built and fails its checks gets a second try quoting the post's line.
    for by_name in (True, False) if message is None else ():
        drafted = template(name, role, context, events, quotable, who["name"], tie, role_free, news,
                           happened.employer(profiles, subject_id, as_of, journey.employer_on(context, as_of)), about,
                           by_name)
        if not by_name and drafted["body"] == message["body"]:
            break  # it named nothing: the first try stands
        opened = quotable or news  # the item the draft opens with, when it opens with one
        note = drafted["note"] if linkedin else ""
        after = outreach.follow_up(drafted["body"], note, name, role) if note else ""
        every = outreach.items(view, subject_id, call, skip=skip, every_post=True)
        message = {**drafted, "note": note, "after": after, "model": "template", "fallback": ran_out,
                   "cites": [i["id"] for i in every if opened and i["url"] == opened["source_url"]][:1]}
        message["checks"] = outreach.check(message, every, role, gi, others, name, as_of, who["name"], role_free,
                                           moments)
        if message["checks"]["passes"]:
            break  # else the second try, quoting the line, stands: a held card reads as it did before
    confidence, falsifiers = ping.confidence(call, role_id, scorecard), ping.falsifiers(call, opener)
    chan, draft = fill(picked, who, contacts, message)
    level = "confirmed" if any(p.level == "confirmed" for p in found) else \
        "none found" if set(checked) & set(ROUTE_SOURCES) else "not checked"
    result = Route(subject_id=subject_id, name=name, role=role, call=call, paths=found, warmth=warm, channel=chan,
                   draft=draft, opener=opener or (news if call.track == "rapport" else None), outreach=message,
                   confidence=confidence, falsifiers=falsifiers, card={}, kind=kind, happened=fresh,
                   route_in={"level": level, "checked": checked, "not_checked": not_checked}, sender=who, gate=gate,
                   signals=signals(call, by_id), role_free=role_free, message=about,
                   profiles=tied(history, as_of, read), email=public_email(contacts))
    result.known, result.unlisted = knows(history, team, as_of)
    if tie:  # a teammate worked with them: ask that teammate on Slack; the draft above is theirs to send
        result.ask = {"to": who["name"], "slack_user_id": who["slack_user_id"], "body": ask_teammate(result, tie)}
    result.card = card(result, profile_url, bands)
    return result


def strength(call):
    """Sort key, strongest first: a pitch before a note about their work, then readiness, then the strongest
    reason to write."""
    return (call.track != "pitch", -call.score, -call.families.get("reason", 0))


def rank(routes):
    """Strongest first (strength), the freshest reason to write breaking ties. The top few per role are the ones
    worth GI's attention this week."""
    newest = sorted(routes, key=lambda r: (r.opener or {}).get("event_date") or "", reverse=True)
    return sorted(newest, key=lambda r: strength(r.call))


def builder_url(card):
    """Slack's Block Kit Builder with the card loaded: see it rendered without posting anything."""
    return "https://app.slack.com/block-kit-builder#" + quote(json.dumps({"blocks": card["blocks"]}), safe="")


def as_test(card, name=None):
    """The demo's card marked as a test at the top and in the notification, so nobody takes a made-up person for a
    real call; its buttons, links and @-mentions go, since an invented id can still be someone real's. ``name``: the
    one made-up person on it; without one (the week's inbox), everyone on it is made up."""
    head, *rest = card["blocks"]
    who = f"{_esc(name)} is a made-up person" if name else "everyone on it is made up"
    note = {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"Test card from the demo: {who} and nothing on this card is real. Its links and buttons are removed."}]}
    head = {**head, "text": {**head["text"], "text": f"Test · {head['text']['text']}"[:150]}}
    rest = json.loads(re.sub(r"<@[A-Z0-9]+>", "a teammate", re.sub(r"<[^<>|]+\|([^<>]+)>", r"\1", json.dumps(
        [b for b in rest if b["type"] != "actions"]))))
    return {"text": f"Test, made-up {'person' if name else 'people'}: {card['text']}", "blocks": [head, note, *rest]}


def send(store, route, to, now=None, client=None, test=False, thread=None):
    """The one way a person's card leaves. It must be clear, its draft must pass its checks, and it must fit its
    role's weekly cap and the week's cap across roles. The card is recorded in the ledger.

    ``to`` is the Slack app's poster (app/slack.py), which adds the ledger buttons, or an incoming webhook's URL.
    ``test`` marks the demo's made-up person, whose buttons record nothing. ``thread`` is the morning list's message
    (``send_morning``) the card goes under; a webhook cannot thread, so there the card follows the list. The ledger is
    read again as of the send, so anything recorded since route() (or after a replay's as-of) counts.
    """
    with sending(store):  # checked, posted and recorded before any other sender checks
        now = now or iso()
        for gate in (route.gate, contact.check(contact.history(store, route.subject_id), now, route.call,
                                               role_id=route.role["id"])):
            if gate.state != "clear":
                raise ValueError(f"{route.name} is not cleared to contact: {gate.reason}")
        if not (checks := route.outreach["checks"])["passes"]:  # the card would say "Don't send this draft yet"
            raise ValueError(f"{route.name}'s draft fails a check ({'; '.join(checks['problems'])}), so no card goes "
                             "out")
        if not contact.room(store, route.role["id"], now):
            raise ValueError(f"{route.role['title']} already has this week's {contact.WEEKLY_CAP} cards")
        if not contact.room_all(store, now):
            raise ValueError(f"This week's {contact.TOTAL_CAP} cards across all roles are already out")
        _post(route.card, to, client, route, test, thread)
        return contact.record(store, route.subject_id, "pinged", at=now, role_id=route.role["id"],
                              evidence=[i for ids in route.call.evidence.values() for i in ids], score=route.call.score)


def send_digest(card, to, client=None, thread=None):
    """Post a card that names no one to contact to GI's channel, through the Slack app or a webhook (``to`` as in
    ``send``): the Monday post and its invites, follow-ups and new starts (app/weekly.py). Each reach in the week
    goes through ``send`` instead, which records it. ``thread`` is the ts of the post to put the card under (an item
    under the Monday post); a webhook cannot thread."""
    return _post(card, to, client, thread=thread) if thread else _post(card, to, client)


def morning(routes, as_of):
    """The morning's short list for GI's channel: who to reach, strongest first, one line each on why and by when.
    Each person's full card goes in its thread (``send_morning``). None when there is no one: nothing posts."""
    if not routes:
        return None

    def why(r):
        said = _clauses(r, named=True)
        return " · ".join(filter(None, [HEADER[r.kind] if r.kind else "", _cap(said[0][1]) if said else ""]))
    lines = [f"• *{_esc(r.name)}*, {_esc(r.role['title'])}: {_esc(why(r))}"
             + (f" · reach by {_short(by, as_of)}" if (by := reach_by(r)) else "") for r in routes]
    n = len(routes)
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Who to reach this morning"}},
              _section(f"*{n} {'person' if n == 1 else 'people'}, strongest first*\n" + "\n".join(lines)),
              {"type": "context", "elements": [{"type": "mrkdwn", "text":
               "Each card, with its draft and buttons, is in the thread. Nothing is sent until a person presses send."}]}]
    return {"text": "Who to reach this morning: " + ", ".join(r.name for r in routes), "blocks": blocks}


def sendable(store, routes, now):
    """The routes ``send`` would post as of ``now``, in order: clear at routing and still clear on the ledger, with a
    draft that passes its checks, the person's first in the list, and within their role's weekly cap and the week's
    cap across roles, counting the ones before them. A list that names people (the morning list, the Monday brief)
    lists only these, so it never names someone whose card then stays back."""
    out, taken, people = [], Counter(), set()
    for r in routes:
        role, key = r.role["id"], contact.person_key(r.subject_id)
        clear = contact.check(contact.history(store, r.subject_id), now, r.call, role_id=role)
        if (key not in people and r.gate.state == "clear" and clear.state == "clear" and r.outreach["checks"]["passes"]
                and contact.room(store, role, now) > taken[role] and contact.room_all(store, now) > len(out)):
            taken[role] += 1
            people.add(key)  # one card per person, whatever roles they are watched for
            out.append(r)
    return out


WAIT_SECONDS = 600  # how long a sender waits for another before it gives up and posts nothing (the next run tries)
_held, _held_lock = {}, threading.Lock()  # per ledger file: (its thread lock, how deep this process holds it)


@contextmanager
def sending(store, wait=None):
    """One sender at a time for a ledger, across processes and threads.

    The morning list (``send_morning``, from route.py --send) and the Monday post (weekly.publish, from the Slack app)
    each hold it from choosing who gets a card to the last card's ledger entry, and read the time inside it, so
    whichever goes second sees the first one's cards. The lock is a file beside the ledger's sqlite file. A store with
    no file (in memory, or not sqlite; the timing store always is) has only this process's threads to wait for. Taken
    again inside, it doesn't wait for itself. A sender still waiting after ``wait`` seconds (default WAIT_SECONDS)
    raises RuntimeError and nothing posts."""
    import fcntl  # POSIX: the Mac and the cloud; only a sender needs it

    path = contact._file(store) if store is not None else None
    with _held_lock:
        lock, depth = _held.setdefault(path, (threading.RLock(), [0]))
    with lock:
        depth[0] += 1
        try:
            if path is None or depth[0] > 1:
                yield
                return
            with open(path.with_name(path.name + ".sending"), "a") as held:
                deadline, said = time.monotonic() + (WAIT_SECONDS if wait is None else wait), False
                while True:
                    try:
                        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # closing the file frees it
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            raise RuntimeError("Another sender kept the ledger's send lock too long, so nothing was "
                                               "posted; the next run tries again.") from None
                        if not said:
                            print("Waiting for the other sender (a morning list or the Monday post) to finish.",
                                  file=sys.stderr)
                            said = True
                        time.sleep(0.2)
                yield
        finally:
            depth[0] -= 1


def send_morning(store, routes, to, now=None, client=None, test=False, held=None):
    """The morning's post: the short list (``morning``), then each person's card in its thread, through ``send``.
    Returns [(route, the ledger entry it recorded)] for the cards sent.

    Only the people ``sendable`` keeps are listed, and nothing posts when no one is left. ``test`` marks the demo's
    made-up people: every card and the list are marked as a test. A card that fails after the list posted (the ledger
    changed in the moment since, or Slack refused it) stays back and the rest still go; ``held``, a list, gets
    (route, why) for each. It holds ``sending`` throughout, so a Monday post going out at the same moment waits."""
    with sending(store):
        now = now or iso()  # read once the other sender is done: its cards count
        ready = [r.model_copy(update={"card": as_test(r.card, r.name)}) if test else r
                 for r in sendable(store, routes, now)]
        if not (listed := morning(ready, now)):
            return []
        posted = _post(as_test(listed) if test else listed, to, client)
        thread = posted.get("ts") if isinstance(posted, dict) else None
        sent = []
        for r in ready:
            try:
                sent.append((r, send(store, r, to, now, client, test, thread)))
            except (ValueError, RuntimeError) as e:  # never a token in either: Slack's refusal says only its status
                if held is not None:
                    held.append((r, str(e)))
        return sent


def _post(card, to, client=None, route=None, test=False, thread=None):
    """Post the card as the Slack app (a person's card gets its ledger buttons; ``thread`` puts it under the morning
    list) or to an incoming webhook, which cannot thread. Only send(), send_digest(), send_morning() and
    accounts.send() call this. Nothing posts while sending is paused (contact.pause)."""
    if pause := contact.paused():
        raise ValueError(f"{contact.said(pause)} Nothing was posted.")
    if not isinstance(to, str):
        return to.post(card, route, test, thread)
    webhook_url = to
    if not webhook_url.startswith("https://"):
        raise ValueError("Slack webhook must be an https URL")
    client = client or httpx.Client(timeout=10)
    try:
        response = client.post(webhook_url, json=card)
    except httpx.HTTPError as e:  # a Mac just awake with no network; the type only, as the message may carry the URL
        raise RuntimeError(f"Slack could not be reached: {type(e).__name__}") from None
    if response.is_error:  # not raise_for_status: its message carries the webhook URL, which is a secret
        raise RuntimeError(f"Slack refused the card: {response.status_code} {response.text[:200]}")
    return response.text


def seed(store, data):
    """Load a {contexts, events} file into a store: the demo's and tests' invented people."""
    for ctx in data.get("contexts", []):
        journey.save_context(store, journey.PersonContext.model_validate(ctx))
    for e in data["events"]:
        day = e["event_date"]
        timeline.add_event(store, {
            "subject_type": "org" if e["subject_id"] == "gi" else "person", "tier": 1, "extractor": "fixture",
            "source_version_hash": "fixture", "date_precision": {4: "year", 7: "month", 10: "day"}[len(day)],
            "observed_at": f"{day}T00:00:00+00:00", **e})
