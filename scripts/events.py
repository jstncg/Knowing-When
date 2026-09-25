"""The event loop on GI's own event records. It proposes only: it contacts no one and sends nothing.

  uv run python scripts/events.py [--live] [--json]
      The Events tab in the terminal: what past events cost and produced, who to add to the next one
      and who is held (and which team owns them), its expected cost, a brief per host, and the last
      event's "how did the chats go?" notes and follow-ups. Simulation, the default, reads the invented records in
      tests/fixtures/events with the invented people of Today's calls; --live reads
      research/private/events against the timelines store Today's calls reads.
  uv run python scripts/events.py --demo
      Simulation, then how two chats at the last event went is saved and the engine's calls change; a
      host marks one person as followed up and one as invited, and the contact ledger holds those people.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import events  # noqa: E402

# The demo's door notes (event, person, kind, what they said, host, wait until) and its invite.
DOOR_NOTES = [("gi-2026-09-salon", "D902", "open", "wants a real conversation about clip pipelines once their launch ships",
               "Luis Example", None),
              ("gi-2026-09-salon", "X901", "wait", "heads down on a deadline; happy to talk after mid-November",
               "Nora Example", "2026-11-16")]
MARKS = [("gi-2026-09-salon", "D902", "sent", "Luis Example"), ("gi-2026-09-games", "X900", "invited", "Nora Example")]


def money(value):
    return f"${value:,.0f}" if value is not None else "n/a"


def person(p, indent="    "):
    links = " · ".join(f"{link['label']} {link['url']}" for link in p["links"]) or "no public profile on file"
    return f"{indent}{p['name']} ({p['call']}): {p['why_them']}\n{indent}  {links}"


def invites(w):
    print(f"Who to invite to GI's next event, from {w['people_file']}:")
    for p in w["invite"]:
        print(person(p, "  "))
    for h in w["held"]:
        print(f"  not inviting {h['name']}: {h['reason']}")


def show(out):
    if out.get("watchlist"):
        invites(out["watchlist"])
    if out["missing"]:
        print(out["missing"])
        return
    print(f"{out['source']}\n\nPast events")
    for r in out["past"]:
        print(f"  {r['day']} {r['name']} ({r['format']}): {money(r['cost'])}, {r['came']} came of {r['said_yes']} yes, "
              f"{money(r['per_head'])} a head; qualified conversations: {r['qualified']} "
              f"({money(r['cost_per_qualified'])} each); into the pipeline within 90 days: {len(r['entered_pipeline'])}")
    if nxt := out["next"]:
        e, est = nxt["event"], nxt["estimate"]
        print(f"\nNext: {e['name']}, {e['city']}, {e['day']} ({nxt['said_yes']} of {e['capacity']} seats taken)")
        if est:
            print(f"  Expected cost: {money(est['mid'])} (range {money(est['low'])} to {money(est['high'])}) "
                  f"for ~{est['expected_guests']} guests, from {', '.join(est['based_on'])}")
        print("  Who to invite (a host sends the invite, then marks it):")
        for p in nxt["invite"]:
            print(person(p))
        for i in nxt["invited"]:
            print(f"    invited: {i['name']}, by {i['by']} on {i['at']}")
        for h in nxt["held"]:
            print(f"    not inviting {h['name']}: {h['reason']}")
        print("\nHost briefs")
        for b in nxt["briefs"]:
            print(f"  {b['host']}")
            for p in b["people"] + b["if_invited"]:
                print(f"    {p['name']} ({p['call']}{'; suggested, not invited yet' if p in b['if_invited'] else ''}): "
                      f"talk about {', '.join(p['talk_about']) or 'their work'}. {p['rule']}")
    if last := out["last"]:
        print(f"\nAfter {last['event']['name']} ({last['event']['day']}): how did the chats go?")
        for p in last["people"]:
            print(f"  {p['name']} ({p['call']}): {p['note'] or 'not saved yet'}")
        for f in last["follow_ups"]:
            state = (f"followed up by {f['sent']['by']} on {f['sent']['at']}" if f["sent"] else
                     f"due {f['due'][:16]}{' OVERDUE' if f['overdue'] else ''}" + (f", held: {f['hold']}" if f["hold"] else ""))
            print(f"  Follow-up to {f['name']}, {state}")
            if not f["hold"] and not f["sent"]:
                print("\n".join("    " + line for line in f["draft"]["body"].split("\n") if line))


def demo():
    show(events.page("simulation"))
    print("\nHow the chats went at the last event (simulated)")
    for event_id, sid, kind, text, host, until in DOOR_NOTES:
        r = events.add_note("simulation", event_id, sid, kind, text, host, until)
        print(f"  {sid} ({kind}): the engine now says {r['call']}"
              + (f"; still holding: {', '.join(r['holding'])}" if r["holding"] else ""))
    for event_id, sid, kind, host in MARKS:
        done = "as invited" if kind == "invited" else "as followed up"
        print(f"  {host} marks {sid} {done}: {events.mark('simulation', event_id, sid, kind, host)['hold']}")
    print()
    show(events.page("simulation"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="GI's real records in research/private/events")
    parser.add_argument("--demo", action="store_true", help="simulation, then how two chats went, a follow-up and an invite")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.demo:
        return demo()
    out = events.page("live" if args.live else "simulation")
    print(json.dumps(out, indent=2)) if args.json else show(out)


if __name__ == "__main__":
    main()
