"""GTM accounts: the timing idea pointed at companies GI could sell agents or data to, or partner with. It is
separate from hiring and ops: its own list and its own Slack channel. It proposes; it contacts no one.

- buys: what the account would buy (agents, data, both), or partner, or watch (builds the same thing: never
  written to). Notes state only public GI facts, so none offers the gameplay data, whatever the account would
  buy. An account that also builds its own models says so on the list (own_models).
- call: a company moment opens a window to write: a new autonomy or robot-learning lead (once they have had two
  weeks to land), a paper on GI's topics by someone at the company, a relevant job post, a funding round or
  acquisition, a public ask GI could answer. A moment goes on the list once.
- who and how: a new lead or a public ask is written to that person; a paper to one of its own authors, as the
  paper lists them at the company (never the account's top author by count); any other moment to the champion.
  Never to someone recruiting watches (by subject id, OpenAlex author id or name), and one note a month per
  account at most, unless the news is about the person written to. The way in is never invented: an open
  conversation, a GI person's public tie, a shared investor, or none. Nothing is drafted without a GI person to
  write it.
- weekly: the Slack list of the few accounts to write to this week (this_week), each with why now, who writes to
  whom, the way in, what would change it and a draft from public GI facts. It leaves only through send(), which
  re-reads the ledger and records each account's contact as team sales.
- rails: the one thing GTM shares with hiring and events is the contact ledger, so one person has one open ask
  across GI: GTM reads it and writes its own postings. in_talks holds an account's people for every team until a
  founder decides (partner_staff).
- release_day: when GI publishes, who at each account should hear about it from GI first.
- find: companies whose people published on GI's topics (OpenAlex); for GI's accounts, postings for that work on
  their public job boards and headlines about funding or a new leader (Google News). Free; runs on the Mac.

GI's customer profile is inferred from public facts and Justin's word (action agents and the data behind them);
GI shared no customer list. Accounts are GI's to pick: live ones go in research/private/accounts/accounts.json;
tests/fixtures/accounts/ holds invented ones.
"""

import hashlib
import json
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import contact, events, routing, today
from .models import iso, parse_time
from .sources import jobs, news, openalex

ROOT = Path(__file__).resolve().parents[1]
RECORDS = {"simulation": ROOT / "tests/fixtures/accounts", "live": ROOT / "research/private/accounts"}
GTM = "gtm"  # the ledger's role id for accounts
CAP = 3  # accounts on the list a week: GTM's own cap, apart from the hiring cards'
# kind: what it is, the window to write (days after it became public), what would change it.
MOMENTS = {
    "new_lead": ("a new autonomy or robot-learning lead", 14, 90, "They leave the role, or the program they lead is cut."),
    "team_paper": ("a paper on GI's topics by someone at the company", 0, 60,
                   "The authors leave, or the work turns out to be a one-off."),
    "job_post": ("a job post for work GI does", 0, 45, "The post closes, or it is for a different product."),
    "funding": ("a funding round or acquisition", 0, 45, "The money goes to a line of work GI's research does not touch."),
    "public_ask": ("a public ask GI could answer", 0, 30, "They say it is solved, or the post is gone."),
}
PERSONAL = {"new_lead", "public_ask"}  # written to the moment's own person, never to a colleague
OWN = "also builds its own models"  # the label on an account that trains its own: a buyer, and a rival on models
LONGEST = max(m[2] for m in MOMENTS.values())
QUIET = timedelta(days=30)  # one note a month per account at most, unless the new moment is about the person written to
# What GI does, in the words of its own job posts: no figures. Nothing public says GI sells its gameplay data, so no
# note offers it (2026-09-24 fact-check).
GI_LINE = "we build world models and large action models, trained on gameplay from Medal"
TOPICS = ("world model", "vision-language-action", "learning from video", "sim-to-real", "embodied agent",
          "latent action", "game agent")
JOB_WORDS = re.compile(r"world model|robot learning|imitation learning|behavio(u)?r cloning|vision-language-action|\bVLA\b|"
                       r"sim-to-real|embodied|video pretraining|foundation model|reinforcement learning|game agent", re.I)
# A job post is for work GI does only when its title names that kind of work, not only its description: a hardware
# role at a company whose every post mentions its foundation models is not one.
JOB_TITLE = re.compile(r"machine learning|\bML\b|neural|\bAI\b|research|scientist|simulation|reinforcement|learning|world model|"
                       r"robot|humanoid|autonomy|embodied|\bVLA\b|\bvision\b|vision-language|perception|"
                       r"(?:motion|behavio(?:u)?r|path|task|trajectory) planning|controls engineer|technical staff|"
                       r"foundation model|game agent", re.I)
NOT_JOB = re.compile(r"hardware(?! acceleration)|mechanical|mechatronic|electrical|firmware|manufactur|technician|supply chain|"
                     r"sales|account exec|accountant|recruit|financ|legal|counsel|marketing|designer|product manager|"
                     r"program manager|\boperator\b(?! learning)|(?:safety|test|vehicle) driver|field (?:engineer|service|"
                     r"technician|operations)|user research|\bux\b|market research|public policy|privacy|coordinator|"
                     r"assistant|partnership|solutions|learning & development|access control", re.I)
# An author is a person: a model, a company or a team that OpenAlex lists as an author ("Gemini 3.1 (Flash)", "OpenAI",
# "Example Robotics Team") never is. "Claude Shannon" is a person; "Claude Team" and "Claude 3 Opus" are not.
MODEL = re.compile(r"^(?:gemini|gpt|chatgpt|claude|llama|qwen|mistral|deepseek|grok|genie|gemma|palm)"
                   r"(?=$|[\s-]*(?:\d|team\b|ai\b|opus|sonnet|haiku|flash|pro\b|ultra|nano))", re.I)
NOT_PERSON = re.compile(r"\d|[()\[\]]|\bet al\b|\b(?:openai|anthropic|deepmind|nvidia|google|microsoft)\b|"
                        r"\b(?:team|group|labs?|collaboration|consortium|contributors|project)\s*$", re.I)
BIG = re.compile(r"\b(google|deepmind|meta|microsoft|nvidia|openai|amazon|apple|tencent|alibaba|bytedance|huawei|baidu|"
                 r"samsung|ibm|intel|sony)\b", re.I)
GI = re.compile(r"general intuition|\bmedal\b", re.I)


def gi_work(title, text=""):
    """Whether a job post is for work GI does: its words (``JOB_WORDS``) in the title or text, and a title that
    names that kind of work (``JOB_TITLE``) and no other (``NOT_JOB``)."""
    title = title or ""
    return bool(JOB_WORDS.search(f"{title} {text or ''}") and JOB_TITLE.search(title) and not NOT_JOB.search(title))


def a_person(name):
    """Whether an author's name can be a person's: not a model (``MODEL``), a company or a team (``NOT_PERSON``), nor
    one word in Latin letters ("Genie", "PaLM-E"); a name in another script may be one word ("李明")."""
    name = " ".join((name or "").split())
    return bool(name and not MODEL.search(name) and not NOT_PERSON.search(name)
                and not re.fullmatch(r"[A-Za-z][A-Za-z.'’-]*", name))
# A paper counts only when its title is on GI's work: one of its phrases, a world model, or a strong word of acting in
# a world (robots, video, driving, games); a world model of text or stories, or another field, doesn't, unless a strong
# word stands beside it ("tokenized world models for driving").
ON_TOPIC = re.compile(r"vision-language-action|\bVLAs?\b|sim-?to-?real|sim2real|\bembodied|\blatent actions?|"
                      r"imitation learning|behavio(u)?r(al)? cloning|\bgame agents?|game-playing|video games?|"
                      r"\bgameplay|\brobot(ic)? (learning|polic(y|ies)|manipulation|foundation)|\bhumanoid|"
                      r"inverse dynamics|video pre-?train|learning from (human )?videos?|\baction models?|visuomotor",
                      re.I)
WORLD_MODEL = re.compile(r"\bworld[- ](?:foundation )?model(?:s|ing)?\b", re.I)
STRONG = re.compile(r"\b(?:robot|video|driving|(?<!text-based )(?<!text )games?\b(?!-theor)|atari|minecraft|manipulat|"
                    r"locomot|navigat|drone)", re.I)
# GI's own work, by a title's words: only then may a note call a paper or a post "close to what we're doing".
GI_OWN = re.compile(r"\bworld[- ]?(?:foundation )?model(?:s|l?ing)?\b|\bworld simulators?|(?<!language[- ])\baction[- ]models?|"
                    r"\blatent[- ]actions?|\bgame(?:-?plays?|[- ]play)\b|\bvideo[- ]?games?|\bgame agents?|"
                    r"(?:neural|generative|learned) game engines?|models? (?:are|as) (?:real-time )?game engines?|game-playing|"
                    r"inverse dynamics|video pre-?train|learning from (?:human )?videos?|\bminecraft|\batari", re.I)
# A title that uses those words about something else: a board game's toy model, a clinic, a classroom, a planner.
NOT_OWN = re.compile(r"\b(?:othello|chess|gpt|addiction|sleep|classroom|education|knee|gait|pddl)\b", re.I)
OFF_TOPIC = re.compile(r"\b(?:language|llms?|text|token|stor(?:y|ies)|narrative|fiction|reasoning|retriev|extraction|"
                       r"molecul|chemi|clinical|medical|financ|econom)", re.I)


class Author(BaseModel):
    name: str = Field(min_length=2)
    url: str = Field("", pattern=r"^(https://.*)?$")  # their OpenAlex page
    at: str = ""           # the company the paper lists them at, as OpenAlex names it
    affiliation: str = ""  # the paper's own words for where they are, when it gives them
    listed_at: list[str] = Field(default_factory=list)  # the places the paper's own line names (_author)
    city: str = ""         # and the city it gives, when it gives one
    own_words: bool = False  # listed_at and city were read from the paper's own line (else from OpenAlex, or not yet)
    first: bool = False    # the paper's first author


class Signal(BaseModel):
    kind: Literal["new_lead", "team_paper", "job_post", "funding", "public_ask"]
    day: date | None = None  # the day it became public; undated evidence never opens a window
    who: str = ""            # the person it is about, when it is about one (a new lead, whoever asked)
    authors: list[Author] = Field(default_factory=list)  # a paper's authors at the account, as the paper lists them
    quote: str = Field(min_length=1)
    source_url: str = Field(pattern=r"^https://")
    # A paper's record: where this version appeared, and the paper's first public version when it came earlier (on
    # arXiv before the proceedings), once scripts/accounts.py authors has looked for one (checked).
    venue: str = ""
    first_public: date | None = None
    first_at: str = ""
    checked: bool = False
    abstract: str = ""  # a paper's abstract, as OpenAlex has it, for whoever writes its gist
    gist: str = ""      # what the paper does, in plain words a person wrote from it: never the title again (gist_problem)
    about: str = ""     # what the paper is about, as a person put it for the note's opener ("saw your paper about
                        # conditioned game agents"), in place of _called's; never the title's words (about_problem)


class Contact(BaseModel):
    name: str = Field(min_length=2)
    title: str = ""
    part: Literal["champion", "decision_maker", ""] = ""  # the technical champion, or who decides
    subject_id: str | None = None  # when GI already watches them: their timeline and ledger key
    source_url: str = Field("", pattern=r"^(https://.*)?$")  # where the name and title were found


class Tie(BaseModel):
    by: str = Field(min_length=2)  # the GI person who holds the tie, and so writes
    to: str = ""                   # the contact it is with; "" for the account as a whole
    what: str = Field(min_length=1)


class Account(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=2)
    segment: str = ""
    buys: Literal["agents", "data", "both", "partner", "watch"] = "agents"  # watch: builds the same thing
    fit: int = Field(3, ge=1, le=5)
    why: str = ""         # why it fits GI, or why not
    owner: str = ""       # GI's one point of contact for the account
    own_models: bool = False  # it also trains its own models: shown on the list as OWN
    ties: list[Tie] = Field(default_factory=list)
    investors: list[str] = Field(default_factory=list)  # investors it shares with GI
    boards: dict[str, str] = Field(default_factory=dict)  # job board to slug, for find
    contacts: list[Contact] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)


def load(folder):
    path = Path(folder) / "accounts.json"
    return [Account.model_validate(a) for a in json.loads(path.read_text())["accounts"]] if path.exists() else None


def key(account, c):
    """The contact's ledger key: their subject id when GI watches them, else one per account and name, accents
    folded and initials kept ("José A. García" and "Jose A. Garcia" are one key, "A. Chen" and "B. Chen" two). A
    name with a letter that doesn't fold to ASCII (李, ø) keys by a hash of the whole name."""
    if c.subject_id:
        return contact.person_key(c.subject_id)  # the ledger's key: no role prefix, which would tell sales the role
    whole = " ".join(unicodedata.normalize("NFKC", c.name).casefold().split())
    slug = _fold(c.name).replace(" ", "-")
    return f"account:{account.id}:{slug or 'n' + hashlib.sha256(whole.encode()).hexdigest()[:12]}"


def _when(day):
    return f"{day:%b} {day.day}"


PREPRINTS = re.compile(r"\b(?:arxiv|biorxiv|medrxiv|chemrxiv|techrxiv|ssrn|research square|preprints\.org)\b", re.I)


def _published(s, today=None):
    """When a paper was out, by month only (a record's day is often not the paper's: a proceedings chapter's online
    day, a listing's): where this version appeared, unless it isn't out yet on ``today``, and when the paper first
    went up if that was a month or more earlier. "in Example Workshop Proceedings, on arXiv since June 2026", "on
    arXiv, July 2026", "July 2026"."""
    out = not today or s.day <= today
    where = f"{'on' if PREPRINTS.search(s.venue) else 'in'} {s.venue}" if s.venue and out else ""
    if s.first_public and (s.first_public.year, s.first_public.month) < (s.day.year, s.day.month):
        first = (f"on {s.first_at} since " if s.first_at else "public since ") + f"{s.first_public:%B %Y}"
        return ", ".join(filter(None, [where or (f"{s.day:%B %Y}" if out else ""), first]))
    return ", ".join(filter(None, [where, f"{s.day:%B %Y}"]))


def _dated(s, today=None):
    """A moment's date as the list and the card say it on ``today``: a paper's by month (_published), anything else
    by day."""
    return _published(s, today) if s.kind == "team_paper" else _when(s.day)


def _public(s):
    """The day a moment became public, which its window counts from: for a paper, its first public version when an
    earlier one is known (a preprint before the proceedings), else its record's day."""
    return min(filter(None, (s.day, s.first_public)))


QUOTE_MAX = 200  # characters of a post or title quoted on the list or in a draft, so a long post never runs past Slack's limit


def _clip(text):
    return text if len(text) <= QUOTE_MAX else text[:QUOTE_MAX - 1].rsplit(" ", 1)[0] + "…"


def _window(s):
    return _public(s) + timedelta(days=MOMENTS[s.kind][1]), _public(s) + timedelta(days=MOMENTS[s.kind][2])


def listed(store, account):
    """{source_url: day} for this account's moments that already went out on a weekly list."""
    urls = {s.source_url for s in account.signals}
    return {u: e["at"][:10] for e in store.all("contact") if e["kind"] == "pinged" and e["role_id"] == GTM
            for u in e["evidence"] if u in urls} if store else {}


def call(account, day, done=()):
    """(action, until, open moments newest first) as of a day: reach_now until the newest open moment's window
    closes, watch_until the next window opens, or quiet. A moment in ``done`` (already listed) is not open again.
    Watch accounts are always quiet."""
    if account.buys == "watch":
        return "quiet", None, []
    windows = [(s, *_window(s)) for s in account.signals if s.day and _public(s) <= day and s.source_url not in done]
    now = sorted((w for w in windows if w[1] <= day <= w[2]), key=lambda w: _public(w[0]), reverse=True)
    if now:
        return "reach_now", now[0][2], [w[0] for w in now]
    later = [w for w in windows if day < w[1]]
    if later:
        return "watch_until", min(w[1] for w in later), []
    return "quiet", None, []


def short(name):
    """An account's company name without the teams GI has in mind: "NVIDIA (Cosmos, Isaac)" is "NVIDIA", so a note
    or a card never reads as if the person written to were on those teams."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", name).strip() or name


def _said(account, s, today):
    return (f"{MOMENTS[s.kind][0][0].upper()}{MOMENTS[s.kind][0][1:]} ({s.who or short(account.name)}, {_dated(s, today)}): "
            f"“{_clip(s.quote)}”")


def why(account, day, action, until, now, done=()):
    if account.buys == "watch":
        return f"{account.why or 'Builds the same thing.'} Watch, don't sell."
    if action == "reach_now":
        return f"{_said(account, now[0], day)}. Write by {_when(until)}."
    if action == "watch_until":
        s = min((s for s in account.signals if s.day and _public(s) <= day < _window(s)[0]), key=lambda s: _window(s)[0])
        return f"{_said(account, s, day)}. Give them two weeks to land: write from {_when(until)}."
    if sent := [(done[s.source_url], s) for s in account.signals if s.source_url in done and s.day and _window(s)[1] >= day]:
        at, s = max(sent, key=lambda x: x[0])
        return f"On the list on {_when(date.fromisoformat(at))} for {MOMENTS[s.kind][0]}: nothing new since."
    last = max((s for s in account.signals if s.day and _public(s) <= day), key=_public, default=None)
    return (f"Nothing new: the last moment, {MOMENTS[last.kind][0]} ({_dated(last, day)}), is past its window."
            if last else "No dated moment on file yet.")


def _conversation(history):
    """The conversation someone at GI has open with them (their latest answer was a reply), or None."""
    answer = next((e for e in reversed(history) if e["kind"] in ("replied", "not_now")), None)
    return answer if answer and answer["kind"] == "replied" and answer.get("by") else None


def _in_conversation(clearance):
    # contact.check's hold for an open conversation: the only hold a note inside that conversation may pass.
    return clearance.state == "hold" and clearance.reason.startswith("In conversation")


def _name(name):
    """A name for comparing: no accents, hyphens as spaces, no initials, no punctuation. "Léna K. Okafor" and
    "Lena Okafor" compare equal. A name with nothing left (another script, only initials) compares as itself."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().replace("-", " ")
    plain = re.sub(r"['\u2019]", "", plain)  # O'Brien is OBrien
    return (" ".join(w for w in re.sub(r"[^a-z ]+", " ", plain).split() if len(w) > 1)
            or " ".join(unicodedata.normalize("NFKC", name).casefold().split()))


def _fold(name):
    """Lowercase ASCII words, accents folded, apostrophes dropped, hyphens as spaces, initials and digits kept; ""
    when a letter doesn't fold to ASCII (李, ø, ł)."""
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch)).lower()
    plain = re.sub(r"['\u2019]", "", plain.replace("-", " "))
    return "" if any(ch.isalpha() and not ch.isascii() for ch in plain) else " ".join(re.sub(r"[^a-z0-9]+", " ", plain).split())


def _openalex(value):
    """An OpenAlex author id ("A123") from an openalex.org URL or a bare id, else ""."""
    m = re.fullmatch(r"(?:https://(?:api\.)?openalex\.org/(?:authors/)?)?([Aa]\d+)", str(value or "").strip())
    return m[1].upper() if m else ""


def _watched(store, records):
    """Everyone recruiting watches, with the role, three ways: by subject id, by OpenAlex author id (the timelines
    store's anchors) and by name. The event records' people and everyone in the timelines store."""
    found = [(p.subject_id, p.name, p.role, "") for p in records["people"]]
    found += [(c["subject_id"], c["name"], c.get("role") or "", _openalex((c.get("anchors") or {}).get("openalex")))
              for c in store.all("person_context")]
    ids, authors, names = {}, {}, {}
    for sid, name, role, author in found:
        title = events._title(role) if role else "a hiring role"
        ids.setdefault(sid, title)
        ids.setdefault(contact.person_key(sid), title)  # a contact's stored id carries no role
        if _name(name):
            names.setdefault(_name(name), title)
        if author:
            authors.setdefault(author, (sid, title))
    return ids, authors, names


def identify(store, c):
    """The subject id of someone the timelines store already has, when the contact's source is their OpenAlex
    author page; else None. A name alone never identifies anyone."""
    author = _openalex(c.source_url)
    found = next((p["subject_id"] for p in store.all("person_context")
                  if author and _openalex((p.get("anchors") or {}).get("openalex")) == author), None) if store else None
    return contact.person_key(found) if found else None  # without its role, which sales never sees


def _history(store, k, as_of):
    """A contact's ledger history as of a time, less the accounts list's own postings, which _posted reads: a
    posting is not a note (what was sent, and any answer, still hold)."""
    return [e for e in contact.history(store, k) if parse_time(e["at"]) <= parse_time(as_of)
            and not (e["kind"] == "pinged" and e["role_id"] == GTM)]


def _posted(store, k, as_of):
    """The day this person was last on the accounts list, when that was under QUIET ago; else None."""
    now = parse_time(as_of)
    at = [parse_time(e["at"]) for e in contact.history(store, k) if e["kind"] == "pinged" and e["role_id"] == GTM
          and now - QUIET < parse_time(e["at"]) <= now]
    return max(at).date() if at else None


# What sales may see of another team's entries: the person's own wish, who they are, a ledger that can't be read and
# an account in talks. Anything else another team has with someone (an accepted offer, a conversation, an invite) is
# hiring's or events' business: sales sees only that someone holds them (HELD).
SEEN = frozenset({"never", "wrong_person", "identity", "unreadable", "partner_staff"})
HELD = "Another team at GI holds them."
WATCHED = "Another team at GI holds them: sales doesn't write to them."
UNREADABLE = "A contact ledger can't be read."  # the page says why once (page's ledger)


def _visible(history):
    return [e for e in history if e.get("team") == "sales" or e["kind"] in SEEN]


def _unnamed(history):
    """The entries with no other team's person or team on them: contact.check names nobody for an entry "by" no
    one on recruiting (its silent default), so a partner hold a recruiter noted reads with no name."""
    return [e if e.get("team") == "sales" else {**e, "by": "", "team": "recruiting"} for e in history]


def _for_sales(history, as_of):
    """contact.check's word on someone as sales may see it: its state always, and its reason when sales' own
    entries and SEEN give the same one, with no other team's names; else HELD, so no other team's reason reaches
    sales. A broken ledger is said once for the page, not here."""
    full = contact.check(history, as_of)
    if full.state == "clear":
        return full
    if any(e["kind"] == "unreadable" for e in history):
        return contact.Clearance(state=full.state, reason=UNREADABLE)
    shown, own = (contact.check(_unnamed(h), as_of) for h in (history, _visible(history)))
    same = (own.state, own.reason) == (shown.state, shown.reason)
    return contact.Clearance(state=full.state, reason=own.reason if same else HELD)


def _on_file(account):
    """The account's contacts, then each paper's authors there who are not among them, as the paper lists them:
    written to about that paper only (``_mine``), and held with the account when it is in talks."""
    on_file = list(account.contacts)
    for s in account.signals:
        for a in s.authors if s.kind == "team_paper" else ():
            if not any(_same(c.name, a.name) for c in on_file):
                on_file.append(Contact(name=a.name, title=f"{'First author' if a.first else 'Author'} of the paper, "
                                                          f"{_listed(a, short(account.name))}", source_url=a.url))
    return on_file


def _listed(a, company):
    """Where the paper lists author ``a``, as the card says it: the places its own line names, and its city ("listed
    at Example University and Amazon, Exampleburg"); the line itself when it reads more than one way ("which lists
    them as “...”"); else, read before the paper's own words were, the company OpenAlex matched without its country
    ("listed at Amazon"), else ``company``."""
    if a.listed_at:
        places = a.listed_at[0] if len(a.listed_at) == 1 else f"{', '.join(a.listed_at[:-1])} and {a.listed_at[-1]}"
        return f"listed at {places}" + (f", {a.city}" if a.city else "")
    if a.own_words and a.affiliation:
        return f"which lists them as “{a.affiliation}”"
    return f"listed at {short(a.at) if a.at else company}"


def people(store, account, as_of, records):
    """Each contact with the ledger's word on them as sales may see it (_for_sales), whether recruiting watches them
    (by subject id, OpenAlex author id or name: a namesake is held too; only whether, never for what), when they were
    last on the accounts list, and a conversation sales has open with them."""
    ids, authors, names = _watched(store, records)
    out, day = [], parse_time(as_of).date().isoformat()
    talks = next((e for c in _on_file(account)  # the account in talks with GI: its hold covers a paper's authors too
                  for e in contact.partner_holds(sorted(_history(store, key(account, c), as_of),
                                                        key=lambda e: parse_time(e["at"])), day)
                  if contact.talks_account(e) == account.id), None)
    for n, c in enumerate(_on_file(account)):
        k = key(account, c)
        history = _history(store, k, as_of)
        paper = n >= len(account.contacts)
        clearance = _for_sales(sorted(history + [talks], key=lambda e: parse_time(e["at"])) if paper and talks else history,
                               as_of)
        out.append({"name": c.name, "title": c.title, "part": c.part, "key": k, "source_url": c.source_url,
                    "paper": paper,
                    "watched": bool(ids.get(c.subject_id) or (authors.get(_openalex(c.source_url)) or (None, None))[1]
                                    or (names.get(_name(c.name)) if _name(c.name) else None)),
                    "posted": _posted(store, k, as_of),
                    "talking": _conversation(_visible(history)) if _in_conversation(clearance) else None,
                    "gate": None if clearance.state == "clear" else {"state": clearance.state, "reason": clearance.reason}})
    return out


def _mine(contacts, s=None):
    """The contacts moment ``s`` may go to: its own person for a new lead or a public ask; for a paper, one of its
    authors there (``_authors``), never a colleague; else anyone on file, but not someone only a paper put there."""
    if s and s.kind in PERSONAL:
        return [c for c in contacts if _same(c["name"], s.who)]
    if s and s.kind == "team_paper":
        return [c for c in contacts if _wrote(c["name"], s)]
    return [c for c in contacts if not c.get("paper")]


def _authors(s):
    """A paper's authors at the account: as the paper lists them (``find``, ``authors_at``), else the one person it
    was typed in with (``who``); none when neither is known."""
    return [a.name for a in s.authors] or ([s.who] if s.who else [])


def _wrote(name, s):
    """``name`` is one of paper ``s``'s authors at the account."""
    return any(_same(name, a) for a in _authors(s))


def _same(a, b):
    """One person's name, however it is written ("Léna Okafor", "lena okafor", "Léna K. Okafor"), unless both give
    initials and they differ ("A. Chen" is not "B. Chen")."""
    if not (a and b) or _name(a) != _name(b):
        return False
    if not (_fold(a) and _fold(b)):  # a letter that doesn't fold (李, ø): the whole names must agree
        return " ".join(unicodedata.normalize("NFKC", a).casefold().split()) == " ".join(
            unicodedata.normalize("NFKC", b).casefold().split())
    initials = [{w for w in _fold(n).split() if len(w) == 1} for n in (a, b)]
    return not all(initials) or initials[0] == initials[1]


def _about(c, s):
    return bool(s) and s.kind in PERSONAL and _same(c["name"], s.who)


def _last_posted(contacts):
    """The last day anyone at the account was on the list, within QUIET; else None."""
    return max((c["posted"] for c in contacts if c["posted"]), default=None)


def _best(contacts, s=None):
    """Who to write to about moment ``s``: of _mine, never someone recruiting watches; someone GI already talks
    with (in that conversation), else a clear champion, else anyone clear. None when nobody fits."""
    fits = [c for c in _mine(contacts, s) if not c["watched"]]
    clear = [c for c in fits if not c["gate"]]
    return (next((c for c in fits if c["talking"]), None) or next((c for c in clear if c["part"] == "champion"), None)
            or (clear[0] if clear else None))


def _nobody(contacts, s=None):
    """Why nobody gets a note about moment ``s``."""
    if s and s.kind in PERSONAL and not s.who:
        return "The source names no one: find out who it is before anyone writes."
    if not (mine := _mine(contacts, s)):
        if s and s.kind in PERSONAL:
            return f"{s.who} is not a contact on file yet."
        if s and s.kind == "team_paper":
            return ("Which of the paper's authors work there isn't on file: `scripts/accounts.py authors` reads it."
                    if not _authors(s) else f"None of the paper's authors there is on file: {', '.join(_authors(s)[:3])}.")
        return "No contact on file yet: the people map adds them."
    if all((c["gate"] or {}).get("reason") == UNREADABLE for c in mine):
        return "Nobody to write to while a contact ledger can't be read."

    def why_not(c):
        return f"{c['name']}: {WATCHED if c['watched'] else (c['gate'] or {}).get('reason', 'not clear.')}"
    return "Nobody to write to. " + " ".join(why_not(c) for c in mine)


def way_in(account, best):
    """(how GI gets to ``best``, who writes), strongest first and never invented: sales' own open conversation; a
    GI person's public tie; an investor on both cap tables; else (None, the owner). Never through someone recruiting
    watches."""
    if t := best["talking"]:
        return (f"{t['by']} ({t['team']}) has been talking with {best['name']} since "
                f"{_when(parse_time(t['at']).date())}: {t['by']} raises it in that conversation. Nobody else writes to "
                f"{best['name']}."), t["by"]
    if tie := next((t for t in account.ties if t.to in ("", best["name"])), None):
        return f"{tie.what} {routing._first(tie.by)} writes.", tie.by
    if account.investors:
        return f"{account.investors[0]} backs both GI and {short(account.name)}: ask them for an introduction.", account.owner
    return None, account.owner


GIST_WORDS = (3, 25)
TITLE_RUN = 4  # words in a row a gist may not share with the title: it says what the paper does, not what it is called


def _words_of(text):
    return re.findall(r"[\w'-]+", (text or "").casefold())


def gist_problem(gist, title):
    """Why ``gist`` can't be a paper note's line on what the paper does, or None: it is 3 to 25 words, and shares no
    run of four words with the title (a note that restates the title names nothing)."""
    words, named = _words_of(gist), _words_of(title)
    if not GIST_WORDS[0] <= len(words) <= GIST_WORDS[1]:
        return f"Say it in {GIST_WORDS[0]} to {GIST_WORDS[1]} words."
    runs = {tuple(named[i:i + TITLE_RUN]) for i in range(len(named) - TITLE_RUN + 1)}
    if any(tuple(words[i:i + TITLE_RUN]) in runs for i in range(len(words) - TITLE_RUN + 1)):
        return "It repeats the title: say what the paper does in other words, from its abstract."
    return None


ABOUT_WORDS = (2, 6)


def about_problem(about, title):
    """Why ``about`` can't open a paper's note ("saw your paper about ..."), or None: 2 to 6 words, not starting
    with "about" (the note says it) or naming where it went up (the note says that too), and no four in a row from
    the title."""
    words = _words_of(about)
    if not ABOUT_WORDS[0] <= len(words) <= ABOUT_WORDS[1]:
        return f"Say it in {ABOUT_WORDS[0]} to {ABOUT_WORDS[1]} words."
    if words[0] == "about":
        return "Leave out the first “about”: the note says it."
    if PREPRINTS.search(about):
        return "Leave out where it went up: the note says where."
    if len(words) >= TITLE_RUN and gist_problem(about, title):
        return "It repeats the title: say what the paper is about in other words."
    return None


# The GI fact closest to a paper, post or ask on GI's own work (GI_OWN): world models, large action models, or both,
# and always what they are trained on; with how the two differ where its title says (a world model written as code).
WORLDS = re.compile(r"\bworld[- ]?(?:foundation )?model(?:s|l?ing)?\b|\bworld simulators?|game engines?|"
                    r"video (?:generation|prediction)", re.I)
ACTIONS = re.compile(r"(?<!language[- ])\baction[- ]models?|\blatent[- ]actions?|\binverse dynamics|\bagents?\b|"
                     r"game-playing|\bplay(?:s|ing)?\b|\bpolic(?:y|ies)\b|imitation|behavio(?:u)?r(?:al)? cloning", re.I)
NAMED = re.compile(r"\bworld[- ]?(?:foundation )?model(?:s|l?ing)?\b|\bworld simulators?|(?<!language[- ])\baction[- ]models?|"
                   r"\blatent[- ]actions?", re.I)  # the kinds of model GI builds, by name: only then "too"
AS_CODE = re.compile(r"\bprogrammatic|\bprogram synthesis|\bcode world models?|\b(?:as|in) (?:python )?code\b|"
                     r"\bsymbolic world", re.I)


def _similar(quote, too=True):
    """"We're building world models at General Intuition too, trained on gameplay video from Medal rather than written
    as code": the GI fact closest to ``quote``, and how the two differ where its words say; "too" only when its
    words name world models or action models (``NAMED``): an agent or a game engine is close, not the same. No
    closing stop."""
    world, act = WORLDS.search(quote), ACTIONS.search(quote)
    what = ("world models and large action models" if bool(world) == bool(act) else
            "world models" if world else "large action models")
    too = too and NAMED.search(quote)
    return (f"We're building {what} at General Intuition{' too' if too else ''}, trained on gameplay video from Medal"
            + (" rather than written as code" if AS_CODE.search(quote) else ""))


# What a paper note calls the paper, in Justin's voice ("saw your Halcyon paper on arXiv"), never by pasting its title:
# the short name before a title's colon, else the thing its title is about when that is a kind of thing (``HEADS``),
# else just "paper".
LABELS = frozenset({"position", "survey", "review", "tutorial", "perspective", "commentary", "correction", "erratum",
                    "editorial", "keynote", "abstract", "demo", "poster", "preprint", "report", "paper", "study",
                    "overview", "note", "notes", "results", "part", "chapter", "appendix", "reply", "response",
                    "comment", "letter"})  # "Position Paper:", "Case Study:": what kind of paper, not its name
LEAD_IN = frozenset({"a", "an", "the", "towards", "toward", "on", "your", "our", "my", "their", "one", "this", "these",
                     "every", "any", "all", "no"})
STOPS = frozenset({"on", "with", "for", "from", "via", "in", "using", "by", "through", "as", "of", "to", "and", "or",
                   "under", "at", "over", "without", "beyond", "into", "across", "against", "vs", "versus", "is", "are",
                   "can", "do", "does", "that", "which", "when", "where", "how", "why", "what", "meets"})
VERBS = frozenset({"beat", "beats", "make", "makes", "help", "helps", "improve", "improves", "enable", "enables",
                   "yield", "yields", "need", "needs", "drive", "drives", "learn", "learns", "outperform",
                   "outperforms", "boost", "boosts", "unlock", "unlocks", "matter", "matters", "become", "becomes",
                   "scale", "scales", "reduce", "reduces", "lead", "leads", "let", "lets", "solve", "solves"})
HEADS = {"agent": "agent", "agents": "agent", "model": "model", "models": "model", "policy": "policy",
         "policies": "policy", "controller": "controller", "controllers": "controller", "planner": "planner",
         "planners": "planner", "simulator": "simulator", "simulators": "simulator", "engine": "engine",
         "engines": "engine", "benchmark": "benchmark", "benchmarks": "benchmark", "dataset": "dataset",
         "datasets": "dataset", "robot": "robot", "robots": "robot", "emulator": "emulator", "emulators": "emulator"}


def _called(title, context=()):
    """"Halcyon" for "Halcyon: Active Abstraction ...", "tide-pool agent" for "Training a Tide-Pool
    Agent on ...", or "" when the title gives neither plainly: a name that is a label ("Position Paper"), or a
    subject of one word ("Models") or that is no kind of thing. A title's capitals read lower unless one of
    ``context`` (what the paper does, its abstract) writes the word so mid-sentence, as a name ("Minecraft")."""
    title = " ".join((title or "").split())
    lead, *rest = re.split(r"\s*:\s+", title, maxsplit=1)
    if rest and 1 <= len(lead.split()) <= 2 and len(lead) <= 20 and all(w[0].isupper() or w[0].isdigit() for w in lead.split()) \
            and not any(w.casefold() in LABELS for w in lead.split()):
        return lead
    words = (rest[0] if rest else title).rstrip(".?!").split()
    at = 1 if words and words[0].casefold().endswith("ing") and len(words[0]) > 4 else 0  # "Training a ... Agent"
    while at < len(words) and words[at].casefold() in LEAD_IN:
        at += 1
    end = next((i for i in range(at, len(words)) if words[i].casefold() in STOPS), len(words))
    at = max([at, *(i + 1 for i in range(at, end) if words[i].casefold() in VERBS)])  # "... Beat Complex World Models"
    subject = words[max(at, end - 4):end]  # the thing, with at most three words before it
    if len(subject) < 2 or subject[-1].casefold() not in HEADS or not all(re.fullmatch(r"[A-Za-z][\w-]*", w)
                                                                           for w in subject):
        return ""
    subject[-1] = subject[-1][0] + HEADS[subject[-1].casefold()][1:]
    long = [w for w in words if len(w) > 3]
    titled = sum(w[0].isupper() for w in long) > len(long) / 2
    first = max(at, end - 4) == 0  # the subject opens the title or its subtitle: its capital is the sentence's

    def case(part, opens):
        named = any(re.search(rf"[^.!?\s]\s+{re.escape(part)}\b", text) for text in context)  # mid-sentence, their words
        return part.lower() if part[1:].islower() and (titled or opens) and not named else part
    # "Video Game Agent" and "Open-Ended" in a title's capitals read lower; "VLM" stays, and "Minecraft" where named
    return " ".join("-".join(case(p, first and i == 0 and j == 0) for j, p in enumerate(w.split("-")))
                    for i, w in enumerate(subject))


def _where(s, today=None):
    """Where a paper went up, as a note says it: the preprint server its first public version was on ("on arXiv"),
    else this version's when that is one and it is out; "" otherwise (a proceedings' or a library's name reads as
    a catalogue, not as where someone saw it)."""
    if s.first_public and s.first_at:
        return f"on {s.first_at}"
    if s.venue and PREPRINTS.search(s.venue) and (not today or not s.day or s.day <= today):
        return f"on {s.venue}"
    return ""


def draft(account, s, to, writer, today=None):
    """A short first note in GI's voice about the moment, signed by whoever writes: public GI facts only, never a
    headline's own words, which can carry figures, and the company by its name alone (``short``).

    A paper is "your Halcyon paper on arXiv" (``_called``, ``_where``; "your paper about ..." where a person set
    ``Signal.about``), never its pasted title, and "your" only to one of its authors there (``_authors``), else "the".
    The note says what caught the eye in the words a person wrote on what it does (``Signal.gist``; assess holds a
    paper without one).

    A paper, post or public ask gets the GI fact closest to it, and how the two differ (``_similar``), only when its
    words name GI's own work (``GI_OWN``, and none of ``OFF_TOPIC`` or ``NOT_OWN``); otherwise the note says only what
    GI does."""
    first, quote, company = routing._first(to["name"]), _clip(s.quote), short(account.name)
    called, where = _called(s.quote, (s.gist, re.sub(r"^\s*abstract\b[:.]?\s*", "", s.abstract, flags=re.I))), _where(s, today)
    about = s.about.strip().rstrip(".,;:")
    paper = " ".join(filter(None, ["your" if _wrote(to["name"], s) else "the", "" if about else called, "paper",
                                   f"about {about}" if about else "", where]))
    own = bool(GI_OWN.search(s.quote or "") and not OFF_TOPIC.search(s.quote or "") and not NOT_OWN.search(s.quote or ""))
    gist = s.gist.strip().rstrip(".")
    seen = f"Hey {first}, saw {paper}." + (f" {gist[0].upper()}{gist[1:]} really caught my eye." if gist else "")
    opening, closing = {
        "new_lead": (f"Hey {first}, congrats on the new role at {company}. At General Intuition {GI_LINE}.",
                     "I'd love to hear what you plan to build first, and I'm happy to share what we've learned if it's useful."),
        "team_paper": (f"{seen} {_similar(s.quote)}.", "Would love to compare notes if you're interested.") if own else
                      (f"{seen} At General Intuition {GI_LINE}.", "Would love to hear more about it if you're up for a chat."),
        "job_post": (f"Hey {first}, saw {company} is hiring for “{quote}”. {_similar(s.quote)}.",
                     "Would love to hear what your team is building if you're up for a chat.")
                    if own else (f"Hey {first}, saw {company} is hiring for “{quote}”. At General Intuition {GI_LINE}.",
                                 "Would love to hear what your team is building if you're up for a chat."),
        "funding": (f"Hey {first}, saw the news about {company}. At General Intuition {GI_LINE}.",
                    "Would love to hear what's next for your team if you're up for a chat."),
        "public_ask": (f"Hey {first}, saw your post: “{quote}”. {_similar(s.quote, too=False)}, so we've spent a lot of "
                       "time on this.", "Happy to share what we've learned if it's useful.") if own else
                      (f"Hey {first}, saw your post: “{quote}”. At General Intuition {GI_LINE}.",
                       "Would love to hear more about what you're after if you're up for a chat."),
    }[s.kind]
    body = f"{opening} {closing}"
    return f"{body}\n\n{routing._first(writer)}"


def _waiting(s):
    """Why a paper's note waits, or "": find read it from OpenAlex and ``scripts/accounts.py authors`` hasn't yet looked
    for an earlier version (its date and window aren't known; one typed in by hand counts from its own day) or read
    where the paper lists an author OpenAlex placed there (``_author``), or no one has yet said what it does
    (``gist``)."""
    if s.kind != "team_paper":
        return ""
    if s.source_url.startswith(FOUND["paper"]) and not s.checked:
        return "Whether the paper went up earlier isn't checked yet: `scripts/accounts.py authors` reads it."
    if s.source_url.startswith(FOUND["paper"]) and any(a.url.startswith(FOUND["paper"][1]) and not a.own_words
                                                       for a in s.authors):  # OpenAlex's match alone, kept before
        return "Where the paper itself lists its authors isn't read yet: `scripts/accounts.py authors` reads it."
    if not s.gist.strip():
        return "Say what the paper does before anyone writes: `scripts/accounts.py gist` lists it with its abstract."
    if problem := gist_problem(s.gist, s.quote):  # typed into the file by hand
        return f"The line on what the paper does can't go out: {problem} (`scripts/accounts.py gist` sets another)."
    if s.about.strip() and (problem := about_problem(s.about, s.quote)):
        return f"The line on what the paper is about can't go out: {problem} (`scripts/accounts.py about` sets another)."
    return ""


def assess(store, as_of, account, records):
    """One account's row: the call, why, the contacts, who writes to whom, the way in and the draft. The note is
    about the newest open moment someone can be written to about. ``blocked`` says why there is no draft."""
    day = parse_time(as_of).date()
    done = listed(store, account)
    action, until, now = call(account, day, done)
    contacts = people(store, account, as_of, records) if store else []
    ready = [(s, b) for s in now if (b := _best(contacts, s))]
    picks = [(s, b) for s, b in ready if not _waiting(s)]  # a paper waiting on either holds back nothing else
    if _last_posted(contacts):  # a quiet month: only news about the person written to may go out
        picks = [(s, b) for s, b in picks if _about(b, s)] or picks
    moment, best = picks[0] if picks else ready[0] if ready else (now[0] if now else None, None)
    until = _window(moment)[1] if moment else until
    route, writer = way_in(account, best) if best else (None, account.owner)
    blocked = None
    if moment and not best:
        blocked = _nobody(contacts, moment)
    elif moment and not writer:
        blocked = "No one at GI owns this account yet: pick an owner to write."
    elif moment and (wait := _waiting(moment)):
        blocked = wait
    elif moment and (last := _last_posted(contacts)) and not _about(best, moment):
        blocked = (f"{account.name} was on the list on {_when(last)}: one note a month per account, unless the news "
                   "is about the person written to.")
    now = [moment, *(s for s in now if s is not moment)] if moment else now
    signals = [{"kind": s.kind, "what": MOMENTS[s.kind][0], "day": s.day.isoformat() if s.day else None,
                "when": _dated(s, day) if s.day else "undated", "who": s.who, "quote": s.quote,
                "source_url": s.source_url, "open": s in now, "about": s is moment}
               for s in sorted(account.signals, key=lambda s: s.day or date.min, reverse=True)]
    return {"id": account.id, "name": account.name, "segment": account.segment, "buys": account.buys,
            "label": OWN if account.own_models else "",
            "fit": account.fit, "action": action, "headline": today.HEADLINE[action],
            "until": _when(until) if until else None, "why": why(account, day, action, until, now, done),
            "contacts": contacts, "write_to": best, "writer": writer, "route_in": route, "blocked": blocked,
            "falsifiers": list(dict.fromkeys(MOMENTS[s.kind][3] for s in now)),
            "moment": next((s for s in signals if s["about"]), None), "signals": signals,
            # the moments this note answers: the one it is about, and any other open one that would go to the same person
            "covers": [s.source_url for s in now if best and (s is moment or _best(contacts, s) is best)],
            "draft": None if blocked or not moment else draft(account, moment, best, writer, day)}


def workspace(mode):
    """((store, as_of), accounts, event records) for a mode, or None when it has no accounts file."""
    accounts = load(RECORDS[mode])
    if accounts is None:
        return None
    found = today.source(mode)
    return (found[0], found[1]) if found else (None, iso()), accounts, events.load(events.RECORDS[mode])


def _cleared(store, row, now):
    """The ledger, read as of now, lets this row's note go out: its person is clear, or the note goes in a
    conversation the writer already has open with them; and nobody at the account was on the list in the last
    month, unless the note is about the person it goes to."""
    to = row["write_to"]
    gate = contact.check(_history(store, to["key"], now), now)
    about = row["moment"]["kind"] in PERSONAL and _same(row["moment"]["who"], to["name"])
    quiet = about or not any(_posted(store, c["key"], now) for c in row["contacts"])
    return quiet and (gate.state == "clear" or bool(to["talking"]) and _in_conversation(gate))


def this_week(store, rows, now):
    """The accounts to write to this week, strongest first: each with a draft, none of its moments already
    posted, cleared by the ledger as of now, within what is left of the week's cap. The weekly list and send()
    both use it, so rows read before a send can't go out twice."""
    if not store:
        return []
    posted = {u for e in store.all("contact") if e["kind"] == "pinged" and e["role_id"] == GTM for u in e["evidence"]}
    return [r for r in rows if r["draft"] and not set(r["covers"]) & posted
            and _cleared(store, r, now)][:contact.room(store, GTM, now, cap=CAP)]


def weekly(top, total, as_of, invented=False):
    """The Slack list: for each of ``top`` (this_week's accounts), why now, who writes to whom, the best way in,
    what would change it, and the draft. ``total`` is how many accounts were read."""
    esc = routing._esc
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"{'Test · ' if invented else ''}Accounts to write to this week"}},
              {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{len(top)} of {total} accounts to write to "
                                                f"this week. As of {as_of}." + (" Invented accounts." if invented else "")}]}]
    for i, r in enumerate(top, 1):
        s, to = r["moment"], r["write_to"]
        name = f"<{routing._link(to['source_url'])}|{esc(to['name'])}>" if to["source_url"] else esc(to["name"])
        blocks += [{"type": "divider"}, routing._section(
            # a paper's card names no segment: the paper, not GI's label for the account, says where its author is
            " · ".join(filter(None, [f"*{i}. {esc(short(r['name']))}*", s["kind"] != "team_paper" and esc(r["segment"]),
                                     r["label"] and f"_{esc(r['label'])}_",
                                     f"write by {r['until']}"])) + "\n"
            f"*Why now:* {esc(s['what'][0].upper() + s['what'][1:])}, {s['when']}: “{esc(_clip(s['quote']))}” "
            f"<{routing._link(s['source_url'])}|source>\n"
            f"*To:* {name}{', ' + esc(to['title']) if to['title'] else ''} · *From:* {esc(r['writer'])}\n"
            f"*Way in:* {esc(r['route_in'] or 'none found, so a cold note')}\n"
            f"*Would change this:* {esc(' '.join(r['falsifiers']))}"),
            routing._section(f"```{esc(r['draft'])}```")]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "Nothing is sent until a person presses send."}]})
    return {"text": f"Accounts to write to this week: {', '.join(short(r['name']) for r in top) or 'none'}", "blocks": blocks}


def page(mode):
    found = workspace(mode)
    if not found:
        return {"missing": f"No accounts yet. GI picks them: add them to {RECORDS[mode]}/accounts.json.", "ledger": None,
                "accounts": []}
    (store, as_of), accounts, records = found
    rows = [assess(store, as_of, a, records) for a in accounts]
    order = ["reach_now", "watch_until", "quiet"]
    rows.sort(key=lambda r: (order.index(r["action"]), r["buys"] == "watch", -r["fit"], r["name"]))
    top = this_week(store, rows, as_of)
    return {"missing": None, "ledger": contact.unreadable(store) if store else None,  # said once, not per contact
            "accounts": rows, "this_week": [r["id"] for r in top],
            "weekly": weekly(top, len(rows), as_of[:10], mode == "simulation")}


def send(store, rows, webhook_url, now=None, client=None, invented=False):
    """The one way the list leaves: this week's accounts as of now, posted as one message, each recorded in the
    ledger as team sales with the moments its note answers, so recruiting and events see it and they are not
    listed again. Returns the account ids sent."""
    now = now or iso()
    top = this_week(store, rows, now)
    if not top:
        raise ValueError((contact.unreadable(store) if store else None)
                         or "No account to send: none is clear to write to, or this week's cap is used up")
    routing._post(weekly(top, len(rows), now[:10], invented), webhook_url, client)
    for r in top:
        contact.record(store, r["write_to"]["key"], "pinged", at=now, role_id=GTM, team="sales", by=r["writer"],
                       evidence=r["covers"], note=r["name"])
    return [r["id"] for r in top]


def in_talks(store, account, by, at=None, until=None, also=()):
    """Mark an account as in talks with GI: every contact on it and every author its papers list there, plus
    ``also`` (people GI watches who work there), is held for every team until a founder decides, or until the day
    given (today ends it). With ``until``, everyone an earlier mark held for this account is given that end too.

    Each account's hold is its own, by account id (contact.partner_holds), so ending one never ends another account's
    hold on the same person, and a renamed account still ends its own; a later partner_staff entry from another path
    (they left) ends them all. Returns the keys marked."""
    note = contact.talks_note(account.name, account.id)
    at = at or iso()
    earlier = []
    if until:
        day, people = parse_time(at).date().isoformat(), {}
        for e in sorted((e for e in store.all("contact") if e["kind"] == "partner_staff"
                         and parse_time(e["at"]) <= parse_time(at)), key=lambda e: parse_time(e["at"])):
            people.setdefault(e["person_id"], []).append(e)
        earlier = sorted(k for k, es in people.items() if any(contact.talks_account(e) == account.id for e in contact.partner_holds(es, day)))
    held = list(dict.fromkeys([key(account, c) for c in _on_file(account)] + [s for s in also if s] + earlier))
    for k in held:
        contact.record(store, k, "partner_staff", at=at, team="sales", by=by, until=until, note=note)
    return held


def release_day(store, as_of, accounts, records, cited=()):
    """When GI publishes: at each account it could sell to or partner with, the person who should hear from GI
    first, who writes, the way in, and whether GI's paper cites them. ``cited``: author names from its bibliography."""
    cited = {c.lower() for c in cited}
    out = []
    for a in sorted((a for a in accounts if a.buys != "watch"), key=lambda a: -a.fit):
        contacts = people(store, a, as_of, records) if store else []
        last = _last_posted(contacts)
        blocked = f"{a.name} was on the list on {_when(last)}: one note a month per account." if last else None
        best = None if blocked else _best(contacts)
        route, writer = way_in(a, best) if best else (None, a.owner)
        out.append({"account": a.name, "fit": a.fit, "writer": writer, "to": best["name"] if best else None,
                    "title": best["title"] if best else "", "route_in": route,
                    "cited": sorted(c.name for c in a.contacts if c.name.lower() in cited),
                    "blocked": blocked or (None if best else _nobody(contacts))})
    return out


def _words(name):
    """A company name's words, without "(United States)"-style suffixes, slashes or punctuation."""
    return re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"\(.*?\)|/.*", "", name.lower())).split()


SUFFIXES = {"inc", "ai", "labs", "lab", "technologies", "technology", "robotics", "games", "studios", "entertainment",
            "corporation", "corp", "co", "ltd", "limited", "gmbh", "llc", "sa", "research", "group"}


def _account_for(name, accounts):
    """The account whose whole name starts ``name``: "Acme Robotics (United States)" is Acme Robotics, but
    "Google (United States)" is not Google DeepMind. A one-word account name must be followed only by words like
    "Inc" or "AI": "Figure AI" is Figure, "Figure Eight" is not."""
    words = _words(name)
    return next((a for a in accounts if (n := _words(a.name)) and words[:len(n)] == n
                 and (len(n) > 1 or set(words[1:]) <= SUFFIXES)), None)


def on_topic(title):
    """Whether a paper's title is on GI's work: ``ON_TOPIC``; a world model with a ``STRONG`` word; else a world
    model or a strong word with no word of text, stories or another field (``OFF_TOPIC``)."""
    title = title or ""
    world, strong = WORLD_MODEL.search(title), STRONG.search(title)
    return bool(ON_TOPIC.search(title) or world and strong or (world or strong) and not OFF_TOPIC.search(title))


def company_papers(pairs):
    """(phrase, work, places) for each (phrase, work) of openalex.companies_works whose title is on GI's work
    (``on_topic``): places are its company institutions other than GI, each with the authorships of its authors there
    who are people (``a_person``) and whose own lines name it (``_names``), as [(institution, [authorship])]; a place
    with no such author is left out. The company watcher reads papers through it; ``find`` keeps its own loop, which
    merges a paper's institutions by account and records each author's lines, with the same rule for who counts."""
    for phrase, work in pairs:
        if not on_topic(work.get("title")):
            continue
        places = {}
        for au in work.get("authorships", []):
            for i in au.get("institutions") or []:
                if i.get("type") == "company" and i.get("id") and not GI.search(i.get("display_name", "")):
                    places.setdefault(i["id"], (i, []))
        for au in work.get("authorships", []):
            person = au.get("author") or {}
            if not (person.get("id") and a_person(person.get("display_name"))):
                continue
            for i in au.get("institutions") or []:
                if i.get("id") in places and _names(au, i) and au not in places[i["id"]][1]:
                    places[i["id"]][1].append(au)
        yield phrase, work, [p for p in places.values() if p[1]]  # never a place with none of its people on it


def find(fetch, since, accounts=(), topics=TOPICS, pages=5, budget=None):
    """Candidate accounts, strongest first: companies with authors on GI's topics since a day (OpenAlex), each with
    its papers and those authors (most papers first), merged into GI's own account when the name matches; and for
    each of ``accounts``, postings for that work on its job boards and headlines about funding or a new leader. GI
    picks its accounts from these.

    Big labs come last: they are more often competitors or partners than buyers. GI itself is left out, and so are
    papers whose title is not on GI's work (``on_topic``) and companies that are no account with fewer than two
    papers.

    The OpenAlex search pages every topic's newest works first; given a ``budget`` (an ``openalex.Budget``, which then
    says why), it stops and keeps what it has when that runs out or OpenAlex refuses or never answers a page. Any
    other failed search, or one with no budget given, stops the run. A failed board or feed is noted on its
    account."""
    found = {}

    def entry(k, name, account=None):
        return found.setdefault(k, {"name": name, "account": account.id if account else None, "openalex": [],
                                    "big": bool(BIG.search(name)), "topics": [], "papers": {}, "authors": {},
                                    "jobs": [], "news": [], "deal_off": None, "errors": []})

    for a in accounts:
        entry(a.id, a.name, a)
    for phrase, work in openalex.companies_works(fetch, topics, since, pages, budget):
        if not on_topic(work.get("title")):
            continue
        companies = {i["id"]: i for au in work.get("authorships", []) for i in au.get("institutions") or []
                     if i.get("type") == "company" and i.get("id") and not GI.search(i.get("display_name", ""))}
        at = {}
        for inst in companies.values():
            a = _account_for(inst["display_name"], accounts)
            c = at[inst["id"]] = entry(a.id if a else inst["id"], a.name if a else inst["display_name"], a)
            if (oid := openalex.short_id(inst["id"])) not in c["openalex"]:
                c["openalex"].append(oid)
            c["papers"].setdefault(work["id"], {"title": work.get("title") or "", "day": work.get("publication_date") or "",
                                                "url": work.get("doi") or work["id"], "venue": venue(work),
                                                "authors": []})
            if phrase not in c["topics"]:
                c["topics"].append(phrase)
        for au in work.get("authorships", []):
            person = au.get("author") or {}
            if not (person.get("id") and a_person(person.get("display_name"))):
                continue
            for inst in {id(at[i["id"]]): i for i in au.get("institutions") or []
                         if i.get("id") in at and _names(au, i)}.values():
                c = at[inst["id"]]
                c["authors"].setdefault(person["id"], {"name": person["display_name"], "url": person["id"],
                                                       "papers": set()})["papers"].add(work["id"])
                listed = c["papers"][work["id"]]["authors"]
                if all(a["url"] != person["id"] for a in listed):
                    listed.append(_author(au, inst))
    for a in accounts:
        c = found[a.id]
        for board, slug in a.boards.items():
            try:
                posts = jobs.postings(fetch, board, slug)
            except Exception as e:  # noqa: BLE001 - shapes unchecked from here: note it on the account, keep going
                c["errors"].append(f"{board}/{slug}: {type(e).__name__}")
                continue
            c["jobs"] += [p | {"text": ""} for p in posts if gi_work(p["title"], p["text"])]
        company = " ".join(_words(a.name))
        if a.buys == "watch" or BIG.search(company):
            continue  # a big company's headlines are mostly about something else; watch accounts get no list anyway
        try:
            c["news"], c["deal_off"] = news.read(fetch, company)
        except Exception as e:  # noqa: BLE001 - a feed that is not the RSS remembered: note it, keep going
            c["errors"].append(f"news: {type(e).__name__}")
    out = []
    for c in found.values():
        if not c["account"] and len(c["papers"]) < 2:
            continue  # one paper from a company GI does not follow is noise more often than a buyer
        c["topics"] = [t for t in topics if t in c["topics"]]  # in the topics' order, however the pages came
        c["papers"] = sorted(c["papers"].values(), key=lambda p: p["day"], reverse=True)
        c["authors"] = sorted(({**p, "papers": len(p["papers"])} for p in c["authors"].values()),
                              key=lambda p: (-p["papers"], p["name"]))
        c["score"] = len(c["papers"]) + 2 * len(c["topics"]) + len(c["jobs"]) + len(c["news"])
        out.append(c)
    return sorted(out, key=lambda c: (c["big"], -c["score"], c["name"]))


# Reading an affiliation line: a piece names an institution when it has one of these words; a department or school
# inside one is left out; a country, a state's code and a postcode say nothing the card needs.
INSTITUTION = re.compile(r"universit|universidad|hochschule|institut|college|academy|polytechn|laborator|\blabs?\b|"
                         r"\bresearch\b|\bcent(?:er|re)\b|foundation|\binc\b|\bcorp|gmbh|\bltd\b|\bllc\b|company", re.I)
SUBUNIT = re.compile(r"^(?:the )?(?:dep(?:artmen)?t\b\.?|department|faculty|division|chair|school of|college of|"
                     r"cent(?:er|re) for|group)", re.I)
COUNTRIES = frozenset({"usa", "u.s.a", "us", "united states", "united states of america", "uk", "united kingdom",
                       "england", "germany", "deutschland", "china", "p.r. china", "pr china", "france", "canada", "japan",
                       "korea", "south korea", "republic of korea", "switzerland", "netherlands", "the netherlands",
                       "india", "israel", "singapore", "spain", "italy", "sweden", "austria", "belgium", "denmark",
                       "finland", "norway", "poland", "australia", "taiwan", "hong kong", "brazil", "portugal",
                       "ireland", "czech republic", "uae", "united arab emirates"})
# Words that stay with a company's name when the line gives them after it ("Google Research", "NVIDIA Research").
AFTER_NAME = frozenset({"research", "ai", "labs", "lab", "brain", "deepmind", "robotics", "web", "services", "agi",
                        "inc", "inc.", "llc", "ltd", "ltd.", "gmbh", "corporation", "corp", "corp.", "america", "asia",
                        "europe"})
SUBJECT = re.compile(r"department|\b(?:engineering|sciences?|informatics|mathematics|physics|computing)\b", re.I)


def _read_line(raw, names):
    """(institutions, city) that one affiliation line of a paper names, in its order and its words: an OpenAlex
    match (``names``) only when the line contains its name, the other pieces with an institution's word as the line
    gives them; the city is the first place after an institution. None when a piece reads more than one way (two
    words run together with no institution's word, which could be a company or a city), or it names no institution:
    the card then quotes the line."""
    text = " ".join((raw or "").split())
    spans = []
    for n in names:
        for name in dict.fromkeys([short(n), n]):
            if name and (m := re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.I)):
                end = m.end()
                while (more := re.match(r"\s+([\w.]+)", text[end:])) and more.group(1).casefold() in AFTER_NAME:
                    end += more.end()
                spans.append((m.start(), end))
                break
    pieces, at = [], 0
    for start, end in sorted(spans):
        if start < at:
            continue  # inside a name already found
        pieces += [("gap", text[at:start]), ("name", text[start:end])]
        at = end
    pieces.append(("gap", text[at:]))
    commas = bool(re.search(r"[,;]", text))
    found, cities = [], []
    for kind, piece in pieces:
        if kind == "name":
            found.append(piece.strip())
            continue
        for chunk in re.split(r"[,;]", re.sub(r"\([^)]*\)", "", piece)):  # "(FAIR)": another name for what is there
            chunk = re.sub(r"\b\d[\d-]*\b|^\s*(?:and|&)\s|\s(?:and|&)\s*$", "", chunk).strip(" .-–()")
            if (not chunk or SUBUNIT.match(chunk) or chunk.casefold().rstrip(".") in COUNTRIES
                    or re.fullmatch(r"[A-Z]{2,3}|[a-z]{1,3}", chunk)):  # a department, a country, "CA", the "ai" of "x.ai"
                continue
            if INSTITUTION.search(chunk):  # before a subject: "Academy of Sciences" is a place
                found.append(chunk)
            elif SUBJECT.search(chunk):
                continue
            elif len(chunk.split()) <= (3 if commas else 1):
                if found:
                    cities.append(chunk)
            else:
                return None
    return (found, cities[0] if cities else "") if found else None


def _author(authorship, inst):
    """An author as a paper lists them at a company: their OpenAlex page, the institution OpenAlex matched, the
    paper's own lines for where they are (``affiliation``), the places and city those lines name in their own words
    (``_read_line``; none when a line reads more than one way, and the card quotes the lines instead), and whether
    they are its first author. OpenAlex's matches are only kept where the line names them: it matches a line that
    runs two names together to places the line never names."""
    person = authorship["author"]
    matched = {i["id"]: i.get("display_name") or "" for i in authorship.get("institutions") or [] if i.get("id")}
    lines = [a for a in authorship.get("affiliations") or [] if (a.get("raw_affiliation_string") or "").strip()]
    places, city, plain = [], "", bool(lines)
    for line in lines:
        got = _read_line(line["raw_affiliation_string"],
                         [matched[i] for i in line.get("institution_ids") or [] if i in matched] or list(matched.values()))
        if got is None:
            plain = False
            break
        places, city = places + got[0], city or got[1]
    said = "; ".join(dict.fromkeys(" ".join(a["raw_affiliation_string"].split()) for a in lines))
    return {"name": person["display_name"], "url": person["id"], "at": inst.get("display_name") or "", "affiliation": said,
            "listed_at": list(dict.fromkeys(places)) if plain else [], "city": city if plain else "", "own_words": True,
            "first": authorship.get("author_position") == "first"}


def venue(work):
    """Where an OpenAlex work appeared, without OpenAlex's parenthesis ("arXiv", not "arXiv (Cornell University)");
    "" when it doesn't say."""
    return short(((work.get("primary_location") or {}).get("source") or {}).get("display_name") or "")


def _title_key(title):
    return " ".join(re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", title or "").casefold()).split())


def first_version(work, found, authors):
    """The earliest of ``found`` (``openalex.versions``) that is an earlier preprint of ``work``: on a preprint server
    (``PREPRINTS``), dated to the day (OpenAlex dates a record it knows only the year of January 1, which never
    counts), the same title however it is cased or punctuated, not ``work`` itself, and with one of ``authors`` (the
    paper's authors at the account) on it: by OpenAlex page when both give one, else by name. (day, where), or None."""
    def by(au):
        person = au.get("author") or {}
        return any(a.get("url") == person["id"] if a.get("url") and person.get("id") else
                   _same(a.get("name"), person.get("display_name")) for a in authors)
    same = [w for w in found if w.get("id") != work.get("id") and PREPRINTS.search(venue(w))
            and (w.get("publication_date") or "")[4:10] not in ("", "-01-01")
            and _title_key(w.get("title")) == _title_key(work.get("title"))
            and any(by(au) for au in w.get("authorships") or [])]
    w = min(same, key=lambda w: w["publication_date"], default=None)
    return (date.fromisoformat(w["publication_date"][:10]), venue(w)) if w else None


def _names(authorship, inst):
    """Whether an author's own lines on a paper name the company OpenAlex put them at (its name, the part before
    OpenAlex's parenthesis, or that without a word like "Technologies", ``SUFFIXES``): OpenAlex matches a line that
    names only a university to a company too. With no line, as OpenAlex has it."""
    lines = [" ".join(a.get("raw_affiliation_string", "").split()) for a in authorship.get("affiliations") or []]
    name = short(inst.get("display_name") or "")
    suffix = "|".join(map(re.escape, SUFFIXES))
    bare = re.sub(rf"(?:\s+(?:{suffix})\.?)+$", "", name, flags=re.I)  # "Acme" of "Acme Technologies"
    whole = [rf"(?<!\w){re.escape(n)}(?!\w)" for n in dict.fromkeys((name, inst.get("display_name") or "")) if n]
    # the short name alone, but not inside another name: "Sky" of "Sky Labs" is not the "Sky" of "Sky Models Group"
    after = "|".join(map(re.escape, SUFFIXES | AFTER_NAME))
    alone = [rf"(?<![\w-]){re.escape(bare)}(?![\w-])(?!\s+(?!(?:{after})\b)[^\W\d_])"] if bare and bare != name else []
    return not any(lines) or any(re.search(p, line, re.I) for line in lines for p in whole + alone)


def authors_at(work, account, accounts):
    """The authors an OpenAlex ``work`` lists at ``account``: at a company institution ``_account_for`` gives to it
    that their own lines name (``_names``), as ``find`` keeps them. For a paper stored before find kept its authors
    (``scripts/accounts.py authors``)."""
    out = []
    for au in work.get("authorships", []):
        person = au.get("author") or {}
        inst = next((i for i in au.get("institutions") or [] if i.get("type") == "company"
                     and _account_for(i.get("display_name") or "", accounts) is account and _names(au, i)), None)
        if inst and person.get("id") and a_person(person.get("display_name")) and all(a["url"] != person["id"] for a in out):
            out.append(_author(au, inst))
    return out


def champion_from(candidate, account, since, least=2):
    """The company author with the most papers on GI's topics since a day, as the account's technical champion,
    when they have at least ``least`` and the account has no champion yet; else None. Their OpenAlex page is the
    source."""
    top = next((a for a in candidate.get("authors") or [] if a_person(a["name"])), None)
    if not top or top["papers"] < least or any(c.part == "champion" or _same(c.name, top["name"])
                                               for c in account.contacts):
        return None
    return Contact(name=top["name"], title=f"Author of {top['papers']} papers on GI's topics since {since}",
                   part="champion", source_url=top["url"])


def signals_from(candidate, since):
    """Dated evidence from a find() candidate since a day, as signals, newest first: its papers, matching job
    posts and headlines. Undated posts are left out: they can never open a window."""
    papers = [Signal(kind="team_paper", day=p["day"], quote=p["title"], source_url=p["url"], authors=p.get("authors", []),
                     venue=p.get("venue", ""))
              for p in candidate["papers"] if p["day"] >= since and p["url"].startswith("https://") and p["title"]]
    posts = [Signal(kind="job_post", day=p["posted"], quote=p["title"], source_url=p["url"])
             for p in candidate["jobs"] if p.get("posted") and p["posted"] >= since and p.get("title")
             and str(p.get("url") or "").startswith("https://")]
    headlines = [Signal(**h) for h in candidate["news"] if h["day"] >= since]
    return sorted(papers + posts + headlines, key=lambda s: s.day, reverse=True)


AUTO_CHAMPION = re.compile(r"^Author of \d+ papers on GI's topics since ")  # champion_from's own title
FOUND = {"headline": ("https://news.google.com/",), "paper": ("https://doi.org/", "https://openalex.org/")}


def recheck(row, store=None, deal_off=None):
    """Drops from an accounts.json row what ``find`` wrote there and today's rules no longer count, and returns how
    many of each it dropped.

    Dropped: headlines from the news feed that are not about the account (``news.kind``; one that now reads as another
    kind is relabelled), papers off GI's work (``on_topic``), job posts whose title is not GI's kind of work
    (``gi_work``'s title half), feed headlines of a deal (``news.deal``) when ``deal_off``, a headline in today's feed,
    says it fell through, and champions it added from papers (``champion_from``), which the next ``find`` adds back
    under the same rules.

    Kept: a headline naming a person, a public ask, and a paper or headline from anywhere else (a job post keeps only
    its title, so every one is checked by that); a champion a tie names or the ledger has anything on (a posting, a
    note, an answer), and every paper champion when there is no ledger to ask. A champion whose name is no person's (a
    model, a team) always goes. A stored subject id loses its role.

    A paper typed in by hand with its DOI looks like one find wrote: ``gone`` lists each drop, so the command line can
    say them."""
    company = " ".join(_words(row["name"]))
    dropped = {"headlines": 0, "papers": 0, "posts": 0, "champions": 0, "gone": []}
    keep = []
    for s in row.get("signals", []):
        url = s.get("source_url", "")
        if s["kind"] in ("funding", "new_lead") and not s.get("who") and url.startswith(FOUND["headline"]):
            if not (now := news.kind(s["quote"], company)):
                dropped["headlines"] += 1
                dropped["gone"].append(f"headline “{_clip(s['quote'])}”")
                continue
            if deal_off and news.deal(s["quote"], company):
                dropped["headlines"] += 1
                dropped["gone"].append(f"headline “{_clip(s['quote'])}”, a deal that fell through (“{_clip(deal_off)}”)")
                continue
            s["kind"] = now
        elif s["kind"] == "team_paper" and url.startswith(FOUND["paper"]) and not on_topic(s["quote"]):
            dropped["papers"] += 1
            dropped["gone"].append(f"paper “{_clip(s['quote'])}”")
            continue
        elif s["kind"] == "job_post" and not (JOB_TITLE.search(s["quote"]) and not NOT_JOB.search(s["quote"])):
            dropped["posts"] += 1  # only the title is kept, so only the title's half of gi_work can be asked
            dropped["gone"].append(f"job post “{_clip(s['quote'])}”")
            continue
        keep.append(s)
    account = Account.model_validate(row)
    tied = [t.to for t in account.ties if t.to]

    def found_by_find(c):
        if c.part == "champion" and AUTO_CHAMPION.match(c.title) and not a_person(c.name):
            return True  # a model or a team find took for an author: never a contact, whatever the ledger says
        return bool(store and c.part == "champion" and AUTO_CHAMPION.match(c.title)
                    and not any(_same(c.name, t) for t in tied) and not contact.history(store, key(account, c)))
    people = [raw for c, raw in zip(account.contacts, row.get("contacts", [])) if not found_by_find(c)]
    for raw in people:  # an id an earlier find stored with its role: the role stays out of GTM's file
        if raw.get("subject_id"):
            raw["subject_id"] = contact.person_key(raw["subject_id"])
    dropped["champions"] = len(account.contacts) - len(people)
    dropped["gone"] += [f"champion {c.name}" for c in account.contacts if found_by_find(c)]
    if "signals" in row:
        row["signals"] = keep
    if "contacts" in row:
        row["contacts"] = people
    return dropped
