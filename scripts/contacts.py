"""Record what happened with a person in the contact ledger (app/contact.py), or show their history.

The ledger is the timing store (today.TIMELINES: TIMELINES_DB, else the pull's folder's timelines.sqlite, which is
research/private/social on the Mac), the one Today's calls, route.py, the Slack buttons and the daily run read, so a
note here holds every card at once.

  uv run python scripts/contacts.py \
      record PERSON_ID KIND [--team TEAM] [--event ID] [--role ROLE] [--by NAME] [--until DAY] [--link URL]...
          [--note TEXT]
  ... scripts/contacts.py show PERSON_ID
  ... scripts/contacts.py pause --by NAME --why TEXT    stop every card and brief from posting to Slack
  ... scripts/contacts.py resume                        post again (route.py --send, the Slack app's brief)
  ... scripts/contacts.py import FILE
      FILE is a JSON list of facts about people, identity (or undergraduate) records:
      [{"person_id": "mts-research:ann-example", "kind": "identity",
        "links": ["https://x.com/ann_example", "https://github.com/ann-example"],
        "note": "Her X bio links this GitHub; the GitHub profile links the X account back."}]
      Each link is the page where the tie shows, on two different sites, and each must be one of the
      profiles the engine reads for them (their X, LinkedIn or GitHub from the people file, or a site on file) or a
      page of it: a Slack card stays at check first until two of those are tied. A row that ties fewer is
      still recorded, with a warning. Safe to re-run: a fact the person already has (same kind, links
      and until) is skipped. Nothing is written if a row is invalid.
      Outcomes (sent, replied, never, ...) happen on a day, so they go in one at a time with record.

KIND: sent, follow_up, replied, not_now (needs --until), never, wrong_person, identity (two --link
pages on different sites that tie the profile to them), checked (an old trigger still stands),
undergraduate (--until their expected graduation, if known), unconfirmed (whether they're still a student;
--note why: a card asks to check first and its draft names no role, and an event's host pitches nothing, until an
undergraduate entry settles it, with --until a past day if they graduated), invited (--until the event's day, --event its id) or
partner_staff (works at a GI partner or customer; --until they leave, if known) or hired (accepted GI's offer;
New starts records it, and hired --until today ends a wrong one). route.py --send writes "pinged" itself. TEAM is who owns the entry: recruiting (the default), events, sales or marketing.
This only writes the local store; it contacts no one.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import contact, journey, today  # noqa: E402
from app.models import iso, parse_time  # noqa: E402


def import_entries(store, rows):
    """Record each fact the person doesn't already have. Every row is checked before any is written."""
    kinds = {"identity", "undergraduate"}
    entries = []
    for n, row in enumerate(rows, 1):
        try:
            if not isinstance(row, dict) or row.get("kind") not in kinds:
                raise ValueError(f"each row is an object whose kind is one of {sorted(kinds)}")
            entries.append(contact.Entry(**{"at": iso(), **row}))
        except (TypeError, ValueError) as e:
            return f"Nothing imported: row {n}: {e}"
    added = 0
    for n, entry in enumerate(entries, 1):
        ctx = journey.load_context(store, entry.person_id) if entry.kind == "identity" else None
        read = [u for u in (ctx.anchors.values() if ctx else []) if u.startswith(("https://", "http://"))]
        past = contact.history(store, entry.person_id)
        wrong = max((parse_time(e["at"]) for e in past if e["kind"] == "wrong_person"), default=None)
        earlier = [e["links"] for e in past if e["kind"] == "identity" and (not wrong or parse_time(e["at"]) > wrong)]
        together = [x.links for x in entries if x.kind == "identity"  # this file's rows for the same person
                    and contact.person_key(x.person_id) == contact.person_key(entry.person_id)
                    and (not wrong or parse_time(x.at) > wrong)]
        if ctx and (loose := contact.untied([*earlier, *together], read) if read else ["none on file"]):
            print(f"Warning: row {n} ({entry.person_id}): no record ties {', '.join(loose)} to a page on another site, "
                  "so their card stays at check first.")
        known = {(e["kind"], frozenset(e["links"]), e["until"]) for e in contact.history(store, entry.person_id)}
        if (entry.kind, frozenset(entry.links), entry.until) not in known:
            contact.record(store, **entry.model_dump())
            added += 1
    print(f"{added} added, {len(entries) - added} already recorded")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("record")
    add.add_argument("person_id")
    add.add_argument("kind", choices=[k for k in contact.Kind.__args__ if k != "pinged"])
    add.add_argument("--team", default="recruiting", choices=contact.Entry.model_fields["team"].annotation.__args__)
    add.add_argument("--event", default="", help="the GI event's id, for invited")
    add.add_argument("--role", default="")
    add.add_argument("--by", default="")
    add.add_argument("--until", default=None)
    add.add_argument("--link", action="append", default=[])
    add.add_argument("--note", default="")
    show = sub.add_parser("show")
    show.add_argument("person_id")
    load = sub.add_parser("import")
    load.add_argument("file", type=Path)
    stop = sub.add_parser("pause", help="stop every card and brief from posting until resume")
    stop.add_argument("--by", required=True)
    stop.add_argument("--why", required=True)
    sub.add_parser("resume")
    args = parser.parse_args()

    if args.command == "pause":
        print(contact.said(contact.pause(args.by, args.why)), f"Recorded in {contact.PAUSE}.")
        return
    if args.command == "resume":
        was = contact.resume()
        print(f"Sending resumed; it was paused: {was['why']}" if was else "Sending was not paused.")
        return
    if args.command != "record" and not today.TIMELINES.exists():  # a note may come before the first pull
        sys.exit(f"No timing store at {today.TIMELINES} yet: pull first, or set TIMELINES_DB.")
    store = today.ledger()
    if args.command == "import":
        sys.exit(import_entries(store, json.loads(args.file.read_text())))
    if args.command == "record":
        try:
            entry = contact.record(store, args.person_id, args.kind, team=args.team, event_id=args.event,
                                   role_id=args.role, by=args.by, until=args.until, links=args.link, note=args.note)
        except ValueError as e:
            sys.exit(f"Not recorded: {e}")
        print(f"Recorded in {today.TIMELINES}:",
              json.dumps({k: v for k, v in entry.items() if v not in ("", [], None)}))
    history = contact.history(store, args.person_id)
    if args.command == "show":
        for e in history:
            print(e["at"][:10], e["kind"], e["team"], e["role_id"] or "-", e["by"] or "-", e["until"] or "", e["note"],
                  f"(recorded in {e['ledger']})" if e.get("ledger") else "")
    gate = contact.check(history, iso())
    print(f"Now: {gate.state}" + (f": {gate.reason}" if gate.reason else ""))


if __name__ == "__main__":
    main()
