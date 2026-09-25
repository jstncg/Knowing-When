"""The Slack app over invented people: cards posted as the app, buttons that write to the ledger, the Home tab.

Every Slack call goes to a mock transport: nothing here reaches Slack."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from app import contact, routing, slack, weekly
from app.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
AS_OF = "2026-09-15T00:00:00+00:00"
LINKS = ["https://rinvale.example/", "https://github.com/rinvale-example"]
TEAM = [{"name": "Dana Kest", "slack_user_id": "U0DANA"}]
DANA = {"id": "U0DANA", "name": "dana", "username": "dana"}
BOT, APP = "xoxb-test-bot-token", "xapp-test-app-token"


@pytest.fixture
def store(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 's.sqlite'}")
    routing.seed(s, json.loads((FIXTURES / "routing" / "timeline.json").read_text()))
    contact.record(s, "A900", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    return s


class Fake:
    """Slack's Web API as a mock transport: records each call and answers ok."""

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer or {}

    def __call__(self, request):
        body = json.loads(request.content) if request.content else {}
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((method, body, request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True, "channel": "C0GI", "ts": "111.222", **self.answer.get(method, {})})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))

    def made(self, method):
        return [body for m, body, _ in self.calls if m == method]


def app(store, fake, clock=lambda: "2026-09-16T10:00:00+00:00", mode="simulation"):
    return slack.App(slack.Slack(BOT, fake.client()), store, mode, TEAM, clock=clock)


def posted_card(store, fake, test=False):
    """Rin's card posted as the app: the blocks Slack would hand back on a button press."""
    r = routing.route(store, "A900", AS_OF, team=[])
    routing.send(store, r, slack.Poster(slack.Slack(BOT, fake.client()), "C0GI"), now=AS_OF, test=test)
    return fake.made("chat.postMessage")[-1]


def press(message, action_id, user=DANA):
    ledger = next(b for b in message["blocks"] if b.get("block_id") == slack.LEDGER)
    button = next(e for e in ledger["elements"] if e["action_id"] == action_id)
    return {"type": "interactive", "envelope_id": "e1", "payload": {
        "type": "block_actions", "user": user, "trigger_id": "t1", "container": {"channel_id": "C0GI",
        "message_ts": "111.222"}, "message": {"text": message["text"], "blocks": message["blocks"]},
        "actions": [{"action_id": action_id, "value": button["value"]}]}}


def submit(view, values, user=DANA):
    return {"type": "interactive", "envelope_id": "e2", "payload": {
        "type": "view_submission", "user": user, "view": {"callback_id": view["callback_id"],
        "private_metadata": view["private_metadata"], "state": {"values": values}}}}


def run(app, envelope):
    answer, job = app.handle(envelope)
    return answer, job() if job else None


def test_the_settings_come_from_the_environment_else_env_and_a_token_in_the_wrong_place_is_named_not_shown(
        tmp_path, monkeypatch):
    for name in slack.NAMES:
        monkeypatch.delenv(name, raising=False)
    assert slack.load(tmp_path) == {n: "" for n in slack.NAMES}
    (tmp_path / ".env").write_text(f"SLACK_BOT_TOKEN={BOT}\nSLACK_CHANNEL=C0FILE\n")
    assert slack.load(tmp_path)["SLACK_CHANNEL"] == "C0FILE"
    monkeypatch.setenv("SLACK_CHANNEL", "C0ENV")
    assert slack.load(tmp_path)["SLACK_CHANNEL"] == "C0ENV"
    monkeypatch.setenv("SLACK_CHANNEL", "")  # set but empty: off for this run, whatever .env says
    assert slack.load(tmp_path)["SLACK_CHANNEL"] == ""
    monkeypatch.setenv("SLACK_BOT_TOKEN", APP)  # the app token where the bot token goes
    with pytest.raises(ValueError, match="SLACK_BOT_TOKEN should start with xoxb-") as wrong:
        slack.load(tmp_path)
    assert APP not in str(wrong.value)
    assert slack.poster({"SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": "", "SLACK_CHANNEL": ""}) is None  # the webhook


def test_a_refused_call_names_the_method_and_what_to_do_never_the_token():
    refused = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "ok": False, "error": "not_in_channel"})))
    with pytest.raises(slack.SlackError) as e:
        slack.Slack(BOT, refused).call("chat.postMessage", channel="C0GI", text="t")
    assert "chat.postMessage: not_in_channel (invite the app" in str(e.value) and BOT not in str(e.value)
    down = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError(BOT))))
    with pytest.raises(slack.SlackError) as e:
        slack.Slack(BOT, down).call("auth.test")
    assert str(e.value) == "Slack auth.test: ConnectError" and BOT not in repr(slack.Slack(BOT))


def test_a_card_posted_as_the_app_carries_the_ledger_buttons_and_is_recorded(store):
    fake = Fake()
    message = posted_card(store, fake)
    method, body, auth = fake.calls[-1]
    assert (method, body["channel"], auth) == ("chat.postMessage", "C0GI", f"Bearer {BOT}")
    ledger = next(b for b in body["blocks"] if b.get("block_id") == slack.LEDGER)
    assert [e["text"]["text"] for e in ledger["elements"]] == ["I sent it", "Not now", "Do you know them?"]
    assert json.loads(ledger["elements"][0]["value"]) == {"p": "A900", "r": "mts-research", "n": "Rin Vale", "t": 0}
    assert body["blocks"][-1]["type"] == "context" and body["blocks"][-2] is ledger  # above the card's last line
    assert contact.history(store, "A900")[-1]["kind"] == "pinged" and message["text"].startswith("Just happened: Rin")


def test_the_digest_goes_as_the_app_without_buttons():
    fake = Fake()
    routing.send_digest({"text": "GI this week", "blocks": []}, slack.Poster(slack.Slack(BOT, fake.client()), "C0GI"))
    assert fake.made("chat.postMessage") == [{"channel": "C0GI", "text": "GI this week", "blocks": [],
                                              "unfurl_links": False, "unfurl_media": False}]


def test_i_sent_it_records_who_sent_it_and_the_card_then_asks_whether_they_replied(store):
    fake = Fake()
    message = posted_card(store, fake)
    run(app(store, fake), press(message, "sent"))
    sent = contact.history(store, "A900")[-1]
    assert (sent["kind"], sent["by"], sent["role_id"]) == ("sent", "Dana Kest", "mts-research")  # the team file's name
    update = fake.made("chat.update")[-1]
    assert (update["channel"], update["ts"]) == ("C0GI", "111.222")
    log = next(b for b in update["blocks"] if b.get("block_id") == slack.LOG)["elements"][0]["text"]
    assert log == "<@U0DANA> sent it, 16 September 2026."
    ledger = next(b for b in update["blocks"] if b.get("block_id") == slack.LEDGER)
    assert [e["action_id"] for e in ledger["elements"]] == ["replied", "not_now"]

    run(app(store, fake), press({"text": message["text"], "blocks": update["blocks"]}, "replied"))
    assert contact.history(store, "A900")[-1]["kind"] == "replied"
    done = fake.made("chat.update")[-1]["blocks"]
    assert not [b for b in done if b.get("block_id") == slack.LEDGER]  # a conversation is open: nothing left to press
    assert next(b for b in done if b.get("block_id") == slack.LOG)["elements"][0]["text"].endswith(
        "Rin replied, per <@U0DANA>, 16 September 2026.")


def test_a_test_cards_buttons_record_nothing(store):
    fake = Fake()
    message = posted_card(store, fake, test=True)
    before = contact.history(store, "A900")
    run(app(store, fake), press(message, "sent"))
    assert contact.history(store, "A900") == before
    log = next(b for b in fake.made("chat.update")[-1]["blocks"] if b.get("block_id") == slack.LOG)
    assert log["elements"][0]["text"].startswith("Test card, nothing recorded: <@U0DANA> sent it")


def test_not_now_asks_for_their_day_refuses_one_already_past_and_holds_them_until_it(store):
    fake = Fake()
    message = posted_card(store, fake)
    a = app(store, fake)
    run(a, press(message, "not_now"))
    [opened] = fake.made("views.open")
    view = opened["view"]
    assert (opened["trigger_id"], view["callback_id"], view["title"]["text"]) == ("t1", "not_now", "Not now")
    assert view["blocks"][1]["element"]["initial_date"] == "2026-12-15"  # 90 days on, to be changed
    answer, job = a.handle(submit(view, {"until": {"day": {"selected_date": "2026-09-16"}}, "note": {"text": {}}}))
    assert answer == {"response_action": "errors", "errors": {"until": "Pick a day after today."}} and job is None
    answer, job = a.handle(submit(view, {"until": {"day": {"selected_date": "2027-01-05"}},
                                         "note": {"text": {"value": "After the defense"}}}))
    assert answer is None
    job()
    wait = contact.history(store, "A900")[-1]
    assert (wait["kind"], wait["until"], wait["note"], wait["by"]) == ("not_now", "2027-01-05", "After the defense",
                                                                        "Dana Kest")
    assert contact.check(contact.history(store, "A900"), "2026-09-17T00:00:00+00:00").state == "hold"
    update = fake.made("chat.update")[-1]["blocks"]
    assert not [b for b in update if b.get("block_id") == slack.LEDGER]
    assert "Not now: Rin asked us to wait until 5 January 2027, per <@U0DANA>" in json.dumps(update)


def test_do_you_know_them_records_who_and_how_and_their_next_card_says_to_ask_them(store):
    fake = Fake()
    message = posted_card(store, fake)
    a = app(store, fake)
    run(a, press(message, "know_them"))
    view = fake.made("views.open")[-1]["view"]
    assert a.handle(submit(view, {"how": {"pick": {}}, "note": {"text": {}}}))[0]["errors"] == {"how": "Pick one."}
    _, job = a.handle(submit(view, {"how": {"pick": {"selected_option": {"value": "Worked together"}}},
                                    "note": {"text": {"value": "At Pine Lab"}}}))
    job()
    knows = contact.history(store, "A900")[-1]
    assert (knows["kind"], knows["by"], knows["note"]) == ("knows", "Dana Kest", "Worked together. At Pine Lab")
    update = fake.made("chat.update")[-1]["blocks"]
    assert [e["action_id"] for e in next(b for b in update if b.get("block_id") == slack.LEDGER)["elements"]] == \
        ["sent", "not_now", "know_them"]  # someone else may know them too
    assert "<@U0DANA> knows Rin (worked together)" in json.dumps(update)
    later = routing.route(store, "A900", "2026-10-20T00:00:00+00:00", team=[{"name": "Dana Kest"}])
    way_in = next(b["text"]["text"] for b in later.card["blocks"] if b.get("text", {}).get("text", "").startswith("*Route in*"))
    assert "Warm path: Dana Kest knows Rin (worked together, said Sep 16). Ask Dana for an intro first." in way_in


def test_someone_not_on_the_team_list_who_knows_them_is_a_name_to_check_never_a_way_in(store):
    fake = Fake()
    message = posted_card(store, fake)
    a = app(store, fake)
    stranger = {"id": "U0JDOE", "name": "jdoe", "username": "jdoe"}
    run(a, press(message, "know_them", stranger))
    view = fake.made("views.open")[-1]["view"]
    _, job = a.handle(submit(view, {"how": {"pick": {"selected_option": {"value": "Met them"}}},
                                    "note": {"text": {"value": "Met at NeurIPS"}}}, stranger))
    job()
    assert contact.history(store, "A900")[-1]["by"] == "jdoe"
    assert "<@U0JDOE> knows Rin (met them), but isn't matched to GI's team list" in json.dumps(
        fake.made("chat.update")[-1]["blocks"])
    later = routing.route(store, "A900", "2026-10-20T00:00:00+00:00", team=[{"name": "Dana Kest"}])
    assert later.known == [] and later.unlisted[0]["by"] == "jdoe"
    way_in = next(b["text"]["text"] for b in later.card["blocks"] if b.get("text", {}).get("text", "").startswith("*Route in*"))
    assert "jdoe said on Slack they know Rin but isn't on GI's team list: check who that is first." in way_in
    assert "ask jdoe" not in way_in.lower()


def test_a_form_saved_after_a_restart_says_so_under_the_card(store):
    fake = Fake()
    message = posted_card(store, fake)
    run(app(store, fake), press(message, "know_them"))
    view = fake.made("views.open")[-1]["view"]
    _, job = app(store, fake).handle(submit(view, {"how": {"pick": {"selected_option": {"value": "Met them"}}},
                                                   "note": {"text": {}}}))  # a new app: the card was not kept
    job()
    reply = fake.made("chat.postMessage")[-1]
    assert (reply["thread_ts"], reply["text"]) == ("111.222", "<@U0DANA> knows Rin (met them): an intro from them "
                                                   "lands better than a cold note, 16 September 2026.")


def test_the_home_tab_shows_the_week_and_is_built_once_until_refreshed(store, monkeypatch):
    fake, built = Fake(), []
    week = weekly.brief

    def counted(mode):
        built.append(mode)
        return week(mode)
    monkeypatch.setattr(weekly, "brief", counted)
    a = app(None, fake)
    opened = {"type": "events_api", "envelope_id": "e3", "payload": {"event": {
        "type": "app_home_opened", "user": "U0DANA", "tab": "home"}}}
    run(a, opened)
    run(a, opened)
    [first, _] = fake.made("views.publish")
    blocks = first["view"]["blocks"]
    assert first["user_id"] == "U0DANA" and first["view"]["type"] == "home" and len(blocks) <= 100
    assert blocks[0]["text"]["text"].startswith("Test · Monday brief") and blocks[-1]["elements"][0]["action_id"] == \
        "home_refresh"
    assert built == ["simulation"]  # the second open reused it
    run(a, {"type": "interactive", "envelope_id": "e4", "payload": {"type": "block_actions", "user": DANA,
            "actions": [{"action_id": "home_refresh", "value": "home"}]}})
    assert built == ["simulation", "simulation"]
    assert run(a, {"type": "events_api", "envelope_id": "e5", "payload": {"event": {
        "type": "app_home_opened", "user": "U0DANA", "tab": "messages"}}}) == (None, None)


def test_a_press_on_a_real_card_with_no_store_here_says_it_was_not_recorded(store):
    fake = Fake()
    message = posted_card(store, fake)
    run(app(None, fake), press(message, "sent"))
    assert "Not recorded, no timing store on this machine" in json.dumps(fake.made("chat.update")[-1]["blocks"])


def test_the_listener_answers_each_envelope_first_then_does_its_work_and_reconnects_when_asked(store, monkeypatch):
    fake = Fake({"apps.connections.open": {"url": "wss://socket.example/link?ticket=secret"}})
    message = posted_card(store, fake)
    envelope = press(message, "sent")
    sent, urls, printed = [], [], []
    connections = [[{"type": "hello"}, envelope, {"type": "disconnect", "reason": "refresh_requested"}],
                   [{"type": "hello"}, {"type": "disconnect", "reason": "link_disabled"}]]

    class Socket:
        def __init__(self, url):
            urls.append(url)
            self.frames = connections.pop(0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.frames:
                raise StopAsyncIteration
            return json.dumps(self.frames.pop(0))

        async def send(self, text):
            sent.append(json.loads(text))

    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    asyncio.run(slack.listen(app(store, fake), APP, connect=Socket, client=fake.client()))
    assert urls == ["wss://socket.example/link?ticket=secret"] * 2 and sent == [{"envelope_id": "e1"}]
    opened = [auth for m, _, auth in fake.calls if m == "apps.connections.open"]
    assert opened == [f"Bearer {APP}"] * 2  # the app-level token opens each connection, the bot token does the rest
    assert "ticket" not in " ".join(printed) and "Socket Mode is off for the app in Slack's settings: stopping." in printed
    assert contact.history(store, "A900")[-1]["kind"] == "sent"


def test_a_dropped_connection_is_reported_without_its_address_and_tried_again(monkeypatch):
    fake, printed, tries = Fake({"apps.connections.open": {"url": "wss://socket.example/link?ticket=secret"}}), [], []

    def connect(url):
        tries.append(url)
        if len(tries) == 1:
            raise RuntimeError(f"could not reach {url}")  # a library error that carries the address

        class Closed:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if tries:
                    tries.append("read")
                    return json.dumps({"type": "disconnect", "reason": "link_disabled"})
                raise StopAsyncIteration
        return Closed()

    async def no_wait(seconds):
        printed.append(f"waited {seconds}")
    monkeypatch.setattr(slack.asyncio, "sleep", no_wait)
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    asyncio.run(slack.listen(slack.App(None, None), APP, connect=connect, client=fake.client()))
    assert len([t for t in tries if t != "read"]) == 2 and "ticket" not in " ".join(printed)
    assert "Slack connection lost (RuntimeError); trying again in 1s" in printed and "waited 1" in printed


def test_the_monday_brief_is_due_once_a_week_from_nine():
    assert slack.monday_due(datetime(2026, 9, 21, 9, 0), None) == "2026-09-21"
    assert slack.monday_due(datetime(2026, 9, 21, 8, 59), None) is None
    assert slack.monday_due(datetime(2026, 9, 22, 10, 0), None) is None  # Tuesday: next Monday's
    assert slack.monday_due(datetime(2026, 9, 21, 15, 0), "2026-09-21") is None  # already posted


def test_the_manifest_asks_only_for_what_the_app_uses():
    m = json.loads(slack.MANIFEST.read_text())
    assert m["oauth_config"]["scopes"] == {"bot": ["chat:write"]}
    assert m["settings"]["socket_mode_enabled"] and m["settings"]["interactivity"]["is_enabled"]
    assert m["settings"]["event_subscriptions"] == {"bot_events": ["app_home_opened"]}
    assert m["features"]["app_home"]["home_tab_enabled"] and "request_url" not in json.dumps(m)


def test_a_form_saved_after_someone_else_pressed_keeps_their_change(store):
    fake = Fake()
    message = posted_card(store, fake)
    a = app(store, fake)
    run(a, press(message, "know_them"))  # Dana opens the form
    run(a, press(message, "sent", user={"id": "U0BOB", "name": "bob"}))  # Bob sends meanwhile, on the same card
    view = fake.made("views.open")[-1]["view"]
    _, job = a.handle(submit(view, {"how": {"pick": {"selected_option": {"value": "Friends"}}}, "note": {"text": {}}}))
    job()
    final = fake.made("chat.update")[-1]["blocks"]
    log = next(b for b in final if b.get("block_id") == slack.LOG)["elements"][0]["text"]
    assert log.startswith("<@U0BOB> sent it") and "<@U0DANA> knows Rin (friends)" in log
    assert [e["action_id"] for e in next(b for b in final if b.get("block_id") == slack.LEDGER)["elements"]] == \
        ["replied", "not_now"]  # Bob's press still stands


def test_a_record_the_ledger_refuses_is_said_on_the_card_and_its_buttons_stay(store):
    fake = Fake()
    message = posted_card(store, fake)
    a = app(store, fake)
    run(a, press(message, "not_now"))
    line = a.record("not_now", {"p": "A900", "r": "mts-research", "n": "Rin Vale", "t": 0}, DANA, until="2026-01-01")
    assert line.startswith("Not recorded (") and "Rin asked us to wait until 1 January 2026" in line
    a.change(("C0GI", "111.222"), line, "not_now", None)
    buttons = next(b for b in fake.made("chat.update")[-1]["blocks"] if b.get("block_id") == slack.LEDGER)
    assert [e["action_id"] for e in buttons["elements"]] == ["sent", "not_now", "know_them"]  # press it again


def test_an_unreadable_monday_file_is_said_and_never_posts_twice(tmp_path, monkeypatch):
    state, posted, printed = tmp_path / "monday.json", [], []
    state.write_text("not json")

    async def once(seconds):
        raise asyncio.CancelledError  # one pass of the loop
    monkeypatch.setattr(slack.asyncio, "sleep", once)
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(slack.mondays(lambda: posted.append(1), state, clock=lambda: datetime(2026, 9, 21, 10, 0)))
    assert posted == [] and printed == ["Can't read monday.json: fix or delete it. No brief until then."]


def test_the_live_home_tab_shows_the_real_week_to_the_team_only(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(slack, "home", lambda mode: {"type": "home", "blocks": [{"type": "section", "text": {
        "type": "mrkdwn", "text": f"the {mode} week"}}]})
    a = app(None, fake, mode="live")
    a.publish_home("U0DANA")
    a.publish_home("U0STRANGER")
    team, other = fake.made("views.publish")
    assert "the live week" in json.dumps(team) and "the live week" not in json.dumps(other)
    assert "Ask whoever runs GI Timing to add your Slack member ID" in json.dumps(other)


def test_the_morning_list_posts_as_the_app_with_each_card_in_its_thread_and_a_late_line_stays_there(store):
    fake = Fake()
    r = routing.route(store, "A900", AS_OF, team=[])
    routing.send_morning(store, [r], slack.Poster(slack.Slack(BOT, fake.client()), "C0GI"), now=AS_OF)
    listed, card = fake.made("chat.postMessage")
    assert "thread_ts" not in listed and listed["text"].startswith("Who to reach this morning")
    assert card["thread_ts"] == "111.222" and any(b.get("block_id") == slack.LEDGER for b in card["blocks"])
    # A form saved after a restart says so in the list's thread, not under a reply (Slack wants the parent's ts).
    message = press({**card}, "not_now")["payload"]
    message["message"]["thread_ts"], message["container"]["message_ts"] = "111.222", "333.444"
    a = app(store, fake)
    a.press(message)
    meta = json.loads(fake.made("views.open")[-1]["view"]["private_metadata"])
    assert (meta["ts"], meta["thread"]) == ("333.444", "111.222")


def test_a_missing_scope_on_the_app_token_says_to_make_a_new_token_not_to_redo_the_manifest():
    def refuse(request):
        return httpx.Response(200, json={"ok": False, "error": "missing_scope"})
    client = httpx.Client(transport=httpx.MockTransport(refuse))
    with pytest.raises(slack.SlackError) as app_token:
        slack.Slack(APP, client).call("apps.connections.open")
    assert "generate a new token with that scope" in str(app_token.value) and APP not in str(app_token.value)
    with pytest.raises(slack.SlackError) as bot_token:
        slack.Slack(BOT, client).call("chat.postMessage", channel="C0GI", text="hi")
    assert "Reinstall to Workspace" in str(bot_token.value) and BOT not in str(bot_token.value)
