"""Past lab joiners: what researchers who joined a frontier lab did before joining.

Input: research/private/answer-keys/mts-roster.json (gitignored; it names real
people, so it is not in git), a hand-checked list of researchers
who publicly joined a frontier lab (joiners) and comparable researchers who did
not move (stayers), each with a public source URL. Output: one JSON per group
with each person's arXiv events from the 180 days and OpenAlex events from the
730 days before the join date (stayers borrow the join date of the joiner at the same index so windows
are comparable). Public professional records only; nothing is contacted.

    uv run python scripts/mts_answer_key.py [--roster PATH] [--out DIR]
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.sources import arxiv, openalex  # noqa: E402
from app.sources.http import Fetcher  # noqa: E402

WINDOW_DAYS = 180
# OpenAlex reaches further back: a colleague's move is seen as two shared works up
# to two years apart (detectors.COLLEAGUE_GAP), and it costs one request either way.
# arXiv stays at WINDOW_DAYS because each revised paper costs an abs-page fetch.
OPENALEX_WINDOW_DAYS = 730


def resolve_author(fetch, person):
    """Pick the OpenAlex author whose affiliations mention a known employer, else the top hit."""
    hint = " ".join(filter(None, (person.get("prior_affiliation"), person.get("affiliation"), person.get("lab")))).lower()
    hits = openalex.search_authors(fetch, person["name"])
    for hit in hits:
        names = " ".join(a["institution"]["display_name"] for a in hit.get("affiliations", [])).lower()
        if any(token in names for token in hint.split() if len(token) > 3):
            return hit, "affiliation_match"
    return (hits[0] if hits else None), "top_hit"


def join_day(join_date):
    """A month-precision join date becomes the 1st, so nothing after the join leaks into the window."""
    return date.fromisoformat(join_date if len(join_date) == 10 else join_date[:7] + "-01")


def window(events, end, days=WINDOW_DAYS):
    start = end - timedelta(days=days)
    return [e for e in events if e["event_date"] and start.isoformat() <= e["event_date"][:10] <= end.isoformat()]


def collect(fetch, person, end, sources=("openalex", "arxiv")):
    since = (end - timedelta(days=WINDOW_DAYS)).isoformat()
    row = {**person, "window_end": end.isoformat(), "window_days": WINDOW_DAYS, "openalex_window_days": OPENALEX_WINDOW_DAYS,
           "openalex": None, "events": [], "errors": []}
    if person.get("join_date") and person["join_date"] != end.isoformat():
        row.update(join_date=end.isoformat(), join_date_stated=person["join_date"])  # the window ends on a full day
    hit = None
    try:
        hit, how = resolve_author(fetch, person)
    except Exception as error:
        row["errors"].append(f"openalex search: {type(error).__name__}: {error}")
    if hit:
        row["openalex"] = {"id": openalex.short_id(hit["id"]), "display_name": hit["display_name"], "match": how,
                           "affiliations": [a["institution"]["display_name"] for a in hit.get("affiliations", [])]}
    subject = row["openalex"]["id"] if hit else f"arxiv:{person['name']}"
    openalex_since = (end - timedelta(days=OPENALEX_WINDOW_DAYS)).isoformat()
    for name, pull, days in (
            ("openalex", lambda: openalex.events(hit["id"], subject, openalex_since, fetch) if hit else [], OPENALEX_WINDOW_DAYS),
            ("arxiv", lambda: arxiv.events(person["name"], subject, since, fetch=fetch), WINDOW_DAYS)):
        if name not in sources:
            continue
        try:
            row["events"] += window(pull(), end, days)
        except Exception as error:  # one dead source must not lose the other
            row["errors"].append(f"{name}: {type(error).__name__}: {error}")
    row["event_counts"] = {}
    for e in row["events"]:
        row["event_counts"][e["event_type"]] = row["event_counts"].get(e["event_type"], 0) + 1
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--roster", default=ROOT / "research/private/answer-keys/mts-roster.json")
    parser.add_argument("--out", default=ROOT / "research/private/answer-keys")
    parser.add_argument("--skip-arxiv", action="store_true", help="OpenAlex only (arXiv's API sheds bulk load with 406)")
    args = parser.parse_args()
    if not Path(args.roster).exists():
        sys.exit(f"No roster at {args.roster}. The roster names real people, so it is private and not in this "
                 "repository; put it at research/private/answer-keys/mts-roster.json or pass --roster.")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    roster = json.loads(Path(args.roster).read_text())
    joiners = [p for p in roster["joiners"] if p.get("join_date")]
    ends = [join_day(p["join_date"]) for p in joiners]
    groups = {"mts-joiners": zip(joiners, ends),
              "mts-stayers": zip(roster["stayers"], (ends[i % len(ends)] for i in range(len(roster["stayers"]))))}
    fetch = Fetcher()
    for name, pairs in groups.items():
        sources = ("openalex",) if args.skip_arxiv else ("openalex", "arxiv")
        rows = [collect(fetch, person, end, sources) for person, end in pairs]
        out = Path(args.out) / f"{name}.json"
        out.write_text(json.dumps({"window_days": WINDOW_DAYS, "count": len(rows), "people": rows}, indent=1))
        print(name, len(rows), "people,", sum(len(r["events"]) for r in rows), "events,",
              sum(bool(r["errors"]) for r in rows), "with errors ->", out)


if __name__ == "__main__":
    main()
