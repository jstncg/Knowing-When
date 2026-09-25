# GI Timing Engine

I built this for General Intuition's case study. For each person GI wants to hire, it decides whether to reach out now, check something first, wait until a date, or stay quiet. Every call comes with the reason, how sure it is, and what would prove it wrong.

The write-up is at [jstncg.github.io/Knowing-When](https://jstncg.github.io/Knowing-When/), and its source is in `site/`. It explains the thinking. This file explains the code.

## What it does

It covers three of GI's open roles: Member of Technical Staff, Senior / Lead Backend Engineer, and Product Designer. Their JDs come from GI's job posts on Ashby, and each has a watchlist of people who might fit it. A fourth, Global Controller, stays in the repo for the golden set and the simulation.

Every morning it reads what's new from each watched person on X, LinkedIn and GitHub. Every event is stamped with the moment it went public, and a model reads each new post for the role.

Then it makes a call. A paper, a release or a launch from the past month is enough to write about someone's work. So is something they said themselves, like "I'm open". Time in a seat and retention cliffs are really guesses about who's about to leave, so they're shown as context and never used as a reason.

A reach becomes a Slack card with who, why now, how sure, the way in, and a draft from the right person at GI. Before any card goes out, it has to pass four checks: is it really them, did something real happen in the last month, is this a bad week for them, and has GI carded them before or used up the week's cards. That's four hiring cards a week at most, and three for any one role. Nothing reaches anyone until someone at GI sends it.

Around that there are ops pages for events, the Monday brief, the hiring budget, new starts, and a monthly one-pager. A separate GTM list does the same thing for 57 companies, and it shares one contact ledger with hiring.

## What's been checked

- **Golden set.** 54 invented scenarios, each with a fixed expected answer. It passes all 54. They check that the rules behave, not that the timing is right.
- **Replay.** I rewound it over 120 past mornings on 38 real people, and each morning it could only see what was public by then. It made 6 cards, none were held, and no rail broke. Their data is private, so the drill below runs the same replay on 16 invented people.
- **Early signs.** A test I wrote down before running it found they don't beat random timing yet (p = 0.36). So an early sign can open a short note about someone's work, but it never gets anyone pitched for a role.

## Run it

You need [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```sh
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765 and switch to **Simulation**. It runs the same engine on invented people, posts and events, and every page is labelled "Invented data". It needs no keys and no private data. **Live data** reads the private timeline store, so it's empty on any machine but mine.

Other things you can run without keys:

```sh
uv run python scripts/golden.py                  # the 54 invented scenarios
uv run python scripts/route.py --demo            # the Slack card, printed, for invented people
uv run python scripts/replay_drill.py --demo --fixtures tests/fixtures/drill --days 120   # the same replay on 16 invented people
uv run python scripts/roles.py add --demo        # preview a sample JD as a role (writes nothing)
uv run python scripts/events.py --demo           # the Events page on invented records
uv run python scripts/accounts.py --demo         # the GTM list on invented companies
```

## Tests

```sh
uv run python -m pytest -q
node --test tests/*.cjs
node tests/e2e/todays_calls.cjs    # needs Playwright with Chromium
```

## Where things are

- `app/detectors.py`: the 42 detectors, over what was public on a given day.
- `app/readiness.py`: turns what the detectors found into a call.
- `app/journey.py`: fills each person's timeline and runs the live call.
- `app/contact.py`: the contact ledger and the checks every card has to pass.
- `app/routing.py`: turns a call into a Slack card.
- `app/accounts.py`: the GTM list.
- `app/static/`: the web app.
- `scripts/daily.py`: the morning read.
- `tests/fixtures/`: every invented person, post and event.

The data on the people it watches stays in `research/private/`, which is never committed.

## What's next

Switching on the call log. It's built to record the engine's call on every watched person each morning, each line chained to the one before. Next it also records every message and reply. Over time that shows how GI actually hires and who writes back, and I'll build the algorithm on top of that.
