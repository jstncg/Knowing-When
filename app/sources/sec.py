"""SEC EDGAR as a tier-1 dated source for the Global Controller lane.

Reads public submissions JSON and 8-K primary documents,
and yields TimelineEvents: item 2.01 (acquisition closed), 5.02 (officer
departure / appointment), 4.01 (auditor change), NT 10-K (late filing) and 10-K
(annual report filed). Every response is cached on disk keyed by URL hash, so
nothing is fetched twice and a cache directory doubles as an offline fixture.

SEC fair-access rules: a descriptive User-Agent with a contact and fewer than
10 requests per second. The contact is SEC_USER_AGENT="Org Name contact@example.com",
else the git user.email.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from ..timeline import TimelineEvent
from .http import host_throttle

RETRY_STATUSES = {429, 500, 502, 503, 504}

EXTRACTOR = "sec_8k_v1"
DEFAULT_CACHE = Path("data/sec-cache")
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
MAX_QUOTE = 300


class SecError(Exception):
    pass


def git_contact_agent() -> str:
    try:
        email = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"GITimingEngine/0.1 {email}" if email else ""


class EdgarClient:
    """Throttled, cached GET. `offline=True` never touches the network (tests)."""

    def __init__(self, cache_dir=DEFAULT_CACHE, user_agent=None, offline=False):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = (user_agent or os.environ.get("SEC_USER_AGENT") or git_contact_agent()
                           or "gi-timing-engine (set SEC_USER_AGENT)")
        self._throttle = host_throttle("www.sec.gov")
        self.offline = offline
        self.fetched = 0
        self._http = httpx.Client(timeout=30, follow_redirects=True,
                                  headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"})

    def _path(self, url):
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    def get(self, url, fresh=False) -> str:
        """The cached body, or a fetch; `fresh` refetches a page that changes (a data set index)."""
        path = self._path(url)
        if path.exists() and not fresh:
            return json.loads(path.read_text())["body"]
        body = self._response(url).text
        path.write_text(json.dumps({"url": url, "fetched_at": datetime.now(timezone.utc).isoformat(), "body": body}))
        return body

    def download(self, url) -> Path:
        """A binary file (a bulk data set zip), cached as-is under the same URL hash."""
        path = self._path(url).with_suffix(Path(urlsplit(url).path).suffix or ".bin")
        if not path.exists():
            part = path.with_suffix(".part")
            part.write_bytes(self._response(url).content)
            part.replace(path)
        return path

    def _response(self, url) -> httpx.Response:
        if self.offline:
            raise SecError(f"Not in offline cache: {url}")
        # EDGAR sheds load with transient 429/5xx and slow reads; back off 2, 4, 8 s.
        for attempt in range(4):
            self._throttle.wait_sync()
            try:
                response = self._http.get(url)
            except httpx.TransportError as error:
                if attempt == 3:
                    raise SecError(f"SEC request failed ({type(error).__name__}) for {url}") from None
                time.sleep(2 ** (attempt + 1))
                continue
            self.fetched += 1
            if response.status_code not in RETRY_STATUSES or attempt == 3:
                break
            time.sleep(2 ** (attempt + 1))
        if response.status_code >= 400:
            raise SecError(f"SEC returned HTTP {response.status_code} for {url}")
        return response

    def get_json(self, url):
        return json.loads(self.get(url))


@dataclass
class Filing:
    cik: str
    company: str
    accession: str
    form: str
    filing_date: str
    report_date: str
    accepted_at: str
    items: list[str]
    primary_doc: str

    @property
    def url(self):
        return f"{ARCHIVES}/{int(self.cik)}/{self.accession.replace('-', '')}/{self.primary_doc}"

    @property
    def index_url(self):
        return index_url(self.cik, self.accession)


def index_url(cik, accession):
    return f"{ARCHIVES}/{int(cik)}/{accession.replace('-', '')}/{accession}-index.htm"


class OfficerEvent(TimelineEvent):
    """A 5.02 departure or appointment with the fields the answer key needs."""
    person_name: str
    title: str
    company: str
    reason: str = ""


# ---------------------------------------------------------------- submissions

def pad_cik(cik) -> str:
    return str(int(cik)).zfill(10)


def submissions(client, cik) -> dict:
    return client.get_json(f"https://data.sec.gov/submissions/CIK{pad_cik(cik)}.json")


def filings(client, cik, since=None, forms=("8-K", "10-K", "NT 10-K")) -> list[Filing]:
    """Filings of one registrant, newest first. Older pages are read only when needed."""
    data = submissions(client, cik)
    company = data.get("name", "")
    out, pages = [], [data["filings"]["recent"]]
    for extra in data["filings"].get("files", []):
        if since is None or extra["filingTo"] >= since:
            pages.append(client.get_json("https://data.sec.gov/submissions/" + extra["name"]))
    for page in pages:
        for i, form in enumerate(page["form"]):
            filing_date = page["filingDate"][i]
            if form not in forms or (since and filing_date < since):
                continue
            out.append(Filing(
                cik=pad_cik(cik), company=company, accession=page["accessionNumber"][i], form=form,
                filing_date=filing_date, report_date=page["reportDate"][i] or filing_date,
                accepted_at=page["acceptanceDateTime"][i], primary_doc=page["primaryDocument"][i],
                items=[s for s in page["items"][i].split(",") if s],
            ))
    return sorted(out, key=lambda f: f.filing_date, reverse=True)


# ---------------------------------------------------------------------- parse

ITEM = re.compile(r"\bItem\s+(\d\.\d{2})\b[.:]?\s*", re.I)
ABBREVIATIONS = ("Mr", "Ms", "Mrs", "Dr", "Jr", "Sr", "Inc", "Corp", "Co", "Ltd", "No", "St")
SENTENCE = re.compile(r"(?<![ .(][A-Z]\.)" + "".join(rf"(?<!\b{a}\.)" for a in ABBREVIATIONS) + r"(?<=[.!?])\s+(?=[A-Z(“\"])")
HEADING = re.compile(r"^(Completion of|Departure of|Changes in|Compensatory Arrangements|Election of|Appointment of)[^.]*\.?\s*", re.I)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE = re.compile(rf"\b({MONTHS}) (\d{{1,2}}),? (\d{{4}})\b")
EFFECTIVE = re.compile(rf"(?:effective|with\seffect)\s+(?:as\sof\s|on\s|from\s)?(?:the\sclose\sof\sbusiness\son\s)?"
                       rf"({MONTHS}) (\d{{1,2}}),? (\d{{4}})", re.I)
# A date this long before the report date is history (a tenure start, an agreement's date), not the event.
HISTORY_DAYS = 30
HONORIFIC = r"(?:Mr|Ms|Mrs|Dr)\.\s+"
NAME_WORD = r"[A-ZÀ-Þ][^\W\d_]*(?:['’\-][^\W\d_]+)*\.?"
NAME = re.compile(rf"\b(?:{HONORIFIC})?((?:{NAME_WORD}|[A-Z]\.)(?:\s(?:{NAME_WORD}|[A-Z]\.|(?:de|van|von|da|la)\b)){{1,3}}(?:,?\s(?:Jr\.|Sr\.|II|III|IV))?)")
SURNAME = re.compile(rf"{HONORIFIC}({NAME_WORD})")
POSSESSIVE = re.compile(r"['’]s?$")
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
# Case-insensitive: some filers write "resign as senior vice president, controller and chief accounting officer".
TITLE_CORE = (r"(?:Chief\s[A-Z][a-z]+(?:\s(?:and\s)?[A-Z][a-z]+)?\sOfficer|Principal\s(?:Accounting|Financial|Executive)\sOfficer"
              r"|(?:Corporate\s|Assistant\s|Global\s|Group\s|Divisional\s|Vice\sPresident,?\s)?Controller"
              r"|(?:Executive\s|Senior\s|Group\s)?Vice\sPresident(?-i:\sof\s[A-Z][a-z]+)?"
              r"|President|Treasurer|General\sCounsel|(?:Corporate\s)?Secretary|Chairman|Chair|Director)")
TITLE = re.compile(rf"\b({TITLE_CORE}(?:(?:,|\sand|,\sand|\s&)\s{TITLE_CORE})*)\b", re.I)
NOT_A_NAME = set((
    "The Company Board Directors Board of Directors Chief Executive Officer Financial Accounting Operating Vice President "
    "Senior Executive Corporate Controller Principal General Counsel Secretary Treasurer Chairman Item Form Exhibit Inc "
    "Corp Corporation Company LLC LLP Ltd Holdings Group Partners Capital Bank Bancorp Trust Effective Committee Audit "
    "Compensation Nominating Governance Securities Exchange Act Commission Section Rule Regulation Registrant Annual "
    "Report Reporting Quarterly Press Release Employment Agreement Separation Transition Date Officer Officers Departure "
    "Resignation Appointment Election Certain Interim Acting Global Assistant Divisional Managing Founder Co-Founder Head "
    "Finance Operations Legal Human Resources Technology Information Marketing Sales Products Product Strategy Business "
    "Development Internal Controls Consolidations Tax Treasury University Mr Ms Mrs Dr On In As At By For From Following "
    "Prior Pursuant Under Upon With Such This These Each Our His Her Their Also Additionally Further Accordingly There "
    "Since Until Merger Parent Plan Committees Boards Subsidiary Subsidiaries"
).split()) | set(MONTHS.split("|"))
DEPART = re.compile(r"\b(resign\w*|retire\w*|depart\w*|step(?:ped|s|ping)?\sdown|terminat\w*|separat\w*|ceased?\w*|"
                    r"no\slonger\sserv\w*|will\sleave|left\s(?:the\sCompany|his|her)|removed|relieved|transition\w*\sout)\b", re.I)
APPOINT = re.compile(r"\b(appoint\w*|named|elect\w*|promot\w*|hired|will\sserve\sas|(?<!continue\s)to\sserve\sas|assume\w*|"
                     r"join\w*|succeed\w*|designat\w*)\b", re.I)
# A capitalised run after these, or before a corporate suffix, is an employer: "Controller of Crimson
# Midstream, LLC", "prior to joining Extraction Oil & Gas", "a NYSE-listed Fortune 500 company".
ORG_BEFORE = re.compile(rf"(?:\b(?:a|an|the|at|join\w*)|(?:{TITLE_CORE}),?\s+(?:of|at|with))\s+$", re.I)
ORG_AFTER = re.compile(r"(?:\s*&|,?\s+(?:Inc|Corp|Corporation|Company|Co|LLC|L\.?P|Ltd|Limited|Holdings|Partners|Group)\b|\s+\d)")
# Giving up a "principal ... officer" designation without leaving, or continuing in another office, is a
# change of role: "will no longer serve as principal accounting officer" / "will continue to serve as CFO".
# Narrower than DEPART: "no longer serve", "ceased" or "removed" can mean giving up one office and keeping another.
LEAVING = re.compile(r"\b(resign\w*|retire\w*|depart\w*|leav\w*|left|terminat\w*|separat\w*|step(?:ped|s|ping)?\sdown)\b", re.I)
DESIGNATION = re.compile(r"principal\s(?:accounting|financial)\sofficer(?:(?:,|\sand|,\sand|\s&)\sprincipal\s(?:accounting|financial)\sofficer)*", re.I)
# "Remain" and "continue to be" take the title directly ("will remain Chief Accounting Officer");
# not the adjectives "remaining" or "continuing".
CONTINUES = re.compile(rf"\b(?:(?:continu\w*|remain\w*)\s+(?:to\sserve\s|serving\s|in\s(?:(?:her|his|their|the)\s)?"
                       rf"(?:current\s)?(?:role|position)s?\s)?as\s+|(?:remains?|continu\w*\s+to\sbe)\s+)(?:the\s|its\s)?"
                       rf"(?:Company['’]s\s)?({TITLE_CORE})(?![^;]*\b(?:until|through|pending)\b)", re.I)
OFFICE = re.compile(r"Officer|Controller|President|Treasurer|Counsel|Secretary", re.I)
OFFICE_NAME = re.compile(rf"\b(?:{TITLE_CORE})\b", re.I)  # one office of a compound title
OFFICE_ABBREVIATIONS = {"CFO": "chief financial officer", "CAO": "chief accounting officer",
                        "PFO": "principal financial officer", "PAO": "principal accounting officer",
                        "CEO": "chief executive officer", "COO": "chief operating officer"}
OFFICE_ABBREVIATION = re.compile(rf"\b({'|'.join(OFFICE_ABBREVIATIONS)})\b")  # capitals only
VP_RANK = re.compile(r"^(?:(?:executive|senior|group) )?vice president(?:,? |$)")
# A name right after these is the predecessor in someone else's appointment; their own filing reports the departure.
PREDECESSOR = re.compile(rf"\b(?:succeed\w*|replac\w*|successor\sto)\s+(?:{HONORIFIC})?$", re.I)
# The words that introduce the role a person is appointed to: "as", "role of", "promoted to".
ROLE_INTRO = re.compile(r"\b(?:as|of|to)\s+(?:the\s+|a\s+|an\s+)?(?:[\w&.]+['’]s\s+)?(?:new\s+|interim\s+|acting\s+)?$", re.I)
REASON = re.compile(r"\b(to\spursue\s[^.,;]+|for\spersonal\sreasons|(?:not|was\snot|were\snot)\s(?:the\sresult\sof|due\sto)\s"
                    r"any\sdisagreement[^.;]*|retire\w*|mutual\w*\sagree\w*|health\sreasons|without\scause|for\scause|"
                    r"elimination\sof\s(?:the|his|her)\sposition|restructuring)", re.I)
COMPLETED = re.compile(r"\b(complet\w*|consummat\w*|closed|closing|acquired)\b", re.I)
ACQUISITION = re.compile(r"acqui|merger|purchase", re.I)
ACQUIRED = re.compile(r"(?<!Buyer\s)(?<!Purchaser\s)\bacquired\b|(?<!Buyer['’]s\s)\bacquisition\sof\b", re.I)
# Item 2.01 also reports dispositions; there the filer is the seller and the other party "the Buyer".
DISPOSAL = re.compile(r"\b(?:sale\sof|sold|disposition\sof|divest\w*|spin-?off|(?:Buyer|Purchaser)\s+(?:purchased|acquired))\b", re.I)
AUDITOR_ACT = re.compile(r"\b(dismiss\w*|resign\w*|engag\w*|appoint\w*|declined\w*|approved)\b", re.I)
AUDITOR = re.compile(r"accountant|auditor|audit firm|LLP", re.I)
LATE_REASON = re.compile(r"unable|could not|additional time|more time|not be able|delay", re.I)
INDEX_DATES = re.compile(r"Filing Date\s+\d{4}-\d{2}-\d{2}.{0,200}?Period of Report\s+\d{4}-\d{2}-\d{2}")


def html_text(body: str) -> str:
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def item_sections(text: str) -> dict[str, str]:
    """Item number -> its section, from the item's heading.

    A heading starts a sentence and is followed by the item's capitalised title;
    "see Item 2.01 below" or "this Item 1.01" is a cross-reference and stays
    inside the section it occurs in.
    """
    first = {}
    for m in ITEM.finditer(text):
        before = text[:m.start()].rstrip()[-1:]
        after = text[m.end():m.end() + 1]
        if (before.islower() or before == ",") or not (after.isupper() or after in "(“\""):
            continue
        first.setdefault(m.group(1), m)
    marks = sorted(first.values(), key=lambda m: m.start())
    return {m.group(1): text[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(text))].strip()
            for i, m in enumerate(marks)}


def sentences(section: str) -> list[str]:
    """Sentences of an item section, without the item's own heading line."""
    return [s.strip() for s in SENTENCE.split(HEADING.sub("", section)) if s.strip()]


def find_sentence(section, *patterns):
    """First sentence matching every pattern, with the first pattern's position."""
    for s in sentences(section or ""):
        found = [p.search(s) for p in patterns]
        if all(found) and not s.lower().startswith("item "):
            return s, found[0].start()
    return None


def quote_of(sentence: str, anchor: int = 0) -> str:
    """The sentence itself, or an exact sub-span around the anchor when it runs long."""
    if len(sentence) <= MAX_QUOTE:
        return sentence
    start = max(0, min(anchor - MAX_QUOTE // 2, len(sentence) - MAX_QUOTE))
    span = sentence[start:start + MAX_QUOTE]
    if start and (cut := span.find(" ")) >= 0:
        span = span[cut + 1:]
    if start + MAX_QUOTE < len(sentence) and (cut := span.rfind(" ")) > 0:
        span = span[:cut]
    return span


def iso_date(match) -> str:
    month, day, year = match.groups()
    return date(int(year), MONTHS.lower().split("|").index(month.lower()) + 1, int(day)).isoformat()


def stated_date(sentence: str, default: str) -> str:
    """The date after 'effective' (or 'with effect'); else the first date named; else the report date.
    Dates more than HISTORY_DAYS before the report date are skipped."""
    floor = (date.fromisoformat(default) - timedelta(days=HISTORY_DAYS)).isoformat() if default else ""
    for pattern in (EFFECTIVE, DATE):
        for m in pattern.finditer(sentence):
            try:
                day = iso_date(m)
            except ValueError:
                continue
            if day >= floor:
                return day
    return default


def name_key(name: str) -> str:
    """The name without initials, suffixes or a possessive: "Jane Q. Doe" and "Jane Doe" match."""
    words = [POSSESSIVE.sub("", w.strip(".,")) for w in name.split()]
    return " ".join(w for w in words if len(w) > 1 and w.lower() not in SUFFIXES)


def names_in(sentence: str, company_words=frozenset()) -> list[tuple[str, int]]:
    out = []
    for m in NAME.finditer(sentence):
        words = [POSSESSIVE.sub("", w.strip(".,")) for w in m.group(1).split()]
        if (len(words) < 2 or not words[-1][:1].isupper() or all(len(w) <= 2 for w in words)
                or {w.lower() for w in words} <= company_words
                or any(w in NOT_A_NAME or (len(w) > 1 and w.isupper() and w.lower() not in SUFFIXES) for w in words)
                or (m.start() == m.start(1) and ORG_BEFORE.search(sentence, 0, m.start(1)))
                or ORG_AFTER.match(sentence, m.end(1))):
            continue
        out.append((POSSESSIVE.sub("", m.group(1)), m.start(1)))
    return out


def _title_for(sentence, titles, pos, appointment):
    """For an appointment, the role introduced after the name ("as", "role of", "promoted to");
    otherwise the first title after the name, or one ending just before it."""
    after = [t for t in titles if 0 < t[1] - pos <= 200]
    if appointment:
        for title, start, _ in after:
            if ROLE_INTRO.search(sentence, 0, start):
                return title
    if after:
        return after[0][0]
    before = [t for t in titles if 0 <= pos - t[2] <= 40]
    return before[-1][0] if before else None


def _clause(sentence, pos, found, start=None):
    """The part of a sentence about the person at pos, up to the next person named: from their own
    name, or from an earlier start such as their departure verb ("the resignation of X")."""
    end = min((p for _, p in found if p > pos), default=len(sentence))
    return sentence[pos if start is None else min(start, pos):end]


def role_change(stop, own) -> bool:
    """The person keeps an office: what they stop serving as (`stop` starts at their own departure
    verb) is only a 'principal ... officer' designation and nothing after says they leave, or their
    own clauses say they continue as an officer. Continuing in every office they leave, or only
    "until" a date, is a hand-over."""
    verb = DEPART.search(stop)
    if not verb:
        return any(OFFICE.search(m.group(1)) for c in own for m in CONTINUES.finditer(c))
    # The offices left are those named before any "continue"/"remain" wording, spelled out or
    # abbreviated ("Interim CFO"); the title that wording names is the office kept.
    kept = CONTINUES.search(stop, verb.end())
    end = kept.start() if kept else len(stop)
    stopped = TITLE.search(stop, verb.end(), end)
    if stopped and DESIGNATION.fullmatch(stopped.group(1)) and not LEAVING.search(stop):
        return True
    left = _offices(stop[:end])
    return any(OFFICE.search(m.group(1)) and not (left and left <= _offices((TITLE.match(c, m.start(1)) or m).group(1)))
               for c in own for m in CONTINUES.finditer(c))


def _offices(text):
    """The offices a span names, spelled out: "Interim CFO and Treasurer" is
    {"chief financial officer", "treasurer"}. A vice-president rank is no office of its own."""
    named = {" ".join(m.group(0).lower().split()) for m in OFFICE_NAME.finditer(text)}
    named = {VP_RANK.sub("", o) for o in named} - {""}  # "vice president, controller" is "controller"
    return named | {OFFICE_ABBREVIATIONS[a] for a in OFFICE_ABBREVIATION.findall(text)}


def officer_mentions(section: str, company: str = "") -> list[dict]:
    """One mention per person in a 5.02 section: name, title, departure or appointment.

    A person's first sentence with a title and a verb decides; later sentences
    ("will continue to serve until a successor is appointed", a biography) add no
    second event. "Mr. Smith" resolves to the full name seen earlier in the
    section. The verb nearest the name decides departure vs appointment, and a
    departure from one office by someone who keeps another is no event.
    """
    company_words = {w.lower() for w in re.findall(r"[^\W\d_]+", company)}
    said = sentences(section)
    surnames, mentioned = {}, []  # per sentence: every (name, position), "Mr. Smith" as the full name
    for sentence in said:
        found = names_in(sentence, company_words)
        for name, _ in found:
            if key := name_key(name):
                surnames.setdefault(key.split()[-1], name)
        for m in SURNAME.finditer(sentence):
            full = surnames.get(POSSESSIVE.sub("", m.group(1).rstrip(".,")))
            if full and all(name_key(n) != name_key(full) for n, _ in found):
                found.append((full, m.start(1)))
        mentioned.append(sorted(found, key=lambda f: f[1]))
    seen, out = set(), []
    for i, (sentence, found) in enumerate(zip(said, mentioned)):
        titles = [(m.group(1), m.start(), m.end()) for m in TITLE.finditer(sentence)]
        departs = [m.start() for m in DEPART.finditer(sentence)]
        appoints = [m.start() for m in APPOINT.finditer(sentence)]
        if not titles or not (departs or appoints):
            continue
        for name, pos in found:
            key = name_key(name)
            if key in seen or PREDECESSOR.search(sentence, 0, pos):
                continue
            d = min((abs(p - pos) for p in departs), default=None)
            a = min((abs(p - pos) for p in appoints), default=None)
            departure = a is None or (d is not None and d <= a)
            title = _title_for(sentence, titles, pos, not departure)
            if not title:
                continue
            seen.add(key)
            reason = None
            if departure:  # what follows can explain it: "Mr. Rhodes is leaving to pursue another opportunity."
                later = [_clause(s, p, names) for s, names in zip(said[i + 1:i + 3], mentioned[i + 1:i + 3])
                         for n, p in names if name_key(n) == key]
                verb = min(departs, key=lambda p: abs(p - pos))  # the verb that made this a departure
                if role_change(_clause(sentence, pos, found, verb), [_clause(sentence, pos, found), *later]):
                    continue
                reason = next(filter(None, map(REASON.search, [sentence, *later])), None)
            out.append({"name": name, "title": title, "departure": departure, "sentence": sentence,
                        "anchor": pos, "reason": reason.group(0) if reason else ""})
    return out


def person_id(name: str, company: str) -> str:
    """An SEC person's subject id from first and last name in either order: "Jane Q. Doe" and
    "DOE JANE Q" are both "doe-jane|acme-widgets-inc"."""
    words = name_key(name).split()
    person = " ".join(sorted({words[0].lower(), words[-1].lower()})) if words else ""
    return "|".join(re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-") for p in (person, company))


# --------------------------------------------------------------------- events

def _base(filing: Filing, body: str, url=None):
    return {"observed_at": filing.accepted_at, "source_url": url or filing.url, "tier": 1, "extractor": EXTRACTOR,
            "source_version_hash": hashlib.sha256(body.encode()).hexdigest()}


def events_from_8k(filing: Filing, body: str) -> list[TimelineEvent]:
    base = _base(filing, body)
    org = {"subject_type": "org", "subject_id": filing.cik, "date_precision": "day", **base}
    sections = item_sections(html_text(body))

    def org_event(event_type, found):
        sentence, anchor = found
        return TimelineEvent(event_type=event_type, event_date=stated_date(sentence, filing.report_date),
                             quote=quote_of(sentence, anchor), **org)

    events = []
    closing = sections.get("2.01", "")
    if (found := find_sentence(closing, COMPLETED, ACQUISITION)) and not (DISPOSAL.search(closing) and not ACQUIRED.search(closing)):
        events.append(org_event("acquisition_closed", found))
    if found := find_sentence(sections.get("4.01"), AUDITOR_ACT, AUDITOR):
        events.append(org_event("auditor_change", found))
    for m in officer_mentions(sections.get("5.02", ""), filing.company):
        events.append(OfficerEvent(
            subject_type="person", subject_id=person_id(m["name"], filing.company),
            event_type="officer_departure" if m["departure"] else "officer_appointment",
            event_date=stated_date(m["sentence"], filing.report_date), date_precision="day",
            quote=quote_of(m["sentence"], m["anchor"]), person_name=m["name"], title=m["title"],
            company=filing.company, reason=m["reason"], **base))
    return events


def event_from_report(filing: Filing, body: str, url=None) -> TimelineEvent:
    """A 10-K is quoted from its index page's dates, an NT 10-K from the reason in its
    Part III narrative; only when neither is found is the quote the filing's own metadata."""
    text = html_text(body)
    if filing.form == "NT 10-K":
        narrative = re.split(r"PART\s+III\W+NARRATIVE", text, maxsplit=1, flags=re.I)[-1]
        found = next((s for s in sentences(narrative) if LATE_REASON.search(s) and not s.lower().startswith("state below")), "")
        quote = quote_of(found) if found else f"Form NT 10-K filed {filing.filing_date} for the period ended {filing.report_date}."
        event_type = "late_filing"
    else:
        found = INDEX_DATES.search(text)
        quote = found.group(0) if found else f"Form 10-K for the period ended {filing.report_date} filed {filing.filing_date}."
        event_type = "annual_report_filed"
    return TimelineEvent(subject_type="org", subject_id=filing.cik, event_type=event_type, event_date=filing.filing_date,
                         date_precision="day", quote=quote, **_base(filing, body, url))


def filing_events(client, filing: Filing) -> list[TimelineEvent]:
    if filing.form == "8-K":
        wanted = {"2.01", "4.01", "5.02"}
        if filing.items and not wanted & set(filing.items):
            return []
        return events_from_8k(filing, client.get(filing.url))
    if filing.form == "10-K":
        # The annual report itself can run to megabytes; its index page is the dated source.
        return [event_from_report(filing, client.get(filing.index_url), filing.index_url)]
    if filing.form == "NT 10-K":
        return [event_from_report(filing, client.get(filing.url))]
    return []


def org_events(client, cik, since=None) -> list[TimelineEvent]:
    """Every event this source can state for one registrant, oldest first."""
    out = []
    for filing in filings(client, cik, since=since):
        out.extend(filing_events(client, filing))
    return sorted(out, key=lambda e: (e.event_date or "", e.observed_at))
