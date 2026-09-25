"""Fill timelines for past lab joiners (offline) or, fetching sources, for a cohort.

    uv run python scripts/build_timelines.py answer-keys [--trust-top-hit] [--name NAME]
    uv run python scripts/build_timelines.py reference  [--people research/private/journey-reference.json]
    uv run python scripts/build_timelines.py mts        [--answer-keys research/private/answer-keys]

answer-keys loads the events scripts/mts_answer_key.py already pulled for
researchers who joined a frontier lab and comparable stayers, without
fetching. The other cohorts run the source adapters. Options: --only a,b /
--skip a,b (adapter ids), --limit N people, --refresh, --db URL, and for paid
sources (off by default) --cap N paid calls per person plus
--crustdata-credits N for the whole run.

Reference people (a private JSON list of PersonContext fields) build into the
pilot store (DATABASE_URL or data/pilot.sqlite), where moment plans read them.
Answer-key people build into research/private/backtest/timelines.sqlite (gitignored),
reading the GI clock and Crustdata history from the pilot store. Prints one
line per person and a coverage table; the full report goes next to the database.
"""

import argparse
import asyncio
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import sys

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app import journey  # noqa: E402
from app.models import iso  # noqa: E402
from app.store import Store  # noqa: E402

PILOT_DB = "sqlite:///data/pilot.sqlite"
PRIVATE_DB = "sqlite:///research/private/backtest/timelines.sqlite"
KEYS = {"exa_key": "EXA_API_KEY", "crustdata_key": "CRUSTDATA_API_KEY"}


def keys():
    """Provider keys from the environment or .env; never printed."""
    local = dotenv_values(ROOT / ".env")
    return {k: os.getenv(env) or local.get(env) or "" for k, env in KEYS.items()}


def people(args, pilot):
    if args.cohort == "reference":
        return journey.reference_contexts(args.people, pilot)
    return [(ctx, as_of) for ctx, as_of, _ in journey.mts_contexts(args.answer_keys)]


def load_answer_keys(args, store):
    report = journey.load_answer_keys(store, args.answer_keys, trust_top_hit=args.trust_top_hit, name=args.name)
    for key, stats in report.items():
        print(f"{key:<11}" + "  ".join(f"{k} {v}" for k, v in stats.items() if not isinstance(v, (dict, list))))
        for title in ("rejected_by_reason", "no_events_by_reason"):
            if stats.get(title):
                print(f"  {title.replace('_', ' ')}:")
                for reason, n in stats[title].most_common():
                    print(f"    {n:>5}  {reason}")
        if stats.get("no_event_people"):
            print("  no-event people:\n" + "\n".join(f"    {p}" for p in stats["no_event_people"]))
    if not any(stats["people"] for stats in report.values()):
        sys.exit("No answer-key rows found; run scripts/mts_answer_key.py first.")


def line(report):
    marks = {"ok": "+", "empty": ".", "error": "E", "blocked": "B", "unavailable": "-", "skipped": "$"}
    cov = " ".join(f"{a}{marks[c['status']]}{c.get('events') or ''}" for a, c in report["coverage"].items())
    return f"{report['name'][:28]:<28} as_of {report['as_of'][:10]}  {cov}"


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cohort", choices=("answer-keys", "reference", "mts"))
    ap.add_argument("--trust-top-hit", action="store_true", help="keep OpenAlex authors matched on name alone")
    ap.add_argument("--name", help="answer-keys: load only people whose name contains this")
    ap.add_argument("--answer-keys", default=ROOT / "research/private/answer-keys")
    ap.add_argument("--people", default=ROOT / "research/private/journey-reference.json")
    ap.add_argument("--db")
    ap.add_argument("--only", default="")
    ap.add_argument("--skip", default="")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--cap", type=int, default=journey.DEFAULT_CAP)
    ap.add_argument("--crustdata-credits", type=int, default=0)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    pilot_url = os.getenv("DATABASE_URL") or PILOT_DB
    pilot = Store(pilot_url)
    url = args.db or (pilot_url if args.cohort == "reference" else PRIVATE_DB)
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    store = pilot if url == pilot_url else Store(url)
    if args.cohort == "answer-keys":
        load_answer_keys(args, store)
        print(f"store {url}")
        return

    unknown = {a for a in (args.only + "," + args.skip).split(",") if a} - journey.ADAPTERS.keys()
    if unknown:
        sys.exit(f"Unknown adapters {sorted(unknown)}; known: {', '.join(journey.ADAPTERS)}")
    only = {a for a in args.only.split(",") if a} or set(journey.ADAPTERS)
    only -= {a for a in args.skip.split(",") if a}

    cases = people(args, pilot)[:args.limit]
    if not cases:
        sys.exit(f"No people for {args.cohort}. The answer key needs scripts/mts_answer_key.py run first; "
                 "reference needs --people.")
    run = journey.Run(store, replay=args.cohort == "mts", settings=keys(), source_store=pilot,
                      crustdata_credits=args.crustdata_credits)
    reports, table, spent = [], defaultdict(Counter), 0
    for ctx, as_of in cases:
        report = await journey.build_journey(ctx, as_of, journey.Budget(args.cap), run=run, only=only,
                                             refresh=args.refresh)
        reports.append(report)
        spent += report["spent"]
        for adapter_id, cov in report["coverage"].items():
            table[adapter_id][cov["status"]] += 1
            table[adapter_id]["events"] += cov.get("events") or 0
        print(line(report), flush=True)

    print(f"\n{len(reports)} people, {spent} paid calls, store {url}\n")
    print(f"{'source':<20}" + "".join(f"{s:>12}" for s in (*journey.STATUSES, "events")))
    for adapter_id, counts in table.items():
        print(f"{adapter_id:<20}" + "".join(f"{counts[s]:>12}" for s in (*journey.STATUSES, "events")))
    details = {a: c["detail"] for r in reports for a, c in r["coverage"].items()
               if c["status"] in ("error", "blocked") and c.get("detail")}
    for adapter_id, detail in details.items():
        print(f"  {adapter_id}: {detail}")

    out_dir = Path(url.removeprefix("sqlite:///")).parent if url.startswith("sqlite:///") else ROOT / "data"
    out = out_dir / f"journey-{args.cohort}-{iso()[:19].replace(':', '')}.json"
    out.write_text(json.dumps({"cohort": args.cohort, "store": url, "reports": reports}, indent=1))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
