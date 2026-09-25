"""Route reach_now people to a ping card: route in, channel, confidence, what would prove it wrong, and a checked
draft. Nothing sends by default. Each card says what it is about: an Early sign, something that Just happened (a
public moment of the last month), or a plain Reach now. Everyone else with such a moment is listed after the cards
with readiness's call (app/happened.py). The LinkedIn profile read (--profiles,
research/private/social/linkedin-profiles.json when it exists) adds leaving a job and two years in the seat to what
is listed; it never changes who is reached.

  uv run python scripts/route.py --demo [--fixtures tests/fixtures/posts ...] [--as-of DATE]
      Invented people (tests/fixtures/routing, posts and moments by default: one per kind of message, as of
      2026-09-15); prints each card and a Block Kit Builder link to see it.
  uv run python scripts/route.py --people research/private/early-signals/people.json
      [--team research/private/gi-team.json] [--contacts research/private/contacts.json] [--subject ID] [--as-of DATE]
      Real people, from the timing store (today.TIMELINES: TIMELINES_DB, else the pull's folder's, on the Mac
      research/private/social/timelines.sqlite), whose ledger the Slack app's buttons and scripts/contacts.py write
      to; pings go to research/private/routing/.
  --contacts holds routes found by hand (an email on their site); their X and LinkedIn come from the people
  file's profiles (saved in the store by posts.py) whenever it has none for them.
  People are ranked strongest first (routing.rank); --top N keeps the first N per role, and no role
  goes past its weekly cap (contact.WEEKLY_CAP cards a week, counting cards already sent); across all
  roles the week holds contact.TOTAL_CAP cards, and the strongest keep them.
  --people FILE routes only that people file's watchlist (its rows with no moment, and nobody with a moment row under
      any id, as the Monday brief keeps them): today's calls. --subject only narrows it, and --send for real people
      needs it, so a replay case is never carded.
  Anyone the contact gate holds or asks to check first is listed with why, never carded; so are the
  follow-ups due. Outcomes (sent, replied, not now, never, identity) go in with scripts/contacts.py.
  --scorecard FILE: confidence from a scripts/posts.py scorecard report; by default the newest one in
      research/private/early-signals/, else every ping says "not measured yet".
  --draft: paid. The model writes each kept person's draft (app/outreach.py, Opus 5 unless --model) and a second
      call checks every factual statement in it against their items and GI's facts; one repair round when a check
      fails; all cached (research/private/routing/drafts.sqlite, so a rerun on the same person and moment pays
      nothing); --max-calls caps the calls. The last line says what the run spent, at Opus 5's list price. Without
      it, the free template.
  --redraft: paid, within --max-usd ($1.00) a run. The free template first; the model writes a draft only for
      a card whose free draft fails its checks (routing.redraft), checked the same way, and the card stays held
      if it still fails. A cached draft is free and always used; one that needs a new call is tried only while
      the run's spend plus that draft's worst case (outreach.worst_usd) fits the cap, so with --max-usd 0 only
      cached drafts are used. A model error holds that card and the run goes on. Without ANTHROPIC_API_KEY it
      says so once and holds those cards, as without the flag.
  --recheck: free. For each saved ping in --out, whether what would prove it wrong has happened by --as-of or
      today. Run it after a fresh pull, so the timeline has the news.
  --send posts the morning list (who to reach, one line each) to GI's channel with each card in its thread, and
  records each card in the contact ledger; nothing posts when no one is new. It posts today's cards, so not
  with --as-of. Only with Justin's go-ahead. It goes through the Slack app when SLACK_BOT_TOKEN and SLACK_CHANNEL
  are set (docs/slack-app-setup.md), so the card has its ledger buttons (I sent it, Not now, Do you know them?);
  else to the webhook in SLACK_ROUTING_WEBHOOK (the environment, else .env).
  --demo --send posts the invented people's morning list and cards, each marked as a test with made-up people.
  They are recorded only in the demo's throwaway store, never the real ledger, and their buttons record nothing:
  a way to see the cards in Slack.
  --inbox instead builds the week's Monday brief (app/inbox.py: the few decisions from Today's calls, the
  Events page, New starts and the Hiring budget, most urgent first; with --demo, the invented people) and prints
  its post; with --send it posts the week as app/weekly.py does: the brief as one post, which contacts no one,
  and in its thread each reach's card (through the gates, recorded in the ledger as --send records a card; with
  --demo, only in the demo's throwaway store), each draft and each button; on a month's first Monday the
  one-pager too. With nothing to decide it prints that and posts nothing.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import (contact, happened, inbox, journey, outreach, pay, ping, routing, slack, starts, today,  # noqa: E402
                 weekly)
from app import scorecard as sc  # noqa: E402
from app.models import iso  # noqa: E402
from app.sources import x_follows  # noqa: E402
from app.store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path("tests/fixtures/routing")
POSTS = Path("tests/fixtures/posts")  # early signs in their own posts
MOMENTS = Path("tests/fixtures/moments")  # an acquisition, a launch, open to work: with the two above, the demo's default
SCORECARDS = Path("research/private/early-signals")


DRAFTS = ROOT / "research/private/routing/drafts.sqlite"  # the model drafts' cache: a rerun pays nothing twice
NO_KEY = "Model drafts are off: ANTHROPIC_API_KEY isn't set, so a card whose free draft fails a check stays held."


def _writer(cache, model, max_calls, as_of, max_usd=None):
    """(outreach.drafter for route(), its budget), with the paid settings scripts/posts.py uses, capped at
    --max-calls (and at ``max_usd``, --redraft's) and cached in ``cache``. (None, the budget) when there is no
    model key: the key is only looked for, never read out."""
    from posts import _paid_settings  # scripts/ is on the path when this script runs

    config, budget = _paid_settings(max_calls, model)
    if not config.get("anthropic_key"):
        return None, budget
    return outreach.drafter(config, budget, cache, model, as_of, max_usd), budget


def recheck(store, out, as_of):
    """Only the fields a recheck needs, so a ping saved in an older format still gets checked."""
    for path in sorted(out.glob("*.json")):
        try:
            saved = json.loads(path.read_text())
            subject, name, made, falsifiers = saved["subject_id"], saved["name"], saved["call"]["as_of"], saved["falsifiers"]
        except (ValueError, KeyError, TypeError):
            print(f"{path.name}: not a saved ping with falsifiers, skipped; route again to recheck it")
            continue
        print(f"{name} (reach now {made[:10]})")
        events = journey.view(store, subject, as_of)  # the view the call read, as ping.status takes it
        for f in falsifiers:
            print(f"  {ping.status(f, events, as_of):<13}{f['claim']}")


def webhook_url():
    """SLACK_ROUTING_WEBHOOK from the environment, else .env, like the other scripts' keys; never printed."""
    return os.getenv("SLACK_ROUTING_WEBHOOK") or dotenv_values(ROOT / ".env").get("SLACK_ROUTING_WEBHOOK") or ""


NO_TARGET = "--send needs the Slack app (SLACK_BOT_TOKEN and SLACK_CHANNEL) or SLACK_ROUTING_WEBHOOK"


def target():
    """Where --send posts, said once: the Slack app when it is set up, else the webhook; '' when neither. The token
    and the webhook are never printed."""
    try:
        settings = slack.load()
    except ValueError as e:  # a token in the wrong setting
        sys.exit(str(e))
    if poster := slack.poster(settings):
        print("Posting as the Slack app: each card has the ledger buttons.")
        return poster
    if settings["SLACK_BOT_TOKEN"] and not settings["SLACK_CHANNEL"]:
        print("SLACK_BOT_TOKEN is set but SLACK_CHANNEL is not, so not the Slack app.")
    if hook := webhook_url():
        print("Posting to the webhook: the cards have no buttons.")
    return hook


def send_inbox(mode, send):
    """The week as app/weekly.py posts it: the brief as one post, what to act on in its thread."""
    found = today.source(mode)
    week = weekly.week(mode, found[1] if found else None)[0]
    if week["missing"]:
        sys.exit(week["missing"])
    card = weekly.post(week)
    alert = weekly.pull_alert(inbox.fresh(week)) if card is None else None
    if card is None and alert is None:  # nothing to decide this week: the brief stays silent
        print(f"Nothing to decide for the week of {week['week_of']}: no post.")
        return
    if alert is not None and not send:  # nothing to decide, but the pull is broken: one line says so
        print(f"Nothing to decide for the week of {week['week_of']}, but the pull is broken; --send posts one line "
              f"(once per problem):\n{alert['text']}")
        return
    if not send:
        print(json.dumps(card, indent=2))
        replies = sum((d["thread"] or {}).get("kind") == "reach" or weekly.reply(d, n, mode) is not None
                      for n, d in enumerate(week["decisions"][:inbox.BRIEF_MAX], start=1))
        print(f"In its thread: {replies} {'reply' if replies == 1 else 'replies'}, each card, draft and button.")
        if mode == "simulation":  # the link carries the card, so real names never go in one
            print("See it rendered (nothing is posted):", routing.builder_url(card))
        return
    if not (to := target()):
        sys.exit(NO_TARGET)
    try:  # the invented people's week goes to Slack marked as a test
        posted = weekly.publish(to, mode)
    except (ValueError, RuntimeError) as e:  # Slack refused it, or sending was paused since this run began
        sys.exit(f"Brief not posted: {e}")
    if alert is not None:
        print("Posted the broken-pull line." if posted else "The broken-pull line was said already: not again.")
        return
    print(f"Posted the Monday brief for the week of {week['week_of']}"
          + (", marked as a test with made-up people." if mode == "simulation" else "."))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true", help="invented people, as of 2026-09-15 unless --as-of")
    parser.add_argument("--fixtures", type=Path, action="append",
                        help="with --demo: a folder of invented people to load (repeatable); default the routing and "
                             "moments fixtures")
    parser.add_argument("--top", type=int, default=None, help="keep the first N per role")
    parser.add_argument("--team", type=Path, default=Path("research/private/gi-team.json"))
    parser.add_argument("--contacts", type=Path, default=Path("research/private/contacts.json"))
    parser.add_argument("--subject", action="append", help="subject id; default every person with a context")
    parser.add_argument("--people", type=Path, help="the people file: route its watchlist only")
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--out", type=Path, default=Path("research/private/routing"))
    parser.add_argument("--send", action="store_true", help="post cards to SLACK_ROUTING_WEBHOOK")
    parser.add_argument("--scorecard", type=Path, help="a scripts/posts.py scorecard report, for confidence")
    parser.add_argument("--draft", action="store_true", help="paid: the model writes each draft")
    parser.add_argument("--model", default=outreach.MODEL)
    parser.add_argument("--max-calls", type=int, default=30, help="with --draft or --redraft: the most model calls")
    parser.add_argument("--redraft", action="store_true",
                        help="paid: the model writes a draft only when the free one fails its checks")
    parser.add_argument("--max-usd", type=float, default=1.00, help="with --redraft: the most this run spends")
    parser.add_argument("--recheck", action="store_true", help="free: check saved pings' falsifiers")
    parser.add_argument("--profiles", type=Path, default=happened.PROFILES,
                        help="the LinkedIn profile read (social_pull.py profiles): left a job, two years in the seat")
    parser.add_argument("--follows", type=Path, default=x_follows.FOLLOWS,
                        help="the X follows pull (social_pull.py follows): whether they follow the sender on X")
    parser.add_argument("--inbox", action="store_true", help="the week's Monday brief card instead of people's cards")
    args = parser.parse_args()
    if args.redraft and not args.draft and args.model != outreach.MODEL:
        parser.error(outreach.UNPRICED)
    if args.send and args.as_of and not args.demo:
        sys.exit("--send posts today's cards for real people, so not with --as-of")
    if args.send and not (args.demo or args.inbox or args.people):
        sys.exit("--send posts real people's cards, so it needs --people: the people file says who is on the "
                 "watchlist, and a replay case never gets a card. Nothing was posted.")
    if args.send and (pause := contact.paused()):
        sys.exit(f"{contact.said(pause)} Nothing is posted until: uv run python scripts/contacts.py resume")
    if args.inbox:
        return send_inbox("simulation" if args.demo else "live", args.send)

    if args.demo:
        store = Store(f"sqlite:///{tempfile.mkdtemp()}/demo.sqlite")
        demo_contacts = {}
        for folder in args.fixtures or [FIXTURES, POSTS, MOMENTS]:
            routing.seed(store, json.loads((folder / "timeline.json").read_text()))
            if (ledger := folder / "ledger.json").exists():  # invented contact history: who is confirmed, who said never
                for entry in json.loads(ledger.read_text()):
                    contact.record(store, **entry)
            if (found := folder / "contacts.json").exists():
                demo_contacts |= {k: v for k, v in json.loads(found.read_text()).items() if k != "note"}
        args.team, args.contacts, args.out = FIXTURES / "team.json", None, None
        args.as_of = args.as_of or "2026-09-15"
    else:  # the timing store, whose ledger the Slack app's buttons and scripts/contacts.py write to
        if not today.TIMELINES.exists():
            sys.exit(f"No timing store at {today.TIMELINES}: pull first, or set TIMELINES_DB.")
        store = today.ledger()
        starts.hold_all("live", store, iso())  # whoever accepted an offer (New starts) is never carded
    as_of = f"{args.as_of}T00:00:00+00:00" if args.as_of else iso()
    if args.recheck:
        return recheck(store, args.out or parser.error("--recheck reads saved pings, so not with --demo"), as_of)
    profiles = happened.load_profiles(args.profiles) if not args.demo else {}
    follows = x_follows.load(args.follows) if not args.demo else {}
    team = json.loads(args.team.read_text())["members"] if args.team.exists() else []  # no team file: no warm paths
    contacts = demo_contacts if args.demo else json.loads(args.contacts.read_text()) if args.contacts.exists() else {}
    everyone = sorted(r["subject_id"] for r in store.all("person_context"))
    watchlist = inbox.listed(sc.load_moments(args.people)) if args.people else []  # the brief's own selection
    # A people file routes its watchlist only, even when that is nobody: never everyone with a context.
    if args.people:  # --subject narrows the watchlist, never adds to it
        subjects = [s for s in args.subject if s in watchlist] if args.subject else watchlist
    else:
        subjects = args.subject or everyone
    newest = sorted(SCORECARDS.glob("*-scorecard.json"))[-1:] if not args.demo else []
    scorecard = json.loads((args.scorecard or newest[0]).read_text()) if args.scorecard or newest else None
    cohort = {s: ((ctx.name if (ctx := journey.load_context(store, s)) else s),
                  outreach.written_by(journey.view(store, s, as_of), s)) for s in everyone}  # for the name swap
    paid = args.draft or args.redraft
    if paid and not args.demo:
        DRAFTS.parent.mkdir(parents=True, exist_ok=True)
    cache = store if args.demo else Store(f"sqlite:///{DRAFTS}") if paid else None
    capped = args.max_usd if args.redraft and not args.draft else None  # --draft: only --max-calls
    writer, budget = _writer(cache, args.model, args.max_calls, as_of, capped) if paid else (None, None)
    if paid and writer is None:
        print(NO_KEY)
    to = target() if args.send else ""
    if args.send and not to:
        sys.exit(NO_TARGET)

    def one(subject, writer=None):
        ctx = journey.load_context(store, subject)
        return routing.route(store, subject, as_of, team, contacts.get(subject, []),
                             profile_url=(ctx.anchors.get("homepage", "") if ctx else ""),
                             scorecard=scorecard, cohort=cohort, writer=writer, profiles=profiles, follows=follows,
                             bands=pay.for_mode("simulation" if args.demo else "live"))  # never GI's pay on made-up people

    found = [r for subject in subjects if (r := one(subject))]
    if writer and args.redraft and not args.draft:  # the model tries each free draft that fails, then the picking
        found, not_tried, failed = routing.redraft(found, lambda r: one(r.subject_id, writer))
        for note in filter(None, [not_tried, *failed]):
            print(note.rstrip(".") + ".")
    kept, quiet = routing.pick(store, found, as_of, top=args.top, drafted=bool(writer) and args.draft)
    if writer and args.draft:  # the paid drafts, only for the people kept
        kept = [one(r.subject_id, writer) or r for r in kept]
    for result in kept:
        subject = result.subject_id
        print(f"{routing.HEADER[result.kind]}: {result.name} ({result.role['id']}, {result.call.track}"
              f"{', ' + happened.line(result.happened[0]) if result.happened else ''}): from {result.sender['name']} "
              f"by {result.channel.kind} -> {result.channel.target or '-'} | route in: {result.route_in['level']} | "
              f"draft {'ready' if result.outreach['checks']['passes'] else 'not ready'} | {result.channel.reason}")
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / f"{subject.replace('/', '_').replace('|', '_')}.json").write_text(result.model_dump_json(indent=2))
        else:
            print(json.dumps(result.card, indent=2))
            print("See it rendered (nothing is posted):", routing.builder_url(result.card))
    refused = 0
    if to:  # the morning list, each card in its thread; the demo's made-up people marked as tests, in its own store
        held = []
        try:
            sent = routing.send_morning(store, kept, to, now=as_of if args.demo else None, test=args.demo, held=held)
        except (ValueError, RuntimeError) as e:  # paused since the run began, or Slack refused the list: never a token
            sys.exit(f"Not posted: {e}")
        refused = len(kept) - len(sent)
        why, posted = {r.subject_id: reason for r, reason in held}, {r.subject_id for r, _ in sent}
        for result in [r for r in kept if r.subject_id not in posted]:
            checks = result.outreach["checks"]
            print(f"Not sent: {result.name}: " + (why.get(result.subject_id) or (
                f"the draft fails a check ({'; '.join(checks['problems'])})." if not checks["passes"] else
                "the ledger changed since routing, or a cap filled, so it isn't on the list.")))
        if not sent:  # held is only filled for cards that failed after the list went
            print(f"Posted the morning list, but none of its {len(held)} card{'s' if len(held) != 1 else ''} went: "
                  "see Not sent above." if held else "Nothing new to reach: nothing posted.")
        else:
            print(f"Posted the morning list with {len(sent)} card{'s' if len(sent) != 1 else ''} in its thread"
                  + (": made-up people, marked as tests; the real ledger is untouched." if args.demo else "."))
    for result in quiet:
        print(f"Kept quiet: {result.name} ({result.role['id']}): "
              + ("check first. " if result.gate.state == "check_first" else "") + result.gate.reason)
    for person_id, reason in contact.follow_ups_due(store, as_of):
        print(f"Follow-up due: {person_id}: {reason}")
    seen, listed = {contact.person_key(r.subject_id) for r in found}, []
    for subject in subjects:
        if (key := contact.person_key(subject)) in seen or \
                contact.check(contact.history(store, subject), as_of).state == "hold":
            continue  # a reach (for any role) is carded or kept quiet above; the contact gate holds this person
        seen.add(key)  # one line per person, whatever roles they are watched for
        ctx = journey.load_context(store, subject)
        call, fresh = happened.watch(store, subject, as_of, ctx.role if ctx else None, profiles=profiles)
        if fresh:
            action = call.action if call else "no call"
            listed.append({"subject_id": subject, "name": ctx.name if ctx else subject, "action": action,
                           "why": call.explanation if call else "", "happened": [h.model_dump() for h in fresh]})
            print(f"Just happened, not a reach: {listed[-1]['name']}: {happened.line(fresh[0])} {fresh[0].source_url}"
                  f" | readiness: {action.replace('_', ' ')}")
    if args.out and listed:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "just-happened.json").write_text(json.dumps(listed, indent=2))
    print(f"{len(found)} of {len(subjects)} people are reach_now as of {as_of[:10]}; "
          f"{len(kept) - refused} carded, {len(quiet) + refused} kept quiet; {len(listed)} more had a public moment "
          "in the last month" + (f"; {budget.used} model calls, ${outreach.usd(budget.tokens):.4f}" if budget else ""))


if __name__ == "__main__":
    main()
