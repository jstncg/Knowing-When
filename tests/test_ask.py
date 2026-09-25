"""The model path of Ask about this person (app/ask.py), in isolation: the one part the end-to-end run can't reach,
since it needs the model's key and costs money, and what the free answers claim. The panel itself is checked end to
end (tests/e2e/todays_calls.cjs).

Ways it could fail, written before its code was reworked (Justin's rule, 2026-09-24):
1. It spends with no key, or counts a call that was never sent against the cap.
2. A question is asked twice and paid twice.
3. It runs past the cap: a question whose worst case would take the spend over CAP_USD is still sent.
4. A failed call that may have been billed is not counted at all.
5. The model cites an item number that isn't in the list, and the page links it to the wrong item or to nothing.
6. An empty, whitespace or overlong question reaches the model.
7. The prompt lets the answer estimate leaving, pay or anything personal, or treat the items as instructions.
8. The model sees another person's items, or items the page would not show for this person.
9. Someone not in the workspace gets an answer instead of a 404.

Written after review, before the code that answers them:
10. A model line says something personal or about leaving or pay, or cites only items that don't exist, and is shown.
11. A post that touches something personal reaches the model in its own words.
12. A free answer claims more than the data holds: "whom they follow on X" checked when the read found nothing
    usable; posts the reader never tagged counted as judged off its topics; a topic "close" on words most topics
    share, or on the reader's own labels; a role with no topics on file read as no match; lines that contradict.
13. A rejected call (a bad key, a rate limit: nothing billed) is charged against the cap.
14. A suggestion that isn't a string answers with a server error instead of a 422.

Written after the Mac's check on real data (2026-09-24), before the code that answers them:
15. The words of a post the post reader read nothing off (a curse, politics, a casual reply) reach the model.
16. The job description match rests on single common words ("app", "back"), which any casual post uses.

Written after the Mac's second check on real data (2026-09-24), before the code that answers them:
17. The words of the post they replied to or quoted reach the model, or a free answer matches on them.
18. The team answer says one shared topic once per teammate, or in a form their own words don't use ("world model"
    for their "world models").

Found in review, before the code that answers them:
19. The team answer still says a topic twice when teammates list their shared topics in another order or share only
    some of them; or it lowercases a name or an acronym ("Doom", "JEPA"), says a stem no one wrote ("physic"), or a
    form their words join into one word ("world-models" in a link).
20. The job description count leaves a post that put a paper out in none of its groups, or counts one paper once per
    version, once more per post about it, or twice when it is on arXiv and accepted at a venue.
21. What would prove the call wrong, which Ask passes on, quotes the post they replied to, or quotes their own words
    as the conflict when the words the engine matched were in the post they answered.
22. A placeholder in a quote's place (not quoted, withheld) is matched as their words.
23. A reply with no words of their own shows an empty quote.

Found in the Mac's check of 243a7b1, before the code that answers it:
24. The job description count's kinds add up to more than its total (one item read as two kinds) and the line doesn't
    say why, so it reads as a wrong sum; or it says so when they don't.
"""

import asyncio
import json
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import ask, main, outreach, providers, routing, today
from app.models import iso, utcnow

KEY = {"anthropic_key": "k"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(ask, "_spent", {"usd": 0.0})
    monkeypatch.setattr(ask, "_answers", {})


@pytest.fixture
def billed(model, monkeypatch):
    """The model stub, with its usage counted as providers._post counts a real reply's."""
    async def post(url, key, payload, provider, budget, workspace_id=None):
        out = await model(url, key, payload, provider, budget, workspace_id)
        for k in budget.tokens:
            budget.tokens[k] += out["usage"][k]
        return out
    monkeypatch.setattr(providers, "_post", post)
    return model


def reply(model, lines):
    return model.reply({"lines": lines})


def asked(question, settings=KEY, subject="X900"):
    return asyncio.run(ask.ask("simulation", subject, question, settings))


def test_no_key_and_bad_questions_never_reach_the_model(model):  # 1, 6
    for question, settings in (("Where do they work?", {}), ("   ", KEY), ("x" * (ask.QUESTION_MAX + 1), KEY)):
        with pytest.raises(ValueError):
            asked(question, settings)
    assert model.calls == [] and ask._spent["usd"] == 0


def test_an_answer_keeps_only_real_cites_and_costs_once(billed):  # 2, 5, 8
    model = billed
    reply(model, [{"text": "They asked for action-labeled gameplay video.", "cites": [1, 1, 99]}])
    first = asked("What did they ask for?")
    assert first["lines"][0]["cites"] == [1] and [s["n"] for s in first["sources"]] == [1]
    assert first["sources"][0]["url"] == "https://x.com/maraquill_example/status/1002"
    cost = outreach.usd({"input_tokens": 10, "output_tokens": 5})
    assert first["usd"] == round(cost, 4) and ask._spent["usd"] == pytest.approx(cost)  # the worst case given back
    again = asked(" What did  they ask for? ")
    assert again["cached"] and again["usd"] == 0 and len(model.calls) == 1
    sent = model.calls[0]["payload"]
    assert "Never estimate whether they would leave" in sent["system"] and "untrusted data" in sent["system"]  # 7
    items = json.loads(sent["messages"][0]["content"])["items"]
    # only Mara's own posts, and only the one the post reader read something off (8, 15)
    assert {i["source"] for i in items} == {"Public posts on X"} and len(items) == 1 and items[0]["read_as"]


def test_the_cap_holds_before_sending_and_a_failed_call_counts_at_its_worst(model):  # 3, 4
    ask._spent["usd"] = ask.CAP_USD - 0.001
    with pytest.raises(ValueError, match="more than"):
        asked("What did they ask for?")
    assert model.calls == []
    ask._spent["usd"] = 0.0
    with pytest.raises(Exception):
        asked("What did they ask for?")  # the stub has no reply: the call fails
    payload = ask.content(ask.facts("simulation", "X900"), "What did they ask for?")
    assert ask._spent["usd"] == pytest.approx(ask.worst_usd(payload))


def test_someone_not_in_the_workspace_gets_a_404_and_a_bad_suggestion_a_422():  # 9
    client = TestClient(main.app)
    assert client.post("/api/calls/nobody/ask", json={"mode": "simulation", "suggested": "jd"}).status_code == 404
    assert client.post("/api/calls/X900/ask", json={"mode": "simulation", "suggested": "pay"}).status_code == 422


def test_model_lines_off_the_rules_are_held_back(billed):  # 10
    reply(billed, [{"text": "They asked for action-labeled gameplay video.", "cites": [1]},
                   {"text": "They are probably ready to leave Acme.", "cites": [1]},
                   {"text": "Their post mentions their divorce.", "cites": [2]},
                   {"text": "They shipped a robot arm.", "cites": [42]},
                   {"text": "The data doesn't say what they built last year.", "cites": []}])
    lines = [line["text"] for line in asked("What are they up to?")["lines"]]
    assert lines[:2] == ["They asked for action-labeled gameplay video.",
                         "The data doesn't say what they built last year."]
    assert lines[2].startswith("3 of the model's lines held back") and len(lines) == 3


def test_a_personal_post_reaches_the_model_withheld_and_an_unread_one_not_at_all(monkeypatch, tmp_path):  # 11, 15
    for name in ("TIMELINES", "TEAM", "CONTACTS", "FOLLOWS"):  # a live workspace of invented people only
        monkeypatch.setattr(today, name, tmp_path / name.lower())
    store = today.ledger()
    for folder in today.DEMO:
        routing.seed(store, json.loads((folder / "timeline.json").read_text()))
    at = iso(utcnow())

    def post(url, quote, event_type="x_post"):
        return {"subject_id": "X900", "event_type": event_type, "event_date": at[:10], "observed_at": at,
                "source_url": f"https://x.com/maraquill_example/status/{url}", "quote": quote}
    routing.seed(store, {"events": [post(9, "Back at my desk after surgery last month, and back on world models."),
                                    post(9, "world models", "gi_topic"),  # read as on GI's topics: shown, withheld
                                    post(10, "is there any online shopping site that isn't fucking awful")]})
    sent = json.dumps(ask.content(ask.facts("live", "X900"), "What's new?"))
    assert "surgery" not in sent and routing.WITHHELD in sent
    assert "shopping" not in sent


def test_free_answers_claim_no_more_than_the_data(monkeypatch):  # 12
    f = ask.facts("simulation", "X900")
    f["follows"] = {"x_handle": "maraquill_example", "follows": None, "complete": False}  # the read found nothing usable
    assert not any("follow" in line["text"] for line in ask.team(f))
    f["follows"] = {"x_handle": "someone_else", "follows": []}  # a read of another account
    assert not any("follow" in line["text"] for line in ask.team(f))
    f["follows"] = {"x_handle": "maraquill_example", "follows": []}
    assert any(line["text"].startswith("On X they follow no one") for line in ask.team(f))
    for sid in ("X900", "D903", "X901", "D901"):
        said = ask.jd(ask.facts("simulation", sid))[0]["text"]
        area, stored, topics, wip, asks = map(int, re.findall(r"\d+", said)[:5])
        assert area <= stored and max(topics, wip, asks) <= area  # the tags within the area, the area within the stored
    matched = ask.jd(ask.facts("simulation", "X900"))[1:-1]
    assert matched and all(line["text"].startswith("Uses the job description's phrase") for line in matched)
    for said in (p for line in matched for p in re.findall(r'"([^"]+)"', line["text"])):  # 16
        assert " " in said or "-" in said or said != said.lower(), said
    assert ask.phrases("US and international tax") == ["international tax"]  # "US" would match "tell us"
    assert ask.phrases("Payments and billing") == []  # a capital where the topic starts is no name
    f = ask.facts("simulation", "D903")  # a casual reply, read as work, uses a topic's words but none of its phrases
    reply_ = next(i for i in f["items"] if i["whose"] == "theirs" and i["read"])
    for said in ("god bring that old app icon back", "a bit of discord in the team today"):  # "Discord" is a name
        f["items"] = [{**reply_, "quote": said}]
        assert ask.jd(f)[-1]["text"].startswith("No phrase from the Product Designer job description"), said
    f = ask.facts("simulation", "X900")
    f["topics"] = []
    assert ask.jd(f)[-1]["text"].startswith("No job description topics are on file")


def test_a_refused_call_costs_nothing(model, monkeypatch):  # 13
    async def refused(url, key, payload, provider, budget, workspace_id=None):
        budget.take()  # as providers._post does before it sends
        raise providers.ProviderRejected("The model refused the key.")
    monkeypatch.setattr(providers, "_post", refused)
    with pytest.raises(providers.ProviderRejected):
        asked("What did they ask for?")
    assert ask._spent["usd"] == 0


def test_a_suggestion_that_is_not_a_name_is_a_422():  # 14
    assert TestClient(main.app).post("/api/calls/X900/ask", json={"mode": "simulation", "suggested": ["jd"]}).status_code == 422


def test_only_their_own_words_are_given_or_matched(monkeypatch, tmp_path):  # 17, 23
    for name in ("TIMELINES", "TEAM", "CONTACTS", "FOLLOWS"):
        monkeypatch.setattr(today, name, tmp_path / name.lower())
    store = today.ledger()
    for folder in today.DEMO:
        routing.seed(store, json.loads((folder / "timeline.json").read_text()))
    at, url = iso(utcnow()), "https://x.com/maraquill_example/status/12"
    answered = "Our quarterly update on the harbour ferry timetable, now with world models."
    routing.seed(store, {"events": [
        {"subject_id": "X900", "event_type": "x_post", "event_date": at[:10], "observed_at": at, "source_url": url,
         "quote": "Grateful for this team." + routing.REPLY_CONTEXT + "@ferrywatch_example: " + answered},
        {"subject_id": "X900", "event_type": "gi_topic", "event_date": at[:10], "observed_at": at, "source_url": url,
         "quote": "world models"}]})
    f = ask.facts("live", "X900")
    sent = json.dumps(ask.content(f, "What's new?"))
    assert "ferry" not in sent and "ferrywatch_example" not in sent and "Grateful for this team." in sent
    # 23: a reply that is only the post it answers (no reader tag holds on it, so it is only ever shown this way)
    bare = {"event_type": "x_reply", "quote": routing.REPLY_CONTEXT.lstrip() + "@ferrywatch_example: " + answered}
    assert routing.own_quote(bare) == routing.NO_WORDS
    mine = next(i for i in f["items"] if i["url"] == url)
    assert not any(mine["n"] in line["cites"] for line in ask.jd(f)[1:])  # "world models" is in the answered post only


def test_a_shared_topic_is_said_once_as_they_write_it():  # 18
    f = ask.facts("simulation", "X900")
    f["items"] = [{**i, "quote": "Our world models keep overfitting to three games."} if i["read"] else i
                  for i in f["items"]]
    f["team"] = [{"name": n, "works_on": ["world model"], "signs_for": []} for n in ("Ana Reyes", "Bo Lindqvist")] + \
        [{"name": "Cy Moss", "works_on": ["world model", "scaling laws"], "signs_for": []}]  # the laws: not theirs
    said = [line["text"] for line in ask.team(f) if "works on" in line["text"] or " work on" in line["text"]]
    assert sum("world model" in t for t in said) == 1 and any("world models" in t for t in said), said
    assert all(said_.count(n) <= 1 for said_ in said for n in ("Ana Reyes", "Bo Lindqvist", "Cy Moss")), said
    assert sum(t.count("Cy Moss") for t in said) >= 1 and sum(t.count("Ana Reyes") for t in said) == 1, said


def test_each_shared_topic_is_said_once_with_their_plural_and_the_team_file_capitals():  # 19, 22
    f = ask.facts("simulation", "X900")
    words = "Code: github.com/ex/world-models. Our World Models learn doom from JEPAs, with scaling laws and physics."
    f["items"] = [{**i, "quote": words} if i["read"] else i for i in f["items"]] + \
        [{**f["items"][0], "n": 99, "whose": "theirs", "quote": q, "read": [], "shipped": ""}
         for q in (routing.UNREAD, routing.WITHHELD)]  # notes in a quote's place: not their words
    f["team"] = [{"name": "Ana Reyes", "works_on": ["world model", "scaling laws"], "signs_for": []},
                 {"name": "Bo Lindqvist", "works_on": ["scaling laws", "world model"], "signs_for": []},  # another order
                 {"name": "Cy Moss", "works_on": ["world model", "Doom", "JEPA"], "signs_for": []},  # some shared
                 {"name": "Dee Park", "works_on": ["physics", "read as a sign", "something personal"], "signs_for": []}]
    said = [line["text"] for line in ask.team(f) if line["text"].startswith("Their items mention")]
    every = " ".join(said)
    for topic in ("world models", "scaling laws", "Doom", "JEPAs", "physics"):
        assert every.count(topic) == 1, (topic, said)
    assert not re.search(r"\bdoom\b|\bjepa|\bphysic\b|World Models|world-models|read as a sign|something personal", every), said
    assert all(line.count(n) <= 1 for line in said for n in ("Ana Reyes", "Bo Lindqvist", "Cy Moss", "Dee Park")), said


def test_the_count_puts_a_post_that_puts_a_paper_out_in_a_group_and_counts_each_paper_once():  # 20
    f = ask.facts("simulation", "X900")
    at = f["as_of"]

    def ev(t, url, quote):
        return {"id": len(url) + len(t), "subject_id": "X900", "subject_type": "person", "event_type": t,
                "event_date": at[:10], "observed_at": at, "source_url": url, "source_version_hash": "h",
                "quote": quote, "tier": 1}
    post = "https://x.com/maraquill_example/status/77"
    f["events"] = [ev("x_post", post, "Our new paper is out: arxiv.org/abs/2608.01234. World models from play."),
                   ev("publication", post, "Our new paper is out"),
                   ev("paper_v1", "https://arxiv.org/abs/2608.01234v1", "World models from play (2608.01234v1): published"),
                   ev("paper_revised", "https://arxiv.org/abs/2608.01234v2", "World models from play (2608.01234v2): v2"),
                   ev("paper_accepted", "https://openreview.net/forum?id=ex0", "World models from play: Accept (Poster) (ICLR 2026)"),
                   ev("paper_accepted", "https://openreview.net/forum?id=ex1", "Latent actions: Accept (Example 2026)")]
    for n, e in enumerate(f["events"]):
        e["id"] = n + 1
    said = ask.jd(f)[0]["text"]
    assert re.search(r"tagged 1 of their 1 ", said) and "1 a paper out" in said, said
    # one on arXiv in two versions and accepted at a venue, one accepted; not the post
    assert "2 papers of theirs are on file from the paper indexes" in said, said


def test_what_would_prove_it_wrong_quotes_only_their_own_words(monkeypatch, tmp_path):  # 21
    for name in ("TIMELINES", "TEAM", "CONTACTS", "FOLLOWS"):
        monkeypatch.setattr(today, name, tmp_path / name.lower())
    store = today.ledger()
    for folder in today.DEMO:
        routing.seed(store, json.loads((folder / "timeline.json").read_text()))
    now = utcnow()

    def ev(t, days, quote, url, **more):
        d = now - timedelta(days=days)
        return {"subject_id": "X900", "event_type": t, "event_date": d.date().isoformat(), "observed_at": iso(d),
                "quote": quote, "source_url": url, **more}
    answered = "Oldco just shipped its first world model, says the harbour master."
    routing.seed(store, {"events": [
        ev("job_started", 600, "Research Scientist, Oldco", "https://www.linkedin.com/in/maraquill-example/1"),
        ev("job_started", 90, "Research Scientist, Newco", "https://www.linkedin.com/in/maraquill-example/2"),
        # after the move, a reply whose answered post names the old employer: the engine holds to check where they work
        ev("x_post", 5, "Proud of this one." + routing.REPLY_CONTEXT + "@ferrywatch_example: " + answered,
           "https://x.com/maraquill_example/status/78")]})
    f = ask.facts("live", "X900")
    wrong = ask.content(f, "What's new?")["call"]["what_would_prove_it_wrong"]
    # the check is there, naming their post and the one it answers, where the engine found the old employer
    assert any("conflicts with their post of" in w and "and the post it answers or quotes" in w for w in wrong), wrong
    assert not any(x in w for w in wrong for x in ("harbour master", "ferrywatch_example", "In reply to", "Proud of")), wrong
    assert f["p"]["falsifiers"] == wrong  # part 5 on the page shows the same


def test_the_count_says_when_an_item_counts_under_more_than_one_kind():  # 24
    f = ask.facts("simulation", "X900")
    at = f["as_of"]

    def ev(n, t, url, quote):
        return {"id": n, "subject_id": "X900", "subject_type": "person", "event_type": t, "event_date": at[:10],
                "observed_at": at, "source_url": url, "source_version_hash": "h", "quote": quote, "tier": 1}
    one, two = "https://x.com/maraquill_example/status/81", "https://x.com/maraquill_example/status/82"
    f["events"] = [ev(1, "x_post", one, "Training our world model on longer clips this week."),
                   ev(2, "gi_topic", one, "world model"), ev(3, "work_in_progress", one, "Training our world model"),
                   ev(4, "x_post", two, "Anyone have spare GPU hours for a world model run?"),
                   ev(5, "technical_ask", two, "spare GPU hours")]
    said = ask.jd(f)[0]["text"]
    counts = list(map(int, re.findall(r"\d+", said)[:7]))
    area, kinds = counts[0], sum(counts[2:])
    assert (area, kinds) == (2, 3) and "; some items count under more than one kind;" in said, said
    f["events"] = [e for e in f["events"] if e["id"] != 2]  # now one kind each: the sum adds up
    said = ask.jd(f)[0]["text"]
    assert "more than one kind" not in said, said
