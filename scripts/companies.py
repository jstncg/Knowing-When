"""The company watcher (app/companies.py): what just happened at the companies GI hires from, and who GI would want
there, so GI can approach several people at one company at once. A preview: it posts nothing and changes no one's
call. Today's calls shows the same read.

  uv run python scripts/companies.py --demo
      The invented read, as of 2026-09-15.
  uv run python scripts/companies.py list
      The watched companies, each open to a team approach, asking sales first or held, and why. Reads nothing
      online.
  uv run python scripts/companies.py read [--pages 2]
      Free, on the Mac (the cloud blocks Google News and OpenAlex): two news searches per company, one every 2 s,
      and at most 7 x --pages OpenAlex search pages, about 10 of its 1,000 free credits a day each. Writes
      research/private/companies/moments-<day>.json, then prints the preview.
  uv run python scripts/companies.py
      The newest read against today's calls.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import accounts, companies, inbox, today  # noqa: E402
from app.models import iso  # noqa: E402
from app.sources import openalex  # noqa: E402
from app.sources.http import Fetcher  # noqa: E402

def _live():
    found = today.source("live")
    if not found:
        sys.exit(f"No timelines store at {today.TIMELINES}: run the pull first.")
    allowed, unlisted, _ = inbox.watchlist("live")
    if unlisted:
        print(unlisted, "No watched person's employer is read.")
    return found[0], allowed


def show(mode):
    p = today.calls(mode, companies=True)["companies"]
    if p["missing"]:
        sys.exit(p["missing"])
    print(f"Company moments from {p['since']} to {p['read_on']}: {len(p['companies'])} companies. A preview: nothing "
          "is sent and no one's call changes.")
    for c in p["companies"]:
        print(f"\n{c['company']}: {c['stance']}. {c['why']}".rstrip())
        for m in c["moments"]:
            more = m["headlines"] - 1
            print(f"  {m['day']}  {m['label']}: \"{m['quote']}\"  {m['source_url']}"
                  + (f"  (and {more} more headline{'' if more == 1 else 's'})" if more else ""))
        for w in c["people"]:
            print(f"  Works there, watched for {w['role']}: {w['name']} (today: {w['call']})")
        if c["authors"]:
            print("  Authors on GI's work there: " + ", ".join(
                f"{a['name']} ({a['papers']} paper{'' if a['papers'] == 1 else 's'})" for a in c["authors"]))
    for e in p["errors"]:
        print(f"Not read: {e}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true", help="the invented read, as of 2026-09-15")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="the watched companies")
    reading = sub.add_parser("read", help="fetch the last month's moments (free, on the Mac)")
    reading.add_argument("--pages", type=int, default=companies.PAGES, help="OpenAlex pages per GI topic")
    args = parser.parse_args()
    if args.demo:
        return show("simulation")
    if args.command == "list":
        store, allowed = _live()
        for c in companies.watched("live", store, allowed):
            print(f"- {c.name}: {c.stance}. {c.why}".rstrip())
        return
    if args.command == "read":
        searches = len(accounts.TOPICS) * args.pages
        store, allowed = _live()
        watched = companies.watched("live", store, allowed)
        print(f"{len(watched)} companies: {2 * len(watched)} news searches and at most {searches} OpenAlex pages, about "
              f"{searches * openalex.CREDITS_A_PAGE} of its 1,000 free credits today. Free.")
        result = companies.read(Fetcher(refresh=True), watched, iso()[:10], pages=args.pages)
        print(result["openalex"], f"Wrote {companies.save(result)}.")
    show("live")


if __name__ == "__main__":
    main()
