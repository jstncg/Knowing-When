"""Candidates for the watchlist: find some on GitHub, then score them against the admit gate (app/admit.py). Free.

    uv run python scripts/people.py find --role backend --query "game streaming" [--query ...] [--since DAY]
    uv run python scripts/people.py admit FILE [FILE ...] [--role ROLE] [--people FILE] [--team FILE] [--db URL]

find    GitHub's repo search for each --query, pushed to since --since (default 180 days ago): the people who own the
        matching repos, into <people file's folder>/candidates-<role>-<day>.json. Organizations are left out.
admit   reads candidates from any JSON list of people (find's output, or a research list: name, role, x_handle,
        linkedin_url, github, evidence_urls; --role for rows without one), leaves out anyone on the people file or
        GI's team file, and scores each one: their own posts on file in the timing store over the last year, their
        GitHub's repos and public work, and ties from what their GitHub profile states about them (the name and X
        it states count toward the people-file check too). Writes admit-<day>.md (a yes or no per person),
        admit-<day>-people.json (the rows a yes adds) and admit-<day>-identity.json (their identity records) beside
        the people file, and prints the daily pull's worst case with the admitted people added. It never changes the
        people file or the ledger: after Justin's yes, back up people.json, add the rows he approved, and run
        scripts/contacts.py import on those people's records.

GITHUB_TOKEN (the environment or .env, never printed) raises GitHub's limit from 60 calls an hour to 5,000: admit
spends four to six per candidate with a GitHub, two for an organization (none on anyone the list itself shows is on
the people file or the team), and stops reading GitHub at the limit, listing the rest as not read.
What it read is kept in admit-<day>-github.json, so a rerun the same day reads only the rest and writes the whole sheet
again: without a token, run it once an hour until nothing is left under "Not read".
"""

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import daily  # noqa: E402  the daily run's cost model
import social_pull  # noqa: E402
from app import admit, today  # noqa: E402
from app.models import iso, utcnow  # noqa: E402
from app.sources import apify, github  # noqa: E402
from app.sources.social import Person  # noqa: E402
from app.store import Store  # noqa: E402

GITHUB_ITEMS = 100  # repos and stars read per candidate; public activity is GitHub's last 300 events


def _read(path):
    data = json.loads(Path(path).read_text())
    return data.get("people", []) if isinstance(data, dict) else data


def find(args, http, token):
    since = args.since or (utcnow() - timedelta(days=180)).date().isoformat()
    out = Path(args.people).parent / f"candidates-{args.role}-{utcnow().date().isoformat()}.json"
    found = {row["github"]: row for row in (_read(out) if out.exists() else [])}  # a second run the same day adds
    for query in args.query:
        for row in github.search_owners(query, since, token, http):
            known = found.setdefault(row["github"], {**row, "role": args.role, "evidence_urls": []})
            known["evidence_urls"] = list(dict.fromkeys(known["evidence_urls"] + row["evidence_urls"]))
    out.write_text(json.dumps(list(found.values()), indent=1))
    print(f"{len(found)} people own repos matching {', '.join(args.query)} since {since} -> {out}")


def pull_worst(pullers):
    """The daily run's pull worst case for these people, as daily.py plans it for one day."""
    since = (utcnow() - timedelta(days=1)).date().isoformat()
    return daily.pull_worst_usd(apify.together_runs(pullers, since, daily.X_CAP, daily.LINKEDIN_PER_PERSON))


def keep(path, cache):
    """After each read, through a temporary file: a run stopped partway keeps what it read, and never half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(path)


def score(args, http, token):
    store = Store(args.db or f"sqlite:///{today.TIMELINES}")
    people = _read(args.people)
    team = json.loads(Path(args.team).read_text()).get("members", []) if Path(args.team).exists() else []
    candidates, not_read = [], []
    for path in args.files:
        rows, bad = admit.load(_read(path), args.role)
        candidates, not_read = candidates + rows, not_read + bad
    known_before, seen, results, limited = [], [], [], ""
    now, day = iso(), utcnow().date().isoformat()
    folder = Path(args.out or Path(args.people).parent)
    read = folder / f"admit-{day}-github.json"  # what GitHub said today, by login: a rerun reads only the rest
    try:
        cache = json.loads(read.read_text()) if read.exists() else {}
    except ValueError:  # a save cut off before this one: read everyone again
        cache = {}
    print("GitHub: read with GITHUB_TOKEN" if token else "GitHub: no GITHUB_TOKEN, so 60 calls an hour (about 15 "
          f"candidates); {len(cache)} read earlier today are not read again")
    for c in candidates:
        if why := admit.known(people, team, c):  # on file already by what the list says: GitHub is never read
            known_before.append((c.label(), why))
            continue
        about = raw = None
        if c.github and (hit := cache.get(c.github.lower())):
            about, raw = hit["about"], hit["raw"]
        elif c.github and limited:
            not_read.append((c.label(), limited))
            continue
        elif c.github:
            try:
                about = github.profile(c.github, token, http)
                raw = (github.fetch(c.github, GITHUB_ITEMS, token, http, starred=False)
                       if about["type"] != "Organization" else None)
                cache[c.github.lower()] = {"about": about, "raw": raw}
                keep(read, cache)
            except httpx.HTTPStatusError as err:
                if err.response.status_code in (403, 429):
                    limited = ("GitHub's hourly limit was reached: set GITHUB_TOKEN (5,000 calls an hour) or run "
                               "again in an hour")
                not_read.append((c.label(), limited or f"GitHub answered {err.response.status_code}"))
                continue
            except (httpx.HTTPError, ValueError) as err:  # a sign-in page instead of JSON is a ValueError
                not_read.append((c.label(), f"GitHub could not be read ({type(err).__name__})"))
                continue
        full = admit.enriched(c, about) if about else c  # with the name and X their GitHub states
        if why := admit.known(people, team, full) or next(
                (f"also proposed as {s.label()}" for s in seen if admit.same_person(s, full)), ""):
            known_before.append((full.label(), why))
            continue
        seen.append(full)
        posts, missing = admit.on_file(store, full, now)
        results.append(admit.assess(c.model_copy(update={"name": full.name}), posts=posts, missing=missing, raw=raw,
                                    about=about, as_of=now))
    watched, _ = daily.watchlist(args.people, store, utcnow())
    rows, identity = admit.proposals(results, utcnow().date().isoformat(),
                                     [row.get("person_id") or row.get("subject_id") or "" for row in people])
    added = [Person.from_dict(row) for row in rows]
    before, after = pull_worst(watched), pull_worst(watched + added)
    cost = (f"The daily pull's worst case is ${before:.3f} a run for the {len(watched)} watched now, and ${after:.3f} "
            f"with the {len(added)} admitted below: about ${30 * after:.2f} a month against the ${daily.CAP_USD:.2f} "
            f"cap, before reading. The X search takes at most {daily.X_CAP} posts a run for everyone together, so more "
            "people on X share it more thinly rather than cost more.")
    sheet = folder / f"admit-{day}.md"
    sheet.write_text(admit.sheet(results, known_before, not_read, cost, day))
    (folder / f"admit-{day}-people.json").write_text(json.dumps(rows, indent=1))
    (folder / f"admit-{day}-identity.json").write_text(json.dumps(identity, indent=1))
    counts = {v: sum(r.verdict == v for r in results) for v in admit.HEADINGS}
    print(", ".join(f"{n} {v}" for v, n in counts.items()) + f"; {len(known_before)} already known; {len(not_read)} not read")
    print(cost)
    print(f"-> {sheet}, and the rows and identity records a yes adds beside it. Nothing was added to the people file "
          "or the ledger.")
    if limited:
        sys.exit(limited)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("find", "admit"))
    ap.add_argument("files", nargs="*", help="admit: JSON lists of candidates")
    ap.add_argument("--role", help="find: the role searched for; admit: the role of rows that name none")
    ap.add_argument("--query", action="append", default=[], help="find: repeat for each search")
    ap.add_argument("--since", help="find: repos pushed to since this day (default 180 days ago)")
    ap.add_argument("--people", default=str(daily.PEOPLE), help="the people file")
    ap.add_argument("--team", default=str(ROOT / "research/private/gi-team.json"), help="GI's team file")
    ap.add_argument("--db", help="the timing store (default today.TIMELINES)")
    ap.add_argument("--out", help="admit: where the sheet goes (default the people file's folder)")
    args = ap.parse_args()
    token = social_pull.key("GITHUB_TOKEN")
    http = httpx.Client(timeout=30)
    if args.command == "find":
        args.role = admit.role_id(args.role)
        if not (args.role and args.query):
            sys.exit(f"find needs --role (one of {', '.join(sorted(admit.ROLES))}) and at least one --query")
        return find(args, http, token)
    if not args.files:
        sys.exit("admit needs at least one candidates file")
    score(args, http, token)


if __name__ == "__main__":
    main()
