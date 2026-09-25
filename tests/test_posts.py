"""Early signs from a person's own posts: the reader, the detectors, the call, the ranking and the draft."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from app import contact, detectors, extraction, journey, providers, readiness, routing, scorecard, today
from app.timeline import DETECTORS, add_event, timeline
from tests.test_detectors import ME, ev, fire

AS_OF = "2026-09-15T00:00:00+00:00"
POST = "https://x.com/me/status/1"
FIXTURES = Path(__file__).parent / "fixtures" / "posts"


def call(*events, as_of=AS_OF):
    events = list(events)
    return readiness.score(detectors.detect(events, ME, as_of), as_of, events)


def posted(day, quote="x", n=None, kind="x_post"):
    return ev(ME, kind, day, quote=quote, source_url=f"{POST}{n or day}")


def tagged(post, sign="gi_topic"):
    return ev(ME, sign, post["event_date"], quote=post["quote"], source_url=post["source_url"])


def test_a_fresh_early_sign_alone_is_a_note_about_their_work_and_goes_stale():
    ask = ev(ME, "technical_ask", "2026-09-10", quote="Anyone have action-labeled game video?")
    now = call(ask)
    assert (now.action, now.track, now.confidence) == ("reach_now", "rapport", "medium")
    assert readiness.opener(now) == ask["id"] and "opens with their work" in now.explanation
    later = call(ask, as_of="2026-09-30T00:00:00+00:00")  # the ask's 14 days are over
    assert (later.action, later.reasons, later.track) == ("quiet", [], None)


def test_an_early_sign_with_a_sign_they_would_listen_is_a_pitch_and_a_paper_is_not_such_a_sign():
    wip = ev(ME, "work_in_progress", "2026-09-10")
    cfo_left = ev("cfo|acme", "officer_departure", "2026-09-01")
    assert call(wip, cfo_left).track == "pitch"
    assert call(wip, ev(ME, "similar_path", "2026-09-12")).track == "rapport"  # it predicts a move: listed, never counted
    assert call(wip, ev(ME, "associate_joined", "2026-09-12")).track == "pitch"  # someone close just joined
    assert call(wip, ev(ME, "paper_v1", "2026-09-05")).track == "rapport"
    # Signals without a sign in their own words keep the rules they had.
    assert call(cfo_left).action == "quiet"
    assert call(ev(ME, "associate_joined", "2026-09-12")).action == "quiet"


def test_holds_and_an_opt_out_still_win_over_an_early_sign():
    wip = ev(ME, "just_submitted", "2026-09-10")
    started = ev(ME, "job_started", "2026-06-01")
    held = call(wip, started)
    assert (held.action, held.until[:10], held.track) == ("watch_until", "2027-06-01", None)
    no = ev(ME, "contact_constraint", None, observed="2026-09-01T00:00:00+00:00", quote="Not looking, no recruiters.")
    assert call(wip, no).action == "quiet"


def test_topic_drift_is_more_posts_on_gis_topics_than_their_own_past():
    old = [posted(d) for d in ("2026-03-01", "2026-04-01", "2026-05-01", "2026-06-01")]
    new = [posted(d, "world models on game clips") for d in ("2026-08-20", "2026-09-10")]
    assert "topic_drift" not in fire(old + new[:1], "2026-09-15")  # one on-topic post is not drift
    drift = fire(old + new + [tagged(p) for p in new], "2026-09-15")["topic_drift"]
    assert (drift["family"], drift["window_open"][:10]) == ("drift", "2026-09-10")
    # Someone who always posted on GI's topics has not drifted toward them.
    always = old + new + [tagged(p) for p in old + new]
    assert "topic_drift" not in fire(always, "2026-09-15")


def test_new_field_contacts_count_only_people_in_the_field_by_then_and_new_to_them(monkeypatch):
    monkeypatch.setattr(detectors, "FIELD", {"@ada": detectors.date(2025, 1, 1), "@ben": detectors.date(2026, 1, 1),
                                             "cy lee": detectors.date(2026, 9, 1)})
    reply = lambda day, to, n: posted(day, "Nice result." + detectors.REPLY_CONTEXT + to, n)  # noqa: E731
    two = [reply("2026-09-01", "@ada", 1), posted("2026-09-05", "cc @Ben: our rollouts", 2)]
    got = fire(two, "2026-09-15")["new_field_contact"]
    assert (got["family"], got["window_open"][:10]) == ("drift", "2026-09-05")
    assert "new_field_contact" not in fire(two[:1], "2026-09-15")  # one is not enough
    # Talking to someone before they were in the field does not count: Cy joined it on 1 September.
    assert "new_field_contact" not in fire([two[0], reply("2026-08-20", "Cy Lee: we should talk", 3)], "2026-09-15")
    # Nor does someone they already talked to within 90 days.
    assert "new_field_contact" not in fire([reply("2026-07-01", "@ben", 4)] + two, "2026-09-15")


def test_a_posting_burst_is_against_their_own_rate():
    usual = [posted(f"2026-{m:02d}-{d:02d}") for m in range(3, 9) for d in (3, 17)]  # two a month
    burst = [posted(f"2026-09-{d:02d}") for d in range(1, 8)]
    got = fire(usual + burst, "2026-09-10")["posting_burst"]
    assert (got["family"], got["window_open"][:10]) == ("drift", "2026-09-07")  # fresh while it lasts, like the others
    assert "posting_burst" not in fire(usual + burst[:3], "2026-09-10")  # 4 in 30 days is under three times their pace


def test_drift_alone_is_never_a_reach_and_with_an_early_sign_it_is_a_pitch():
    old = [posted(d) for d in ("2026-03-01", "2026-04-01", "2026-05-01", "2026-06-01")]
    new = [posted(d, "world models on game clips") for d in ("2026-08-20", "2026-09-10")]
    burst = [posted(f"2026-09-{d:02d}", n=f"b{d}") for d in range(1, 8)]
    drifting = old + new + [tagged(p) for p in new] + burst
    alone = call(*drifting)
    assert {"topic_drift", "posting_burst"} <= set(alone.reasons) and alone.action == "quiet"
    pitched = call(*drifting, tagged(new[-1], "work_in_progress"))
    assert (pitched.action, pitched.track) == ("reach_now", "pitch")


def test_the_reader_dates_a_sign_by_the_post_and_reads_only_the_persons_own_words(model, store, settings):
    text = ("Our world model overfits to three games. Who has action-labeled gameplay video?"
            + extraction.REPLY_CONTEXT + "@lab: We just shipped a world model trained on gameplay.")
    model.reply({"events": [
        {"post": 1, "event_type": "gi_topic", "event_date": "2026-10-01", "about_subject": True,
         "quote": "Our world model overfits to three games."},
        {"post": 1, "event_type": "technical_ask", "event_date": None, "about_subject": True,
         "quote": "Who has action-labeled gameplay video?"},
        # The post they answered is someone else's words.
        {"post": 1, "event_type": "work_in_progress", "event_date": None, "about_subject": True,
         "quote": "We just shipped a world model trained on gameplay."},
    ]})
    one = {"source_url": POST, "event_date": "2026-09-10", "observed_at": "2026-09-10T14:00:00+00:00", "quote": text}
    events = asyncio.run(extraction.extract_posts([one], {"type": "person", "id": ME, "name": "Me"},
                                                  settings={**settings, "small_model": "claude-sonnet-5"}, store=store))
    assert {(e.event_type, e.event_date) for e in events} == {("gi_topic", "2026-09-10"),
                                                              ("technical_ask", "2026-09-10")}
    assert model.calls[0]["payload"]["model"] == "claude-sonnet-5"
    sent = json.loads(model.calls[0]["payload"]["messages"][0]["content"])
    assert sent["focus"] == extraction.FOCUS and not any("Controller" in line for line in sent["focus"])
    # Each watched role's spec topics say what is close to its work; GitHub activity counts for engineers.
    assert [line.split(":")[0] for line in sent["focus"][1:]] == [
        "Member of Technical Staff", "Senior / Lead Backend Engineer", "Product Designer"]
    assert "DIAMOND" in sent["focus"][2] and "Discord" in sent["focus"][3]
    # A person's posts are read for their own role only.
    assert extraction.focus_for("backend") == [extraction.GI, sent["focus"][2]]
    # A web page is not a post: its text is all on the page, "In reply to" or not.
    model.reply({"events": [{"event_type": "work_in_progress", "event_date": None, "about_subject": True,
                             "quote": "We just shipped a world model trained on gameplay."}]})
    page = asyncio.run(extraction.extract_events(text, "https://me.example", "2026-09-10T14:00:00+00:00",
                                                 {"type": "person", "id": ME, "name": "Me"},
                                                 settings=settings, store=store, published_at="2026-09-10"))
    assert [e.event_type for e in page] == ["work_in_progress"]


def test_a_quote_that_is_the_public_moment_is_never_also_an_early_sign(model, store, settings):
    text = "Our paper on world models from gameplay is out today!"
    model.reply({"events": [
        {"event_type": "publication", "event_date": None, "about_subject": True, "quote": text},
        {"event_type": "work_in_progress", "event_date": None, "about_subject": True, "quote": text},
        {"event_type": "gi_topic", "event_date": None, "about_subject": True, "quote": text},
    ]})
    events = asyncio.run(extraction.extract_events(text, POST, "2026-09-10T14:00:00+00:00",
                                                   {"type": "person", "id": ME, "name": "Me"},
                                                   settings=settings, store=store, tier=1, published_at="2026-09-10"))
    assert [e.event_type for e in events] == ["publication"]


def post(store, n, text, day):
    return add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "x_post", "event_date": day,
                             "date_precision": "day", "observed_at": f"{day}T12:00:00+00:00",
                             "source_url": f"{POST}{n}", "source_version_hash": f"h{n}", "quote": text, "tier": 1,
                             "extractor": "apify_x_v1"})


def test_read_posts_reads_a_week_per_call_newest_first_and_stops_at_the_cap(model, store, settings):
    post(store, 1, "Old thoughts on world models.", "2026-08-01")
    post(store, 2, "Anyone have action-labeled game video?", "2026-09-10")
    post(store, 3, "Our rollouts drift after 40 frames.", "2026-09-12")
    add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "profile_change", "event_date": None,
                      "date_precision": None, "observed_at": "2026-09-01T00:00:00+00:00", "quote": "[]", "tier": 3,
                      "extractor": "test"})
    model.reply({"events": [
        {"post": 1, "event_type": "technical_ask", "event_date": None, "about_subject": True,
         "quote": "Anyone have action-labeled game video?"},
        {"post": 2, "event_type": "work_in_progress", "event_date": None, "about_subject": True,
         "quote": "Our rollouts drift after 40 frames."},
        # A quote from the other post in the batch is not this post's words.
        {"post": 2, "event_type": "technical_ask", "event_date": None, "about_subject": True,
         "quote": "Anyone have action-labeled game video?"},
    ]})
    subject = {"type": "person", "id": ME, "name": "Me"}
    assert extraction.unread(store, subject, role="mts-research", settings=settings, as_of=AS_OF) == 2
    first = asyncio.run(extraction.read_posts(store, subject, role="mts-research", settings=settings,
                                              budget=providers.Budget({"max_calls_per_run": 1}), as_of=AS_OF))
    assert (first["posts"], first["calls"], first["events"], len(first["errors"])) == (3, 2, 2, 1)  # the cap stopped August
    sent = json.loads(model.calls[0]["payload"]["messages"][0]["content"])  # one call per calendar week
    assert [p["text"] for p in sent["posts"]] == ["Anyone have action-labeled game video?", "Our rollouts drift after 40 frames."]
    assert [p["text"] for p in sent["earlier_posts"]] == ["Old thoughts on world models."]  # the past, as context only
    signs = {(e["event_type"], e["source_url"], e["event_date"], e["observed_at"]) for e in timeline(store, ME, AS_OF)
             if e["event_type"] in extraction.SIGN_TYPES}
    assert signs == {("technical_ask", f"{POST}2", "2026-09-10", "2026-09-10T12:00:00+00:00"),
                     ("work_in_progress", f"{POST}3", "2026-09-12", "2026-09-12T12:00:00+00:00")}
    model.reply({"events": []})  # August, read now; September is cached
    again = asyncio.run(extraction.read_posts(store, subject, role="mts-research", settings=settings,
                                              budget=providers.Budget({"max_calls_per_run": 2}), as_of=AS_OF))
    later = json.loads(model.calls[1]["payload"]["messages"][0]["content"])
    assert len(model.calls) == 2 and again["errors"] == [] and later["earlier_posts"] == []
    assert [p["text"] for p in later["posts"]] == ["Old thoughts on world models."]  # a week never sees a later week
    assert [e["event_type"] for e in timeline(store, ME, AS_OF)].count("technical_ask") == 1
    assert extraction.unread(store, subject, role="mts-research", settings=settings, as_of=AS_OF) == 0


def test_a_new_reading_retires_the_old_ones_tags_and_a_month_dated_post_is_read_in_its_week(model, store, settings):
    post(store, 1, "Our rollouts drift after 40 frames.", "2026-09-12")
    add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "linkedin_post", "event_date": "2026-09",
                      "date_precision": "month", "observed_at": "2026-09-13T08:00:00+00:00",
                      "source_url": "https://www.linkedin.com/posts/me-2", "source_version_hash": "l2",
                      "quote": "Training on more games this week.", "tier": 1, "extractor": "crustdata"})
    subject = {"type": "person", "id": ME, "name": "Me"}
    model.reply({"events": [{"post": 1, "event_type": "work_in_progress", "event_date": None, "about_subject": True,
                             "quote": "Our rollouts drift after 40 frames."}]})
    first = asyncio.run(extraction.read_posts(store, subject, role="mts-research", settings=settings,
                                              budget=providers.Budget({"max_calls_per_run": 2}), as_of=AS_OF))
    assert (first["calls"], first["events"], first["errors"]) == (1, 1, [])  # one week: the 12th and the 13th
    # Another model reads the same week and finds nothing: the first reading's tag goes.
    model.reply({"events": []})
    other = {**settings, "small_model": "claude-opus-5"}
    assert extraction.unread(store, subject, role="mts-research", settings=other, as_of=AS_OF) == 1
    again = asyncio.run(extraction.read_posts(store, subject, role="mts-research", settings=other,
                                              budget=providers.Budget({"max_calls_per_run": 2}), as_of=AS_OF))
    assert (again["retired"], len(model.calls)) == (1, 2)
    assert not [e for e in timeline(store, ME, AS_OF) if e["event_type"] in extraction.SIGN_TYPES]


def test_the_audit_matches_the_timeline_to_exactly_the_cached_reading(model, store, settings):
    push = {"subject_type": "person", "subject_id": ME, "event_type": "github_activity", "date_precision": "day",
            "source_url": "https://github.com/me/wm", "source_version_hash": "p", "quote": "Pushed to me/wm",
            "tier": 1, "extractor": "github_v2"}
    for day in ("2026-08-03", "2026-09-07"):  # the same push text twice, weeks apart: two items
        add_event(store, {**push, "event_date": day, "observed_at": f"{day}T09:00:00+00:00"})
    subject = {"type": "person", "id": ME, "name": "Me"}
    wip = {"post": 1, "event_type": "work_in_progress", "event_date": None, "about_subject": True,
           "quote": "Pushed to me/wm"}
    for _ in range(4):
        model.reply({"events": [wip]})
    read = lambda types: asyncio.run(extraction.read_posts(  # noqa: E731
        store, subject, role="backend", settings=settings, budget=providers.Budget({"max_calls_per_run": 4}),
        types=types, as_of=AS_OF))
    for _ in range(2):  # a second, cached read changes nothing
        read(detectors.READ_TYPES)
        signs = [e for e in timeline(store, ME, AS_OF) if e["event_type"] == "work_in_progress"]
        assert len(signs) == 2
    todo, expected = extraction.audit(store, subject, role="backend", settings=settings, as_of=AS_OF)
    held = {e["id"] for e in extraction.post_readings(store, ME)}
    assert (todo, expected) == ([], held)
    # Read with other item types, the same posts are another reading: the first one's tags are stale for it.
    todo, expected = extraction.audit(store, subject, role="backend", settings=settings,
                                      types=detectors.REPLAY_TYPES, as_of=AS_OF)
    assert todo == [] and expected == set() and held - expected == held


def test_nothing_is_taken_from_a_post_about_family_health_immigration_or_mood(model, store, settings):
    posts = [{"source_url": f"{POST}{n}", "event_date": "2026-09-10", "observed_at": "2026-09-10T14:00:00+00:00",
              "quote": text} for n, text in enumerate(["Hacking on world models while my visa renewal drags on.",
                                                       "Hacking on world models from game clips."], 1)]
    model.reply({"events": [{"post": n, "event_type": "work_in_progress", "event_date": None, "about_subject": True,
                             "quote": "Hacking on world models"} for n in (1, 2)]})
    events = asyncio.run(extraction.extract_posts(posts, {"type": "person", "id": ME, "name": "Me"},
                                                  settings=settings, store=store))
    assert [e.source_url for e in events] == [f"{POST}2"]
    for text in ("We had a baby!", "Feeling down after the layoffs", "Back from surgery", "burnt out this month"):
        assert extraction.personal({"quote": text})
    # Only their own words count: the post they answered is someone else's.
    assert not extraction.personal({"quote": "Try our demo" + extraction.REPLY_CONTEXT + "@a: my kids love it"})


def test_github_items_are_read_with_posts_and_count_toward_drift(model, store, settings):
    post(store, 1, "Weekend project below.", "2026-09-10")
    add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "github_repo", "event_date": "2026-09-11",
                      "date_precision": "day", "observed_at": "2026-09-11T08:00:00+00:00",
                      "source_url": "https://github.com/me/clip-store", "source_version_hash": "g1",
                      "quote": "Created me/clip-store: sharded storage for game clips (Java)", "tier": 1,
                      "extractor": "github_v2"})
    model.reply({"events": []})
    got = asyncio.run(extraction.read_posts(store, {"type": "person", "id": ME, "name": "Me"}, role="backend",
                                            settings=settings, budget=providers.Budget({"max_calls_per_run": 2}),
                                            as_of=AS_OF))
    assert (got["posts"], got["calls"]) == (2, 1) and "clip-store" in model.calls[0]["payload"]["messages"][0]["content"]
    old = [posted(d) for d in ("2026-03-01", "2026-04-01", "2026-05-01", "2026-06-01")]
    repos = [ev(ME, "github_repo", d, source_url=f"https://github.com/me/r{d}") for d in ("2026-08-20", "2026-09-10")]
    assert "topic_drift" in fire(old + repos + [tagged(r) for r in repos], "2026-09-15")
    # Pushes to one repo share its URL; each is its own item.
    pushes = [ev(ME, "github_activity", d, quote=f"Pushed to me/wm: step {d}", source_url="https://github.com/me/wm",
                 source_version_hash=d) for d in ("2026-08-20", "2026-09-10")]
    on_topic = [{**tagged(p), "source_version_hash": p["source_version_hash"]} for p in pushes]
    assert "topic_drift" in fire(old + pushes + on_topic, "2026-09-15")


def test_the_demo_ranks_a_pitch_first_quotes_the_post_and_lists_every_signal(store, monkeypatch):
    monkeypatch.setattr(journey, "notes_only", frozenset)  # every sign may pitch, as before the live policy
    routing.seed(store, json.loads((FIXTURES / "timeline.json").read_text()))
    contacts = json.loads((FIXTURES / "contacts.json").read_text())
    routes = [r for s in ("X900", "X902", "X901") if (r := routing.route(store, s, AS_OF, [], contacts.get(s, [])))]
    assert [r.name for r in routing.rank(routes)] == ["Noa Brandt", "Mara Quill"]  # Ivy just started: wait
    mara = next(r for r in routes if r.name == "Mara Quill")
    assert mara.call.track == "rapport" and "Has anyone found a good source" in mara.draft["body"]
    assert "We're hiring for our Member of Technical Staff role" in mara.draft["body"]  # one clause, whatever the track
    noa = next(r for r in routes if r.name == "Noa Brandt")
    assert noa.call.track == "pitch" and "hiring for our Product Designer role" in noa.draft["body"]
    assert "Rebuilding our component library" in noa.draft["body"]
    why = next(b["text"]["text"] for b in noa.card["blocks"] if b.get("text", {}).get("text", "").startswith("*Why now"))
    sources = next(b["text"]["text"] for b in noa.card["blocks"] if b.get("text", {}).get("text", "").startswith("*Trigger"))
    assert "• Noa posted work in progress on Sep 5: “Rebuilding our component library" in sources
    assert "• Noa's posts turned toward GI's topics, as of Aug 12: " in sources
    assert sources.count("|linkedin.com>") >= 2  # each signal, linked
    assert not any(reason in why + sources for reason in noa.call.reasons)  # the engine's own labels stay off the card


def test_hand_labels_without_a_person_id_still_read_under_a_stand_in(model, store, settings):
    from scripts import posts as cli

    rows = [{"person_id": "", "person": "Ada Example", "role": "backend", "url": POST, "date": "2026-09-10",
             "text": "Anyone have a good dataset of game clips with inputs?", "label": "asked_for_input"}]
    people, role = cli.labeled(rows, {})
    [((pid, name), posts)] = people.items()
    assert (pid, name, role[pid]) == ("label:ada-example", "Ada Example", "backend")
    model.reply({"events": [{"post": 1, "event_type": "technical_ask", "event_date": None, "about_subject": True,
                             "quote": "Anyone have a good dataset of game clips with inputs?"}]})
    events = asyncio.run(extraction.extract_posts(posts, {"type": "person", "id": pid, "name": name},
                                                  settings=settings, store=store, focus=extraction.focus_for("backend")))
    assert [e.event_type for e in events] == ["technical_ask"]  # under an empty id the timeline dropped it


def test_a_quote_post_with_no_words_of_its_own_gives_the_reader_no_sign(model, store, settings):
    model.reply({"events": [{"post": 1, "event_type": "work_in_progress", "event_date": None, "about_subject": True,
                             "quote": "We are training a world model on gameplay."}]})
    one = {"source_url": POST, "event_date": "2026-09-10", "observed_at": "2026-09-10T14:00:00+00:00",
           "quote": "In reply to @lab: We are training a world model on gameplay."}  # stored stripped
    assert asyncio.run(extraction.extract_posts([one], {"type": "person", "id": ME, "name": "Me"},
                                                settings=settings, store=store)) == []


def test_a_cached_reading_is_held_to_the_persons_own_words(store):
    post = {"source_url": POST, "event_date": "2026-09-10", "observed_at": "2026-09-10T14:00:00+00:00",
            "quote": "In reply to @lab: Our world model is training on gameplay."}
    cached = [{"post": 1, "event_type": "work_in_progress", "event_date": "2026-09-10", "date_precision": "day",
               "about_subject": True, "quote": "Our world model is training on gameplay."}]
    me = {"type": "person", "id": ME, "name": "Me"}
    assert extraction._rows(cached, [post], me, "test") == []
    own = {**post, "quote": "Our world model is training on gameplay."}
    assert [e.event_type for e in extraction._rows(cached, [own], me, "test")] == ["work_in_progress"]


def test_a_release_a_build_made_is_never_read_as_a_launch_even_from_a_cached_reading(store):
    post = {"source_url": "https://github.com/me/tool/releases/tag/rules-20260811-0932", "event_date": "2026-09-13",
            "observed_at": "2026-09-13T09:32:00+00:00", "quote": "Released rules-20260811-0932 of me/tool"}
    cached = [{"post": 1, "event_type": "project_release", "event_date": "2026-09-13", "date_precision": "day",
               "about_subject": True, "quote": "Released rules-20260811-0932 of me/tool"}]
    me = {"type": "person", "id": ME, "name": "Me"}
    assert extraction._rows(cached, [post], me, "test") == []
    person = type("P", (), {"person_id": ME, "github": "me", "since": "", "until": ""})
    build = {"type": "ReleaseEvent", "repo": {"name": "me/tool"}, "created_at": "2026-09-13T09:32:00Z",
             "payload": {"release": {"tag_name": "rules-20260811-0932", "html_url": post["source_url"]}}}
    shipped = {**build, "payload": {"release": {"tag_name": "v1.0", "name": "Tool 1.0", "html_url": "https://x.test"}}}
    from app.sources import github
    assert [e["quote"] for e in github.events(person, {"events": [build, shipped]})] == ["Released v1.0 of me/tool: Tool 1.0"]
    assert {"profile_job_started", "profile_job_ended"} <= set(scorecard.LIVE_ONLY)  # a replay never reads a profile


def test_the_live_list_names_real_early_signs():
    """A misspelt name would quietly let that sign pitch again, and a missing file would let them all; a noise test
    that keeps a sign takes it off the list, so the list is a non-empty subset of the early signs."""
    assert journey.notes_only() and journey.notes_only() <= set(scorecard.EARLY_SIGNS) <= DETECTORS.keys()


def test_live_calls_keep_signs_the_noise_test_dropped_to_a_note(store):
    routing.seed(store, json.loads((FIXTURES / "timeline.json").read_text()))
    assert "work_in_progress" in journey.notes_only() and "topic_drift" in journey.notes_only()
    noa = routing.route(store, "X902", AS_OF, [])  # work in progress and topic drift: a pitch on every sign
    assert (noa.call.action, noa.call.track) == ("reach_now", "rapport")
    assert "noise test has not shown" in noa.call.explanation and "Rebuilding our component library" in noa.draft["body"]
    everything = readiness.score(journey.detect(store, "X902", AS_OF), AS_OF)  # the replay's engine: unchanged
    assert everything.track == "pitch"


def test_posts_reads_the_timing_store_unless_told_otherwise(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("posts_script", today.ROOT / "scripts" / "posts.py")
    posts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(posts)
    monkeypatch.setattr(today, "TIMELINES", tmp_path / "timelines.sqlite")
    monkeypatch.delenv("DATABASE_URL")
    assert posts.store_of(None).engine.url.database == str(tmp_path / "timelines.sqlite")  # not the web app's
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'timelines.sqlite'}")  # the Mac's old way: same file
    assert posts.store_of(None).engine.url.database == str(tmp_path / "timelines.sqlite")
    assert posts.store_of(f"sqlite:///{tmp_path / 'other.sqlite'}").engine.url.database == str(tmp_path / "other.sqlite")
    monkeypatch.setattr(contact, "ROOT", tmp_path)  # the Mac's relative URL, read against the repo root
    monkeypatch.setenv("DATABASE_URL", "sqlite:///timelines.sqlite")
    assert posts.store_of(None).engine.url.database == str(tmp_path / "timelines.sqlite")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'pilot.sqlite'}")  # another file: never silently
    with pytest.raises(SystemExit, match="--db sqlite:///"):
        posts.store_of(None)


def test_the_card_quotes_a_listed_post_up_to_a_whole_item_and_claims_no_match_it_cannot_name(store, monkeypatch):
    """End to end on an invented post shaped like a real card's (a list, over the card's 90 characters): the trigger
    line quotes whole items and never cuts one, and it says what the reader found, work in progress, without calling
    it close to GI's, which the reader does not record."""
    monkeypatch.setattr(journey, "notes_only", frozenset)
    post = ("clip board round two…\n1. load a match\n2. circle the player you want to follow\n3. say 'keep the camera on them "
            "through the fight' and watch it recut")
    data = json.loads((FIXTURES / "timeline.json").read_text())
    routing.seed(store, {**data, "events": [{**e, "quote": post} if e["subject_id"] == "X902" and e["event_date"] ==
                                            "2026-09-05" and e["event_type"] in ("linkedin_post", "work_in_progress")
                                            else e for e in data["events"]]})
    contacts = json.loads((FIXTURES / "contacts.json").read_text())
    noa = routing.route(store, "X902", AS_OF, [], contacts.get("X902", []))
    sources = next(b["text"]["text"] for b in noa.card["blocks"] if b.get("text", {}).get("text", "").startswith("*Trigger"))
    assert "• Noa posted work in progress on Sep 5: “clip board round two… 1. load a match 2. circle the player you " \
           "want to follow” <https://www.linkedin.com/posts/noabrandt-example-2005|linkedin.com>" in sources
    assert "close to GI's" not in json.dumps(noa.card) and "keep the camera" not in sources


# How a post nobody read anything off can reach a reader, listed before the fix (a burst of posting counts raw posts,
# and the first of them can be a curse or politics):
#   1. The Slack card's trigger line quotes it; the morning list and the Monday post send that card.
#   2. The card's signals, which Today's calls and the Events page read, carry its words.
#   3. The model writer gets it among the items a draft may quote.
#   4. The Events page quotes the not-quoted note as their words, or calls the post personal.
#   5. A post the reader did read (a sign's own post) loses its quote: it keeps it.
#   6. A free draft that passed its checks fails them, its card held: a push with no messages quotes its repo's line,
#      which the reader tagged nothing on (tests/test_routing.py).
#   7. A record that is no post (a door note) shares an unread post's key and shows the note instead of its words.
#   8. The free draft's checks lose a post the reader read nothing off: the name swap counts a phrase of its (the
#      template's own stock words), so a card that went before is held, or one held goes (tests/test_routing.py).
def test_a_post_nobody_read_anything_off_is_never_quoted_on_a_card_or_sent_to_the_writer(store, monkeypatch):
    monkeypatch.setattr(journey, "notes_only", frozenset)
    data = json.loads((FIXTURES / "timeline.json").read_text())
    rant = "What a damn circus this election is, vote the bums out."
    burst = [{"subject_id": "X902", "event_type": "x_post", "event_date": day, "observed_at": f"{day}T15:00:00+00:00",
              "quote": text, "source_url": f"https://x.com/noabrandt_example/status/{n}"}
             for n, (day, text) in enumerate([("2026-08-20", rant)] + [(f"2026-09-{d:02d}", "Long week.")
                                                                       for d in range(6, 13)])]
    routing.seed(store, {**data, "events": data["events"] + burst})
    contacts = json.loads((FIXTURES / "contacts.json").read_text())
    given = []

    def writer(name, role, track, items, *args, **kwargs):
        given.extend(items)  # what a model draft could quote; the free template writes this one

    noa = routing.route(store, "X902", AS_OF, [], contacts.get("X902", []), writer=writer)
    assert "posting_burst" in noa.call.reasons and noa.call.action == "reach_now"
    trigger = routing.trigger(noa)
    assert f"• Noa posted far more than usual, as of Aug 20: {routing.UNREAD} <https://x.com/noabrandt_example/status/0" \
           "|x.com>" in trigger  # 1: dated and linked, never in their words
    assert "election" not in json.dumps(noa.card) and "election" not in json.dumps(noa.signals)  # 1, 2
    assert "election" not in noa.model_dump_json()  # nor the draft, its checks or anything the route carries
    assert given and not any("election" in i["text"] or "Long week" in i["text"] for i in given)  # 3
    assert "• Noa posted work in progress on Sep 5: “Rebuilding our component library" in trigger  # 5: read, quoted


def test_only_a_post_ever_shows_the_not_quoted_note_in_its_place():  # 7, as above
    call = readiness.Readiness(as_of=AS_OF, score=0.5, action="reach_now", families={}, reasons=["reason"], holds=[],
                               open_windows=[], earliest_close=None, explanation="", evidence={"reason": ["n1"]})
    key = {"source_url": None, "source_version_hash": "fixture", "observed_at": "2026-09-10T00:00:00+00:00"}
    post = {"id": "p1", "subject_id": "X902", "event_type": "x_post", "event_date": "2026-09-10", "quote": "Long week.",
            **key}
    note = {"id": "n1", "subject_id": "X902", "event_type": "work_in_progress", "event_date": "2026-09-10",
            "quote": "Sketching a clip timeline for the replay tool", **key}
    [shown] = routing.signals(call, {"p1": post, "n1": note})
    assert shown["quote"] == note["quote"]
