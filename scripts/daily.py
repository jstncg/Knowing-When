"""The daily run for the live watchlist, on a schedule on Justin's Mac.

It pulls what everyone posted since the last run, reads it for early signs, saves the cards and, with --post (as the
scheduled job runs it), posts the morning list of who to reach to GI's Slack channel.

    uv run python scripts/daily.py [--people research/private/early-signals/people.json] [--dry-run]
    uv run python scripts/daily.py plist [--people FILE] > ~/Library/LaunchAgents/com.gi-timing-engine.daily.plist
    uv run python scripts/daily.py head

A run covers the people file's watchlist (no until date, no moment). It leaves out anyone the ledger says never and
anyone GI hired (a hired entry in force; New starts' hires get theirs first, starts.hold_all, as route.py does).

  1. Pull from the day of the last run that pulled everything: LOOKBACK_DAYS back on the first run, never more than
     MAX_DAYS. X is one combined search for every handle (apify.x_together_inputs, up to X_CAP tweets), LinkedIn is
     up to LINKEDIN_PER_PERSON posts a profile, and GitHub is free. The events go on the timeline. No pull coverage
     is recorded: the scorecard scores only people with a moment, and a combined search's cap is shared, so it says
     nothing about any one person. Then, free and once a day, the news and WARN notices of each employer the LinkedIn
     profile read found (social_pull.py profiles; journey.employer_sources).
  2. Read (scripts/posts.py read) the weeks with a post in the last READ_DAYS days. A week already read is cached, so
     only weeks with a new post cost a call. With --post, each person's call as of the run then goes on the call log
     (app/call_log.py, calls.jsonl in the same folder), the forward test of early signs. A failure to write it is a
     note, never a failed run. `head` prints the log's chain head to copy off the Mac.
  3. Route (scripts/route.py --people --redraft), saving each card to research/private/routing/. The model writes a
     draft only for a card whose free draft fails its checks, within DRAFT_RUN_USD a run and what is left of the
     month. Drafts are cached, so one already written costs nothing, and with nothing left only those are used. A
     card whose draft still fails stays held.
     With --post (the launchd job's), route.py --send also posts the morning list (who to reach, one line each). As
     the Slack app when it is set up, each card goes in the list's thread with its ledger buttons (they answer while
     scripts/slack_app.py run is going); else through the webhook, the cards as messages after the list. Nothing
     posts when no one is new, while sending is paused (scripts/contacts.py pause), or on a Monday: the Monday post
     (app/weekly.py, sent by scripts/slack_app.py run --live --monday from 09:00) carries the week's reach cards, and
     a morning list first would take them. Without that post, Monday's reaches go in Tuesday's list. The cards are
     saved either way.
     Each card posted goes in the contact ledger as a ping, so no one is carded twice; the only other ledger entries a
     run writes are New starts' hired ones. No paid --draft, no Monday brief (--inbox), never the demo. Without
     --post, a run by hand posts nothing.
  4. With --post, even after a failed run: when the pull is broken, one plain line in the same channel says what is
     wrong and the command that checks or fixes it, once per problem (pull_check). Broken means the pause file, a last
     run that went wrong (a failed actor, an error, a pull or read skipped), no pull, or data over 2 days old. A quiet
     day, or a run marked partial only for a note (a capped search, a card kept back), posts nothing.

The cap is CAP_USD a calendar month (UTC) for pulls, reading and model drafts together. Before each paid step, its
worst case is checked against what is left of the month; a step that does not fit is skipped and the log says so.
Each run adds one line with its cost to <dir>/daily-runs.jsonl: the actors at list price for the items returned (X
billed at least 50 a search) plus RUN_FEE_USD a run as an allowance for start fees, reading at posts.READ_MODEL's list
price for the tokens it used, and model drafts at outreach's list price for Opus 5 (draft_usd, from route.py's last
line). Beside it, apify_reported keeps what each Apify run's own record says it cost, to check those prices against;
it never counts toward the cap. While <dir>/daily.pause exists, each run logs one line and does nothing else. A
missing or edited log starts the month's count again from what it holds.

<dir> is scripts/social_pull.py's (research/private/social on the Mac). The timeline store is today.TIMELINES
(TIMELINES_DB, else <dir>/timelines.sqlite), the one ledger Today's calls, route.py, the Slack buttons and
scripts/contacts.py share.

--dry-run prints what a run would pull and read, each step's worst case, the month so far, and whether and where it
would post (give --post, as the job does). It spends nothing and writes nothing to the ledger or the log. To see the
cards the morning list would carry from what the timeline holds now: uv run python scripts/route.py --people FILE
(saves them, posts nothing).

plist prints the launchd job: every day at 07:30 local, weekends too, with --post; a Mac asleep then runs it when it
wakes. A job loaded before --post existed posts nothing until it is printed again, removed and started. To start it:
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.gi-timing-engine.daily.plist
and to remove it: launchctl bootout gui/$(id -u)/com.gi-timing-engine.daily. macOS may refuse a scheduled job access
to a checkout under ~/Documents, ~/Desktop or ~/Downloads ("Operation not permitted" in daily-output.log): then give
the Python it runs Full Disk Access, or keep the checkout elsewhere.
"""

import argparse
import asyncio
from collections import Counter
from datetime import date, timedelta
import fcntl
import json
from math import floor
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import httpx  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

import posts  # noqa: E402
import social_pull  # noqa: E402
from app import call_log, contact, extraction, journey, providers, slack, starts, today  # noqa: E402
from app import scorecard as sc  # noqa: E402
from app.models import iso, utcnow  # noqa: E402
from app.sources import apify, github  # noqa: E402
from app.sources.http import Fetcher  # noqa: E402
from app.store import Store  # noqa: E402

CAP_USD = 6.0  # a month, pulls and reading together (Justin, 2026-09-24)
X_CAP = 150  # tweets a run for everyone together
LINKEDIN_PER_PERSON = 5
GITHUB_PER_PERSON = 100
RUN_FEE_USD = 0.01  # a run of the LinkedIn posts actor cost $0.025 on 2026-09-23 against $0.016 at list price
LOOKBACK_DAYS, MAX_DAYS, READ_DAYS = 3, 7, 7
PEOPLE = ROOT / "research/private/early-signals/people.json"
CALLS = "calls.jsonl"  # the call log, in the same folder as the run's own log
LABEL = "com.gi-timing-engine.daily"


def log_rows(log):
    """The log's runs. A line that is not a run is left out, and so is its cost: the log is this script's own."""
    rows = []
    for line in log.read_text().splitlines() if log.exists() else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def month_usd(rows, now):
    return round(sum(r.get("usd", 0) for r in rows if str(r.get("at", ""))[:7] == now.isoformat()[:7]), 4)


def since_day(rows, today):
    """The day of the last run whose Apify runs all came back (pulled), never more than MAX_DAYS back; LOOKBACK_DAYS
    back on the first run."""
    last = max((r["pulled"] for r in rows if r.get("pulled")), default="")
    if not last:
        return today - timedelta(days=LOOKBACK_DAYS)
    return max(date.fromisoformat(last), today - timedelta(days=MAX_DAYS))


def capped(planned, raw):
    """A note for each run that came back at its cap, or within a tenth of it, since an actor may stop a little short.
    Each run takes the newest first, so what it left out is older than the oldest item it returned, and no later run
    asks for it. Raise the cap if these recur."""
    notes, searches = [], _searches(planned)
    tweets = [t for t in raw.get(apify.X_SEARCH, []) if not t.get("noResults")]
    for handles in searches:  # one search: all its items, whoever wrote them
        mine = tweets if len(searches) == 1 else [t for t in tweets if _author(t) in handles]
        if len(mine) >= 0.9 * X_CAP:
            oldest = min((d for t in mine if (d := apify.posted_day(t))), default="?")
            notes.append(f"X search came back with {len(mine)} of its cap of {X_CAP}: anything before {oldest} "
                         "may be missing")
    by_profile = {}
    for post in raw.get(apify.LINKEDIN_POSTS, []):
        by_profile.setdefault(str((post.get("author") or {}).get("publicIdentifier") or ""), []).append(post)
    return notes + [f"LinkedIn came back with its cap of {LINKEDIN_PER_PERSON} posts for {who}: older ones may be "
                    "missing" for who, found in by_profile.items() if len(found) >= LINKEDIN_PER_PERSON]


def _searches(planned):
    return [set(re.findall(r"from:(\w+)", r[1]["searchTerms"][0].lower())) for r in planned if r[0] == apify.X_SEARCH]


def _author(tweet):
    return ((tweet.get("author") or {}).get("userName") or "").lower()


def pull_worst_usd(planned):
    return sum(worst for *_, worst in planned) + RUN_FEE_USD * len(planned)


def pulled_usd(planned, raw):
    """What the pull cost at list price: X per search, at least X_MIN_PER_SEARCH each (a tweet whose author is in no
    search's handles counts too), LinkedIn per post, a failed run at its worst case, and the start-fee allowance."""
    failed = [f["input"] for f in raw.get("failed", []) if "actor" in f]
    usd = sum(worst for _, actor_input, _, worst in planned if actor_input in failed)
    searches = _searches([r for r in planned if r[1] not in failed])
    authors = Counter(_author(t) for t in raw.get(apify.X_SEARCH, []) if not t.get("noResults"))
    counted = sum(max(sum(authors[h] for h in handles), apify.X_MIN_PER_SEARCH) for handles in searches)
    stray = sum(n for h, n in authors.items() if not any(h in handles for handles in searches))
    usd += apify.PRICE[apify.X_SEARCH] * (counted + stray)
    usd += apify.PRICE[apify.LINKEDIN_POSTS] * len(raw.get(apify.LINKEDIN_POSTS, []))
    return usd + RUN_FEE_USD * len(planned)


def read_usd(tokens):
    return providers.usd(tokens, posts.USD_IN, posts.USD_OUT)


def watchlist(people_file, store, now, hires=()):
    """(pull rows, read rows) of the watchlist: no until date, no moment. Left out: a never in the ledger, a hire of
    GI's (a hired entry in force at now) and the subject ids in hires."""
    everyone = sc.load_moments(people_file)
    live = {p.subject_id for p in everyone if not p.moments and p.subject_id not in hires
            and not contact.opted_out(history := contact.history(store, p.subject_id))
            and not contact.joining(history, now)}
    pullers = [p for p in social_pull.load_people(people_file) if p.person_id in live and not p.until]
    ids = {p.person_id for p in pullers}  # a read row has no until of its own: scorecard fills in today
    return pullers, [p for p in everyone if p.subject_id in ids]


def github_pull(people):
    """(raw per person id, failures): GitHub is free, so it runs whatever the cap."""
    found, failed, token = {}, [], social_pull.key("GITHUB_TOKEN")
    for p in (p for p in people if p.github):
        try:
            found[p.person_id] = github.fetch(p.github, GITHUB_PER_PERSON, token)
        except (httpx.HTTPError, ValueError) as e:  # ValueError: a body that is not JSON (a Wi-Fi sign-in page)
            failed.append({"github": p.github, "error": f"{type(e).__name__}: {e}"[:300]})
    return found, failed


def pull(people, planned, worst):
    """(raw, notes): GitHub, then the planned Apify runs, each within its share of worst. The only note is that the
    Apify runs could not start."""
    raw, notes = {}, []
    raw["github"], raw["failed"] = github_pull(people)
    if planned:
        try:
            client = apify.Client(social_pull.key("APIFY_TOKEN"))
        except apify.ApifyError as e:
            return raw, [f"pull skipped: {e}"]
        items, failed = asyncio.run(apify.pull(client, planned, round(worst - RUN_FEE_USD * len(planned), 4)))
        raw |= items
        raw["failed"] += failed
        raw["charged"] = client.charged
    print(f"raw {social_pull.save(raw)}")
    return raw, notes


def plan_reads(store, readers, left, since):
    """(jobs, calls, worst $, people left out): each person's unread weeks since ``since``, for as many people as fit
    in what is left of the month at their worst case. The rest wait for next month's cap."""
    settings = {"small_model": posts.READ_MODEL}
    jobs, skipped, calls, worst = [], [], 0, 0.0
    for subject, role, types in posts.targets(store, readers, [p.subject_id for p in readers]):
        todo = extraction.pending(store, subject, role=role, settings=settings, types=types, since=since)
        if todo and worst + posts.estimate_usd(todo, worst=True) <= left:
            jobs.append((subject, role, types))
            calls, worst = calls + len(todo), worst + posts.estimate_usd(todo, worst=True)
        elif todo:
            skipped.append(subject["id"])
    print(f"Read: {calls} calls for {len(jobs)} people with a new post since {since[:10]}, worst case ${worst:.3f}"
          + (f"; {len(skipped)} more do not fit this month" if skipped else ""))
    return jobs, calls, worst, skipped


ROUTE = [sys.executable, str(ROOT / "scripts/route.py")]  # --send only when the run posts; never --draft or --inbox
DRAFT_RUN_USD = 1.00  # model drafts a run at most, only for cards whose free draft fails its checks


POSTED = ("Posted the morning list", "Nothing new to reach")  # route.py's lines for what --send did


def route(people_file, store_url, post, max_usd=None):
    """What route.py did, as a tuple:

    - its last line (who is carded, who kept quiet, what its model drafts cost), or None when it failed;
    - with post, its line for what went to Slack, else None;
    - its reason when it failed, else None;
    - how many cards it kept but did not send;
    - how many model drafts failed (their cards are held).

    The cards are saved to research/private/routing/ before anything posts. ``max_usd`` is what model drafts may spend
    (--redraft): 0 uses only drafts already cached, None makes no model drafts."""
    drafts = ["--redraft", "--max-usd", f"{max_usd:.2f}"] if max_usd is not None else []
    done = subprocess.run(ROUTE + ["--people", str(people_file), *drafts] + (["--send"] if post else []), cwd=ROOT,
                          env={**os.environ, "TIMELINES_DB": store_url.removeprefix("sqlite:///")}, capture_output=True,
                          text=True)
    print(done.stdout + done.stderr)
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    said = next((line for line in lines if line.startswith(POSTED)), None) if post else None  # even if it failed after
    # route.py's own reason (no Slack set up, paused since, Slack unreachable or refusing it): never a token
    error = (done.stderr.strip().splitlines() or ["route.py failed"])[-1] if done.returncode else None
    return (lines[-1] if lines and not error else None), said, error, sum(line.startswith("Not sent:") for line in lines), \
        sum("'s model draft failed" in line for line in lines)


def drafted(line):
    """(model calls, US$) from route.py's last line; (0, 0.0) when it made none."""
    m = re.search(r"; (\d+) model calls, \$([\d.]+)", line or "")
    return (int(m[1]), float(m[2])) if m else (0, 0.0)


def slack_target():
    """Where route.py --send would post, in words: never a token or the webhook."""
    try:
        settings = slack.load()
    except ValueError as e:  # a token in the wrong setting
        return f"nowhere: {e}"
    if settings["SLACK_BOT_TOKEN"] and settings["SLACK_CHANNEL"]:
        return "as the Slack app, each card in the list's thread"
    if os.getenv("SLACK_ROUTING_WEBHOOK") or dotenv_values(ROOT / ".env").get("SLACK_ROUTING_WEBHOOK"):
        return "through the webhook, to its channel, the cards after the list (no buttons)"
    return "nowhere: Slack is not set up (docs/slack-app-setup.md), so the run will say so and only save the cards"


MONDAY = "Monday: the Monday post carries this week's reach cards"


def not_posting(post, now):
    """Why this run's route posts nothing, or None when it posts: no --post, a pause on sending, or a Monday by the
    Mac's own clock (as launchd's 07:30 is), when the Monday post names the week's reaches."""
    if not post:
        return "no --post"
    if pause := contact.paused():
        return contact.said(pause)
    return MONDAY if now.astimezone().weekday() == 0 else None


def commit():
    """The checkout's commit, which made the day's calls, with "-dirty" when its tracked files were changed since;
    "unknown" when git can't say."""
    try:
        done = subprocess.run(["git", "describe", "--always", "--dirty", "--abbrev=12", "--exclude=*"], cwd=ROOT,
                              capture_output=True, text=True)
    except OSError:
        return "unknown"
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,40}(-dirty)?", sha) else "unknown"


def log_calls(path, store, people, now, notes):
    """Put each watched person's call as of now on the call log (app/call_log.py); returns how many were logged. The
    log is a record the run keeps, not a step it rests on: a failure to write it is a note, never a failed run."""
    try:
        return call_log.log(path, store, people, iso(now), commit())
    except Exception as e:  # noqa: BLE001
        notes.append(f"call log not written: {type(e).__name__}: {e}"[:300])
        return 0


def run(people_file, folder, store_url, dry_run=False, now=None, post=False):
    """One daily run; returns the line it logged (None on a dry run, which logs nothing). Each paid step is logged at
    its worst case until it returns, so a run that stops on an error still logs at least what it spent, with the
    error's type."""
    clock = (lambda: now) if now else utcnow  # a test's fixed time, else the real one when each step asks
    now = clock()
    log = folder / "daily-runs.jsonl"
    if (folder / "daily.pause").exists():
        return _log(log, {"at": iso(now), "status": "paused", "usd": 0.0, "month_usd": month_usd(log_rows(log), now)})
    folder.mkdir(parents=True, exist_ok=True)
    lock = (folder / "daily.lock").open("w")
    try:  # one run at a time, or two could both pass the cap check; the lock goes when the process does
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        sys.exit(BUSY)
    rows = log_rows(log)  # read once the lock is held, so it holds every earlier run's cost
    spent = month_usd(rows, now)
    line = {"at": iso(now), "status": "failed", "usd": 0.0, "month_usd": spent}

    def charge(usd):
        line.update(usd=round(usd, 4), month_usd=round(spent + usd, 4))

    readers = None
    try:
        store = Store(store_url)
        if dry_run:  # New starts' hires, left out as the run will once it has put them in the ledger
            unheld = {h.subject_id for h in starts.load("live")[0].hires if h.subject_id}
            unread = None
        else:  # the same hired entries route.py writes later in the run, in time to skip the pull
            unheld, unread = (), starts.hold_all("live", store, iso(now))
        pullers, readers = watchlist(people_file, store, now, unheld)
        since = since_day(rows, now.date()).isoformat()
        print(f"{len(pullers)} watchlist people ({sum(bool(p.x_handle) for p in pullers)} on X, "
              f"{sum(bool(p.linkedin_url) for p in pullers)} on LinkedIn); ${spent:.3f} of ${CAP_USD:.2f} spent this month")
        pullers = [replace(p, since=since) for p in pullers]
        planned = apify.together_runs(pullers, since, X_CAP, LINKEDIN_PER_PERSON)
        worst = pull_worst_usd(planned)
        runs = ", ".join(f"{actor.split('~')[1]} up to {cap}" for actor, _, cap, _ in planned) or "none"
        print(f"Pull since {since}: {len(planned)} Apify runs ({runs}), worst case ${worst:.3f}; "
              f"GitHub for {sum(bool(p.github) for p in pullers)}, free")
        # A gap longer than READ_DAYS reads back to the first day pulled.
        read_since = min(f"{since}T00:00:00+00:00", iso(now - timedelta(days=READ_DAYS)))
        if dry_run:
            print(json.dumps([{"actor": a, "input": i} for a, i, *_ in planned], indent=1))
            print("(The read counts the weeks unread before today's pull; the run reads what the pull adds too.)")
            plan_reads(store, readers, CAP_USD - spent, read_since)
            if unread := starts.load("live")[1]:
                print(f"New starts could not be read, so a hire not yet in the ledger may be pulled: {unread}")
            where, why_not = slack_target(), not_posting(post, now)
            print("Then route.py saves the cards, and " + (
                f"posts nothing: {why_not.rstrip('.')}." if why_not else f"posts {where}" if where.startswith("nowhere")
                else f"posts the morning list {where} (nothing when no one is new)."))
            return None
        posts.save_people(store, readers)  # names, roles and profiles, as read and route.py know them
        notes = [] if pullers else ["no one to pull: the people file has no live watchlist, or the ledger holds "
                                    "everyone (a ledger that cannot be read holds everyone)"]
        if unread:
            notes.append(f"New starts could not be read, so a hire not yet in the ledger may be pulled: {unread}")
        # Free: the news and WARN notices of each employer a profile read found, for the employer-side reasons.
        employers = asyncio.run(journey.employer_sources(store, [p.subject_id for p in readers], iso(now),
                                                         Fetcher(refresh=True)))
        if failed := sorted({s for r in employers.values() for s, c in r["coverage"].items()
                             if c["status"] in ("error", "blocked")}):
            notes.append(f"employer sources failed for some employers: {', '.join(failed)}")
        if planned and worst > CAP_USD - spent:
            notes.append(f"pull skipped: its worst case ${worst:.3f} is more than the ${CAP_USD - spent:.3f} left "
                         "this month")
            planned = []
        line["since"] = since
        charge(worst if planned else 0.0)
        before = len(store.all("timeline_event"))
        raw, not_started = pull(pullers, planned, worst)
        if charged := raw.get("charged"):  # Apify's own figures beside our estimate, to check RUN_FEE_USD and prices
            line["apify_reported"] = charged
        if not_started:
            notes, planned = notes + not_started, []
        usd = pulled_usd(planned, raw) if planned else 0.0
        charge(usd)
        social_pull.ingest(store, pullers, raw, X_CAP, record_coverage=False)
        line["new_events"] = len(store.all("timeline_event")) - before
        if planned and not any("actor" in f for f in raw.get("failed", [])):  # a failed actor run: ask again
            line["pulled"] = now.date().isoformat()  # GitHub fetches its latest items whatever the day
        notes += capped(planned, raw)
        jobs, calls, read_worst, skipped = plan_reads(store, readers, CAP_USD - spent - usd, read_since)
        if skipped:
            notes.append(f"read skipped for {len(skipped)} people: their worst case is more than what is left "
                         "this month")
        line["read_calls"] = 0
        if jobs:
            charge(usd + read_worst)
            budget = posts.read(store, jobs, calls, posts.READ_MODEL, read_since)
            line["read_calls"], line["read_tokens"] = budget.used, budget.tokens  # so the log shows whether the cache was read
            charge(usd + read_usd(budget.tokens))
        if post:  # the scheduled run's calls only: never a run by hand
            line["calls_logged"] = log_calls(folder / CALLS, store, readers, now, notes)
        so_far = line["usd"]  # pulls and reading, as charged
        left = CAP_USD - spent - so_far
        # route.py's calls never cost more than its cap (outreach.write), so the cap is its worst case
        draft_cap = floor(max(0.0, min(DRAFT_RUN_USD, left)) * 100) / 100  # as route.py reads it: two decimals
        if not draft_cap:
            notes.append(f"model drafts: only ones already written, with ${max(left, 0):.3f} left this month")
        charge(so_far + draft_cap)  # its worst case until route.py says what it spent
        why_not = not_posting(post, clock())  # at route time: a run begun late Sunday that ends on Monday posts nothing
        line["route"], said, error, unsent, failed_drafts = route(people_file, store_url, not why_not, draft_cap)
        if failed_drafts:  # held cards; an error-status reply cost nothing, one lost in transit counts at its worst
            notes.append(f"{failed_drafts} model draft{'s' if failed_drafts != 1 else ''} failed, so those cards stay "
                         "held: why is in daily-output.log")
        if line["route"] is not None:  # a route.py that failed keeps its worst case in the log
            line["draft_calls"], line["draft_usd"] = drafted(line["route"])
            charge(so_far + line["draft_usd"])
        if why_not:
            line["slack"] = f"not posted: {why_not}"
        else:
            line["slack"] = said or f"not posted: {error or 'route.py did not say'}"
            if error:  # routing is free, so a run that does not post still says who is carded; nothing posts twice
                notes.append(f"route.py failed{' after posting' if said else ''}: {error}")
                line["route"] = route(people_file, store_url, False)[0]  # no model drafts: its worst case stays charged
            elif not said:
                notes.append("route.py did not say whether it posted the morning list")
            if unsent:
                notes.append(f"{unsent} card{'s' if unsent != 1 else ''} kept but not sent: why is in daily-output.log")
        if error and why_not:
            notes.append(f"route.py failed: {error}")
        failed = [f.get("actor") or f.get("github") for f in raw.get("failed", [])]
        line |= {"status": "partial" if failed or notes or not line["route"] else "ok",
                 **({"failed": failed} if failed else {}), **({"notes": notes} if notes else {})}
        return _log(log, line)  # inside the lock, so the next run's cap check sees this run's cost
    except BaseException as e:  # the line still goes in the log, with what was spent; the error goes on up
        if not dry_run:
            if post and readers is not None and "calls_logged" not in line:
                said = []  # the morning's calls on what the store holds: a missed morning drops every open window
                line["calls_logged"] = log_calls(folder / CALLS, store, readers, now, said)
                line |= {"notes": said} if said else {}
            _log(log, line | {"error": type(e).__name__})
        raise
    finally:
        lock.close()


def _log(log, line):
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(json.dumps(line) + "\n")
    print(json.dumps(line))
    return line


def plist(people_file, folder):
    """The launchd job: this checkout's Python runs this script every day at 07:30 local, its output to the folder.
    launchd does not read the shell's environment, so SOCIAL_DIR and TIMELINES_DB, if set now, go in the job. The
    scheduled run then uses the same log, cap and store as a run by hand. DATABASE_URL is the web app's, so it stays
    out."""
    folder = Path(folder).resolve()
    same = {"SOCIAL_DIR": str(folder)} if os.environ.get("SOCIAL_DIR") else {}
    if os.environ.get("TIMELINES_DB"):
        same["TIMELINES_DB"] = str(today.TIMELINES.resolve())
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "--people", str(Path(people_file).resolve()),
                             "--post"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", **same},
        "StartCalendarInterval": {"Hour": 7, "Minute": 30},
        "StandardOutPath": str(folder / "daily-output.log"),
        "StandardErrorPath": str(folder / "daily-output.log"),
    }).decode()


BUSY = "Another daily run is going; this one did nothing."


def pull_check(folder, store_url):
    """After a --post run, even one that failed: one plain line in GI's channel when the pull is broken
    (today.freshness), with the command that checks or fixes it. Once per problem (app/weekly.py alert_once,
    remembered in <dir>/pull-alert.json, which the Monday post reads too); never on a quiet day, and never while
    sending is paused. Returns what it posted or why not, for daily-output.log."""
    from app import weekly  # only a --post run needs it

    fresh = today.freshness("live", Store(store_url), log=folder / "daily-runs.jsonl")
    if not fresh["broken"]:
        weekly.alert_once(fresh, None, folder / "pull-alert.json")  # the pull is fine: the next problem is said again
        return "Pull check: nothing broken."
    if pause := contact.paused():
        return f"Pull check: {fresh['broken']}, not posted: {contact.said(pause)}"
    try:
        settings = slack.load()
    except ValueError as e:  # a token in the wrong setting
        return f"Pull check: {fresh['broken']}, not posted: {e}"
    to = slack.poster(settings) or os.getenv("SLACK_ROUTING_WEBHOOK") or \
        dotenv_values(ROOT / ".env").get("SLACK_ROUTING_WEBHOOK")
    if not to:
        return f"Pull check: {fresh['broken']}, not posted: Slack is not set up"
    said = weekly.alert_once(fresh, to, folder / "pull-alert.json")
    return f"Pull check posted: {said}" if said else f"Pull check: {fresh['broken']}, already said"


def old_store():
    """Why not to run, when a sqlite DATABASE_URL names a file other than the timing store. A job printed before
    TIMELINES_DB named its store that way; the run would switch stores without a word, and the old store's never and
    pinged entries would stop holding the cards. None when it names the same file, or there is none."""
    named = contact._sqlite(os.getenv("DATABASE_URL"))
    if named and named.resolve() not in (today.TIMELINES.resolve(), (ROOT / "data/pilot.sqlite").resolve()):
        return (f"DATABASE_URL names {named}, but the timing store is {today.TIMELINES}: DATABASE_URL no longer picks "
                "it. To keep that file, set TIMELINES_DB to it (and print the job again: daily.py plist); else unset "
                "DATABASE_URL. Nothing ran.")
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", nargs="?", default="run", choices=("run", "plist", "head"))
    ap.add_argument("--people", type=Path, default=PEOPLE, help="the people file; its watchlist is pulled")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and worst cases; spend nothing")
    ap.add_argument("--post", action="store_true", help="post the morning list to Slack, as the launchd job does")
    args = ap.parse_args()
    folder = social_pull.PRIVATE
    if args.command == "plist":
        return print(plist(args.people, folder), end="")
    if args.command == "head":  # the call log's chain head, to copy off the Mac
        return print(json.dumps(call_log.head(folder / CALLS)))
    store_url = f"sqlite:///{today.TIMELINES}"
    check = args.post and not args.dry_run
    try:
        if not args.people.exists():
            sys.exit(f"No people file at {args.people}")
        if stray := old_store():
            sys.exit(stray)
        run(args.people, folder, store_url, args.dry_run, post=args.post)
    except SystemExit as e:
        check = check and e.code != BUSY  # the run going now checks for itself
        raise
    finally:
        if check:
            try:
                print(pull_check(folder, store_url))
            except Exception as e:  # noqa: BLE001 - never hides the run's own error; the type only, never a token
                print(f"Pull check failed: {type(e).__name__}")


if __name__ == "__main__":
    main()
