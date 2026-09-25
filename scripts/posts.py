"""Read people's posts into early signs, replay when the engine would have reached out, and test the signs.

  uv run python scripts/posts.py plan --people FILE [--model M] [--usd-in 2 --usd-out 10]
      Free. Per person: scored or why not, posts and stars in the pull, where the pull is complete from,
      the windows the scorecard would replay, and the calls and estimated cost of reading them (at READ_MODEL's
      list price per million tokens by default). Totals per role.
  uv run python scripts/posts.py read [--people FILE] [--subject ID ...] [--max-calls 24]
      Paid: one model call per calendar week of a person's posts, comments, replies and GitHub items
      (25 at most per call, the 10 before it as context), read for their role's topics and cached, so
      a week read once costs nothing again. People with a moment are read as the replay reads them
      (posts and stars, detectors.REPLAY_TYPES), the watchlist with everything (READ_TYPES); people with a
      moment who are not scored (plan says why) are not read. Counts the calls first and stops before
      any if they are more than --max-calls.
      Writes early signs (work in progress, a
      technical ask, a submission not yet public, a post on GI's topics) and career facts to the
      person's timeline, and retires what an earlier reading said about the same posts. --people saves
      each listed person's name and role first and reads them.
      --days N reads only the watchlist's weeks with a post in the last N days: the same cached calls
      the full read makes for those weeks, so they are never paid twice.
  uv run python scripts/posts.py happened --people FILE [--days 30] [--as-of DATE] [--profiles FILE]
      Free. Each watchlist person's public moments of the last --days (a paper out, a launch, leaving a
      job, the employer closing or acquired, open to work; app/happened.py), from what the read put on
      the timeline, and how many of those weeks are unread, with what reading them would cost. The
      LinkedIn profile read (research/private/social/linkedin-profiles.json) adds leaving a job and two
      years in the seat, this month or last.
  uv run python scripts/posts.py when [--subject ID ...] [--since DATE] [--until DATE] [--demo]
      Free. Day by day, when the engine would have said reach now (a note about their work, or a
      pitch), wait, or nothing, and why. --demo replays invented people from tests/fixtures/posts.
  uv run python scripts/posts.py check --labels FILE [--people FILE] [--max-calls 24]
      Paid, cached: reads the hand-labeled posts as read does (each person's role from --people, else
      the row's) and counts how often the reader's sign matches the label. Prints no post text.
  uv run python scripts/posts.py scorecard --people FILE [--seed 0] [--out DIR] [--model M] [--skip-unread]
      Free. The noise test (each sign in the 8 weeks before a moment against the same people's
      other 8-week stretches, on half the people) and the scorecard on the other half: moments seen
      coming, weeks early, false alarms, against random timing and waiting for the news. Only people
      scored count (scorecard.scoreable: a moment, not set aside, at least 12 posts over a recorded
      pull, and windows on both sides), only where their pull is complete, and only once their timeline
      holds exactly the current reading (--model's); --skip-unread leaves out, with the reason, anyone
      whose reading is not.

--people is the one people file the pull reads too (scripts/social_pull.py): a JSON list of
{person_id, name, role, x_handle, linkedin_url, github, since, until, moments: [{date, kind,
source_url, what}]}, with until and moments left out for the watchlist.

--db picks the store, else the timing store Today's calls and the daily run read (app/today.py TIMELINES). A
sqlite DATABASE_URL (the web app's store) that names another file stops the run instead of being used.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import contact, detectors, extraction, happened, journey, providers, readiness, routing, today  # noqa: E402
from app import scorecard as sc  # noqa: E402
from app.models import iso, parse_time  # noqa: E402
from app.store import Store  # noqa: E402

FIXTURES = Path("tests/fixtures/posts")
PRIVATE = Path("research/private/early-signals")
# The hand labels predate the early signs: an ask or a stated need is a technical ask, aligned work is work in progress.
LABEL_AS = {"asked_for_input": "technical_ask", "stated_need": "technical_ask", "aligned_work": "work_in_progress",
            "none": None}
OPENING = ("work_in_progress", "technical_ask", "just_submitted")
READ_MODEL = "claude-sonnet-5"  # tagging posts is a small task: the cheaper model, cached per week of posts
# READ_MODEL's list prices, US$ per million tokens (Anthropic's model table as of 2026-06-24): plan's default
# estimate (--usd-in, --usd-out) and what scripts/daily.py counts for reading.
USD_IN, USD_OUT = 2.0, 10.0
OVERHEAD_TOKENS, OUT_TOKENS = 800, 300  # per call: the output schema and framing; a week's answer, mostly empty


def targets(store, people, subjects):
    """(subject, role, types) per person to read. A person with a moment is read as the replay reads them (posts
    and stars); the watchlist with everything, GitHub included."""
    by_id = {p.subject_id: p for p in people}
    out = []
    for subject in subjects:
        ctx, person = journey.load_context(store, subject), by_id.get(subject)
        out.append(({"type": "person", "id": subject, "name": ctx.name if ctx else subject},
                    person.role if person else ctx.role if ctx else None,
                    detectors.REPLAY_TYPES if person and person.moments else detectors.READ_TYPES))
    return out


def read(store, jobs, max_calls, model, since=None):
    config, budget = _paid_settings(max_calls, model)
    needed = sum(extraction.unread(store, s, role=role, settings=config, types=types, since=since)
                 for s, role, types in jobs)
    print(f"{needed} calls to make for {len(jobs)} people (weeks already read are free)")
    if needed > max_calls:
        sys.exit(f"That is more than --max-calls {max_calls}. Raise it: a read cut short leaves older weeks "
                 "unread, and the scorecard does not run on them.")
    for subject, role, types in jobs:
        result = asyncio.run(extraction.read_posts(store, subject, role=role, settings=config, budget=budget,
                                                   types=types, since=since))
        print(f"{subject['id']}: {result['posts']} posts in {result['calls']} batches, {result['events']} events, "
              f"{result['retired']} retired", *result["errors"], sep="\n  ")
    print(f"{budget.used} model calls")
    return budget


def estimate_usd(todo, usd_in=USD_IN, usd_out=USD_OUT, worst=False):
    """The estimated cost of the calls for these unread batches: about 4 characters a token and a mostly empty
    answer, or with worst, 2 characters a token and each call's whole output limit (scripts/daily.py's cap)."""
    chars = 2 if worst else 4
    tokens_in = sum((len(extraction.POSTS_SYSTEM) + len(json.dumps(c))) // chars + OVERHEAD_TOKENS for c in todo)
    return (tokens_in * usd_in + len(todo) * (extraction.POSTS_MAX_TOKENS if worst else OUT_TOKENS) * usd_out) / 1e6


def moments_now(store, people, days, as_of, model, usd_in, usd_out, profiles=None):
    """Each watchlist person's public moments of the last ``days`` (happened.recent) and their unread weeks then."""
    settings, since = {"small_model": model}, (parse_time(as_of) - timedelta(days=days)).isoformat()
    todo = []
    for s, role, types in targets(store, people, [p.subject_id for p in people if not p.moments]):
        unread = extraction.pending(store, s, role=role, settings=settings, types=types, as_of=as_of, since=since)
        todo += unread
        call, found = happened.watch(store, s["id"], as_of, role, days, profiles)
        print(f"{s['id'][:30]:<31}{len(unread):>3} unread week(s)  " + ("; ".join(
            f"{happened.line(h)} {h.source_url}" for h in found) or "nothing public in the window")
            + (f" | readiness: {call.action.replace('_', ' ')}" if found and call else ""))
    print(f"\nReading the unread weeks: {len(todo)} calls with {model}, about ${estimate_usd(todo, usd_in, usd_out):.2f} "
          f"(assumed prices): posts.py read --people FILE --days {days} --max-calls {len(todo)}. "
          "The full read covers the same weeks at no extra cost.")


def plan(store, people, model, usd_in, usd_out):
    """What a read and a scorecard would do, without a call: per person and per role."""
    scored, skipped = sc.scoreable(store, people)
    records, rows, settings = sc.coverage(store), store.all("timeline_event"), {"small_model": model}
    read = {p.subject_id for p in scored} | {p.subject_id for p in people if not p.moments}
    jobs = {s["id"]: (s, role, types) for s, role, types in targets(store, people, [p.subject_id for p in people])}
    by_role = defaultdict(Counter)
    print(f"{'person':<22}{'role':<18}{'posts':>6}{'stars':>6}  {'posts from':<12}{'stars':<6}{'windows':>8}"
          f"{'calls':>6}  status")
    for p in people:
        s, role, types = jobs[p.subject_id]
        todo = extraction.pending(store, s, role=role, settings=settings, types=types) if p.subject_id in read else []
        start, keep_stars = sc.complete(p, records.get(p.subject_id))
        windows = sc.stretches(p, complete_from=start) if p.moments and start is not None else []
        before = sum(1 for *_, m in windows if m)
        t = by_role[p.role]
        t["people"] += 1
        t["scored"] += p in scored
        t["moments_with_a_window"] += before if p in scored else 0
        t["ordinary_windows"] += len(windows) - before if p in scored else 0
        t["calls"] += len(todo)
        t["usd"] += estimate_usd(todo, usd_in, usd_out)
        since = "no record" if p.subject_id not in records else "failed" if start is None else start or "the start"
        print(f"{p.subject_id[:21]:<22}{p.role[:17]:<18}{sc.posts_in_pull(rows, p):>6}"
              f"{sc.posts_in_pull(rows, p, ('github_star',)):>6}  {since:<12}{'kept' if keep_stars else 'out':<6}"
              f"{f'{before}+{len(windows) - before}' if p.moments else '-':>8}{len(todo):>6}  "
              f"{'scored' if p in scored else skipped[p.subject_id]}")
    print(f"\n{'role':<18}{'people':>7}{'scored':>7}{'moments with a window':>23}{'ordinary':>9}{'calls':>7}{'est. $':>8}")
    for role, t in sorted(by_role.items()):
        print(f"{role:<18}{t['people']:>7}{t['scored']:>7}{t['moments_with_a_window']:>23}{t['ordinary_windows']:>9}"
              f"{t['calls']:>7}{t['usd']:>8.2f}")
    calls, usd = sum(t["calls"] for t in by_role.values()), sum(t["usd"] for t in by_role.values())
    print(f"Read: {calls} calls with {model}, about ${usd:.2f} at ${usd_in:g} in and ${usd_out:g} out per million "
          "tokens (assumed prices; weeks already read cost nothing). Windows: before a moment + ordinary.")


def readings(store, people, model):
    """Per person, what keeps their timeline from being exactly the current reading's: batches not read yet,
    and tags the cached batches do not give (stale) or give but the timeline lacks (missing). Tags on repos and
    pushes are left out: the replay never reads them."""
    settings, problems, current = {"small_model": model}, {}, set()
    live_only = sc.unreplayed(store.all("timeline_event"))
    for s, role, types in targets(store, people, [p.subject_id for p in people]):
        current.add(extraction.reading(extraction.focus_for(role), settings, types))
        todo, expected = extraction.audit(store, s, role=role, settings=settings, types=types)
        held = {e["id"] for e in extraction.post_readings(store, s["id"]) if extraction.item(e) not in live_only}
        counts = {"unread": len(todo), "stale": len(held - expected), "missing": len(expected - held)}
        if any(counts.values()):
            problems[s["id"]] = {k: v for k, v in counts.items() if v}
    return problems, sorted(current)


def _paid_settings(max_calls, model):
    from app.main import settings  # the app's settings: ANTHROPIC_API_KEY from the environment, else .env

    config = {**settings(), "small_model": model}  # structured.ask reads posts with the small model
    budget = providers.Budget(config)
    budget.limit = max(1, max_calls)  # this script's own cap, typed on the command line; the app's 24 is per click
    return config, budget


def labeled(rows, roles):
    """({(subject id, name): posts}, {subject id: role}) from hand-label rows. A row without a person_id reads as a
    stand-in id from the person's name: a timeline event needs a subject id, and without one every tag is dropped."""
    people, role = {}, {}
    for row in rows:
        pid = row.get("person_id") or "label:" + "-".join(row["person"].lower().split())
        role.setdefault(pid, roles.get(pid) or row.get("role"))
        people.setdefault((pid, row["person"]), []).append({
            "source_url": row["url"], "event_date": row["date"][:10], "observed_at": f"{row['date'][:10]}T12:00:00+00:00",
            "quote": row["text"] + (extraction.REPLY_CONTEXT + row["in_reply_to"] if row.get("in_reply_to") else "")})
    return people, role


def check(labels, max_calls, model, roles):
    """Read the hand-labeled posts the way read does (batched per person, their role's focus) and count
    agreement; row numbers and tags only, never the text."""
    config, budget = _paid_settings(max_calls, model)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    store = Store(f"sqlite:///{PRIVATE / 'labels-check.sqlite'}")  # the read cache: a second check costs nothing
    rows = json.loads(Path(labels).read_text())
    people, role = labeled(rows, roles)
    read = defaultdict(set)
    for (person_id, name), posts in people.items():
        for i in range(0, len(posts), extraction.POSTS_PER_CALL):
            try:
                events = asyncio.run(extraction.extract_posts(
                    posts[i:i + extraction.POSTS_PER_CALL], {"type": "person", "id": person_id, "name": name},
                    settings=config, store=store, budget=budget, focus=extraction.focus_for(role[person_id])))
            except providers.ProviderError as error:
                sys.exit(str(error))
            for e in events:
                read[e.source_url].add(e.event_type)
    right = sign_right = 0
    for n, row in enumerate(rows, 1):
        tags = sorted(read[row["url"]] & set(extraction.SIGN_TYPES))
        expected, opening = LABEL_AS[row["label"]], [t for t in tags if t in OPENING]
        ok = expected in opening if expected else not opening
        right += ok
        sign_right += bool(expected) == bool(opening)
        print(f"{n:>3}  label {row['label']:<16} read {','.join(tags) or 'none':<40} {'ok' if ok else 'MISS'}")
    print(f"\n{right} of {len(rows)} match the label; {sign_right} of {len(rows)} agree on sign or no sign. "
          f"{budget.used} model calls.")


def pct(value):
    return "n/a" if value is None else f"{100 * value:.0f}%"


def save_people(store, people):
    """Each listed person's name, role and profiles, so read, when, route.py and the app know who they are; their
    subject ids."""
    for p in people:
        known = journey.load_context(store, p.subject_id)  # keep what a research build already found
        ctx = known.model_copy(update={"role": p.role, "anchors": {**p.anchors(), **known.anchors}}) if known else \
            journey.PersonContext(subject_id=p.subject_id, name=p.name or p.subject_id, role=p.role, anchors=p.anchors())
        journey.save_context(store, ctx)
    return [p.subject_id for p in people]


def store_of(db):
    """The store to read and write: --db, else the timing store. Before, a run with no DATABASE_URL wrote its
    readings into the web app's data/pilot.sqlite, where Today's calls never looks."""
    if db:
        return Store(db)
    named = contact._sqlite(os.getenv("DATABASE_URL"))
    if named and named.resolve() != today.TIMELINES.resolve():
        sys.exit(f"DATABASE_URL names {named}, but posts.py reads the timing store {today.TIMELINES}. Pass "
                 f"--db sqlite:///{named} to use that file, or unset DATABASE_URL. Nothing ran.")
    return today.ledger()  # the same file, its folder made first


def scorecard(store, moments, seed, out, model, skip_unread):
    listed = sc.load_moments(moments)
    people, skipped = sc.scoreable(store, listed)
    keyed = [p for p in listed if p.moments]
    problems, current = readings(store, people, model)
    if problems and not skip_unread:
        sys.exit(f"Not exactly the current reading ({', '.join(current)}): {problems}. Run posts.py read --people "
                 f"{moments} --model {model} (weeks already read are free), or --skip-unread to score without them.")
    for subject, problem in problems.items():
        skipped[subject] = f"reading incomplete: {problem}"
    people = [p for p in people if p.subject_id not in problems]
    print(f"{len(people)} of the {len(keyed)} people with a moment are scored; the method cannot see the others.",
          *(f"{s}: {why}" for s, why in skipped.items() if why != "watchlist"), sep="\n  ")
    records = sc.coverage(store)
    complete = {p.subject_id: sc.complete(p, records.get(p.subject_id)) for p in people}
    roles = {p.subject_id: p.role for p in people}
    observations = sc.replay_moments(store, people, readiness.score, others=[p.subject_id for p in listed],
                                     run=lambda view, subject, as_of: journey.detect(view, subject, as_of,
                                                                                     role=roles[subject]))
    if not detectors.FIELD:
        print(f"No field list at {detectors.FIELD_PATH}: new_field_contact cannot fire.")
    discovery, held_out = sc.split(people, seed)
    signs = sc.noise(observations, discovery)
    drop = sorted(s for s in sc.EARLY_SIGNS if not signs["signs"][s]["kept"])
    # Every sign selects nothing, so it is scored on everyone; only the kept signs need the half the test never saw.
    cards = {"all_signs": sc.scorecard(observations, None, seed),
             "kept_signs": sc.scorecard(sc.rescored(observations, readiness.score, set(drop)), held_out, seed)}
    field = detectors.FIELD_PATH
    report = {"generated_at": iso(), "config": {
                  "people": str(moments), "seed": seed, "stretch_days": sc.STRETCH.days,
                  "warm_up_days": sc.WARM_UP.days, "keep": [sc.KEEP_LIFT, sc.KEEP_P],
                  "reader": {"rubric": extraction.RUBRIC_VERSION, "model": model, "readings": current},
                  "replayed_types": list(detectors.REPLAY_TYPES),
                  "pull_complete": {s: {"posts_from": c[0], "stars": c[1]} for s, c in complete.items()},
                  "field": {"path": str(field), "sha256": hashlib.sha256(field.read_bytes()).hexdigest()
                            if field.exists() else None, "people": len(detectors.FIELD)}},
              "halves": {"noise_test": sorted(discovery), "scorecard": sorted(held_out)},
              "moments_in_key": len(keyed), "scored": len(people), "not_scored": skipped,
              "noise": signs, "dropped": drop, "scorecard": cards, "observations": observations}
    path = Path(out) / f"{date.today().isoformat()}-scorecard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1))

    w = signs["windows"]
    print(f"Noise test on {w['people']} people: {w['before_moment']} windows before a moment, {w['ordinary']} ordinary.")
    print(f"{'sign':<26}{'before':>12}{'ordinary':>12}{'lift':>7}{'p':>8}  kept")
    for sign, r in signs["signs"].items():
        lift = "n/a" if r["lift"] is None else f"{r['lift']:.1f}"
        print(f"{sign:<26}{r['before_moment']:>4} {pct(r['before_moment_rate']):>6} {r['ordinary']:>4} "
              f"{pct(r['ordinary_rate']):>6}{lift:>7}{r['p']:>8.3f}  {'yes' if r['kept'] else ''}")
    print(f"\nScorecard: every sign on all {len(people)} people; kept signs on the other {len(held_out)} "
          f"(not kept, not yet shown to beat an ordinary stretch: {', '.join(drop) or 'none'}).")
    print(f"{'policy':<22}{'seen coming':>13}{'median weeks early':>20}{'false alarms':>14}{'p':>7}")
    for label, card in (("engine, all signs", cards["all_signs"]), ("engine, kept signs", cards["kept_signs"]),
                        ("any reach, all signs", {"engine": cards["all_signs"]["any_reach"]})):
        e = card["engine"]
        if label == "engine, kept signs" and set(drop) >= set(sc.EARLY_SIGNS):
            print(f"{label:<22}{'n/a: no sign was kept':>33}")
            continue
        p = sc.edge_p(e)
        print(f"{label:<22}{e['seen_coming']:>5} of {e['moments']:<5}{str(e['lead_weeks']['median'] or 'n/a'):>20}"
              f"{pct(e['false_alarm_rate']):>14}{'n/a' if p is None else f'{p:.2f}':>7}")
    r = cards["all_signs"]["random_timing"]
    print(f"{'random timing':<22}{pct(r['seen_rate']):>13}{str(r['median_lead_weeks'] or 'n/a'):>20}{pct(r['false_alarm_rate']):>14}")
    print(f"{'waiting for the news':<22}{'0%':>13}{'0':>20}{'0%':>14}")
    print("p: the chance a gap over random timing this big is luck (one-sided Fisher, reaches before a moment "
          "against reaches in ordinary stretches). Above 0.05, the policy is not distinguishable from random timing.")
    print(f"wrote {path}")


def _state(call):
    if call is None:
        return "no timeline", ""
    if call.action == "reach_now":
        return f"reach now ({'note about their work' if call.track == 'rapport' else 'pitch'})", call.explanation
    if call.action in ("watch_until", "respect_follow_up"):
        return f"wait until {call.until[:10]}", call.explanation
    return call.action.replace("_", " "), call.explanation


def when(store, subjects, since, until):
    """Print each change of the engine's call from since to until, with the post it quotes."""
    for subject in subjects:
        ctx = journey.load_context(store, subject)
        print(f"\n{ctx.name if ctx else subject} ({ctx.role if ctx else 'no role'})")
        last, day = None, since
        while day <= until:
            as_of = f"{day.isoformat()}T23:59:59+00:00"
            call = journey.assess(store, subject, as_of, ctx.role if ctx else None)
            state, why = _state(call)
            if state != last:
                print(f"  {day}: {state}. {why}")
                by_id = {e["id"]: e for e in journey.view(store, subject, as_of)}
                for reason, ids in (call.evidence.items() if call else ()):
                    for e in (by_id[i] for i in ids[:1]):
                        print(f"      {e['event_date']} {reason}: \"{e['quote']}\" {e['source_url']}")
                last = state
            day += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("plan", "read", "happened", "when", "check", "scorecard"))
    parser.add_argument("--subject", action="append", help="subject id; default every person with a context")
    parser.add_argument("--max-calls", type=int, default=providers.MAX_CALLS_PER_RUN[1])
    parser.add_argument("--model", default=READ_MODEL, help="the model that reads posts (read, check, scorecard)")
    parser.add_argument("--since", type=date.fromisoformat, default=date.today() - timedelta(days=90))
    parser.add_argument("--until", type=date.fromisoformat, default=date.today())
    parser.add_argument("--demo", action="store_true", help="invented people, 1 July to 15 October 2026")
    parser.add_argument("--labels", type=Path, help="check: hand-labeled posts (JSON list)")
    parser.add_argument("--people", type=Path, help="the people file (see above)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, default=PRIVATE)
    parser.add_argument("--skip-unread", action="store_true",
                        help="scorecard: leave out, with the reason, people whose reading is incomplete")
    parser.add_argument("--usd-in", type=float, default=USD_IN, help="plan: assumed $ per million input tokens")
    parser.add_argument("--usd-out", type=float, default=USD_OUT, help="plan: assumed $ per million output tokens")
    parser.add_argument("--days", type=int, help="read: only the watchlist's last N days; happened: the window (30)")
    parser.add_argument("--as-of", help="happened: the day to look back from (default now)")
    parser.add_argument("--db", help="the store (a sqlite:/// URL); default the timing store, app/today.py TIMELINES")
    parser.add_argument("--profiles", type=Path, default=happened.PROFILES,
                        help="happened: the LinkedIn profile read (left a job, two years in the seat)")
    args = parser.parse_args()

    if args.command == "check":
        roles = {p.subject_id: p.role for p in sc.load_moments(args.people)} if args.people else {}
        return check(args.labels or parser.error("check needs --labels"), args.max_calls, args.model, roles)
    if args.command == "scorecard":
        return scorecard(store_of(args.db), args.people or parser.error("scorecard needs --people"), args.seed, args.out,
                         args.model, args.skip_unread)
    if args.command == "plan":
        store = store_of(args.db)
        people = sc.load_moments(args.people or parser.error("plan needs --people"))
        save_people(store, people)  # names and roles, as read saves them, so plan counts the same calls
        return plan(store, people, args.model, args.usd_in, args.usd_out)
    if args.command == "happened":
        store = store_of(args.db)
        people = sc.load_moments(args.people or parser.error("happened needs --people"))
        save_people(store, people)
        as_of = f"{args.as_of}T23:59:59+00:00" if args.as_of else iso()
        return moments_now(store, people, args.days or happened.WINDOW_DAYS, as_of, args.model, args.usd_in,
                           args.usd_out, happened.load_profiles(args.profiles))

    if args.demo:
        if args.command == "read":
            sys.exit("--demo replays fixtures that are already read; it makes no model call")
        store = Store(f"sqlite:///{tempfile.mkdtemp()}/posts-demo.sqlite")
        routing.seed(store, json.loads((FIXTURES / "timeline.json").read_text()))
        args.since, args.until = date(2026, 7, 1), date(2026, 10, 15)
    else:
        store = store_of(args.db)
    people = sc.load_moments(args.people) if args.people and not args.demo else []
    listed = save_people(store, people)
    subjects = args.subject or listed or sorted(r["subject_id"] for r in store.all("person_context"))
    if args.command == "read":
        if people:
            _, skipped = sc.scoreable(store, people)
            if dropped := {s: why for s, why in skipped.items() if why != "watchlist"}:
                print("Not read (not scored):", *(f"{s}: {why}" for s, why in dropped.items()), sep="\n  ")
            subjects = [s for s in subjects if s not in dropped]
        since = None
        if args.days and not people:
            parser.error("read --days needs --people, to tell the watchlist apart")
        if args.days:  # the watchlist's recent weeks only: what happened.recent needs read
            subjects = [s for s in subjects if not any(p.subject_id == s and p.moments for p in people)]
            since = (parse_time(iso()) - timedelta(days=args.days)).isoformat()
        read(store, targets(store, people, subjects), args.max_calls, args.model, since)
    else:
        when(store, subjects, args.since, args.until)


if __name__ == "__main__":
    main()
