"""Timeline bridges for the sources that emitted nothing: Wayback page history,
Exa web mentions and Crustdata company records. (OpenAlex citations of GI live
in app.sources.openalex.gi_citation_events.)

Each bridge is a pure function from what a source returned to TimelineEvent
dicts for one person. Identity is the caller's job (app.journey admits a record
only when two identifiers agree). A bridge refuses only what it can see is not
first-person: a feed page (a LinkedIn or X profile) mixes other people's posts
into the owner's page, so anything read from one is a tier-2 mention and never
a statement by the person.
"""

import re
from datetime import date, timedelta

from .models import iso, parse_time, utcnow
from .providers import FEED_PAGES
from .timeline import TimelineEvent

TALK = re.compile(r"\b(keynote|invited talk|talk (?:at|on)|speak(?:ing)? at|will present|presenting at|"
                  r"seminar|colloquium|panel(?:ist)?|tutorial|fireside)\b", re.I)
AVAILABLE = re.compile(r"\b(on the (?:academic |industry )?job market|open to (?:new )?(?:roles|opportunities)|"
                       r"looking for (?:a |my )?(?:new |next )?(?:role|position|opportunit\w*)|"
                       r"available (?:from|starting)|seeking (?:a )?(?:full[- ]time |new )(?:role|position))", re.I)
GI_NAME = re.compile(r"\bGeneral Intuition\b", re.I)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MONTH = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
DAY_DATE = re.compile(rf"\b{MONTH} (\d{{1,2}})(?:st|nd|rd|th)?,? (\d{{4}})\b|\b(\d{{4}})-(\d{{2}})-(\d{{2}})\b", re.I)
MONTH_DATE = re.compile(rf"\b{MONTH} (\d{{4}})\b", re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+")
# A roster drop is dated only when the two captures bracket it this closely.
ROSTER_GAP_DAYS = 45
# Crustdata company: a drop this large against a point up to HEADCOUNT_SPAN earlier.
HEADCOUNT_DROP = 0.10
HEADCOUNT_SPAN = timedelta(days=200)


def is_feed(url: str) -> bool:
    return any(part in (url or "") for part in FEED_PAGES)


def _tokens(name: str) -> list[str]:
    return re.sub(r"[^a-z ]+", " ", name.lower()).split()


def name_matches(a: str, b: str) -> bool:
    """Same surname and first initial, either order ("Mei Lin" / "Lin Mei"); middle names ignored."""
    x, y = _tokens(a), _tokens(b)
    if not x or not y:
        return False
    if set(x) == set(y):
        return True
    return x[-1] == y[-1] and x[0][0] == y[0][0] and (len(x[0]) == 1 or len(y[0]) == 1 or x[0] == y[0])


def name_in(text: str, name: str) -> bool:
    """The full name appears in the text (case-insensitive, whitespace-tolerant)."""
    words = _tokens(name)
    return bool(words) and re.search(r"\b" + r"\s+".join(map(re.escape, words)) + r"\b", text or "", re.I) is not None


def stated_date(text: str) -> tuple[str, str] | None:
    """The first day or month a sentence names, at the precision it states."""
    if m := DAY_DATE.search(text):
        if m.group(1):
            y, mo, d = int(m.group(3)), MONTHS.index(m.group(1)[:3].lower()) + 1, int(m.group(2))
        else:
            y, mo, d = int(m.group(4)), int(m.group(5)), int(m.group(6))
        try:
            return date(y, mo, d).isoformat(), "day"
        except ValueError:
            return None
    if m := MONTH_DATE.search(text):
        return f"{m.group(2)}-{MONTHS.index(m.group(1)[:3].lower()) + 1:02d}", "month"
    return None


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE.split(text or "") if s.strip()]


def _event(subject, event_type, *, observed_at, quote, source_url, version, tier, extractor,
           dated=None, subject_type="person"):
    day, precision = dated or (None, None)
    return TimelineEvent(
        subject_type=subject_type, subject_id=subject, event_type=event_type, event_date=day,
        date_precision=precision, observed_at=observed_at, source_url=source_url,
        source_version_hash=version, quote=quote[:300], tier=tier, extractor=extractor,
    ).model_dump()


def _statements(subject, lines, *, first_person, observed_at, source_url, version, tier, extractor, name=""):
    """paper_talk and self_stated_availability from the lines of one page.

    A talk needs a stated date (the detector brackets it). Availability is only
    ever read from the person's own page; a third party saying it is hearsay.
    On a third-party page a talk line must name the person.
    """
    out = []
    for line in lines:
        mentions = first_person or (name and name_in(line, name))
        if mentions and TALK.search(line) and (when := stated_date(line)):
            out.append(_event(subject, "paper_talk", dated=when, observed_at=observed_at, quote=line,
                              source_url=source_url, version=version, tier=tier, extractor=extractor))
        if first_person and AVAILABLE.search(line):
            out.append(_event(subject, "self_stated_availability", dated=stated_date(line),
                              observed_at=observed_at, quote=line, source_url=source_url,
                              version=version, tier=tier, extractor=extractor))
    return out


# ------------------------------------------------------------------ Wayback

def page_history_events(subject, name, versions, *, own_page):
    """Events from successive archived versions of one page (app.sources.wayback.PageVersion).

    own_page: the person's homepage, where every change is about them. Any other
    page (a lab roster, a team page) only speaks about lines naming the person,
    and the person's name disappearing from it between two close captures is a
    dated departure. Every observed_at is the capture time.
    """
    from .sources.wayback import diff_versions

    out = []
    for i, new in enumerate(versions):
        feed = is_feed(new.url)
        tier = 2 if feed else 1
        base = dict(observed_at=new.observed_at, source_url=new.source_url, version=new.content_hash,
                    tier=tier, extractor="wayback_v1")
        if i == 0:
            lines = sentences(new.text)
        else:
            old = versions[i - 1]
            added = diff_versions(old, new).added
            lines = [s for phrase in added for s in sentences(phrase)]
            if own_page or any(name_in(s, name) for s in lines):
                quote = " ".join(lines) or "Content removed or reordered; nothing added."
                out.append(_event(subject, "page_changed", quote=quote, **base))
            gap = parse_time(new.capture_at) - parse_time(old.capture_at)
            if (not own_page and not feed and name_in(old.text, name) and not name_in(new.text, name)
                    and gap <= timedelta(days=ROSTER_GAP_DAYS)):
                out.append(_event(subject, "job_ended", dated=(new.capture_at[:7], "month"),
                                  quote=f"{name} listed on the {old.capture_at[:10]} capture of {new.url}, "
                                        f"absent from the {new.capture_at[:10]} capture", **base))
        if not feed:
            out += _statements(subject, lines, first_person=own_page, name=name, **base)
    return out


# ---------------------------------------------------------------------- Exa

def web_mention_events(subject, name, docs, *, own_domains=()):
    """Events from Exa search documents (providers._exa_docs) already admitted for this person.

    A page's publication date is its observed_at; an undated page is observed
    now, so it never reaches a historical replay.
    """
    out, now = [], iso()
    for doc in docs:
        url = doc["url"]
        published = doc.get("published_at")
        observed = iso(min(parse_time(published), utcnow())) if published else now
        feed = is_feed(url)
        host = re.sub(r"^www\.", "", url.split("/")[2].lower()) if "//" in url else ""
        own = not feed and host in own_domains
        lines = sentences(doc.get("text", ""))
        about = [s for s in lines if name_in(s, name)] or lines[:1]
        base = dict(observed_at=observed, source_url=url, version=doc["content_hash"], tier=2, extractor="exa_search_v1")
        out.append(_event(subject, "web_mention", dated=(published[:10], "day") if published else None,
                          quote=f"{doc.get('title', '')}: {' '.join(about)}", **base))
        if feed:
            continue
        out += _statements(subject, lines, first_person=own, name=name, **base)
        gi = [s for s in lines if GI_NAME.search(s) and (own or name_in(s, name))]
        if gi and published:
            out.append(_event(subject, "gi_attention", dated=(published[:10], "day"), quote=gi[0], **base))
    return out


# ----------------------------------------------------------- Crustdata company

def _series(record, *paths):
    for path in paths:
        value = record
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
    return []


def _day(row, *keys):
    for key in keys:
        if isinstance(row.get(key), str) and re.match(r"\d{4}-\d{2}-\d{2}", row[key]):
            return row[key][:10]
    return None


def company_events(subject, org_subject, company, record, *, source_url, version):
    """equity_refresh (person, tier 3) per funding round and headcount_change (org) per sharp drop.

    A round is an inference about the person: new money usually revalues or
    refreshes employee equity, which holds people in place for a while. The
    field names follow Crustdata's company record as documented for the
    screener API; unknown shapes yield nothing rather than a guess.
    """
    out = []
    rounds = _series(record, ("funding_and_investment", "funding_milestones_timeseries"), ("funding_rounds",))
    last = record.get("last_funding_date") or (record.get("funding_and_investment") or {}).get("last_funding_date")
    if isinstance(last, str) and not any(_day(r, "date", "announced_date") == last[:10] for r in rounds):
        rounds.append({"date": last[:10], "round": record.get("last_funding_round_type", "")})
    seen = set()
    for row in rounds:
        day = _day(row, "date", "announced_date", "funding_date")
        if not day or day in seen or day > iso()[:10]:
            continue
        seen.add(day)
        kind = row.get("round") or row.get("funding_round_type") or row.get("round_type") or "a funding round"
        amount = row.get("funding_milestone_amount_usd") or row.get("amount_usd")
        quote = f"{company} raised {kind}" + (f" (${int(amount):,})" if isinstance(amount, (int, float)) else "") + \
                f" on {day}; inferred: a new round revalues or refreshes employee equity"
        out.append(_event(subject, "equity_refresh", dated=(day, "day"), observed_at=f"{day}T00:00:00+00:00",
                          quote=quote, source_url=source_url, version=version, tier=3,
                          extractor="crustdata_company_v1"))
    points = sorted(((d, int(n)) for row in _series(record, ("headcount", "linkedin_headcount_timeseries"),
                                                    ("headcount_timeseries",))
                     if (d := _day(row, "date")) and isinstance(n := row.get("employee_count", row.get("headcount")), (int, float))),
                    key=lambda p: p[0])
    dropping = False
    for i, (day, count) in enumerate(points):
        earlier = [n for d, n in points[:i] if date.fromisoformat(day) - date.fromisoformat(d) <= HEADCOUNT_SPAN]
        drop = earlier and count <= max(earlier) * (1 - HEADCOUNT_DROP)
        if drop and not dropping:
            out.append(_event(org_subject, "headcount_change", subject_type="org", dated=(day, "day"),
                              observed_at=f"{day}T00:00:00+00:00", source_url=source_url, version=version, tier=1,
                              extractor="crustdata_company_v1",
                              quote=f"{company} headcount {count} on {day}, down from {max(earlier)} within {HEADCOUNT_SPAN.days} days"))
        dropping = bool(drop)
    return out
