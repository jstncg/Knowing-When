"""Pull what a list of people post, reply and comment on X and LinkedIn (Apify, paid) and build on GitHub (free).

    uv run python scripts/social_pull.py pull   --people research/private/social/people.json [--paid]
    uv run python scripts/social_pull.py ingest --people FILE --raw research/private/social/raw/<run>.json
    uv run python scripts/social_pull.py search --query '"excited to join" "product designer"' [--paid]

people is a JSON list of {"person_id", "x_handle", "linkedin_url", "github", "since", "until"}
(all but person_id optional; give only the platforms wanted for that person). GitHub is free
(GITHUB_TOKEN raises its limit from 60 calls an hour). A past moment sets until to its day and
since some months before; a watchlist sets since only.

pull    one-time history for everyone. Without --paid it prints the actor inputs
        and a worst-case cost and spends nothing. --per-person N items per person
        per platform (default 400); --max-usd caps the whole pull (default 10) and a
        worst case above it is refused; --comments adds LinkedIn comments.
        --links adds the people the links command found; --since sets one start date
        for everyone (the watchlist's LinkedIn posts: --since 60 days ago).
ingest  re-parses a saved raw pull into the store; free, no network.
Both record, per person and platform, the day the pull holds everything from
(scorecard.pull_coverage: a platform that may have hit its cap is complete only
from its oldest item), so the scorecard replays only complete windows. A pull
saves the window each person was asked for and its --per-person; for a raw pull
from before it did, give ingest --asked (the people file it ran with) and the
pull's --per-person, or no coverage is recorded from it.
The daily pull for the watchlist is scripts/daily.py.
search  LinkedIn posts matching --query (repeatable), newest first; writes who posted
        to <dir>/search-<time>.json for building people lists. --must keeps hits
        mentioning one of the given words.
links   free: for people in --people without a linkedin_url, finds one in their X bio
        (from the saved X pulls in <dir>/raw/) or their GitHub profile, and names the
        personal site to check by hand when there is none; writes
        <dir>/linkedin-links.json; blank when there is none. Never searches by name;
        watchlist people only, like profiles.
profiles  each watchlist person's current job and start month from their LinkedIn
        profile ($4 per 1,000; dry run without --paid), for --people plus the found
        --links, into <dir>/linkedin-profiles.json; --raw re-reads a saved pull free.
        Skips anyone with an until date (a replay case): today's profile would leak a
        past move. Saves each one's employer and when they joined it in their context
        in the store, then reads
        that employer's news and WARN notices, free (journey.employer_sources).
follows --schema  free: reads each candidate Apify actor for X following lists (app/sources/x_follows.py):
        its pricing, input fields and output fields, into <dir>/follows-schemas.json. Nothing runs.
follows   which GI team handles (--team, research/private/gi-team.json) each watchlist person with an X handle
        follows, for the channel: an X message lands in their inbox only when they follow the sender. Leaves out
        anyone the ledger says never. Without --paid it prints accounts x accounts read x price and spends nothing;
        --paid runs x_follows.ACTOR once per account (up to x_follows.CAP read each), within --max-usd and never
        above x_follows.MAX_USD, keeps only team handles, into <dir>/x-follows.json after each account, and deletes
        each run's dataset on Apify. Checks the actor's schema and price first, free. Only rows that say they are
        follows count; if the first account's rows don't say, it stops there (--rows-are-follows once checked).

Raw actor output goes to <dir>/raw/ and events to --db, else the timing store (TIMELINES_DB, else
<dir>/timelines.sqlite: the one ledger route.py, the Slack buttons and scripts/contacts.py share), where <dir> is
SOCIAL_DIR, else /mnt/project-files/private/social in the cloud, else research/private/social (contact.SOCIAL).
APIFY_TOKEN and GITHUB_TOKEN are read from the environment or .env and never printed.
"""

import argparse
import asyncio
from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
import sys

from dotenv import dotenv_values
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app import contact, journey, scorecard, today  # noqa: E402
from app.models import iso, utcnow  # noqa: E402
from app.sources import apify, github, linkedin_profiles, x_follows  # noqa: E402
from app.sources.http import Fetcher  # noqa: E402
from app.sources.social import Person  # noqa: E402
from app.store import Store  # noqa: E402
from app.timeline import add_event  # noqa: E402

# Real people's data, outside git: the project's shared folder when this runs in the cloud, else research/private/.
PRIVATE = contact.SOCIAL


def key(name):
    return os.getenv(name) or dotenv_values(ROOT / ".env").get(name) or ""


def load_people(path, since=None, links=None):
    """The people file, plus the rows the links command found (--links): a found URL fills a
    person's blank one or adds the person; it never replaces a URL the file already has."""
    if since:
        try:
            since = date.fromisoformat(since).isoformat()  # a date, or in_window compares against text
        except ValueError:
            sys.exit(f"--since must be a date like 2026-07-25, not {since!r}")
    people = {}
    for row in json.loads(Path(path).read_text()):
        if row["person_id"] in people:
            sys.exit(f"{row['person_id']} is in {path} twice")
        people[row["person_id"]] = Person.from_dict(row)
    for row in json.loads(Path(links).read_text()) if links else []:
        found = Person.from_dict({"person_id": row["person_id"], "linkedin_url": row.get("linkedin_url")})
        known = people.get(found.person_id)
        if not known:
            people[found.person_id] = found
        elif found.linkedin_url and not known.linkedin_url:
            people[found.person_id] = replace(known, linkedin_url=found.linkedin_url)
    return [replace(p, since=since) for p in people.values()] if since else list(people.values())


def save(raw):
    out = PRIVATE / "raw" / f"{iso()[:19].replace(':', '')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(raw))
    return out


def asked_windows(path):
    """{person id: [since, until]} from the people file a pull ran with, for a raw pull that predates raw["asked"]."""
    return {p.person_id: [p.since, p.until] for p in load_people(path)} if path else None


def ingest(target, people, raw, per_person, asked=None, record_coverage=True):
    """The raw pull's events onto the timeline, and (record_coverage) the days it holds everything from."""
    by_id = {p.person_id: p for p in people}
    events, rejected = apify.events(people, {k: v for k, v in raw.items() if k in apify.PARSERS})
    events += [e for person_id, pulled in raw.get("github", {}).items() if person_id in by_id
               for e in github.events(by_id[person_id], pulled)]
    for event in events:
        add_event(target, event)
    counts = {}
    for e in events:
        counts[e["event_type"]] = counts.get(e["event_type"], 0) + 1
    print("events " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "no events")
    for actor, reasons in rejected.items():
        print(f"rejected {actor.split('~')[1]}: " + (", ".join(f"{r} {n}" for r, n in reasons.items()) or "none"))
    for failure in raw.get("failed", []):
        print(f"FAILED {failure.get('actor') or failure.get('github')}: {failure['error']}")
    print(f"people with any event: {len({e['subject_id'] for e in events})} of {len(people)}")
    if not record_coverage:
        return
    windows = raw.get("asked") or asked or {}
    if unknown := [p.person_id for p in people if p.person_id not in windows]:
        print(f"No window recorded for {len(unknown)} of {len(people)} people, so no coverage is recorded for them "
              "from this pull: give --asked with the people file it ran with, and its --per-person.")
    found = scorecard.pull_coverage(people, raw, per_person, asked)
    scorecard.save_coverage(target, found)
    since = {p.person_id: p.since for p in people}
    for person_id, platforms in found.items():
        if cut := {k: day for k, (day, _) in platforms.items() if day != since[person_id]}:
            print(f"COVERAGE {person_id}: "
                  + ", ".join(f"{k} failed" if v is None else f"{k} complete from {v or 'the start'}" for k, v in cut.items()))


async def pull(args, people, comments_limit=None):
    planned = apify.runs(people, args.per_person, comments_limit)
    estimate = apify.estimate_usd(planned)
    print(f"{len(people)} people: {sum(bool(p.x_handle) for p in people)} on X, "
          f"{sum(bool(p.linkedin_url) for p in people)} on LinkedIn, {sum(bool(p.github) for p in people)} on GitHub "
          f"(free); up to {args.per_person} items each per platform; worst case ${estimate} (cap ${args.max_usd})")
    if (extra := min(apify.open_worst_usd(people, args.per_person), round(args.max_usd - estimate, 2))) > 0:
        print(f"Plus up to ${extra} within the cap if windowed X searches come back empty and are re-asked.")
    if any(p.until and p.linkedin_url for p in people):
        print("Note: LinkedIn posts come newest first with no end date, so a past window may spend its cap on later posts.")
    if not args.paid:
        print(json.dumps([{"actor": actor, "input": actor_input} for actor, actor_input, _, _ in planned], indent=1))
        print("Dry run: add --paid to spend.")
        return
    if estimate > args.max_usd:
        sys.exit(f"The worst case ${estimate} is above --max-usd {args.max_usd}: the runs would be cut short. "
                 "Raise --max-usd or lower --per-person.")
    # GitHub is free and goes first, so a failed paid run still leaves it saved.
    today = utcnow().date().isoformat()  # an open window runs to today
    raw = {"github": {}, "failed": [], "per_person": args.per_person,
           "asked": {p.person_id: [p.since, p.until or today] for p in people}}
    for p in (p for p in people if p.github):
        try:
            raw["github"][p.person_id] = github.fetch(p.github, args.per_person, key("GITHUB_TOKEN"))
        except httpx.HTTPError as e:
            raw["failed"].append({"github": p.github, "error": f"{type(e).__name__}: {e}"[:300]})
    out = save(raw)
    if planned:
        client = apify.Client(key("APIFY_TOKEN"))
        pulled, failed = await apify.pull(client, planned, args.max_usd)
        raw |= pulled
        raw["failed"] += failed
        out.write_text(json.dumps(raw))
        left = args.max_usd - sum(worst for *_, worst in planned)  # unrounded, so the re-ask stays under the cap
        failed_searches = {f["input"]["searchTerms"][0] for f in failed if f["actor"] == apify.X_SEARCH}
        few = [p for p in apify.thin(people, raw.get(apify.X_SEARCH, []), args.per_person)
               if apify.x_input(p, 0)["searchTerms"][0] not in failed_searches]
        if few and (retry := apify.open_runs(few, args.per_person, left)):
            print(f"re-asking {len(few)} people with an empty or thin windowed search, without the end date, "
                  f"up to {retry[0][2]} items each")
            pulled, failed = await apify.pull(client, retry, left)
            raw[apify.X_SEARCH] = raw.get(apify.X_SEARCH, []) + pulled.get(apify.X_SEARCH, [])
            raw["failed"] += failed
            out.write_text(json.dumps(raw))
            for p in apify.short_of_window(few, pulled.get(apify.X_SEARCH, []), retry[0][2]):
                print(f"SHORT {p.person_id}: the cap ran out on tweets after {p.until}, before the window")
    print(f"raw {out}")
    ingest(store(args), people, raw, args.per_person)


async def search(args):
    """Who posted about the queries on LinkedIn, newest first; written to a file for the people lists, not the timeline."""
    actor_input = apify.search_input(args.query, args.per_person, args.posted_limit)
    estimate = round(apify.PRICE[apify.LINKEDIN_SEARCH] * args.per_person * len(args.query), 2)
    print(f"{len(args.query)} queries, up to {args.per_person} posts each; worst case ${estimate} (cap ${args.max_usd})")
    if not args.paid:
        print(json.dumps(actor_input, indent=1) + "\nDry run: add --paid to spend.")
        return
    items = await apify.Client(key("APIFY_TOKEN")).run(apify.LINKEDIN_SEARCH, actor_input,
                                                       max_items=args.per_person * len(args.query), max_usd=args.max_usd)
    save({apify.LINKEDIN_SEARCH: items})
    hits = apify.search_hits(items)
    if args.must:
        words = [w.strip().lower() for w in args.must.split(",") if w.strip()]
        hits = [h for h in hits if any(w in (h["text"] + " " + h["headline"]).lower() for w in words)]
    out = PRIVATE / f"search-{iso()[:19].replace(':', '')}.json"
    out.write_text(json.dumps(hits, indent=1))
    print(f"{len(items)} posts, {len(hits)} kept -> {out}")


def find_links(args):
    people = load_people(args.people)
    if past := [p.person_id for p in people if p.until]:
        sys.exit(f"{len(past)} people have an until date ({', '.join(past)}): links are for the watchlist only.")
    bios = linkedin_profiles.x_bios(sorted((PRIVATE / "raw").glob("*.json")))
    found, sites, http = {}, {}, httpx.Client(timeout=30)
    for p in (p for p in people if p.github and not p.linkedin_url):
        try:
            found[p.person_id], sites[p.person_id] = linkedin_profiles.github_links(p.github, http, key("GITHUB_TOKEN"))
        except (httpx.HTTPError, ValueError) as e:  # ValueError: a body that is not JSON
            print(f"FAILED github {p.github}: {type(e).__name__}: {e}"[:300])
    rows = linkedin_profiles.links(people, bios, found, sites)
    out = PRIVATE / "linkedin-links.json"
    out.write_text(json.dumps(rows, indent=1))
    for row in rows:
        print(f"{row['person_id']}: {row['linkedin_url'] or 'none'} ({row['source']})"
              + (f"; their site {row['personal_site']} may link it, to check by hand" if row.get("personal_site") else ""))
    print(f"{sum(bool(r['linkedin_url']) for r in rows)} of {len(rows)} found -> {out}")


async def follows(args):
    if not args.schema:
        return await follows_pull(args)
    client, found = apify.Client(key("APIFY_TOKEN")), []
    for actor_id in x_follows.CANDIDATES:
        try:
            actor, build = await client.actor(actor_id)
        except httpx.HTTPStatusError as e:  # the status only: the request carries the token in its headers
            print(f"{actor_id}: not readable ({e.response.status_code})")
            continue
        found.append(x_follows.summary(actor_id, actor, build, iso()))
        print("\n".join(x_follows.lines(found[-1])) + "\n")
    out = PRIVATE / "follows-schemas.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"read_on": utcnow().date().isoformat(), "actors": found}, indent=1))
    print(f"{len(found)} of {len(x_follows.CANDIDATES)} actors read, free -> {out}")


def opted_out(args):
    """The people whose ledger in the timing store says never: they asked to be left alone, so not even read.
    Nothing when this machine has no timing store yet (it is not created for this)."""
    url = args.db or (f"sqlite:///{today.TIMELINES}" if today.TIMELINES.exists() else "")
    if not url:
        return set()
    return {e["person_id"] for e in Store(url).all("contact") if e["kind"] == "never"}


async def follows_pull(args):
    if not args.people:
        sys.exit("follows needs --people")
    everyone, never = load_people(args.people), opted_out(args)
    watchlist = [p for p in everyone if p.x_handle and not p.until and contact.person_key(p.person_id) not in never]
    handles = {}  # one read per account: a person watched for two roles is read, and paid for, once
    for p in watchlist:
        handles.setdefault(p.x_handle.lower(), []).append(p.person_id)
    team = x_follows.team_handles(json.loads(Path(args.team).read_text())["members"]) if Path(args.team).exists() else set()
    if not team:
        sys.exit(f"No X handles in {args.team}: nothing to look for.")
    cap_usd = min(args.max_usd, x_follows.MAX_USD)
    worst = x_follows.worst_usd(len(handles))
    left_out = len(everyone) - len(watchlist)
    print(f"{len(handles)} watchlist people with an X handle x up to {x_follows.CAP:,} accounts they follow and "
          f"{x_follows.FOLLOWERS_MAX} follower rows the actor may add (dropped) x ${x_follows.PRICE}, each run capped "
          f"at ${x_follows.run_usd():.2f} = worst case ${worst} (cap ${cap_usd}); looking for {len(team)} team handles."
          + (f" {left_out} others left out: no X handle, a past moment, or they asked never to be contacted."
             if left_out else ""))
    if not handles:
        return
    if not args.paid:
        print(json.dumps(x_follows.follows_input(watchlist[0].x_handle, followers=x_follows.FOLLOWERS_MAX), indent=1)
              + "\nDry run: add --paid to spend.")
        return
    if worst > cap_usd:
        sys.exit(f"The worst case ${worst} is above the cap ${cap_usd}: fewer people, or a lower cap.")
    client = apify.Client(key("APIFY_TOKEN"))
    actor, build = await client.actor(x_follows.ACTOR)  # free: the fields and the price must hold before any run
    now = iso()
    schema = x_follows.summary(x_follows.ACTOR, actor, build, now)
    if problems := x_follows.schema_problems(schema) + x_follows.price_problems(actor, now):
        sys.exit("Not run: " + "; ".join(problems) + ". Run follows --schema and update app/sources/x_follows.py.")
    followers = x_follows.fewest_followers(schema)  # required by the actor even with getFollowers off
    limit, per_run = x_follows.CAP + x_follows.FOLLOWERS_MAX, x_follows.run_usd()  # room for every row it may send
    read_on = utcnow().date().isoformat()
    out = PRIVATE / "x-follows.json"
    kept = x_follows.load(out)
    for person_ids in handles.values():
        handle = next(p.x_handle for p in watchlist if p.person_id == person_ids[0])
        try:
            items = await client.run(x_follows.ACTOR, x_follows.follows_input(handle, followers=followers),
                                     max_items=limit, max_usd=per_run, keep=False)
        except (apify.ApifyError, httpx.HTTPError) as e:  # the type and status only: the request carries the token
            print(f"FAILED {', '.join(person_ids)}: {type(e).__name__}")
            continue
        row = x_follows.result(handle, items, team, x_follows.CAP, read_on, limit, args.rows_are_follows)
        kept |= {pid: row for pid in person_ids}
        # Saved after each person: a pull that dies partway keeps what was already paid for.
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"actor": x_follows.ACTOR, "cap": x_follows.CAP, "people": kept}, indent=1))
        if x_follows.named(items) and not row["read"] and not args.rows_are_follows:
            print(f"{', '.join(person_ids)}: unknown ({x_follows.shape(items)}).\nStopped after one account: no row "
                  "says it is a follow, so a teammate who follows them could read as one they follow. If "
                  f"@{handle.lstrip('@')}'s Following count on X matches the rows not saying which list, they are "
                  "follows: run again with --rows-are-follows. If they follow 1,000 or more, this check can't tell, and "
                  "more than 1,000 such rows means follower rows are mixed in: then don't use it.")
            break
        print(f"{', '.join(person_ids)}: " + (f"follows {', '.join('@' + h for h in row['follows'])}" if row["follows"]
                                              else "follows none of the team" if row["follows"] == [] else
                                              f"unknown: read {row['read']:,}, about the cap, and no team handle "
                                              "among them" if not row["complete"] else "unknown: nothing read "
                                              "(protected, suspended or renamed?)")
              + f" ({x_follows.shape(items)})")
    if client.undeleted:
        print(f"{len(client.undeleted)} run datasets could not be deleted on Apify; they hold whole following lists. "
              "Delete them in the Apify console, Storage.")
    print(f"-> {out}. Copy it to research/private/social/ on the machine that routes, if this is not it.")


async def profiles(args):
    people = load_people(args.people, links=args.links)
    if past := [p.person_id for p in people if p.until]:
        print(f"Skipped {len(past)} replay cases (an until date): a profile read today would show their move before "
              "the replay reaches it. Profiles are for the watchlist only.")
    people = [p for p in people if p.linkedin_url and not p.until]
    if args.raw:
        items = json.loads(Path(args.raw).read_text()).get(linkedin_profiles.PROFILE, [])
    else:
        urls = sorted({p.linkedin_url for p in people})
        estimate = linkedin_profiles.worst_usd(urls)
        print(f"{len(urls)} profiles; worst case ${estimate} (cap ${args.max_usd})")
        if not args.paid:
            print(json.dumps(linkedin_profiles.profile_input(urls), indent=1) + "\nDry run: add --paid to spend.")
            return
        if estimate > args.max_usd:
            sys.exit(f"The worst case ${estimate} is above --max-usd {args.max_usd}.")
        items = await apify.Client(key("APIFY_TOKEN")).run(
            linkedin_profiles.PROFILE, linkedin_profiles.profile_input(urls), max_items=len(urls), max_usd=args.max_usd)
        print(f"raw {save({linkedin_profiles.PROFILE: items})}")
    rows = linkedin_profiles.roles(people, items)
    out = PRIVATE / "linkedin-profiles.json"
    # A saved pull is named by its time (save), so a re-read keeps the day the profiles were read.
    pulled_on = Path(args.raw).stem[:10] if args.raw else utcnow().date().isoformat()
    out.write_text(json.dumps({"pulled_on": pulled_on, "people": rows}, indent=1))
    target = store(args)  # each latest job's start, so no reach lands in someone's first year (hold_short_tenure)
    starts = linkedin_profiles.job_events(rows, pulled_on)
    for event in starts:
        add_event(target, event)
    print(f"{len(starts)} job starts on the timeline: the first-year hold reads them")
    employed, missing = journey.save_employers(target, rows, pulled_on)
    if missing:
        print(f"No context in the store yet for {', '.join(missing)}: the daily run adds them; read again with --raw")
    news = await journey.employer_sources(target, employed, iso(), Fetcher(refresh=True))  # free: news and WARN
    for person_id, report in news.items():
        print(f"employer news for {person_id}: " + ", ".join(
            f"{source} {entry['status']}" + (f" ({entry['events']})" if entry.get("events") else "")
            for source, entry in report["coverage"].items()))
    for person_id, row in rows.items():
        if row.get("moved_to"):
            print(f"MOVED {person_id}: now {row['moved_to']}; use it in the people file for later pulls")
        now = row.get("current") or []
        print(f"{person_id}: {row['status']}" + (f", {now[0]['title']} at {now[0]['company']} since {now[0]['started'] or '?'}"
                                                 if now else ""))
    print(f"{sum(r['status'] == 'current' for r in rows.values())} of {len(rows)} with a current job -> {out}")


def store(args):
    url = args.db or f"sqlite:///{today.TIMELINES}"
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    print(f"store {url}")
    return Store(url)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("pull", "ingest", "search", "links", "profiles", "follows"))
    ap.add_argument("--people", help="pull, ingest: the JSON list of people")
    ap.add_argument("--raw", help="ingest, profiles: a saved raw pull")
    ap.add_argument("--links", help="pull, profiles: also the people the links command found (linkedin-links.json)")
    ap.add_argument("--since", help="pull: one start date (YYYY-MM-DD) for everyone, replacing the file's")
    ap.add_argument("--asked", help="ingest: the people file a raw pull ran with, when the pull did not record it")
    ap.add_argument("--query", action="append", default=[], help="search: repeat for each query")
    ap.add_argument("--posted-limit", default="year", help="search: 24h, week, month, 3months, 6months, year or any")
    ap.add_argument("--must", help="search: keep hits whose text or headline has one of these comma-separated words")
    ap.add_argument("--comments", action="store_true", help="also pull LinkedIn comments ($2 per 1,000)")
    ap.add_argument("--per-person", type=int, default=400)
    ap.add_argument("--max-usd", type=float, default=10.0)
    ap.add_argument("--paid", action="store_true")
    ap.add_argument("--schema", action="store_true", help="follows: read the candidate actors' schemas, free")
    ap.add_argument("--team", default=str(ROOT / "research/private/gi-team.json"), help="follows: the team file")
    ap.add_argument("--rows-are-follows", action="store_true",
                    help="follows: count rows that don't say which list they are from, once checked they are follows")
    ap.add_argument("--db")
    args = ap.parse_args()
    if args.command == "follows":
        return asyncio.run(follows(args))
    if args.command == "search":
        if not args.query:
            sys.exit("search needs --query")
        return asyncio.run(search(args))
    if not args.people:
        sys.exit(f"{args.command} needs --people")
    if args.command == "links":
        return find_links(args)
    if args.command == "profiles":
        return asyncio.run(profiles(args))

    if args.command == "ingest":
        if not args.raw:
            sys.exit("ingest needs --raw")
        ingest(store(args), load_people(args.people), json.loads(Path(args.raw).read_text()), args.per_person,
               asked_windows(args.asked))
    else:
        asyncio.run(pull(args, load_people(args.people, args.since, args.links), "any" if args.comments else None))


if __name__ == "__main__":
    main()
