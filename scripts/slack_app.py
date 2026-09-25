"""Run GI's Slack app on this machine: the buttons on the cards and the Home tab. Setup: docs/slack-app-setup.md.

  uv run python scripts/slack_app.py check
      Checks SLACK_BOT_TOKEN and SLACK_APP_TOKEN with Slack and that SLACK_CHANNEL is set (the environment, else
      .env). Prints nothing secret and posts nothing.
  uv run python scripts/slack_app.py run [--live] [--monday]
      Stays connected to Slack until Ctrl-C. A button pressed on a card writes to the contact ledger in the timing
      store (today.TIMELINES: TIMELINES_DB, else the pull's folder's, research/private/social on the Mac), the one
      route.py --send and scripts/contacts.py read and write; a test card's buttons record nothing. The Home tab
      shows the invented people's week, or with --live the real people's, to the team file's people (their
      slack_user_id) only: with Justin's go-ahead.
      --monday also posts the week to SLACK_CHANNEL (#weekly-recap) every Monday from 9am, once a week: the brief as
      one post, each card, draft and button in its thread, and on a month's first Monday the one-pager (the invented
      people's, marked as a test, unless --live).
Cards reach the channel from scripts/route.py --send, which goes through the app once it is set up.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import slack, today, weekly  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MONDAYS = ROOT / "research/private/routing/monday-brief.json"


def settings():
    try:
        found = slack.load()
    except ValueError as e:
        sys.exit(str(e))
    if missing := [n for n in slack.NAMES if not found[n]]:
        sys.exit(f"Not set yet: {', '.join(missing)}. See docs/slack-app-setup.md, step 4.")
    return found


def check():
    found = settings()
    try:
        me = slack.Slack(found["SLACK_BOT_TOKEN"]).call("auth.test")
        print(f"Bot token works: the app is {me.get('user')} in {me.get('team')}.")
        slack.Slack(found["SLACK_APP_TOKEN"]).call("apps.connections.open")
        print("App token works: the app can connect.")
    except slack.SlackError as e:
        sys.exit(str(e))
    print(f"Cards go to channel {found['SLACK_CHANNEL']}. If the app is not in it yet, type /invite @GI Timing there.")


def store():
    """The timing run's store, whose ledger route.py --send reads; None when this machine has none yet."""
    return today.ledger() if today.TIMELINES.exists() else None


def brief(post_to, mode):
    """Post the week (app/weekly.py): the brief as one post, what to act on in its thread, and on a month's first
    Monday the one-pager (the invented people's, marked as a test, in the simulation). Each reach it names is
    recorded as a card about that person. False when nothing went out: the channel stays silent then."""
    return lambda: weekly.publish(post_to, mode)


async def run(live, monday):
    found, mode = settings(), "live" if live else "simulation"
    team = json.loads(today.TEAM.read_text())["members"] if today.TEAM.exists() else []  # who pressed a button
    ledger = store()
    print(f"Home tab: {'the real people' if live else 'the invented people, marked as a test'}. Ledger: "
          + (f"{today.TIMELINES.relative_to(ROOT) if today.TIMELINES.is_relative_to(ROOT) else today.TIMELINES}"
             if ledger else "none on this machine"))
    app = slack.App(slack.Slack(found["SLACK_BOT_TOKEN"]), ledger, mode, team)
    jobs = [asyncio.create_task(slack.listen(app, found["SLACK_APP_TOKEN"]))]
    if monday:
        state = MONDAYS if live else MONDAYS.with_name("monday-brief-test.json")
        jobs.append(asyncio.create_task(slack.mondays(brief(slack.poster(found), mode), state)))
    done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)  # one stopping stops the other
    for job in jobs:
        job.cancel()
    for job in done:
        if not job.cancelled() and job.exception():  # the type only, in case the message carries a secret
            print(f"Stopped: {type(job.exception()).__name__}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="check the tokens with Slack; posts nothing")
    go = sub.add_parser("run", help="stay connected: buttons and the Home tab")
    go.add_argument("--live", action="store_true", help="the Home tab shows the real people (Justin's go-ahead)")
    go.add_argument("--monday", action="store_true", help="post the week's brief every Monday from 9am")
    args = parser.parse_args()
    if args.command == "check":
        return check()
    try:
        asyncio.run(run(args.live, args.monday))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
