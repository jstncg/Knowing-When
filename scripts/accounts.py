"""GTM accounts: the weekly Slack list of the few accounts worth writing to, and the free finder behind it. GTM is
separate from hiring and ops: its list goes to its own Slack channel.

  uv run python scripts/accounts.py --demo
      Invented accounts, as of 2026-09-15: the weekly list, then what marking one account "in talks" does to
      recruiting, then the release-day list. Nothing is posted.
  uv run python scripts/accounts.py
      GI's accounts (research/private/accounts/accounts.json) against the timing run's ledger
      (research/private/social/timelines.sqlite): the weekly list, then every account's call.
  uv run python scripts/accounts.py find --since 2025-09-24 [--pages 5] [--update [--recheck]]
      Free, on the Mac (the cloud blocks OpenAlex, the job boards and Google News): companies with authors on
      GI's topics since the day; for the accounts, matching posts on their public job boards and headlines about
      their own funding round, acquisition or new leader (last 30 days). Writes
      research/private/accounts/candidates.json. --update adds the newest dated papers, posts and headlines of
      the last 90 days (at most 5 per account per run) to accounts.json, and, for an account with no champion
      yet, the company author with the most papers on GI's topics (two or more) as its champion. --recheck first
      drops from accounts.json the feed headlines, DOI or OpenAlex papers, job posts and paper champions today's
      rules no longer count (and a deal's headlines when today's feed says it fell through), and lists each (a paper typed in by hand with its DOI is checked too: read the list). Every
      run keeps the file as it was in accounts.before-recheck-<time>.json. A champion the ledger has anything on
      stays, and so does every champion when there is no timelines store to ask or the search stopped early.
      OpenAlex without a key gives 1,000 credits a day: a full run is at most 35 search pages, one a second, about
      350 credits. It stops by itself before 500, or before fewer than 100 would be left today, and keeps and
      writes what it fetched, but adds no champion from it; its OpenAlex line says what the run cost.
  uv run python scripts/accounts.py authors
      Free, on the Mac, after each find --update: for each DOI or OpenAlex paper in accounts.json still inside its
      window, the authors OpenAlex lists at that account on the paper itself, every place it lists them at and who
      is first author, and where the paper appeared (one request a paper not read yet); then, once per paper with
      an author there, OpenAlex's other works of the same title by an author in common (one search), so a
      proceedings chapter of a paper on arXiv since June says so and its window counts from June. A paper's note
      goes only to one of its authors there, and a paper not yet looked at this way waits. The file as it was is
      kept in accounts.before-authors-<time>.json first.
  uv run python scripts/accounts.py gist [LINK "what the paper does"]
      A paper's note says what caught the eye in a line a person writes from its abstract, never the title again: until
      then it waits. With no arguments, lists the papers waiting, with their abstracts; with a paper's link and a
      line, sets it (3 to 25 words, no four in a row from the title), keeping the file before in
      accounts.before-gist-<time>.json.
  uv run python scripts/accounts.py about LINK "what the paper is about"
      Where a title has no short name to open the note with ("saw your Halcyon paper"), a person's words for what the
      paper is about: "saw your paper about conditioned game agents on arXiv" (2 to 6 words, no four in a row from
      the title), keeping the file before in accounts.before-about-<time>.json.
  uv run python scripts/accounts.py people --describe
      Free, on the Mac: the Apify store's LinkedIn people-search actors, their prices and input fields, to price
      the decision-maker pull before anything runs. It starts no run.
  uv run python scripts/accounts.py in-talks ACCOUNT_ID --by NAME [--until DAY] [--also SUBJECT_ID ...]
      GI is in talks with the account: its people (and --also, people GI watches who work there) are held for
      every team until a founder decides. Run it again with --until DAY to end every hold it made that day.
  uv run python scripts/accounts.py release [--cited FILE]
      Release day: who at each account should hear from GI first. --cited: a text file of author names from
      the paper's bibliography, one per line.
  --send posts the weekly list to GTM's own channel and records it in the ledger. Only with Justin's go-ahead.

Setup for --send (Justin, once): in Slack, add an incoming webhook for the GTM channel, then set
SLACK_GTM_WEBHOOK to its URL in your shell or in .env, the way SLACK_ROUTING_WEBHOOK is set for the hiring cards.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter

import httpx
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import dotenv_values  # noqa: E402

from app import accounts, contact, today  # noqa: E402
from app.sources import apify, openalex, x_follows  # noqa: E402
from app.sources.http import Fetcher  # noqa: E402

LIVE = accounts.RECORDS["live"]
UPDATE_MAX = 5  # newest new signals added per account per run: enough for a why now, not a paper dump
LI_AT = re.compile(r"li_?at", re.I)  # LinkedIn's login cookie, beside the login words the follows pull refuses


def _key(name):
    """A setting from the environment, else .env, like the other scripts' keys; never printed."""
    return os.getenv(name) or dotenv_values(Path(__file__).resolve().parents[1] / ".env").get(name) or ""


def webhook_url():
    """SLACK_GTM_WEBHOOK: GTM's own channel."""
    return _key("SLACK_GTM_WEBHOOK")


def _print_list(message):
    for block in message["blocks"]:
        text = block.get("text") or {}
        print(text.get("text", "") if isinstance(text, dict) else "", *(e["text"] for e in block.get("elements", [])))


def _print_calls(rows):
    print("\nEvery account (ledger keys in brackets, for scripts/contacts.py):")
    for r in rows:
        print(f"- {r['name']} ({r['buys']}{', ' + r['label'] if r['label'] else ''}, fit {r['fit']}): {r['headline']}. {r['why']}"
              + (f" {r['blocked']}" if r["blocked"] else ""))
        for c in r["contacts"]:
            about = (f"in conversation with {c['talking']['by']}" if c["talking"] else
                     f"held: {c['gate']['reason']}" if c["gate"] else accounts.WATCHED if c["watched"] else "clear")
            print(f"    {c['name']} [{c['key']}]: {about}")


def _print_release(rows):
    print("\nRelease day: who should hear from GI first")
    for r in rows:
        if not r["to"]:
            print(f"- {r['account']}: {r['blocked']}")
            continue
        who = f"{r['to']}{', ' + r['title'] if r['title'] else ''}"
        print(f"- {r['account']}: {who}, from {r['writer'] or 'no owner yet: pick one'}"
              + (f"; GI's paper cites {', '.join(r['cited'])}" if r["cited"] else "")
              + (f". Way in: {r['route_in']}" if r["route_in"] else ""))


def demo():
    page = accounts.page("simulation")
    _print_list(page["weekly"])
    _print_calls(page["accounts"])
    (store, as_of), found, records = accounts.workspace("simulation")
    birchwood = next(a for a in found if a.id == "birchwood-lab")
    print("\nNora Example, who owns Birchwood Lab, marks it as in talks with GI.")
    accounts.in_talks(store, birchwood, by="Nora Example", at=as_of)
    lena = contact.check(contact.history(store, "D904"), as_of)
    print(f"Recruiting's gate for Lena Okafor (watched for MTS, joined Birchwood in September): {lena.state}. {lena.reason}")
    _print_release(accounts.release_day(store, as_of, found, records, cited=["Lee Example"]))


def find(args):
    searches, budget = len(accounts.TOPICS) * args.pages, openalex.Budget()
    if searches * openalex.CREDITS_A_PAGE > budget.most:
        sys.exit(f"{searches} OpenAlex search pages is past the {budget.most} credits a run may spend: lower --pages")
    print(f"OpenAlex: at most {searches} search pages, one a second, about {searches * openalex.CREDITS_A_PAGE} of the "
          f"1,000 free credits a day at {openalex.CREDITS_A_PAGE} a page. It stops by itself before {budget.most}, or before "
          f"fewer than {budget.keep} would be left today, and keeps what it fetched.")
    found = accounts.load(LIVE) or []
    candidates = accounts.find(Fetcher(refresh=True), args.since, found, pages=args.pages, budget=budget)
    print(budget.said())
    LIVE.mkdir(parents=True, exist_ok=True)
    (LIVE / "candidates.json").write_text(json.dumps(candidates, indent=1, ensure_ascii=False))
    print(f"{len(candidates)} companies; wrote {LIVE / 'candidates.json'}")
    for c in candidates[:40]:
        print(f"- {c['name']}{' [big lab]' if c['big'] else ''}{' [account]' if c['account'] else ''}: "
              f"{len(c['papers'])} papers, {len(c['jobs'])} posts, {len(c['news'])} headlines"
              + (f"; topics {', '.join(c['topics'])}" if c["topics"] else "")
              + (f"; most papers: {c['authors'][0]['name']} ({c['authors'][0]['papers']})" if c["authors"] else "")
              + (f"; errors {', '.join(c['errors'])}" if c["errors"] else ""))
    if args.update and found:
        store = (today.source("live") or (None,))[0]  # to give a champion GI already watches their subject id
        data = json.loads((LIVE / "accounts.json").read_text())
        if args.recheck:
            backup = LIVE / f"accounts.before-recheck-{datetime.now():%Y%m%d-%H%M%S-%f}.json"  # one per run
            backup.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            dropped, gone = Counter(), []
            offs = {c["account"]: c.get("deal_off") for c in candidates if c["account"]}
            for row in data["accounts"]:
                # A search the budget ended early can't pick champions (its paper counts are partial), so it can't
                # add back the paper champions recheck would drop: with no ledger to ask, recheck keeps every person.
                found = accounts.recheck(row, None if budget.stopped else store, deal_off=offs.get(row["id"]))
                gone += [f"{row['name']}: {g}" for g in found.pop("gone")]
                dropped.update(found)
            counts = ", ".join(f"{dropped[k]} {w if dropped[k] == 1 else w + 's'}" for k, w in
                               (("headlines", "headline"), ("papers", "paper"), ("posts", "job post")))
            champs = f"{dropped['champions']} champion{'' if dropped['champions'] == 1 else 's'}"
            print(f"Recheck: dropped {counts} that no longer count, and {champs} from papers, "
                  f"to add back under the same rules"
                  + (" (the search stopped early, so champions from papers stay as they are until a full run)"
                     if budget.stopped else "")
                  + f". The file before is {backup}" + "".join(f"\n  - {g}" for g in gone))
        recent = (date.today() - timedelta(days=accounts.LONGEST)).isoformat()  # past every window: evidence only
        added, champions = 0, []
        for c in candidates:
            row = next((a for a in data["accounts"] if a["id"] == c["account"]), None)
            if not row:
                continue
            seen = {s["source_url"] for s in row.setdefault("signals", [])}
            new = [s.model_dump(mode="json") for s in accounts.signals_from(c, max(args.since, recent))
                   if s.source_url not in seen][:UPDATE_MAX]
            row["signals"] += new
            added += len(new)
            if not budget.stopped and (champ := accounts.champion_from(c, accounts.Account.model_validate(row),
                                                                       args.since)):  # partial counts pick no one
                champ.subject_id = accounts.identify(store, champ)  # a watched researcher is then never written to
                row.setdefault("contacts", []).append(champ.model_dump(exclude_defaults=True))
                champions.append(row["name"])
        (LIVE / "accounts.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
        print(f"Added {added} dated signals (papers, posts, headlines) and {len(champions)} champions from papers "
              f"({', '.join(champions) or 'none'}) to accounts.json"
              + ("; new champions wait for a full run" if budget.stopped else ""))


def authors(fetch=None, budget=None):
    """Reads each DOI or OpenAlex paper in accounts.json still inside its window, after copying the file aside: its
    authors at the account in the paper's own words (for a paper stored before find kept them, or before it kept
    where the paper lists them), where it appeared, and, once, its first public version when OpenAlex has an earlier
    one of the same title by an author in common. Says what it read, which papers list no one there (their notes
    stay held), which went up earlier, and which it couldn't read. The title searches stay inside ``budget``
    (OpenAlex's credits for the run, ``openalex.Budget``); a paper past it is looked at on the next run."""
    path = LIVE / "accounts.json"
    if not path.exists():
        sys.exit(f"No accounts yet: add them to {path}")
    data = json.loads(path.read_text())
    listed = [accounts.Account.model_validate(a) for a in data["accounts"]]
    backup = LIVE / f"accounts.before-authors-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
    backup.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    # a Fetcher that refreshes, as find's: a cached page's credit count is old (openalex.Budget)
    fetch, since = fetch or Fetcher(refresh=True), date.today() - timedelta(days=accounts.MOMENTS["team_paper"][2])
    budget = budget or openalex.Budget()
    read, nobody, failed, looked, earlier = [], [], [], 0, []
    for row, account in zip(data["accounts"], listed):
        for raw, s in zip(row.get("signals", []), account.signals):
            fresh = s.authors and all(a.own_words for a in s.authors)  # read from the paper's own lines
            if s.kind != "team_paper" or fresh and s.checked or not s.day or s.day < since:
                continue
            if not s.source_url.startswith(accounts.FOUND["paper"]):  # typed in by hand: its note never waits on this
                if not s.authors:
                    failed.append(f"{account.name}: “{accounts._clip(s.quote)}” (not a DOI or OpenAlex link)")
                continue
            try:
                work = openalex.work(fetch, s.source_url)
                found = accounts.authors_at(work, account, listed)
                there = found or [a.model_dump() for a in s.authors]
                if there and not s.checked and (got := openalex.versions(fetch, work.get("title") or s.quote, budget)) is not None:
                    looked += 1
                    first = accounts.first_version(work, got, there)
                    if first and first[0] < s.day:
                        raw["first_public"], raw["first_at"] = first[0].isoformat(), first[1]
                        earlier.append(f"{accounts.short(account.name)}: “{accounts._clip(s.quote)}”: "
                                       f"{'on ' + first[1] if first[1] else 'public'} since {first[0]:%B %Y}")
                    raw["checked"] = True
            except (httpx.HTTPError, ValueError) as e:
                failed.append(f"{account.name}: “{accounts._clip(s.quote)}” ({type(e).__name__})")
                continue
            if not s.venue and (where := accounts.venue(work)):
                raw["venue"] = where
            if not s.abstract and (text := openalex.abstract(work)):
                raw["abstract"] = text  # for whoever writes what the paper does (gist)
            if found:
                raw["authors"] = found
                read.append(f"{accounts.short(account.name)}: “{accounts._clip(s.quote)}”: {', '.join(a['name'] for a in found)}")
                continue
            # none there by their own lines: an author OpenAlex alone placed there before goes; one read from the
            # paper's own lines, or typed in by hand, stays
            kept = [a for a in s.authors if a.own_words or not a.url.startswith(accounts.FOUND["paper"][1])]
            if len(kept) < len(s.authors):
                raw["authors"] = [a for a, was in zip(raw.get("authors", []), s.authors) if was in kept]
            if not kept:
                nobody.append(f"{account.name}: “{accounts._clip(s.quote)}”")
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"Read the authors of {len(read)} paper{'' if len(read) == 1 else 's'}; the file before is {backup}"
          + "".join(f"\n  - {r}" for r in read))
    if looked:
        print(f"Looked for an earlier version of {looked} paper{'' if looked == 1 else 's'}"
              + (":" + "".join(f"\n  - {r}" for r in earlier) if earlier else ": none went up earlier."))
    if budget.stopped:
        print(f"Stopped looking for earlier versions: {budget.stopped}. The rest wait for the next run.")
    if nobody:
        print("Lists no one at the account, so its note stays held:" + "".join(f"\n  - {r}" for r in nobody))
    if failed:
        print("Couldn't read:" + "".join(f"\n  - {r}" for r in failed))


def gist(url=None, text=None):
    """With no arguments, lists each paper in its window whose note waits for a line on what it does (none yet, or
    one the check refuses, with why), with its abstract. With a paper's link and a line, sets that line after checking
    it (``accounts.gist_problem``) and copying the file aside."""
    path = LIVE / "accounts.json"
    if not path.exists():
        sys.exit(f"No accounts yet: add them to {path}")
    data = json.loads(path.read_text())
    since = date.today() - timedelta(days=accounts.MOMENTS["team_paper"][2])
    papers = [(row, raw, accounts.Signal.model_validate(raw)) for row in data["accounts"] for raw in row.get("signals", [])
              if raw.get("kind") == "team_paper"]
    if url is None:
        open_ = [(row, s, s.gist and accounts.gist_problem(s.gist, s.quote)) for row, _, s in papers
                 if s.day and s.day >= since and (not s.gist or accounts.gist_problem(s.gist, s.quote))]
        print(f"{len(open_)} paper{'' if len(open_) == 1 else 's'} waiting for a line on what it does. Say it in plain words "
              "that read before “really caught my eye”, from the abstract, never the title's own:")
        for row, s, problem in open_:
            print(f"\n- {accounts.short(row['name'])}: “{s.quote}” {s.source_url}\n  "
                  + (f"Its line now, “{s.gist}”, can't go out: {problem}\n  " if problem else "")
                  + (s.abstract[:900] + ("…" if len(s.abstract) > 900 else "") if s.abstract else
                     "(no abstract stored: `scripts/accounts.py authors` reads it, else open the link)"))
        print('\nSet one: uv run python scripts/accounts.py gist LINK "what the paper does"')
        return
    _set(data, [(raw, s) for _, raw, s in papers], url, text, "gist", accounts.gist_problem, "does")


def about(url, text):
    """Sets what a paper is about, for its note's opener ("saw your paper about conditioned game agents on arXiv"),
    after checking it (``accounts.about_problem``) and copying the file aside."""
    path = LIVE / "accounts.json"
    if not path.exists():
        sys.exit(f"No accounts yet: add them to {path}")
    data = json.loads(path.read_text())
    papers = [(raw, accounts.Signal.model_validate(raw)) for row in data["accounts"] for raw in row.get("signals", [])
              if raw.get("kind") == "team_paper"]
    _set(data, papers, url, text, "about", accounts.about_problem, "is about")


def _set(data, papers, url, text, field, problem_of, what):
    """Sets ``field`` on the papers with the link ``url`` to ``text`` once ``problem_of`` finds nothing wrong with it,
    keeping the file before in accounts.before-FIELD-<time>.json."""
    match = [(raw, s) for raw, s in papers if s.source_url == url]
    if not match:
        sys.exit(f"No paper in accounts.json has the link {url}")
    line = " ".join((text or "").split()).rstrip(".")
    if problem := problem_of(line, match[0][1].quote):
        sys.exit(f"Not set: {problem}")
    backup = LIVE / f"accounts.before-{field}-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
    backup.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    for raw, _ in match:
        raw[field] = line
    (LIVE / "accounts.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"Set what “{accounts._clip(match[0][1].quote)}” {what}: “{line}”. The file before is {backup}")


def describe():
    """The Apify store's LinkedIn people-search actors with their pricing, and the first one's input fields. Only
    reads Apify's public listings: no actor runs, nothing is charged. The response shapes are printed as they come,
    since they have not been checked from here."""
    token = _key("APIFY_TOKEN")
    http = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {token}"} if token else {})
    found = http.get(f"{apify.API}/store", params={"search": "linkedin profile search", "limit": 8}).json()
    items = (found.get("data") or {}).get("items") or []
    for a in items:
        print(f"\n{a.get('username')}/{a.get('name')}: {a.get('title')}\n  pricing: "
              + json.dumps(a.get("currentPricingInfo") or a.get("pricingInfos") or a.get("pricing"))[:600])
    if not items:
        return print(json.dumps(found)[:2000])
    pick = next((a for a in items if a.get("username") == "harvestapi"), items[0])
    actor = http.get(f"{apify.API}/acts/{pick['username']}~{pick['name']}").json().get("data") or {}
    build = ((actor.get("taggedBuilds") or {}).get("latest") or {}).get("buildId")
    schema = (http.get(f"{apify.API}/actor-builds/{build}").json().get("data") or {}).get("inputSchema") if build else None
    fields = json.loads(schema).get("properties", {}) if isinstance(schema, str) else (schema or {}).get("properties", {})
    print(f"\nInput fields of {pick['username']}/{pick['name']}:")
    for name, spec in fields.items():
        print(f"  {name} ({spec.get('type')}): {(spec.get('description') or spec.get('title') or '')[:160]}")
    login = [n for n, spec in fields.items()  # the same login guard as the paid X pull (x_follows), plus li_at
             if x_follows._logs_in(n, spec.get("title"), spec.get("editor"), spec.get("description"))
             or LI_AT.search(f"{n} {spec.get('title') or ''} {spec.get('description') or ''}")]
    print("\nAsks for a login, cookie or session: " + ("unknown: no input fields came back" if not fields else
          f"yes, or mentions one ({', '.join(login)}): read those fields before using it" if login else "no"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["find", "in-talks", "release", "people", "authors", "gist", "about"])
    parser.add_argument("account", nargs="?", help="in-talks: the account id; gist, about: the paper's link")
    parser.add_argument("text", nargs="?", help="gist: what the paper does; about: what it is about, in plain words")
    parser.add_argument("--demo", action="store_true", help="invented accounts, as of 2026-09-15")
    parser.add_argument("--since", help="find: the first publication or posting day, YYYY-MM-DD")
    parser.add_argument("--pages", type=int, default=5, help="find: pages of 200 works per topic")
    parser.add_argument("--update", action="store_true", help="find: add new papers and posts to accounts.json")
    parser.add_argument("--recheck", action="store_true",
                        help="find --update: first drop the headlines, papers and paper champions the rules no longer count")
    parser.add_argument("--by", help="in-talks: the GI person who marks it")
    parser.add_argument("--until", help="in-talks: the day the hold ends, if any")
    parser.add_argument("--also", action="append", default=[], help="in-talks: a watched person's subject id")
    parser.add_argument("--cited", type=Path, help="release: author names from GI's paper, one per line")
    parser.add_argument("--describe", action="store_true", help="people: price the LinkedIn people search (free)")
    parser.add_argument("--send", action="store_true", help="post the weekly list to SLACK_GTM_WEBHOOK")
    args = parser.parse_args()

    if args.demo:
        return demo()
    if args.command == "find":
        if not args.since:
            parser.error("find needs --since YYYY-MM-DD")
        if args.recheck and not args.update:
            parser.error("--recheck goes with --update")
        return find(args)
    if args.command == "gist":
        return gist(args.account, args.text)
    if args.command == "about":
        if not (args.account and args.text):
            parser.error('about needs LINK "what the paper is about"')
        return about(args.account, args.text)
    if args.command == "authors":
        return authors()
    if args.command == "people":
        if not args.describe:
            parser.error("people needs --describe for now: the paid pull comes once its price is known")
        return describe()
    found = accounts.workspace("live")
    if not found:
        sys.exit(f"No accounts yet: add them to {LIVE / 'accounts.json'}")
    (store, as_of), listed, records = found
    if not store:
        sys.exit(f"No timelines store at {today.TIMELINES}: the ledger lives there")
    if args.command == "in-talks":
        account = next((a for a in listed if a.id == args.account), None)
        if not account or not args.by:
            parser.error("in-talks needs a known account id and --by NAME")
        held = accounts.in_talks(store, account, by=args.by, until=args.until, also=args.also)
        if args.until:
            return print(f"{account.name}: the in-talks holds on {len(held)} people end on {args.until}.")
        return print(f"{account.name} is in talks: {len(held)} people held for every team until a founder decides.")
    if args.command == "release":
        cited = args.cited.read_text().splitlines() if args.cited else ()
        return _print_release(accounts.release_day(store, as_of, listed, records, cited))
    page = accounts.page("live")
    if page["ledger"]:
        print(page["ledger"])
    _print_list(page["weekly"])
    _print_calls(page["accounts"])
    if args.send:
        if pause := contact.paused():
            sys.exit(f"{contact.said(pause)} Nothing is posted until: uv run python scripts/contacts.py resume")
        if not (url := webhook_url()):
            sys.exit("No SLACK_GTM_WEBHOOK in the environment or .env: see the setup note in --help")
        sent = accounts.send(store, page["accounts"], url)
        print(f"Posted the weekly list: {', '.join(sent)}")


if __name__ == "__main__":
    main()
