"""The week in GI's one Slack channel: the Monday post, what to act on in its thread, the buttons there, and the
one-pager on a month's first Monday. Invented people only; every Slack call goes to a mock transport."""

import json
import shutil
from datetime import date
from pathlib import Path
import subprocess
import sys
import threading

import httpx
import pytest

from app import contact, events, hiring, inbox, routing, slack, starts, today, weekly
from app.store import Store
from tests.test_contact import AS_OF, FIXTURES, LINKS
from tests.test_contact import slack as webhook_client
from tests.test_slack import BOT, DANA, TEAM, Fake, run

ROOT = Path(__file__).resolve().parents[1]

MONDAY, FIRST_MONDAY = date(2026, 9, 14), date(2026, 10, 5)
REACHES = ["A900", "X900", "X902"]  # Rin Vale, Mara Quill, Noa Brandt: the simulation's week, in the brief's order


@pytest.fixture
def simulation():
    today._demo_store.cache_clear()
    hiring.reset()
    starts.reset()
    yield
    today._demo_store.cache_clear()
    hiring.reset()
    starts.reset()


def poster(fake):
    return slack.Poster(slack.Slack(BOT, fake.client()), "C0GI")


def texts(card):
    return "\n".join(b["text"]["text"] for b in card["blocks"] if b["type"] == "section")


def ledger(store):
    return [e for e in store.all("contact") if e["kind"] == "pinged"]


def week():
    return weekly.week("simulation", today.DEMO_AS_OF)[0]


def test_the_post_is_the_briefs_decisions_in_order_and_what_to_act_on_goes_in_its_thread(simulation):
    w = week()
    post = weekly.post(w)
    titles = [d["title"] for d in w["decisions"]]
    assert all(t in texts(post) for t in titles)
    assert texts(post).index(titles[0]) < texts(post).index(titles[-1])
    assert "Draft" not in texts(post) and "<https://" not in json.dumps(post)  # short: drafts and links are below
    replies = [(d["title"], (weekly.reply(d, n, "simulation") or {}).get("text"))
               for n, d in enumerate(w["decisions"], 1)]
    assert replies == [("Get Wren Example ready to start Oct 5", "Test: Get Wren Example ready to start Oct 5"),
                       ("Approve 2 invites for World model evals night (Sep 24)",
                        "Test: Approve 2 invites for World model evals night (Sep 24)"),
                       ("Reach Rin Vale for Member of Technical Staff", None),  # the card itself, through routing.send
                       ("Reach Mara Quill for Member of Technical Staff", None),
                       ("Reach Noa Brandt for Product Designer", None),
                       ("Check Lena Okafor before anyone writes (Member of Technical Staff)", None),  # said in full
                       ("Event spend is $218 over for Q3 2026", None)]


def test_nothing_to_decide_posts_nothing_unless_the_one_pager_is_due(simulation, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(weekly, "week", lambda mode, now: ({"missing": None, "decisions": [], "week_of": "2026-09-14",
                                                            "as_of": "2026-09-15", "source": "Invented people"}, {}))
    assert weekly.publish(poster(fake), "simulation", MONDAY) is False and fake.made("chat.postMessage") == []
    assert weekly.publish(poster(fake), "simulation", FIRST_MONDAY) is True
    [page] = fake.made("chat.postMessage")
    assert page["blocks"][0]["text"]["text"] == "Test · Monthly one-pager: September 2026" and "thread_ts" not in page


def test_the_week_goes_out_as_one_post_with_its_replies_under_it_in_order_and_names_its_reaches_once(simulation):
    fake = Fake()
    store = today.source("simulation")[0]
    assert weekly.publish(poster(fake), "simulation", MONDAY) is True
    first, *replies = fake.made("chat.postMessage")
    assert "thread_ts" not in first and first["blocks"][0]["text"]["text"].startswith("Test · Monday brief")
    assert len(replies) == 5 and all(r["thread_ts"] == "111.222" for r in replies)  # under the post
    assert [r["text"].split(": ", 1)[-1][:12] for r in replies[:2]] == ["Get Wren Exa", "Approve 2 in"]
    cards = replies[2:]
    assert all(any(b.get("block_id") == slack.LEDGER for b in r["blocks"]) for r in cards)  # ledger buttons
    assert all(c["text"].startswith("Test, made-up person: ") for c in cards)
    assert [n for c in cards for n in ("Rin Vale", "Mara Quill", "Noa Brandt") if n in c["text"]] == [
        "Rin Vale", "Mara Quill", "Noa Brandt"]  # in the post's order
    assert all(json.loads(b["elements"][0]["value"])["t"] == 1 for r in cards for b in r["blocks"]
               if b.get("block_id") == slack.LEDGER)  # invented people: a test's buttons record nothing
    assert sorted(e["person_id"] for e in ledger(store)) == sorted(REACHES)  # each card recorded once, by send
    assert {e["at"] for e in ledger(store)} == {today.DEMO_AS_OF}  # the invented week's own clock, never today
    assert not any(d["title"].startswith("Reach") for d in week()["decisions"])  # named once


def test_the_post_names_only_the_people_whose_cards_go_out(simulation, monkeypatch):
    monkeypatch.setattr(contact, "room_all", lambda store, now: 2)  # the week's cap across roles: two cards left
    fake = Fake()
    weekly.publish(poster(fake), "simulation", MONDAY)
    first, *replies = fake.made("chat.postMessage")
    assert "Reach Rin Vale" in texts(first) and "Reach Mara Quill" in texts(first)
    assert "Noa Brandt" not in json.dumps(fake.made("chat.postMessage"))  # never named, then held back
    assert sorted(e["person_id"] for e in ledger(today.source("simulation")[0])) == ["A900", "X900"]


def test_a_reply_slack_refuses_never_posts_the_week_twice_and_says_why(simulation, capsys):
    fake = Fake()

    class Flaky:
        def post(self, card, route=None, test=False, thread=None):
            if thread:
                raise slack.SlackError("chat.postMessage: invalid_blocks")
            return poster(fake).post(card, route, test)

    assert weekly.publish(Flaky(), "simulation", MONDAY) is True
    assert len(fake.made("chat.postMessage")) == 1  # the post once; each reply failed on its own
    assert capsys.readouterr().out.count("didn't go out: chat.postMessage: invalid_blocks") == 5
    assert ledger(today.source("simulation")[0]) == []  # a card that never posted is not recorded


def test_through_a_webhook_the_replies_follow_the_post(simulation, monkeypatch):
    sent = []

    def hook(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text="ok")
    client = httpx.Client(transport=httpx.MockTransport(hook))
    monkeypatch.setattr(routing.httpx, "Client", lambda **kw: client)
    assert weekly.publish("https://hooks.slack.com/services/T0/B0/x", "simulation", MONDAY) is True
    assert len(sent) == 6 and sent[0]["text"].startswith("Test, made-up people: Monday brief")
    assert '"type": "button"' not in json.dumps(sent) and "record nothing" not in json.dumps(sent[1:3])  # the app hears
    assert "Invited" not in json.dumps(sent) and "Draft from Dana Kest" in json.dumps(sent)  # the drafts stay


def test_while_sending_is_paused_nothing_posts(simulation, monkeypatch):
    monkeypatch.setattr(contact, "paused", lambda: {"at": "2026-09-14T00:00:00+00:00", "by": "Justin", "why": "a check"})
    fake = Fake()
    with pytest.raises(ValueError, match="Sending is paused"):
        weekly.publish(poster(fake), "simulation", FIRST_MONDAY)
    assert fake.made("chat.postMessage") == [] and ledger(today.source("simulation")[0]) == []


def test_a_new_start_gets_a_done_button_per_open_item_and_never_the_visa(simulation):
    starts.update("simulation", "wren", "visa", state="ask")  # open and due: still only on the New starts page
    [start] = [weekly.reply(d, n, "simulation") for n, d in enumerate(week()["decisions"], 1)
               if d["title"].startswith("Get")]
    buttons = [json.loads(b["accessory"]["value"]) for b in start["blocks"] if b.get("accessory")]
    assert [b["k"] for b in buttons] == ["relocation", "desk"]
    assert "visa" not in json.dumps(start).lower()


def app(fake, mode="simulation"):
    store = today.source("simulation")[0]
    return slack.App(slack.Slack(BOT, fake.client()), store, mode, TEAM, clock=lambda: "2026-09-16T10:00:00+00:00")


def pressing(reply, action_id, row=0, user=DANA, t=0):
    block = next(b for b in reply["blocks"] if b.get("block_id") == f"ops:{row}")
    button = block.get("accessory") or block["elements"][0]
    assert button["action_id"] == action_id
    value = json.loads(button["value"])
    return {"type": "interactive", "envelope_id": "e1", "payload": {
        "type": "block_actions", "user": user, "trigger_id": "t1",
        "container": {"channel_id": "C0GI", "message_ts": "333.444"},
        "message": {"text": reply["text"], "blocks": reply["blocks"]},
        "actions": [{"action_id": action_id, "block_id": block["block_id"], "value": json.dumps({**value, "t": t})}]}}


def reply_for(title):
    return next(weekly.reply(d, n, "simulation") for n, d in enumerate(week()["decisions"], 1)
                if d["title"].startswith(title))


def test_done_marks_the_item_on_new_starts_and_the_button_gives_way_to_who_did_it(simulation):
    fake = Fake()
    run(app(fake), pressing(reply_for("Get Wren"), "ops_done"))
    wren = starts.page("simulation")["hires"][0]
    assert next(i for i in wren["items"] if i["key"] == "relocation")["state"] == "done"
    [update] = fake.made("chat.update")
    row = next(b for b in update["blocks"] if b.get("block_id") == "ops:0")
    assert "accessory" not in row
    assert any(b["type"] == "context" and "Relocation: done, per <@U0DANA>" in b["elements"][0]["text"]
               for b in update["blocks"])
    assert any(b.get("accessory") for b in update["blocks"])  # the other item's button stays


def test_invited_records_the_invite_in_the_ledger_from_whoever_pressed(simulation):
    fake = Fake()
    run(app(fake), pressing(reply_for("Approve"), "ops_invited", row=0))
    store = today.source("simulation")[0]
    [e] = [e for e in contact.history(store, "C801") if e["kind"] == "invited"]
    assert e["by"] == "Dana Kest" and e["team"] == "events"
    assert "<@U0DANA> invited Sam" in json.dumps(fake.made("chat.update"))
    assert "Sam Okoro" not in [p["name"] for p in inbox.week("simulation")["event"]["invite"]]  # asked once


def test_a_press_the_record_refuses_keeps_its_button_and_says_why(simulation):
    fake = Fake()
    press = pressing(reply_for("Approve"), "ops_invited", row=0)
    run(app(fake), press)
    run(app(fake), press)  # the same invite again: one ask at a time
    last = fake.made("chat.update")[-1]
    assert "Not recorded" in json.dumps(last)
    assert next(b for b in last["blocks"] if b.get("block_id") == "ops:0").get("accessory")


def test_a_test_press_or_one_from_another_week_records_nothing(simulation):
    fake = Fake()
    reply = reply_for("Approve")
    run(app(fake), pressing(reply, "ops_invited", t=1))
    run(app(fake, mode="live"), pressing(reply, "ops_invited"))
    store = today.source("simulation")[0]
    assert not any(e["kind"] == "invited" for e in contact.history(store, "C801"))
    said = json.dumps(fake.made("chat.update"))
    assert "Test, nothing recorded" in said and "the app is running the live one" in said


def test_the_simulation_s_replies_carry_test_buttons(simulation):
    assert all(json.loads((b.get("accessory") or {}).get("value") or '{"t": 1}')["t"] == 1
               for n, d in enumerate(week()["decisions"], 1) if (r := weekly.reply(d, n, "simulation"))
               for b in r["blocks"])


def test_a_teammates_private_note_never_reaches_the_channel(simulation):
    contact.record(today.source("simulation")[0], "X901", "knows", at="2026-09-14T00:00:00+00:00", by="Omar Lind",
                   note="Friends. Told me in confidence they're interviewing")  # the form's how, then its free text
    said = json.dumps(reply_for("Approve"))
    assert "Omar Lind knows them (Friends)" in said and "confidence" not in said


def test_i_sent_it_records_the_follow_up_once_as_the_team_and_role_of_the_note_it_answers(simulation):
    store = today.source("simulation")[0]
    contact.record(store, "X951", "sent", at="2026-09-05T12:00:00+00:00", team="events", role_id="mts-research",
                   by="Nora Example")
    reply = reply_for("Send X951 their follow-up")
    fake = Fake()
    press = pressing(reply, "ops_followed")
    run(app(fake), press)
    run(app(fake), press)  # pressed twice before the first update landed
    [f] = [e for e in contact.history(store, "X951") if e["kind"] == "follow_up"]
    assert (f["team"], f["role_id"], f["by"]) == ("events", "mts-research", "Dana Kest")
    assert "no follow-up is due for them now" in json.dumps(fake.made("chat.update")[-1])
    assert "X951" not in dict(contact.follow_ups_due(store, "2026-09-17T00:00:00+00:00"))  # the app's clock is past it


def test_a_follow_up_after_the_night_goes_with_its_draft_and_records_the_send(simulation):
    events.add_note("simulation", "gi-2026-09-salon", "X901", "open", "Would like to see the demo.", "Nora Example")
    reply = reply_for("Send Ivy Stroud their follow-up")
    assert "_Draft:_ *Agents in games salon: following up*" in json.dumps(reply)
    fake = Fake()
    run(app(fake), pressing(reply, "ops_followed"))
    assert any(e["kind"] == "sent" and e["by"] == "Dana Kest"
               for e in contact.history(today.source("simulation")[0], "X901"))
    assert "<@U0DANA> sent Ivy their follow-up" in json.dumps(fake.made("chat.update"))
    assert not any(d["title"].startswith("Send Ivy") for d in week()["decisions"])


def test_a_one_pager_that_fails_after_the_week_is_up_never_sends_the_week_again(simulation, monkeypatch, capsys):
    def broken(mode):
        raise KeyError("month")
    monkeypatch.setattr(weekly, "one_pager", broken)
    fake = Fake()
    assert weekly.publish(poster(fake), "simulation", FIRST_MONDAY) is True  # the week is up: said, never raised
    assert "one-pager didn't go out: KeyError" in capsys.readouterr().out
    monkeypatch.setattr(weekly, "week", lambda mode, now: ({"missing": None, "decisions": [], "week_of": "2026-10-05",
                                                            "as_of": "2026-10-05", "source": "Invented people"}, {}))
    with pytest.raises(KeyError):  # nothing went out: the Monday job may try again
        weekly.publish(poster(fake), "simulation", FIRST_MONDAY)


def test_reaches_the_post_leaves_out_go_in_the_next_morning_s_list_and_the_rest_wait_a_week(simulation, monkeypatch):
    monkeypatch.setattr(inbox, "BRIEF_MAX", 3)  # the post holds three: a new start, the invites and Rin
    w, routes = weekly.week("simulation", today.DEMO_AS_OF)
    said = json.dumps(weekly.post(w))
    assert "2 more people to reach go in the next morning's list, within the week's cap. 2 more wait for next week." \
        in said  # Mara and Noa; the check on Lena and the event budget
    monkeypatch.setattr(inbox, "BRIEF_MAX", 5)
    said = json.dumps(weekly.post(w))
    assert "2 more wait for next week." in said and "morning" not in said  # every reach is in the post
    monkeypatch.setattr(inbox, "BRIEF_MAX", 3)
    weekly.publish(poster(Fake()), "simulation", MONDAY)
    store = today.source("simulation")[0]
    assert [e["person_id"] for e in ledger(store)] == ["A900"]  # only the card the post carries is recorded
    left = routing.sendable(store, [routes["X900"], routes["X902"]], today.DEMO_AS_OF)
    assert [r.subject_id for r in left] == ["X900", "X902"]  # so the morning list can still carry both


@pytest.fixture
def live(tmp_path, monkeypatch):
    """The live week over a copy of the invented store, as if it were the Mac's timelines, and a people file."""
    demo, real = today._demo_store()[0], tmp_path / "timelines.sqlite"
    copy = Store(f"sqlite:///{real}")
    for kind in ("person_context", "timeline_event", "contact"):  # the ledger too: who they are, what went out
        for row in demo.all(kind):
            copy.put(row["id"], kind, row)
    monkeypatch.setattr(today, "TIMELINES", real)
    monkeypatch.setattr(today, "iso", lambda: today.DEMO_AS_OF)
    monkeypatch.setitem(events.RECORDS, "live", tmp_path / "no-events")  # never the real research/private records
    people = tmp_path / "people.json"
    monkeypatch.setattr(events, "WATCHLIST", people)
    return people


def test_live_the_monday_post_names_only_the_watchlist_never_a_replay_case(simulation, live):
    moment = [{"date": "2026-09-01", "kind": "job", "source_url": "https://example.org/news"}]
    live.write_text(json.dumps([
        {"person_id": "A900", "name": "Rin Vale", "role": "mts-research", "since": "2026-01-01", "moments": moment},
        {"person_id": "X900", "name": "Mara Quill", "role": "mts-research", "since": "2026-01-01"},
        {"person_id": "X902", "name": "Noa Brandt", "role": "product-designer", "since": "2026-01-01"}]))
    w, routes = weekly.week("live", today.DEMO_AS_OF)
    said = json.dumps(weekly.post(w))
    assert "Reach Mara Quill" in said and "Reach Noa Brandt" in said
    assert "Rin" not in said and "A900" not in routes  # the scorecard's replay case: never a live card
    assert "Lena" not in said  # nor anyone the people file doesn't list


def test_live_the_watchlist_is_route_py_s_own_ids_and_a_replay_case_under_any_id_stays_out(simulation, live):
    moment = [{"date": "2026-09-01", "kind": "job", "source_url": "https://example.org/news"}]
    live.write_text(json.dumps([
        {"person_id": "A900", "role": "mts-research", "since": "2026-01-01", "moments": moment},
        {"person_id": "backend:A900", "role": "backend", "since": "2026-01-01"},  # the same person, no moment
        {"person_id": "X900", "role": "mts-research", "since": "2026-01-01"}]))
    w, routes = weekly.week("live", today.DEMO_AS_OF)
    assert "Rin" not in json.dumps(weekly.post(w)) and "A900" not in routes


def test_live_a_follow_up_keeps_the_person_s_name_off_the_watchlist_too(simulation, live):
    live.write_text(json.dumps([{"person_id": "X900", "role": "mts-research", "since": "2026-01-01"}]))
    contact.record(Store(f"sqlite:///{today.TIMELINES}"), "D904", "sent", at="2026-09-01T12:00:00+00:00", by="Dana")
    titles = [d["title"] for d in weekly.week("live", today.DEMO_AS_OF)[0]["decisions"]]
    assert "Send Lena Okafor their follow-up" in titles  # a note already went to her: her name, not her ledger id


def test_live_a_replay_case_with_a_note_out_gets_no_follow_up_and_nobody_does_until_the_file_reads(simulation, live):
    contact.record(Store(f"sqlite:///{today.TIMELINES}"), "D904", "sent", at="2026-09-01T12:00:00+00:00", by="Dana")
    watched = {"person_id": "X900", "role": "mts-research", "since": "2026-01-01"}
    moment = [{"date": "2026-09-01", "kind": "job", "source_url": "https://example.org/news"}]
    for people, named in (([watched], True),  # a note went to her: her follow-up, though the file doesn't list her
                          ([watched, {"person_id": "mts-research:D904", "role": "mts-research", "since": "2026-01-01",
                                      "moments": moment}], False),  # the scorecard's replay case, by another id
                          ("{not json", False), (None, False)):  # who the replay cases are is unknown: nobody
        if people is None:
            live.unlink()
        else:
            live.write_text(people if isinstance(people, str) else json.dumps(people))
        w = weekly.week("live", today.DEMO_AS_OF)[0]
        assert ("D904" in [f["subject_id"] for f in w["follow_ups"]]) is named
        assert ("Lena" in json.dumps(weekly.post(w))) is named


def test_live_one_read_of_the_people_file_decides_the_week_s_follow_ups(simulation, live, monkeypatch):
    contact.record(Store(f"sqlite:///{today.TIMELINES}"), "D904", "sent", at="2026-09-01T12:00:00+00:00", by="Dana")
    moment = [{"date": "2026-09-01", "kind": "job", "source_url": "https://example.org/news"}]
    live.write_text(json.dumps([{"person_id": "mts-research:D904", "role": "mts-research", "since": "2026-01-01",
                                 "moments": moment}]))
    read, reads = events._people_file, []

    def flaky():  # the file breaks after the brief's first read of it
        reads.append(1)
        return read() if len(reads) == 1 else ([], "The people file could not be read: broken since")
    monkeypatch.setattr(events, "_people_file", flaky)
    w = weekly.week("live", today.DEMO_AS_OF)[0]
    assert len(reads) > 1 and "D904" not in [f["subject_id"] for f in w["follow_ups"]]


def test_live_a_replay_case_s_follow_up_after_the_night_is_never_in_the_post(simulation, live, tmp_path, monkeypatch):
    shutil.copytree(ROOT / "tests/fixtures/events", tmp_path / "events")  # GI's event records, as if real
    monkeypatch.setitem(events.RECORDS, "live", tmp_path / "events")
    events.add_note("live", "gi-2026-09-salon", "X901", "open", "up for a real conversation", "Nora Example")
    watched = {"person_id": "X900", "role": "mts-research", "since": "2026-01-01"}
    moment = [{"date": "2026-09-01", "kind": "job", "source_url": "https://example.org/news"}]
    for people, named in (([watched], True),  # she came and talked with Nora: her follow-up
                          ([watched, {"person_id": "mts-research:X901", "role": "mts-research", "since": "2026-01-01",
                                      "moments": moment}], False),  # the scorecard's replay case, by another id
                          ('[{"person_id": "X902"}]', False), (None, False)):  # won't read, or missing: nobody
        if people is None:
            live.unlink()
        else:
            live.write_text(people if isinstance(people, str) else json.dumps(people))
        w = weekly.week("live", today.DEMO_AS_OF)[0]
        assert ("X901" in [f["subject_id"] for f in w["follow_ups"]]) is named
        assert ("Ivy" in json.dumps(weekly.post(w))) is named


def test_live_without_a_people_file_that_reads_the_post_names_nobody_and_says_why(simulation, live, tmp_path,
                                                                                    monkeypatch):
    shutil.copytree(ROOT / "tests/fixtures/events", tmp_path / "events")  # GI's event records, as if real
    monkeypatch.setitem(events.RECORDS, "live", tmp_path / "events")
    for broken in (None, "{not json", "null", json.dumps([{"person_id": "X900", "since": "2026-01-01"}])):
        if broken is None:
            live.unlink(missing_ok=True)
        else:
            live.write_text(broken)
        w = weekly.week("live", today.DEMO_AS_OF)[0]
        first, *rest = w["decisions"]
        assert first["title"] == "Fix the people file" and first["overdue"]
        assert first["why"].startswith("The people file (people.json) " + ("is missing" if broken is None
                                                                            else "doesn't read"))
        assert not any(d["title"].startswith(("Reach", "Check", "Approve", "Fix the people file before")) for d in rest)
        said = json.dumps(weekly.post(w))
        assert "reach, check or invite" in said and str(tmp_path) not in said and "pydantic" not in said
        assert w["event"]["invite"] == [] and w["event"]["blocked"]  # the web brief's panel names nobody either
        assert ("Events page" in first["why"]) is (broken is not None)  # it says why only when the file is there


# Another process holding a ledger's send lock, as route.py --send does while its morning list goes out: it cards the
# person on the line it is given (a ledger entry), then lets go.
HOLD = """
import json, sys
from app import contact, routing
from app.store import Store
s = Store(sys.argv[1])
with routing.sending(s):
    print("held", flush=True)
    if line := sys.stdin.readline().strip():
        contact.record(s, **json.loads(line))
"""


@pytest.fixture
def holder():
    started = []

    def hold(path):
        other = subprocess.Popen([sys.executable, "-c", HOLD, f"sqlite:///{path}"], cwd=ROOT, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        started.append(other)
        assert other.stdout.readline().strip() == "held"
        return other
    yield hold
    for other in started:  # a test that failed midway never leaves the lock held
        other.kill()
        other.wait()


def carded(route, at):
    """The ledger entry send() writes for this route's card."""
    return {"person_id": route.subject_id, "kind": "pinged", "at": at, "role_id": route.role["id"],
            "evidence": [i for ids in route.call.evidence.values() for i in ids], "score": route.call.score}


def test_the_morning_list_waits_for_another_sender_and_counts_the_card_it_sent(tmp_path, monkeypatch, holder):
    s = Store(f"sqlite:///{tmp_path / 't.sqlite'}")
    routing.seed(s, json.loads((FIXTURES / "routing" / "timeline.json").read_text()))
    contact.record(s, "A900", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    rin = routing.route(s, "A900", AS_OF, team=[])
    clock = ["2026-09-15T08:00:00+00:00"]
    monkeypatch.setattr(routing, "iso", lambda: clock[0])
    other, seen, done = holder(tmp_path / "t.sqlite"), [], []
    morning = threading.Thread(target=lambda: done.append(
        routing.send_morning(s, [rin], "https://hooks.slack.example/x", client=webhook_client(seen))), daemon=True)
    morning.start()
    morning.join(0.5)
    assert morning.is_alive() and seen == []  # it waits for the other sender
    clock[0] = "2026-09-15T08:10:00+00:00"  # the other sender cards Rin at 08:05, then lets go at 08:10
    other.communicate(json.dumps(carded(rin, "2026-09-15T08:05:00+00:00")) + "\n", timeout=30)
    morning.join(30)
    assert done == [[]] and seen == []  # the time is read once it may send: the other's card counts, nothing posts
    assert [e["at"] for e in s.all("contact") if e["kind"] == "pinged"] == ["2026-09-15T08:05:00+00:00"]


def test_the_monday_post_waits_for_a_morning_list_going_out_and_never_cards_its_people_twice(simulation, holder):
    store = today.source("simulation")[0]
    rin = weekly.week("simulation", today.DEMO_AS_OF)[1]["A900"]
    other, fake, done = holder(contact._file(store)), Fake(), []
    monday = threading.Thread(target=lambda: done.append(weekly.publish(poster(fake), "simulation", MONDAY)),
                              daemon=True)
    monday.start()
    monday.join(0.5)
    assert monday.is_alive() and fake.made("chat.postMessage") == []  # it waits for the morning list
    other.communicate(json.dumps(carded(rin, today.DEMO_AS_OF)) + "\n", timeout=30)  # which cards Rin Vale
    monday.join(60)
    assert done == [True]
    first, *replies = fake.made("chat.postMessage")
    assert "Rin Vale" not in json.dumps([first, *replies]) and "Reach Mara Quill" in texts(first)
    assert sorted(e["person_id"] for e in ledger(store)) == sorted(REACHES)  # Rin once, by the morning list


def test_a_sender_that_waits_too_long_posts_nothing_and_says_why(tmp_path, holder, capsys, monkeypatch):
    s = Store(f"sqlite:///{tmp_path / 't.sqlite'}")
    other = holder(tmp_path / "t.sqlite")
    with pytest.raises(RuntimeError, match="kept the ledger's send lock too long, so nothing was posted"):
        with routing.sending(s, wait=0.3):
            raise AssertionError("never reached while the other sender holds it")
    assert "Waiting for the other sender" in capsys.readouterr().err
    other.communicate("\n", timeout=30)
    with routing.sending(s, wait=0.3):  # free again
        pass
    monkeypatch.chdir(tmp_path)
    with routing.sending(Store("sqlite:///:memory:")):  # no file, so no lock file beside it
        pass
    assert not list(tmp_path.glob(":memory:*"))
