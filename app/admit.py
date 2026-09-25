"""Who joins the watchlist: candidates from any source, kept only when they post enough to show a sign and every
profile the engine would read for them can be tied to a page of theirs on another site.

One card per person, ever, caps what a watchlist can give, and only people who post can give a card at all: the
scorecard's floor is one post a month (scorecard.MIN_POSTS), and a card is held until identity records tie every
profile the engine reads (contact.untied). So a candidate is scored before joining, free:
  activity  their own posts, comments and replies on file in the timing store over the last year (people pulled
            before), and their GitHub: repos they made and public activity (github.fetch, the daily run's reader
            input). An X or LinkedIn no one ever pulled is "unmeasured": a paid pull would say.
  ties      only what the person wrote about themselves: their GitHub profile's own X account, site and linked
            accounts (github.profile). A handle that merely spells their name is never proposed; a GitHub that names
            a different X account than the one on file ties nothing, and one under another name than the
            candidate's is not taken as theirs at all: its X, its name and its activity count for nothing.
The result is a sheet for a yes or no per person (``sheet``) and the rows each yes would add: a people file row
and identity records in scripts/contacts.py import's shape. Nothing here writes to the people file or the ledger.
"""

from datetime import timedelta
import ipaddress
import json
import re
import unicodedata
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError, field_validator

from . import contact
from .detectors import POST_TYPES
from .models import parse_time
from .role_compiler import ROLE_IDS
from .scorecard import MIN_POSTS
from .sources import github
from .sources.social import GITHUB_LOGIN, HANDLE, Person, linkedin_url

YEAR = timedelta(days=365)
GITHUB_WORK = ("github_repo", "github_activity")  # their own work; a star is someone else's
ROLES = {r["id"] for r in json.loads((contact.CONFIG / "roles.json").read_text())["roles"]}
ENGINE_READS = ("x.com", "twitter.com", "github.com", "linkedin.com")  # a profile there is no site of their own
NOT_A_HANDLE = {"home", "i", "intent", "share", "search", "hashtag", "explore", "settings", "messages"}


def role_id(value):
    """GI's role id for a role as the research lists write it ("mts", "product_designer"), or ""."""
    value = (value or "").strip().lower().replace("_", "-")
    value = ROLE_IDS.get(value, value)
    return value if value in ROLES else ""


class Candidate(BaseModel):
    """One person someone proposes, from any source: a research list, a GitHub search, a name Justin typed."""
    name: str = ""
    role: str = Field(min_length=1)
    x_handle: str = ""
    linkedin_url: str = ""
    github: str = ""
    evidence_urls: list[str] = Field(default_factory=list)
    source: str = ""  # where the candidate came from, in a few words
    note: str = ""

    @field_validator("name", "x_handle", "linkedin_url", "github", "source", "note", mode="before")
    @classmethod
    def _none_is_blank(cls, value):
        return "" if value is None else value

    @field_validator("role")
    @classmethod
    def _role(cls, value):
        if not (found := role_id(value)):
            raise ValueError(f"not one of GI's roles: {value!r}")
        return found

    @field_validator("x_handle")
    @classmethod
    def _handle(cls, value):
        value = value.strip().lstrip("@")
        if value and not HANDLE.fullmatch(value):
            raise ValueError(f"not an X handle: {value!r}")
        return value

    @field_validator("github")
    @classmethod
    def _login(cls, value):
        value = value.strip()
        if value and not GITHUB_LOGIN.fullmatch(value):
            raise ValueError(f"not a GitHub login: {value!r}")
        return value

    @field_validator("linkedin_url")
    @classmethod
    def _linkedin(cls, value):
        value = value.strip()
        if value and not (value := linkedin_url(value if "://" in value else "https://" + value)):
            raise ValueError("not a LinkedIn profile")
        return value

    def profiles(self):
        """The profile pages the daily run would read for them, as contact.untied reads them."""
        return [u for u in (self.x_handle and f"https://x.com/{self.x_handle}", self.linkedin_url,
                            self.github and f"https://github.com/{self.github}") if u]

    def label(self):
        return self.name or self.github or self.x_handle or self.linkedin_url


class Result(BaseModel):
    candidate: Candidate
    verdict: str  # admit, tie_first, too_quiet, unmeasured, skipped
    why: str
    posts_a_year: int | None = None  # their own X and LinkedIn items on file over the last year; None: never pulled
    github_a_year: int | None = None  # repos made and public activity over the last year; None: no GitHub
    github_name: str = ""  # the name their GitHub profile gives, to check against the candidate's
    add_x: str = ""  # an X account their GitHub names, when none is on file
    records: list[dict] = Field(default_factory=list)  # identity records, contacts.py import's shape


def load(rows, role=None):
    """Candidates from a list of dicts in any of the research files' shapes (affiliation and reason go in the note);
    ``role`` for rows that name none. Rows that won't read are returned apart, with why."""
    out, bad = [], []
    for row in rows:
        try:
            out.append(Candidate.model_validate({
                **row, "role": row.get("role") or role or "", "evidence_urls": row.get("evidence_urls") or [],
                "note": row.get("note") or "; ".join(str(row[k]) for k in ("affiliation", "reason") if row.get(k))}))
        except ValidationError as err:
            first = err.errors()[0]
            bad.append((row.get("name") or row.get("github") or "?", f"{'.'.join(map(str, first['loc']))}: {first['msg']}"))
    return out, bad


def _words(name):
    """A name's words in lowercase, accents and punctuation dropped ("José  García." -> ["jose", "garcia"])."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return re.findall(r"[^\W_]+", plain.casefold())


def _slug(text):
    """What a people file id is made of: a name's or handle's ASCII words joined by "-"."""
    return "-".join(w for w in (w.encode("ascii", "ignore").decode() for w in _words(text)) if w)


def names_match(a, b):
    """Whether two names can be one person's: every word of the shorter is a word of the longer, or its initial
    ("Rin V." and "Rin Vale"; never "Alex Chen" and "Alex Johnson")."""
    short, long = sorted((_words(a), _words(b)), key=len)
    return bool(short) and all(any(w == u or len(w) == 1 and u.startswith(w) or len(u) == 1 and w.startswith(u)
                                   for u in long) for w in short)


def same_person(a, b):
    """Whether two candidates are one person: a profile page in common, or the same name, word for word."""
    name = _words(a.name)
    return bool(contact._pages(a.profiles()) & contact._pages(b.profiles()) or name and name == _words(b.name))


def known(people, team, candidate):
    """Why the candidate is someone already known, or "": on the people file (``same_person``), or on GI's team file
    (one of their profiles is a member's X, GitHub, LinkedIn or listed page, or the same name)."""
    for row in people:
        other, _ = load([{**row, "role": "backend"}])  # any role: only their profiles and name are compared
        if other and same_person(candidate, other[0]):
            return f"already on the people file as {row.get('person_id') or row.get('subject_id')}"
    name, theirs = _words(candidate.name), contact._pages(candidate.profiles())
    for m in team:
        pages = contact._pages([u for u in (m.get("x_handle") and f"https://x.com/{str(m['x_handle']).lstrip('@')}",
                                            m.get("x") and f"https://x.com/{str(m['x']).lstrip('@')}",
                                            m.get("github") and f"https://github.com/{m['github']}",
                                            m.get("linkedin_url"), m.get("source_url")) if u])
        if theirs & pages or name and name == _words(m.get("name") or ""):
            return "on GI's team"
    return ""


def stated_x(about):
    """The X accounts their GitHub profile names: its X field, then X links among its linked accounts."""
    found = [(about.get("twitter_username") or "").strip().lstrip("@")]
    for url in about.get("social") or []:
        site, path = contact._page(url)
        if site == "x.com" and (handle := path.strip("/")) and "/" not in handle:
            found.append(handle)
    kept = {}
    for h in found:
        if HANDLE.fullmatch(h) and h.lower() not in NOT_A_HANDLE:
            kept.setdefault(h.lower(), h)  # X handles ignore case: @Rin_V is @rin_v
    return list(kept.values())


def not_theirs(candidate, about):
    """Why this GitHub is not taken as the candidate's, or "": an organization's, or one under another name."""
    if about.get("type") == "Organization":
        return "an organization's GitHub, not a person's"
    if (named := about.get("name", "")) and candidate.name and not names_match(named, candidate.name):
        return f"check the GitHub is theirs: it is named {named!r}, not {candidate.name!r}"
    return ""


def enriched(candidate, about):
    """The candidate with what their GitHub profile says filled in where the row is blank: its name, and the X
    account it names when it names one; unchanged when the GitHub is not theirs (``not_theirs``). The known-person
    check and the posts on file read this."""
    if not_theirs(candidate, about):
        return candidate
    xs = stated_x(about)
    return candidate.model_copy(update={"name": candidate.name or about.get("name", ""),
                                        "x_handle": candidate.x_handle or (xs[0] if len(xs) == 1 else "")})


def on_file(store, candidate, as_of):
    """(Their own X and LinkedIn posts, comments and replies in the store over the year to ``as_of``, the X and
    LinkedIn pages no person context reads): found through any context that reads one of their profiles. The count
    is None when no context reads any (never pulled)."""
    pages = {p for p in contact._pages(candidate.profiles()) if p[0] != "github.com"}
    by_subject = {r["subject_id"]: contact._pages((r.get("anchors") or {}).values()) for r in store.all("person_context")}
    subjects = {s for s, read in by_subject.items() if pages & read}
    missing = sorted(f"https://{site}{path}" for site, path in pages - set().union(set(), *by_subject.values()))
    if not subjects:
        return None, missing
    since = parse_time(as_of) - YEAR
    return sum(1 for e in store.all("timeline_event") if e["subject_id"] in subjects and e["event_type"] in POST_TYPES
               and since <= parse_time(e["observed_at"]) <= parse_time(as_of)), missing


def github_year(raw, login, as_of):
    """Repos they made and their public activity over the year to ``as_of``, from github.fetch's lists, as the post
    reader would get them (github.events; public activity reaches back 90 days only)."""
    person = Person(person_id=login, github=login)
    since = parse_time(as_of) - YEAR
    return sum(1 for e in github.events(person, raw) if e["event_type"] in GITHUB_WORK
               and since <= parse_time(e["observed_at"]) <= parse_time(as_of))


def _site(blog):
    """Their site as a URL, or "" when the blog field is not one: a word, a sentence, an email address, an IP
    address, or a profile on a site the engine reads."""
    blog = blog.strip()
    url = blog if blog.startswith(("http://", "https://")) else "https://" + blog
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return ""
    ok = (blog and " " not in blog and "@" not in parts.netloc and not _is_ip(host)
          and re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", host)
          and not any(host == site or host.endswith("." + site) for site in ENGINE_READS))
    return url if ok else ""


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def ties(candidate, about):
    """(identity records, an X account to add, or a problem): only links their GitHub profile states, each paired with
    the GitHub profile itself, two sites. ``about`` is github.profile's."""
    page = f"https://github.com/{candidate.github}"
    if problem := not_theirs(candidate, about):
        return [], "", problem
    xs = stated_x(about)
    if candidate.x_handle and (other := [h for h in xs if h.lower() != candidate.x_handle.lower()]):
        return [], "", f"their GitHub names another X account, @{other[0]}, than the one on file"
    if len(xs) > 1:
        return [], "", f"their GitHub names two X accounts, {', '.join('@' + h for h in xs)}"
    records = [{"links": [page, f"https://x.com/{h}"], "note": f"Their GitHub profile names X @{h}."} for h in xs]
    for url in about.get("social") or []:
        if (found := linkedin_url(url)) and found == candidate.linkedin_url:
            records.append({"links": [page, found], "note": "Their GitHub profile links this LinkedIn."})
    if site := _site(about.get("blog") or ""):
        records.append({"links": [page, site], "note": "Their GitHub profile names this site as theirs."})
    return records, "" if candidate.x_handle else (xs[0] if xs else ""), ""


def assess(candidate, *, posts=None, missing=(), raw=None, about=None, as_of):
    """The verdict for one candidate from what was read: ``posts`` and ``missing`` (on_file, for the candidate with the
    X their GitHub names), ``raw`` (github.fetch's lists) and ``about`` (github.profile's), both None without a
    GitHub."""
    github_n = github_year(raw, candidate.github, as_of) if raw is not None else None
    records, add_x, problem = ties(candidate, about) if about is not None else ([], "", "")
    elsewhere = not_theirs(candidate, about) if about is not None else ""
    profiles = candidate.profiles() + ([f"https://x.com/{add_x}"] if add_x else [])
    loose = contact.untied([r["links"] for r in records], profiles)
    active = (posts or 0) >= MIN_POSTS or (github_n or 0) >= MIN_POSTS
    result = dict(candidate=candidate, posts_a_year=posts, github_a_year=github_n, add_x=add_x,
                  github_name=(about or {}).get("name", ""), records=[{"kind": "identity", **r} for r in records])
    if (about or {}).get("type") == "Organization":
        return Result(**result, verdict="skipped", why=problem)
    if not profiles:
        return Result(**result, verdict="skipped", why="no X, LinkedIn or GitHub: nothing to watch")
    if elsewhere:  # someone else's repos say nothing about how much the candidate posts
        return Result(**{**result, "github_a_year": None}, verdict="tie_first", why=elsewhere)
    if not active and missing:
        return Result(**result, verdict="unmeasured", why=f"never pulled: {', '.join(missing)}")
    if not active:
        return Result(**result, verdict="too_quiet", why=f"fewer than {MIN_POSTS} of their own items in the last year")
    if problem or loose:
        return Result(**result, verdict="tie_first", why=problem or f"untied: {', '.join(loose)}")
    return Result(**result, verdict="admit", why="posts enough, and every profile is tied")


def person_id(candidate, taken=()):
    """Their people file id: the role, then a slug of their name (else of their first profile's handle), with "-2",
    "-3" ... when its ledger key (contact.person_key: the id without its role) is one of ``taken``'s."""
    handle = candidate.github or candidate.x_handle or candidate.linkedin_url.rstrip("/").rsplit("/")[-1]
    slug = _slug(candidate.name) or _slug(handle)
    keys, n, pid = {contact.person_key(t) for t in taken}, 1, f"{candidate.role}:{slug}"
    while contact.person_key(pid) in keys:
        n += 1
        pid = f"{candidate.role}:{slug}-{n}"
    return pid


def proposals(results, since, taken=()):
    """What each "admit" adds once Justin says yes: (its people file rows, watched from ``since``; its identity
    records with its id, a list in scripts/contacts.py import's shape). No id shares a ledger key with ``taken`` (the
    people file's ids) or another admit's: the daily run refuses a people file that names one id twice. Nothing else
    is proposed."""
    rows, records, taken = [], [], list(taken)
    for r in (r for r in results if r.verdict == "admit"):
        c, pid = r.candidate, person_id(r.candidate, taken)
        taken.append(pid)
        rows.append({"person_id": pid, "name": c.name, "role": c.role, "x_handle": c.x_handle or r.add_x,
                     "linkedin_url": c.linkedin_url, "github": c.github, "since": since})
        records += [{"person_id": pid, **record} for record in r.records]
    return rows, records


HEADINGS = {"admit": "Admit: posts enough, every profile tied",
            "tie_first": "Check first: a profile untied, or a GitHub that may not be theirs",
            "unmeasured": "Unmeasured: their X or LinkedIn was never pulled", "too_quiet": "Too quiet to show a sign",
            "skipped": "Nothing to watch"}


def sheet(results, known_before, not_read, cost, day):
    """The yes or no sheet, in markdown: the admits in full, the rest a line each."""
    out = [f"# Candidates for the watchlist, {day}", "",
           "Private: real people's names and profiles. Keep it in research/private.", "",
           f"**The gate.** A candidate is admitted when they have {MIN_POSTS} or more of their own items in the last "
           "year (posts, comments and replies on file, or repos and public work on GitHub) and every profile the "
           "engine would read for them is tied to a page of theirs on another site. The ties come only from what "
           "their own GitHub profile states. Nothing goes in before a yes.", "", f"**Cost.** {cost}", ""]
    for verdict, heading in HEADINGS.items():
        group = [r for r in results if r.verdict == verdict]
        if not group:
            continue
        out += [f"## {heading} ({len(group)})", ""]
        for r in sorted(group, key=lambda r: (r.candidate.role, r.candidate.label().lower())):
            c = r.candidate
            who = f"{c.label()} ({c.role}" + (f", from {c.source}" if c.source else "") + ")"
            activity = ", ".join(f"{n} {what}" for n, what in ((r.posts_a_year, "posts on file"),
                                                              (r.github_a_year, "GitHub items")) if n is not None)
            if verdict != "admit":
                out.append(f"- {who}: {r.why}" + (f" ({activity} in the last year)" if activity else ""))
                continue
            out += [f"### {who}", "", f"- Profiles: {', '.join(c.profiles())}", f"- Last year: {activity}"]
            out += [f"- Their GitHub's name: {r.github_name}" if r.github_name else
                    "- Their GitHub gives no name: check it is theirs"] if c.github else []
            out += [f"- Evidence: {', '.join(c.evidence_urls)}"] if c.evidence_urls else []
            out += [f"- Adds X @{r.add_x}: their GitHub profile names it"] if r.add_x else []
            out += [f"- Identity: {' + '.join(rec['links'])}. {rec['note']}" for rec in r.records]
            out += ["- Yes / No:", ""]
        out.append("")
    for heading, rows in (("Already known", known_before), ("Not read", not_read)):
        if rows:
            out += [f"## {heading} ({len(rows)})", ""] + [f"- {name}: {why}" for name, why in rows] + [""]
    return "\n".join(out)
