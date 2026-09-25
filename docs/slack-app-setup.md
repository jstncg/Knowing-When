# Set up the GI Timing Slack app (about 10 minutes)

The app posts to #weekly-recap: on a morning with someone new to reach, one short list of who and why, with each
person's card in its thread and three buttons on the card. Every Monday it posts the week as one post (who to reach,
who to invite to the next event, follow-ups, new starts, budget), with each card, draft and button in its thread, and
on a month's first Monday the founders' one-pager. Pressing a button writes what happened to the contact ledger (or
New starts), so nobody is contacted twice. The app's Home tab shows the week too. It runs on your Mac and contacts
no one.

## 1. Create the app (3 minutes)
1. In Terminal, in the repo: `git pull`, then `pbcopy < config/slack-app-manifest.json` (copies the app's settings).
2. Go to https://api.slack.com/apps and click **Create New App**, then **From a manifest**.
3. Pick GI's workspace and click **Next**.
4. On the **JSON** tab, delete what is there and paste (Cmd+V). Click **Next**, then **Create**.

## 2. Install it and copy the bot token (1 minute)
1. In the left menu, click **Install App**, then **Install to Workspace**, then **Allow**.
2. Copy the **Bot User OAuth Token**. It starts with `xoxb-`.

## 3. Make the app token (1 minute)
1. In the left menu, click **Basic Information**. Scroll to **App-Level Tokens** and click **Generate Token and Scopes**.
2. Name it `socket`, click **Add Scope**, pick `connections:write`, then click **Generate**.
3. Copy the token. It starts with `xapp-`.

## 4. Add three lines to .env (2 minutes)
1. In Slack, open #weekly-recap. Type `/invite @GI Timing` and send it.
2. Click the channel's name. The **Channel ID** is at the bottom of the About tab (it starts with `C`).
3. In Terminal, in the repo: `open -e .env` (the file your other keys are in), and add:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   SLACK_CHANNEL=C...
   ```
   Leave the webhook line as it is: it is the fallback. Don't paste the tokens anywhere else, including chat.

## 5. Check it and start it (2 minutes)
In Terminal, in the repo:
```
uv run python scripts/slack_app.py check
```
You should see three lines: "Bot token works: the app is ... in ...", "App token works: the app can connect." and
"Cards go to channel C...". The check tests the two tokens only, not the channel ID or the invite: a wrong ID or a
missing `/invite` shows up when the first card posts (step 6), with what to do. Then start the app, and leave this
window open:
```
uv run python scripts/slack_app.py run
```
It says "Connected to Slack." The buttons only work while this window runs.

## 6. Try it on a made-up person (2 minutes)
In a second Terminal window:
```
uv run python scripts/route.py --demo --send
```
"Test · Who to reach this morning" appears in the channel, listing four made-up people: a paper, open to work and
two early signs, each with its own message. Their cards are in its thread. The other two (an acquisition and a
launch) wait: the week's cap is four cards across roles. To post one of them on its own, add `--subject M901` (the
acquisition) or `--subject M902` (the launch). Press the buttons on a card. A test card records nothing: it shows what
would have been recorded. Then open **GI Timing** under Apps in Slack's sidebar. The Home tab shows the invented
people's week.

To see a Monday: `uv run python scripts/route.py --demo --inbox --send` posts "Test · Monday brief" with the week in
its thread: a new start's open items with a Done button each, the next event's invites with their drafts and an
Invited button each, and the reach cards. Its buttons record nothing either.

## What the buttons do
| Button | Records | Then |
|---|---|---|
| I sent it | You sent the note | The card asks: They replied, or Not now |
| They replied | A conversation is open | No new approach to them |
| Not now | The day they asked us to wait until | Every card about them waits until that day |
| Do you know them? | That you know them, and how | Their next card says to ask you for an intro first |
| Invited (Monday thread) | You sent them the event invite | They leave the invite list; nothing else goes to them before the event |
| I sent it (a follow-up) | You sent their follow-up | It leaves the brief |
| Done (a new start) | That item is done on New starts | It leaves the brief; the rest stay |

## Going live (only when you say so)
- `uv run python scripts/slack_app.py run --live`: the Home tab shows the real people, only to the people in
  `research/private/gi-team.json` with a `slack_user_id` (in Slack: their profile, **⋮**, **Copy member ID**).
- `uv run python scripts/route.py --send --people research/private/early-signals/people.json`: the morning list of
  real people (the people file's watchlist only), each card in its thread with the buttons. With no one new to
  reach, it posts nothing. It says whether it posted as the app or to the webhook. Both read the same contact
  log, `research/private/social/timelines.sqlite`, so a button pressed today holds the next card.
- Add `--monday` to `run` to post the week to the channel every Monday from 9am: one post, each card, draft and
  button in its thread, and on a month's first Monday the one-pager. The reach cards in it go through the same
  checks and weekly cap as `--send`, so a person is never on both the Monday post and a morning list.
- The old webhook still works: take `SLACK_BOT_TOKEN` out of `.env` and `--send` goes to `SLACK_ROUTING_WEBHOOK` again.
  A webhook cannot thread, so there the cards follow the list as their own messages, without buttons.

## If something goes wrong
The error says what to do. The common ones:
- `not_in_channel`: type `/invite @GI Timing` in the channel.
- `channel_not_found`: `SLACK_CHANNEL` needs the channel's ID, not its name.
- `invalid_auth`: the token was copied wrong or regenerated. Copy it again.
