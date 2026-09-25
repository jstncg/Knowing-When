"""Just happened: public moments of the last month, labeled on readiness's reaches and listed beside them."""

import asyncio
import json
from pathlib import Path

from app import detectors, extraction, happened, journey, outreach, providers, readiness, routing
from app.store import Store
from app.timeline import add_event
from tests.test_detectors import ME, ev

AS_OF = "2026-09-15T00:00:00+00:00"
FIXTURES = Path(__file__).parent / "fixtures"


def test_recent_moments_are_the_persons_own_and_their_employers_of_the_last_30_days():
    post = lambda day, n, text: ev(ME, "x_post", None, f"{day}T10:00:00+00:00", text,  # noqa: E731
                                   source_url=f"https://x.com/me/status/{n}")
    out, draft = post("2026-09-11", 1, "Δ-IRIS is up on arXiv: arxiv.org/abs/2609.00002"), post("2026-09-12", 2, "Training v2")
    cites, late = post("2026-09-13", 3, "Prototype on arxiv.org/abs/2601.1 by Ha"), post("2026-09-14", 4, "Laid off")
    read = lambda p, kind, day=None: {**ev(ME, kind, day, p["observed_at"], p["quote"]),  # noqa: E731
                                      "source_url": p["source_url"], "source_version_hash": p["source_version_hash"]}
    events = [
        ev(ME, "publication", None, "2026-09-05T10:00:00+00:00", "Our paper is out"),
        ev(ME, "job_ended", "2026-09-01", "2026-09-06T10:00:00+00:00", "My last day at Acme was 1 September 2026"),
        ev(ME, "job_ended", "2026-10-31", "2026-09-10T10:00:00+00:00", "My last day at Beta is 31 October 2026"),
        late, {**read(late, "layoff", "2026-07-01"), "quote": "Laid off"},  # their own post tells it late
        ev(ME, "layoff", "2026-06-01", "2026-09-14T10:00:00+00:00", "Laid off 1 June 2026"),  # a page seen lately
        ev(ME, "publication", "2023", "2026-09-12T10:00:00+00:00", "Published 2023"),  # old news on a CV page
        ev(ME, "publication", "2026", "2026-09-12T10:00:00+00:00", "2026: a paper from February"),  # this year's, too
        post("2026-09-13", 5, "It's up to us: see arxiv.org/abs/2601.2"),
        read(post("2026-09-13", 5, "It's up to us: see arxiv.org/abs/2601.2"), "work_in_progress"),
        ev(ME, "project_release", None, "2026-08-16T10:00:00+00:00", "We launched v1"),  # 30 days ago: past it
        ev(ME, "gi_topic", None, "2026-09-12T10:00:00+00:00", "World models for games"),  # an early sign's tag
        out, draft, cites,
        read(out, "work_in_progress"),  # read as work in progress, but their paper is out
        read(draft, "work_in_progress"), read(cites, "work_in_progress"),  # a draft; someone else's paper
        ev("person:peer", "publication", None, "2026-09-12T10:00:00+00:00", "A colleague's paper"),
        ev("org:acme", "company_closure", "2026-09-02", None, "Acme is winding down on 2 September 2026"),
        ev("org:acme", "acquisition_closed", "2026-09-03", None, "Acme completed its acquisition of Gamma"),
        ev(ME, "self_stated_availability", None, "2026-09-16T10:00:00+00:00", "Open to work"),  # after as of
    ]
    found = happened.recent(events, ME, AS_OF)
    assert [(h.kind, h.date, h.day) for h in found] == [
        ("paper", "2026-09-11", 4), ("left_job", "2026-09-10", 5), ("paper", "2026-09-05", 10),
        ("company_closed", "2026-09-02", 13), ("left_job", "2026-09-01", 14), ("left_job", "2026-07-01", 76)]
    assert happened.line(found[2]) == "paper out · 2026-09-05 · day 10 of about 30"


def test_a_reach_says_whether_it_is_an_early_sign_or_something_that_just_happened(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'r.sqlite'}")
    routing.seed(store, json.loads((FIXTURES / "routing" / "timeline.json").read_text()))
    rin = routing.route(store, "A900", AS_OF, [])
    assert rin.kind == "just_happened" and (rin.happened[0].kind, rin.happened[0].day) == ("paper", 26)
    assert rin.card["blocks"][0]["text"]["text"] == "Rin Vale" and rin.card["text"].startswith("Just happened: Rin Vale")
    assert "Just happened, day 26 of about 30" in rin.card["blocks"][2]["text"]["text"]  # the role line: the month
    trigger = next(b["text"]["text"] for b in rin.card["blocks"] if b.get("text", {}).get("text", "").startswith("*Trigger"))
    assert "<https://openreview.net/" in trigger  # the moment, linked
    store = Store(f"sqlite:///{tmp_path / 'p.sqlite'}")
    routing.seed(store, json.loads((FIXTURES / "posts" / "timeline.json").read_text()))
    mara = routing.route(store, "X900", AS_OF, [])
    assert (mara.name, mara.kind, mara.card["text"]) == ("Mara Quill", "early_sign",
                                                         "Early sign: Mara Quill (Member of Technical Staff)")
    assert "· Early sign ·" in mara.card["blocks"][2]["text"]["text"]


def test_a_paper_alone_opens_a_note_about_their_work_labeled_just_happened(store):
    add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "paper_v1", "event_date": "2026-09-08",
                      "date_precision": "day", "observed_at": "2026-09-08T12:00:00+00:00",
                      "source_url": "https://arxiv.org/abs/2609.00001", "source_version_hash": "p",
                      "quote": "Submitted 8 Sep 2026: 'Action tokenizers for games'", "tier": 1, "extractor": "test"})
    call, fresh = happened.watch(store, ME, AS_OF)
    assert (call.action, call.track, [(h.kind, h.day) for h in fresh]) == ("reach_now", "rapport", [("paper", 7)])
    card = routing.route(store, ME, AS_OF, [])
    assert (card.kind, card.opener["event_type"]) == ("just_happened", "paper_v1")
    # The draft opens with the paper; the role comes after, in one line (the voice Justin approved).
    assert card.draft["body"].startswith("Hey person:me, congrats on the new paper! Saw your paper on arXiv: ") \
        and card.message == "paper"
    # Past about a month it no longer opens a note: the paper's own window runs longer.
    assert journey.assess(store, ME, "2026-10-10T00:00:00+00:00").action != "reach_now"


def test_their_launch_waits_out_launch_week_then_opens_a_note(store):
    add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "launch_announced",
                      "event_date": "2026-09-20", "date_precision": "day", "observed_at": "2026-09-01T12:00:00+00:00",
                      "source_url": "https://me.example/launch", "source_version_hash": "l",
                      "quote": "Brambleworld ships on 20 September 2026", "tier": 1, "extractor": "test"})
    calls = {day: journey.assess(store, ME, f"2026-{day}T00:00:00+00:00") for day in ("09-15", "09-24", "09-30")}
    assert [(c.action, c.track) for c in calls.values()] == [("watch_until", None), ("watch_until", None),
                                                              ("reach_now", "rapport")]
    card = routing.route(store, ME, "2026-09-30T00:00:00+00:00", [])
    assert card.draft["body"].startswith("Hey person:me, congrats on the launch! Saw the announcement: "
                                         "\"Brambleworld ships on 20 September 2026.\"") and card.message == "launch"


def test_reading_only_recent_weeks_makes_the_full_reads_calls_for_them(model, store, settings):
    for n, day in enumerate(("2026-06-02", "2026-07-07", "2026-09-01", "2026-09-08")):
        add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "x_post", "event_date": day,
                          "date_precision": "day", "observed_at": f"{day}T09:00:00+00:00",
                          "source_url": f"https://x.com/me/status/{n}", "source_version_hash": str(n),
                          "quote": f"post {n}", "tier": 1, "extractor": "apify_x"})
    subject, since = {"type": "person", "id": ME, "name": "Me"}, "2026-08-16T00:00:00+00:00"
    kwargs = {"role": "backend", "settings": settings, "types": detectors.READ_TYPES, "as_of": AS_OF}
    assert (extraction.unread(store, subject, **kwargs), extraction.unread(store, subject, since=since, **kwargs)) == (4, 2)
    for _ in range(2):
        model.reply({"events": []})
    got = asyncio.run(extraction.read_posts(store, subject, budget=providers.Budget({"max_calls_per_run": 2}),
                                            since=since, **kwargs))
    assert got["calls"] == 2 and extraction.unread(store, subject, **kwargs) == 2  # the full read skips them


def test_a_post_read_as_their_work_that_puts_their_paper_out_is_just_happened_never_an_early_sign(store):
    base = {"subject_type": "person", "subject_id": ME, "event_date": "2026-09-11", "date_precision": "day",
            "observed_at": "2026-09-11T10:00:00+00:00", "source_url": "https://x.com/me/status/1", "tier": 1}
    for n, text in enumerate(("Our new paper is up on arXiv: arxiv.org/abs/2609.00002",
                              "Still training v2 of our world model, clips soon")):
        post = {**base, "source_url": f"https://x.com/me/status/{n}", "source_version_hash": str(n)}
        add_event(store, {**post, "event_type": "x_post", "quote": text, "extractor": "apify_x"})
        add_event(store, {**post, "event_type": "work_in_progress", "quote": text[:20],
                          "extractor": "claude_extract_v1:posts:test"})
    acts = {a["detector_id"]: a for a in journey.detect(store, ME, AS_OF)}
    assert [len(acts["work_in_progress"]["evidence_event_ids"])] == [1]  # the draft only: the paper is news
    assert len(acts["paper_v1"]["evidence_event_ids"]) == 1  # the post that puts it out, read as the paper
    call, fresh = happened.watch(store, ME, AS_OF)
    assert (call.action, call.track, [h.kind for h in fresh]) == ("reach_now", "rapport", ["paper"])
    assert happened.kind(call, fresh) == "just_happened"  # the paper is public: the reach is no longer ahead
    assert readiness.opener(call) == acts["work_in_progress"]["evidence_event_ids"][0]  # the note opens on the draft


def test_a_paper_out_post_the_reader_called_work_in_progress_opens_a_note_about_the_paper(store):
    """Just happened lists it as a paper, so readiness reads it as one, never quiet beside it and never an early sign.
    A paper tag with no date of its own stays as the reader dated it: nothing here dates it."""
    post = {"subject_type": "person", "subject_id": ME, "observed_at": "2026-09-11T10:00:00+00:00", "tier": 1,
            "source_url": "https://x.com/me/status/1", "source_version_hash": "1", "event_date": "2026-09-11",
            "date_precision": "day"}
    text = "Our new paper is up on arXiv: arxiv.org/abs/2609.00002"
    add_event(store, {**post, "event_type": "x_post", "quote": text, "extractor": "apify_x"})
    add_event(store, {**post, "event_type": "work_in_progress", "quote": text[:28],
                      "extractor": "claude_extract_v1:posts:test"})
    call, fresh = happened.watch(store, ME, AS_OF)
    assert (call.action, call.reasons, [h.kind for h in fresh], happened.kind(call, fresh)) == (
        "reach_now", ["new_work"], ["paper"], "just_happened")
    other = {**post, "subject_id": "person:other", "source_url": "https://x.com/other/status/1", "event_date": None,
             "date_precision": None}
    add_event(store, {**other, "event_type": "x_post", "quote": "honored to have work accepted", "extractor": "apify_x"})
    add_event(store, {**other, "event_type": "publication", "quote": "honored to have work accepted",
                      "extractor": "claude_extract_v1:posts:test"})
    assert "paper_v1" not in {a["detector_id"] for a in journey.detect(store, "person:other", AS_OF)}


def test_a_month_or_year_on_a_page_counts_from_its_first_day_even_once_past():
    page = lambda day, text, when: ev(ME, "publication", when, f"{day}T10:00:00+00:00", text)  # noqa: E731
    events = [page("2027-01-12", "2026: Action tokenizers", "2026"), page("2026-10-12", "Sep 2026: a paper", "2026-09")]
    assert happened.recent(events, ME, "2027-01-13T00:00:00+00:00") == []
    assert happened.recent(events, ME, "2026-10-13T00:00:00+00:00") == []  # 1 September is 42 days back
    assert [h.day for h in happened.recent(events, ME, "2026-09-20T00:00:00+00:00")] == []  # not yet public
    assert [h.day for h in happened.recent([page("2026-09-10", "Sep 2026: a paper", "2026-09")], ME,
                                           "2026-09-20T00:00:00+00:00")] == [19]


def test_out_of_is_not_out():
    assert not detectors.paper_out("This is out of scope for arxiv.org/abs/2601.1")
    assert not detectors.paper_out("Our method is out-of-distribution robust: arxiv.org/abs/2601.1")
    assert not detectors.paper_out("Extending our paper arxiv.org/abs/2501.00001 to video, training now")
    assert detectors.paper_out("Our paper is out: arxiv.org/abs/2609.00002")


def test_the_guard_reads_only_the_persons_own_words_and_both_tags():
    assert detectors.paper_out("OUR NEW PAPER: ARXIV.ORG/html/2609.1") and detectors.paper_out(
        "It’s out! arxiv.org/abs/2609.00002")  # case; a curly apostrophe
    assert not detectors.paper_out("Still training v2\n\nIn reply to @ha: our paper is out arxiv.org/abs/2601.1")
    assert not detectors.paper_out("In reply to @ha: Our paper is out arxiv.org/abs/2601.1")  # a bare quote post
    post = ev(ME, "x_post", None, "2026-09-11T10:00:00+00:00", "We present Δ-IRIS: arxiv.org/abs/2609.00002",
              source_url="https://x.com/me/status/1")
    tag = {**ev(ME, "just_submitted", "2026-09-11", post["observed_at"], "We present Δ-IRIS"),
           "source_url": post["source_url"], "source_version_hash": post["source_version_hash"],
           "extractor": "claude_extract_v1:posts:test"}
    assert detectors.as_said([post, tag]) == [post, {**tag, "event_type": "publication"}]  # news, never a sign


def test_a_quote_post_with_no_words_of_its_own_gives_the_reader_none():
    assert detectors.own_words("In reply to @ha: our world model is training") == ""
    assert detectors.contacts({"quote": "In reply to @ha: thanks @lecun"}) == {"@ha"}
    assert detectors.contacts({"quote": "Nice @kim\n\nIn reply to @ha: thanks @lecun"}) == {"@kim", "@ha"}


def test_a_linkedin_profile_read_adds_leaving_a_job_and_two_years_in_the_seat_this_month_or_last():
    row = lambda **kw: {"linkedin_url": "https://www.linkedin.com/in/me", "headline": "", "current": [],  # noqa: E731
                        "last_ended": None, **kw}
    job = lambda title, company, started: {"title": title, "company": company, "started": started}  # noqa: E731
    profiles = {"pulled_on": "2026-09-14", "people": {
        "a": row(last_ended={**job("Engineer", "Acme", "2023-01"), "ended": "2026-08"},
                 current=[job("Advisor", "Side Lab", "2026-09")]),  # an advisory seat is not a new job
        "b": row(last_ended={**job("Engineer", "Acme", "2023-01"), "ended": "2026-08"},
                 current=[job("Staff Engineer", "Beta", "2026-09")]),  # moved straight on
        "c": row(last_ended={**job("Engineer", "Acme", "2023-01"), "ended": "2026-06"}),  # three months back
        "d": row(headline="Research Engineer at Brambleworld",
                 current=[job("Board member", "Club", "2024-08"), job("Angel", "Fund", "2024-09"),
                          job("Research Engineer", "Brambleworld", "2024-09"), job("Engineer", "Old", "2024-08")]),
        "e": row(current=[job("Engineer", "Gamma", "2024")]),  # a year alone places no month
        "f": row(last_ended={**job("Advisor", "Small Co", "2025-01"), "ended": "2026-08"},
                 current=[job("Research Scientist", "Lab", "2023-01")]),  # an advisory seat ended; the job goes on
    }}
    got = {pid: [(h.kind, h.date, h.day, h.note) for h in happened.from_profile(profiles, pid, AS_OF)]
           for pid in profiles["people"]}
    assert got == {"a": [("left_job", "2026-08", 45, "last month")], "b": [], "c": [],
                   "d": [("two_years", "2026-09", 14, "this month")], "e": [], "f": []}
    [h] = happened.from_profile(profiles, "d", AS_OF)
    assert happened.line(h) == "two years in the seat · 2026-09 · this month" and "Brambleworld since 2024-09" in h.quote
    assert happened.from_profile(profiles, "a", "2026-09-13T00:00:00+00:00") == []  # read after the call's day
    assert happened.from_profile({}, "a", AS_OF) == []


def test_the_profile_read_is_listed_beside_the_timeline_and_on_the_card_never_deciding_a_reach(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'r.sqlite'}")
    routing.seed(store, json.loads((FIXTURES / "routing" / "timeline.json").read_text()))
    profiles = {"pulled_on": "2026-09-01", "people": {"A900": {
        "linkedin_url": "https://www.linkedin.com/in/rin", "headline": "", "last_ended": None,
        "current": [{"title": "Research Scientist", "company": "Lab", "started": "2024-08"}]}}}
    rin = routing.route(store, "A900", AS_OF, [], profiles=profiles)
    assert rin.kind == "just_happened" and [h.kind for h in rin.happened] == ["paper", "two_years"]
    assert "Also in the last month: two years in the seat, last month (<https://www.linkedin.com/in/rin|LinkedIn>)" in \
        [e["text"] for b in rin.card["blocks"] if b["type"] == "context" for e in b["elements"]]
    assert routing.route(store, "A900", AS_OF, []).happened[0].kind == "paper"  # nothing is read unless passed


def _add(store, subject, event_type, day, quote, url, observed=None, subject_type="person"):
    add_event(store, {"subject_type": subject_type, "subject_id": subject, "event_type": event_type, "event_date": day,
                      "date_precision": "day", "observed_at": observed or f"{day}T12:00:00+00:00", "source_url": url,
                      "source_version_hash": url[-6:], "quote": quote, "tier": 1, "extractor": "test"})


def test_only_their_own_post_that_their_company_was_acquired_opens_a_note(store):
    acts = lambda events: {a["detector_id"] for a in detectors.detect(events, ME, "2026-09-15")}  # noqa: E731
    filing = ev("org:acme", "acquisition_closed", "2026-09-03", None, "Acme completed its acquisition of Gamma")
    own = ev(ME, "acquisition", "2026-09-03", "2026-09-03T10:00:00+00:00", "Brambleworld was acquired by Acme")
    assert "acquired" not in acts([filing]) and {"acquired", "acquisition_closed"} <= acts([own])
    _add(store, ME, "acquisition", "2026-09-03", "Brambleworld was acquired by Acme!", "https://x.com/me/status/7")
    card = routing.route(store, ME, AS_OF, [])
    assert (card.call.track, card.kind, card.opener["event_type"]) == ("rapport", "just_happened", "acquisition")
    assert card.draft["body"].startswith("Hey person:me, congrats on the acquisition! Saw your X post: "
                                         "\"Brambleworld was acquired by Acme!\"")
    # Deal week: congratulations, what GI builds and an open door, never the role (Justin, 2026-09-24).
    body = card.draft["body"]
    assert "Not sure what you're looking for next, but if this interests you, I'd love to chat." in body
    assert card.role["title"] not in body and card.message == "acquisition"
    assert "hiring" not in body.lower() and card.role_free == "acquisition"
    assert not [p for p in card.outreach["checks"]["problems"] if "hiring" in p]  # neither "names it" nor "doesn't"
    assert "their company was just acquired" in json.dumps(card.card)


def test_an_acquisition_post_that_names_a_layoff_gets_no_congratulations_and_no_role(store):
    _add(store, ME, "acquisition", "2026-09-03", "Brambleworld has been acquired by Acme. Sadly most of our design "
         "team was laid off as part of the deal.", "https://x.com/me/status/8")
    seen = {}

    def writer(name, role, track, *rest, **given):
        seen.update(role_free=given["role_free"], message=given["message"])
        return None
    card = routing.route(store, ME, AS_OF, [], writer=writer)
    assert (card.role_free, card.message) == ("acquisition", "work") and seen == {"role_free": "acquisition",
                                                                                  "message": "work"}
    draft = card.draft
    for text in (draft["subject"], draft["body"], draft["note"]):
        assert "congrat" not in text.lower() and "laid off" not in text and "acquisition" not in text.lower()
    assert card.role["title"] not in draft["body"] and "hiring" not in draft["body"].lower()
    assert "Not sure what you're looking for next" not in draft["body"]
    assert "No congratulations" in outreach.DEAL_QUIET  # the paid writer's shape for it (outreach.write)


def test_a_moment_told_late_is_not_just_happened(store):
    call = readiness.Readiness(as_of=AS_OF, score=0.5, action="reach_now", families={}, reasons=["r"], holds=[],
                               open_windows=[], earliest_close=None, explanation="", evidence={"r": ["m1"]},
                               track="rapport")
    late = happened.Happened(kind="open_to_work", event_id="m1", date="2026-07-15", day=62, source_url="",
                             quote="Open to new roles since June")
    assert happened.kind(call, [late]) is None  # older than a month: not "just", and no early sign either
    assert happened.kind(call, [late.model_copy(update={"day": 6})]) == "just_happened"


def test_in_deal_week_no_draft_names_the_role_whatever_the_reach_rests_on(store):
    from app import today

    _add(store, ME, "acquisition", "2026-09-10", "Brambleworld was acquired by Acme!", "https://x.com/me/status/9")
    _add(store, ME, "paper_v1", "2026-09-12", "Submitted 12 Sep 2026: 'Brambleworld'", "https://arxiv.org/abs/2609.00012")
    card = routing.route(store, ME, AS_OF, [])  # the paper leads now, and the acquisition is a listening reason
    assert card.role_free == "acquisition" and card.role["title"] not in card.draft["body"]
    assert "hiring" not in card.draft["body"].lower() and "their company was just acquired" in json.dumps(card.card)
    ctx = journey.load_context(store, ME) or journey.PersonContext(subject_id=ME, name="Me", role="mts-research")
    shown = today.ping(store, ctx, card.call, AS_OF, [], [])
    assert shown["track"] == today.DEAL  # Today's calls says why the role stays out
    seen = {}

    def writer(name, role, track, *rest, **given):
        seen.update(track=track, role_free=given["role_free"], message=given["message"])
        return None  # the free template then, checked the same way
    routing.route(store, ME, AS_OF, [], writer=writer)
    assert seen == {"track": "rapport", "role_free": "acquisition", "message": "acquisition"}  # never told to pitch
    later = routing.route(store, ME, "2026-10-20T00:00:00+00:00", [])  # 40 days on: the role is back, if it reaches
    assert later is None or later.role_free == ""


def test_a_paper_and_a_launch_together_are_one_reason_a_note_never_a_pitch(store):
    _add(store, ME, "paper_v1", "2026-09-01", "Submitted 1 Sep 2026: 'Brambleworld'", "https://arxiv.org/abs/2609.00009")
    _add(store, ME, "launch_announced", "2026-09-02", "Brambleworld is live today", "https://brambleworld.example/")
    call = journey.assess(store, ME, "2026-09-12T00:00:00+00:00")
    assert (call.action, call.track, set(call.reasons)) == ("reach_now", "rapport", {"new_work", "launch"})


def test_an_acquisition_post_is_an_employer_reason_for_three_weeks(store):
    _add(store, ME, "acquisition", "2026-08-28", "Brambleworld was acquired by Acme!", "https://x.com/me/status/8")
    _add(store, ME, "paper_v1", "2026-09-06", "Submitted 6 Sep 2026: 'Brambleworld'", "https://arxiv.org/abs/2609.00009")
    call = journey.assess(store, ME, AS_OF)  # the post 18 days back, the preprint 9: an employer reason and new work
    assert (call.action, call.track, set(call.reasons)) == ("reach_now", "pitch", {"acquisition_closed", "new_work"})
    # Past three weeks (detectors.HEADLINE_DAYS) the post is no reason of its employer's, and never on its fading
    # strength after: their post and preprint are public moments, one reason, a note.
    later = journey.assess(store, ME, "2026-09-20T12:00:00+00:00")
    assert (later.action, later.track, set(later.reasons)) == ("reach_now", "rapport", {"new_work", "acquired"})


def test_a_note_quotes_the_paper_never_a_talk(store):
    _add(store, ME, "paper_v1", "2026-09-01", "Submitted 1 Sep 2026: 'Brambleworld'", "https://arxiv.org/abs/2609.00009")
    _add(store, ME, "paper_talk", "2026-09-20", "Invited talk at the Games Workshop, 20 September 2026",
         "https://games.example/talks", observed="2026-09-10T12:00:00+00:00")
    card = routing.route(store, ME, "2026-09-12T00:00:00+00:00", [])
    assert card.opener["event_type"] == "paper_v1" and "Saw your paper on arXiv:" in card.draft["body"]
    assert "Invited talk" not in card.draft["body"]


def test_an_undergraduate_with_a_paper_out_gets_the_note(store):
    from app import contact

    _add(store, ME, "paper_v1", "2026-09-08", "Submitted 8 Sep 2026: 'Brambleworld'", "https://arxiv.org/abs/2609.00009")
    call = journey.assess(store, ME, AS_OF)
    contact.record(store, ME, "identity", at="2026-08-01T00:00:00+00:00",
                   links=["https://me.example/", "https://github.com/me-example"])
    contact.record(store, ME, "undergraduate", at="2026-08-01T00:00:00+00:00")
    behind = [e for e in journey.view(store, ME, AS_OF) if e["event_type"] == "paper_v1"]
    gate = contact.check(contact.history(store, ME), AS_OF, call, behind)
    assert (gate.state, gate.rapport_only) == ("clear", True)
    assert contact.check(contact.history(store, ME), AS_OF, call, [{"observed_at": AS_OF}]).state == "hold"
    # A paper older than about a month is nothing to write a note about.
    old = {**behind[0], "event_date": "2026-08-05"}
    assert readiness.news(call, [old]) is None and readiness.news(call, behind) == behind[0]["id"]


def test_a_moment_known_to_the_month_still_gets_a_note_never_a_pitch(store):
    add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "launch_announced", "event_date": "2026-10",
                      "date_precision": "month", "observed_at": "2026-09-01T12:00:00+00:00",
                      "source_url": "https://brambleworld.example/", "source_version_hash": "m",
                      "quote": "Brambleworld ships in October 2026", "tier": 1, "extractor": "test"})
    card = routing.route(store, ME, "2026-10-10T00:00:00+00:00", [])
    assert (card.call.track, card.opener["event_type"]) == ("rapport", "launch_announced")
    # The announcement is dated by when it was posted; the October it names stays in its own words.
    assert "congrats on the launch! Saw the announcement: \"Brambleworld ships in October 2026.\"" \
        in card.draft["body"]


def test_a_draft_names_where_they_work_now_from_the_profile_read_else_leaves_it_out():
    def read(status, jobs, headline="", pulled_on="2026-09-20"):
        return {"pulled_on": pulled_on, "people": {"mts-research:rin-vale": {
            "status": status, "current": jobs, "headline": headline}}}
    at = "2026-09-24T00:00:00+00:00"
    moved = read("current", [{"title": "Research Engineer", "company": "Northwind Labs", "started": "2025-02"},
                             {"title": "Advisor", "company": "Tiny Startup", "started": "2026-01"}])
    assert happened.employer(moved, "mts-research:rin-vale", at, "Old Lab") == "Northwind Labs"
    assert happened.employer(moved, "mts-research:rin-vale", at, "Northwind Labs Inc.") == "Northwind Labs Inc."
    assert happened.employer(moved, "mts-research:rin-vale", "2026-09-01T00:00:00+00:00", "Old Lab") == "Old Lab"
    assert happened.employer(moved, "mts-research:sol-park", at, "Old Lab") == "Old Lab"  # no read for them
    two = read("current", [{"title": "Engineer", "company": "Acme", "started": "2026-05"},
                           {"title": "Lecturer", "company": "City College", "started": "2019-09"}], "Lecturer at City College")
    assert happened.employer(two, "mts-research:rin-vale", at, "Old Lab") == "City College"  # the one their headline names
    metaculus = read("current", [{"title": "Forecaster", "company": "Metaculus", "started": "2026-03"}])
    assert happened.employer(metaculus, "mts-research:rin-vale", at, "Meta") == "Metaculus"  # whole names, not letters
    agents = read("current", [{"title": "Engineer", "company": "X", "started": "2020-01"},
                              {"title": "Engineer", "company": "Northwind", "started": "2026-06"}], "building next-gen agents")
    assert happened.employer(agents, "mts-research:rin-vale", at, "Old Lab") == "Northwind"  # "X" is not in "next"
    assert happened.employer(read("current", [{"title": "Board member", "company": "Acme"}]), "mts-research:rin-vale", at,
                             "Old Lab") == ""
    assert happened.employer(read("no_current_role", []), "mts-research:rin-vale", at, "Old Lab") == ""
    assert happened.employer(read("not_returned", []), "mts-research:rin-vale", at, "Old Lab") == "Old Lab"
    context = journey.PersonContext(subject_id="mts-research:rin-vale", name="Rin Vale", role="mts-research",
                                    employer="Old Lab")
    role = routing.role_of(context)
    draft = routing.template("Rin Vale", role, context, [], sender_name="Ana", employer="Northwind Labs")
    assert "saw your work at Northwind Labs" in draft["body"] and "Old Lab" not in draft["body"]
    assert "saw your work" not in routing.template("Rin Vale", role, context, [], sender_name="Ana", employer="")["body"]
    assert "saw your work at Old Lab" in routing.template("Rin Vale", role, context, [], sender_name="Ana")["body"]


def test_a_new_version_reads_as_a_release_on_the_card_never_a_launch(store):
    add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "project_release",
                      "event_date": "2026-09-10", "date_precision": "day", "observed_at": "2026-09-10T15:00:00+00:00",
                      "source_url": "https://x.com/me/status/17", "source_version_hash": "r",
                      "quote": "Pocket Diary v2.3 is out now", "tier": 1, "extractor": "test"})
    card = routing.route(store, ME, "2026-09-20T00:00:00+00:00", [])  # past launch week's hold
    assert card.call.action == "reach_now" and card.message == "release"
    [moment] = card.happened
    assert moment.release and happened.line(moment).startswith("new release · ")
    assert routing.trigger(card).startswith("• Person:me shipped Pocket Diary v2.3 on Sep 10: ")
    said = routing.why_now(card)
    assert "launch" not in said and "A new release is a natural moment" in said and "a month after the release" in said
    assert card.signals[0]["what"] == "something they released"
    assert card.draft["body"].startswith("Hey person:me, congrats on shipping Pocket Diary v2.3! Saw your X post. General")
    assert "Sep 10" not in card.draft["body"] + card.draft["note"]  # the card's Trigger dates it; the draft never does


def test_a_release_whose_words_are_withheld_still_reads_as_a_release_and_never_names_it():
    from app.readiness import Readiness

    post = {"id": "p1", "subject_id": ME, "event_type": "launch_announced", "event_date": "2026-09-10",
            "observed_at": "2026-09-10T15:00:00+00:00", "source_url": "https://x.com/me/status/18",
            "quote": "Back from parental leave: Pocket Diary v2.3 is out now"}
    call = Readiness(as_of="2026-09-20T00:00:00+00:00", score=0.5, action="reach_now", families={}, reasons=["launch"],
                     holds=[], open_windows=[], earliest_close=None, explanation="", evidence={"launch": ["p1"]},
                     track="rapport")
    [signal] = routing.signals(call, {"p1": post})
    assert signal["quote"] == routing.WITHHELD and signal["release"] and signal["shipped"] == ""
    card = routing.Route.model_construct(name="person:me", subject_id=ME, call=call, signals=[signal])
    assert routing._clauses(card)[0][1] == "person:me shipped a new release on Sep 10"
    assert routing._natural(card) == routing.NOW["release"]
