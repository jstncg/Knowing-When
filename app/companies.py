"""The company watcher: what just happened at the companies GI hires from, so GI can approach several people at one
company at once. A preview: it lays out each company's moments and the people GI would want there, and it posts
nothing, opens no window and changes no one's call.

Watched: GI's collaborators, GTM's accounts (accounts.json), any company in companies.json beside the reads, and each
watched person's employer (their LinkedIn profile read). ``read`` fetches the last month's moments from free sources
(on the Mac: the cloud blocks them): the company was bought, is laying people off, or a deal to buy it fell through
(news.employer), and papers on GI's work with its people among the authors (OpenAlex, accounts.on_topic). A company
event is a moment that just happened, never a guess that anyone will leave. A big company's headline counts only
when it names its AI or research team: "Amazon to cut 14,000 corporate jobs" is not its robotics team's news, nor is
a roundup of cuts "amid the AI boom". The preview shows each story once, however many headlines carry it.

A company GI works with is held: its collaborators on MIRA (Kyutai and Epic Games, 6 July 2026). One GI sells to or partners with, a GTM account, asks sales
first: sales pitches its people. An account GTM only watches builds what GI builds: a rival, open to a team approach
like any other employer.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import accounts
from .sources import news, openalex

ROOT = Path(__file__).resolve().parents[1]
RECORDS = {"simulation": ROOT / "tests/fixtures/companies", "live": ROOT / "research/private/companies"}
DAYS = 30  # a company moment just happened for a month, as a person's public moments on Today's calls
PAGES = 2  # OpenAlex pages per GI topic: at most 14 pages, about 140 of the 1,000 credits a day it gives free
COLLABORATORS = {"Kyutai": "MIRA (6 July 2026)", "Epic Games": "MIRA (6 July 2026)"}
BUYS = {"agents": "GI's agents", "data": "GI's gameplay data", "both": "GI's agents and data"}
# A big company's AI or research team, as a headline names it: "cuts 600 jobs in its AI unit", "lays off robotics
# researchers", "AI infrastructure unit", "DeepMind". "Amid the AI boom" or "as AI replaces workers" is the industry,
# not a team.
AI_WORK = re.compile(r"\b(?:(?:AI|GenAI|artificial intelligence|research|robotics|autonomy|self-driving|"
                     r"world models?)(?:\s+(?!(?:replaces?|replacing|eats|takes|kills|will|to|is|as)\b)[\w-]+)?\s+"
                     r"(?:units?|teams?|divisions?|groups?|labs?|org|organi[sz]ations?|arms?|staff|staffers|"
                     r"researchers|engineers|scientists|workers|employees|jobs|roles)|superintelligence|DeepMind|"
                     r"Reality Labs|(?-i:FAIR))\b", re.I)
# Whose sale a deal that fell through was, by the words around a name: right before the deal's own word ("Acme deal",
# "Acme's acquisition by Birch") unless a buyer's words follow ("Birch's bid for Acme", "Birch deal to buy Acme"), or
# a few words after a buyer's ("acquisition of AI startup Acme", "walks away from Acme", "deal for Acme"). "Birch and
# Acme end their merger" names neither.
TARGET = (r"{name}(?:['’]s)?\s+(?:sale|deal|acquisition|takeover|purchase|buyout|merger|bid)s?\b"
          r"(?!(?:\s+(?:bid|offer|deal)s?)?\s+(?:of|for|to\s+(?:buy|acquire)|with)\b)|"
          r"\b(?:acquire|acquiring|buy|buying|(?:acquisition|purchase|takeover|pursuit|buyout)\s+of|"
          r"(?:bid|deal|offer)\s+for|(?:talks|merger|agreement)\s+with|away\s+from|out\s+of)"
          r"\s+(?:\S+\s+){{0,4}}?{name}")
STORY_DAYS = 14  # one company's headlines of one kind, each within two weeks of the one before, are one story
NEWS = {"acquisition": news.bought, "layoffs_reported": news.layoffs, "acquisition_called_off": news.sale_called_off}
LABEL = {"acquisition": "Bought", "layoffs_reported": "Laying people off",
         "acquisition_called_off": "A deal to buy it fell through", "team_paper": "Paper on GI's work"}
TEAM_NEWS = ("acquisition", "layoffs_reported", "acquisition_called_off")  # news about the whole company, first
STANCE = {"collaborator": "Held", "partner": "Ask sales first", "account": "Ask sales first", "rival": "Open", "": "Open"}
STANCES = ["Open", "Ask sales first", "Held"]  # the preview's order


@dataclass(frozen=True)
class Company:
    name: str
    relation: str = ""  # collaborator, partner, account (buys from GI), rival (GTM watches it), or "" for any other
    why: str = ""       # what the relation means for approaching its team

    @property
    def stance(self):
        return STANCE[self.relation]

    @property
    def search(self):
        """The name as headlines write it: "Nimbus Labs (Tern)" is Nimbus Labs, "Birch Robotics / Birch AI" is
        Birch Robotics."""
        return " ".join(accounts._words(self.name))


def _key(name):
    return tuple(accounts._words(name))


def relation(name, found):
    """A Company for ``name`` against GTM's accounts ``found``: a collaborator is held, a GTM partner or buyer asks
    sales first, a rival or anyone else is open."""
    if ours := accounts._account_for(name, [Company(c) for c in COLLABORATORS]):
        return Company(name, "collaborator", f"GI works with them: {COLLABORATORS[ours.name]}.")
    a = next((a for a in found if _key(a.name) == _key(name)), None) or accounts._account_for(name, found)
    if not a:
        return Company(name)
    if a.buys == "watch":
        return Company(name, "rival", "A rival: GTM watches it, since it builds what GI builds.")
    if a.buys == "partner":
        return Company(name, "partner", "A GTM partner: approach its team only with sales.")
    return Company(name, "account", f"A GTM account that buys {BUYS[a.buys]}: sales pitches its people.")


def _json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def employers(store, allowed=None):
    """{subject id: employer} for the people in ``store`` with one (``allowed``: only these subject ids)."""
    return {row["subject_id"]: row["employer"] for row in store.all("person_context")
            if (row.get("employer") or "").strip() and (allowed is None or row["subject_id"] in allowed)}


def watched(mode, store, allowed=None):
    """Every watched company once, each with its relation (``relation``): collaborators, GTM's accounts, the
    companies file's, then the employers of the people in ``store`` (``allowed``: only these subject ids). A name
    that starts with one already listed is that company ("Driftwire Inc" is Driftwire)."""
    found = accounts.load(accounts.RECORDS[mode]) or []
    extra = [c["name"] for c in _json(RECORDS[mode] / "companies.json", {}).get("companies", [])]
    out = []
    for name in (*COLLABORATORS, *(a.name for a in found), *extra, *employers(store, allowed).values()):
        if _key(name) and not accounts._account_for(name, out):
            out.append(relation(name, found))
    return out


def read(fetch, companies, day, budget=None, pages=PAGES):
    """The moments of the ``DAYS`` before ``day`` at each of ``companies``, fetched: {read_on, since, moments, errors,
    openalex}. Each moment is {company, kind, day, quote, source_url}, a paper's (accounts.company_papers) with its
    authors there. A deal headline published before one saying a deal to buy the company fell through (``_target``)
    is left out and the call-off kept, as journey._news supersedes a stored deal; a read keeps no earlier reads, so
    only the feed's own count. A feed that fails is noted and the read goes on."""
    since = (date.fromisoformat(day) - timedelta(days=DAYS)).isoformat()
    moments, errors = [], []
    for c in companies:
        try:
            rows = news.employer(fetch, c.search)
        except Exception as e:  # noqa: BLE001 - one company's feed must not end the read
            errors.append(f"{c.name}: news {type(e).__name__}")
            continue
        off = [r["at"] for r in rows
               if r["event_type"] == "acquisition_called_off" and _target(r["quote"], c.search)]
        moments += [m for r in rows if since <= r["day"] <= day
                    and not (r["event_type"] == "acquisition" and any(r["at"] <= at for at in off))
                    and _counts(m := {"company": c.name, "kind": r["event_type"], "day": r["day"],
                                      "quote": r["quote"], "source_url": r["source_url"]})]
    budget, papers = budget or openalex.Budget(), {}
    try:  # the budget already ends the search, keeping what came, on a rate limit, a server error or a timeout
        works = openalex.companies_works(fetch, accounts.TOPICS, since, pages, budget)
    except Exception as e:  # noqa: BLE001 - the news is still worth keeping
        works = []
        errors.append(f"OpenAlex: {type(e).__name__}")
    for _, work, places in accounts.company_papers(works):
        for inst, authorships in places:
            if c := accounts._account_for(inst["display_name"], companies):
                m = papers.setdefault((c.name, work["id"]), {
                    "company": c.name, "kind": "team_paper", "day": work.get("publication_date") or "",
                    "quote": work.get("title") or "", "source_url": work.get("doi") or work["id"], "authors": []})
                m["authors"] += [{"name": p["display_name"], "url": p["id"]} for p in (au["author"] for au in authorships)
                                 if p["id"] not in (a["url"] for a in m["authors"])]
    return {"read_on": day, "since": since, "moments": moments + list(papers.values()), "errors": errors,
            "openalex": budget.said()}


def _counts(m):
    """Whether a news moment still counts by today's rules: its kind's own reading in sources.news, a deal that fell
    through only when the company is what it would buy (``_target``), and a big company's only when it names its AI
    or research team (``AI_WORK``). So a read saved under an older rule drops what that rule misread. A paper always
    counts."""
    if m["kind"] not in NEWS:
        return True
    name = Company(m["company"]).search
    return bool(NEWS[m["kind"]](m["quote"], name)
                and (m["kind"] != "acquisition_called_off" or _target(m["quote"], name))
                and (not accounts.BIG.search(m["company"]) or AI_WORK.search(m["quote"])))


def _target(headline, company):
    """Whether ``headline`` names ``company`` as what a deal would buy (``TARGET``), never the buyer that walks away:
    "Birch said to walk away from $6B Acme purchase" is Acme's sale called off, not Birch's. Only the watcher reads
    this; a person's employer news keeps news.sale_called_off as it is, so no one's call moves."""
    return bool(re.search(TARGET.format(name=news._name(news._plain(company))), headline, re.I))


def save(result, folder=None):
    """Writes a read to ``moments-<day>.json`` in ``folder`` (the live one by default) and returns the path."""
    folder = folder or RECORDS["live"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"moments-{result['read_on']}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    return path


def latest(folder):
    reads = sorted(folder.glob("moments-*.json")) if folder.is_dir() else []
    return json.loads(reads[-1].read_text()) if reads else None


def preview(mode, store, calls, allowed=None):
    """The newest read laid out per company, open ones first (``STANCES``), then by their newest news and paper: its
    relation, stance and why, its moments (each news story once, dated by its first headline, with how many carried
    it: ``STORY_DAYS``; and only what today's rules still count: ``_counts``), the people in ``store`` who work there
    (``allowed``: only these) with today's call on each (``calls``, Today's calls' pings), and the authors of its
    papers who are no one in ``store``: a replay case or anyone else the engine knows is never named as an author.
    Nobody is contacted from here: a card would still pass the contact gate. Nothing is fetched; a read that won't
    load says so."""
    empty = {"read_on": None, "companies": [], "errors": []}
    try:
        saved = latest(RECORDS[mode])
        if saved is None:
            return {**empty, "missing": "No company read yet. On the Mac: uv run python scripts/companies.py read"}
        rows = _rows(saved["moments"], accounts.load(accounts.RECORDS[mode]) or [], store, calls, allowed)
        return {"read_on": saved["read_on"], "since": saved["since"], "companies": rows, "errors": saved["errors"],
                "missing": None}
    except (OSError, ValueError, KeyError, TypeError) as err:  # a bad file, even hand-edited, must not break the page
        return {**empty, "missing": f"The company read won't load: {err!r}"}


def _rows(moments, found, store, calls, allowed):
    call = {c["subject_id"]: c for c in calls}
    staff = employers(store, allowed)
    known = {row["name"].casefold() for row in store.all("person_context")}
    rows, stories = {}, {}  # stories: (company, kind): [the story shown, the day of its newest headline]
    for m in sorted(moments, key=lambda m: m["day"]):  # a story's first headline first
        if not _counts(m):
            continue
        story = stories.get((m["company"], m["kind"])) if m["kind"] in NEWS else None
        if story and (date.fromisoformat(m["day"]) - date.fromisoformat(story[1])).days <= STORY_DAYS:
            story[0]["headlines"] += 1  # the same news, told again
            story[1] = m["day"]
            continue
        c = relation(m["company"], found)
        row = rows.setdefault(c.name, {"company": c.name, "relation": c.relation, "stance": c.stance, "why": c.why,
                                       "moments": [], "people": [], "authors": []})
        row["moments"].append(shown := {**m, "label": LABEL[m["kind"]], "headlines": 1})
        stories[m["company"], m["kind"]] = [shown, m["day"]]
    for row in rows.values():
        row["moments"].sort(key=lambda m: m["day"], reverse=True)
        row["moments"].sort(key=lambda m: m["kind"] not in TEAM_NEWS)
        here = Company(row["company"])
        row["people"] = [{"subject_id": sid, "name": call[sid]["name"], "role": call[sid]["role"]["title"],
                          "call": call[sid]["headline"]}
                         for sid, employer in staff.items() if sid in call and accounts._account_for(employer, [here])]
        authors = {}
        for m in row["moments"]:
            for a in m.get("authors", []):
                if a["name"].casefold() not in known:
                    authors.setdefault(a["url"], {**a, "papers": 0})["papers"] += 1
        row["authors"] = sorted(authors.values(), key=lambda a: (-a["papers"], a["name"]))
    order = sorted(rows.values(), key=lambda r: r["moments"][0]["day"], reverse=True)
    order.sort(key=lambda r: (STANCES.index(r["stance"]), r["moments"][0]["kind"] not in TEAM_NEWS))
    return order
