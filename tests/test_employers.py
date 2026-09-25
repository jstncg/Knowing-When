"""A watched person's employer, from their LinkedIn profile read, and what the news says happened to it: invented
people and headlines, no network."""

import asyncio

import pytest

from app import contact, journey, readiness
from app.sources import linkedin_profiles as lp
from app.sources import news, warn
from app.sources.social import Person
from app.store import Store
from app.timeline import add_event

AS_OF = "2026-09-24T12:00:00+00:00"
READ_ON = "2026-09-24"
ME = "person:rin"


def fetch_of(feed):
    return lambda url: feed.encode()


def rss(*items):
    return "<rss><channel>" + "".join(
        f"<item><title>{title} - Example Wire</title><link>https://example.org/news/{n}</link>"
        f"<pubDate>{published}</pubDate></item>" for n, (title, published) in enumerate(items)) + "</channel></rss>"


def person(store, **kw):
    journey.save_context(store, journey.PersonContext(**{"subject_id": ME, "name": "Rin Vale", "role": "product-designer",
                                                         "related": ["gi"], **kw}))


def profile(company, started="2024-03", status="current", joined=None):
    return {ME: {"status": status, "linkedin_url": "https://www.linkedin.com/in/rin", "current": [
        {"title": "Product Designer", "company": company, "started": started, "joined": joined or started}] if company else []}}


def employer_news(store, feed, as_of=AS_OF, monkeypatch=None):
    if monkeypatch:  # the WARN file is the other free employer source: none here
        monkeypatch.setattr(warn, "load", lambda fetch=None: [])
    return asyncio.run(journey.employer_sources(store, [ME], as_of, fetch_of(feed)))


@pytest.mark.parametrize("headline, company, want", [
    ("Birch to acquire Acme for $610M", "Acme", True),
    ("Birch completes acquisition of Acme", "Acme", True),
    ("Acme to be acquired by Birch", "Acme", True),
    ("Acme agrees to sell itself to Birch", "Acme", True),
    ("Atlassian to acquire The Browser Company for $610 million", "The Browser Company", True),
    ("Birch completes Acme acquisition", "Acme", True),
    ("Birch to acquire Snap", "Snap Inc.", True),  # the profile's legal name, as headlines write it
    ("Birch to acquire Acme Robotics", "Acme Robotics, Inc.", True),
    ("Foxconn acquires Apple supplier Luxshare", "Apple", False),  # a longer name that starts with theirs
    ("Birch acquires Meta Materials", "Meta", False),
    ("Birch acquires Scale Computing", "Scale AI", False),
    ("Acme acquires Birch", "Acme", False),  # the employer buying is not the employer bought
    ("Birch acquires Acme's gaming unit", "Acme", False),
    ("Acme sells robot unit to Birch", "Acme", False),
    ("Birch in talks to acquire Acme", "Acme", False),  # talks and rumors are no deal
    ("Birch nears deal to buy Acme", "Acme", False),
    ("Acme said to be exploring a sale", "Acme", False),
    ("Birch walks away from Acme deal", "Acme", False),
    ("Did Birch buy Discord to rebrand it as 'xD'? Fact-checking viral claim", "Discord", False),  # a question
    ("Birch to acquire Acme? Here's what we know", "Acme", False),
])
def test_an_employer_is_bought_only_when_a_headline_says_the_deal_is_agreed_or_done(headline, company, want):
    assert news.bought(headline, company) is want


@pytest.mark.parametrize("headline, want", [
    ("Acme lays off 200", True),
    ("Acme lays off 200 in May", True),
    ("Acme to cut 10% of staff", True),
    ("Acme slashes 1,000 jobs", True),
    ("Layoffs at Acme", True),
    ("Acme layoffs hit AI team", True),
    ("Acme's layoffs hit AI team", True),
    ("Layoffs at Acme Materials", False),
    ("Acme cuts prices", False),  # a cut of people, never of anything else
    ("Acme cuts ties with Birch", False),
    ("Acme denies layoffs", False),
    ("Acme CEO says no more layoffs", False),
    ("Acme weighs layoffs", False),
    ("Birch lays off 100 after buying Acme", False),  # someone else's cuts
    ("Is Acme laying off staff?", False),
    ("Acme lays off 200, viral post claims", False),
])
def test_layoffs_count_only_with_the_employer_as_their_subject(headline, want):
    assert news.layoffs(headline, "Acme") is want


def kinds(feed, company="Acme"):
    return {(r["event_type"], r["day"]) for r in news.employer(fetch_of(feed), company)}


def test_employer_news_keeps_deals_layoffs_and_a_sale_called_off():
    feed = rss(("Birch to acquire Acme", "Tue, 01 Sep 2026 14:00:00 GMT"),
               ("Acme lays off 40 after the deal", "Thu, 10 Sep 2026 09:30:00 GMT"),
               ("Acme opens a Paris office", "Fri, 11 Sep 2026 09:30:00 GMT"),
               ("Birch in talks to acquire Acme", "Mon, 24 Aug 2026 09:30:00 GMT"))
    rows = news.employer(fetch_of(feed), "Acme")
    assert {(r["event_type"], r["at"]) for r in rows} == {("layoffs_reported", "2026-09-10T09:30:00+00:00"),
                                                           ("acquisition", "2026-09-01T14:00:00+00:00")}
    off = feed.replace("Acme opens a Paris office", "Birch walks away from Acme deal")
    assert kinds(off) == {("layoffs_reported", "2026-09-10"), ("acquisition", "2026-09-01"),
                          ("acquisition_called_off", "2026-09-11")}
    # Never the company's own bid or other news.
    for other in ("Acme drops bid to buy Birch", "Acme loses bid for Pentagon contract"):
        assert kinds(feed.replace("Acme opens a Paris office", other)) == kinds(feed)


def test_the_profile_read_names_the_employer_and_a_new_one_drops_the_old_ones_ids(store):
    person(store, related=["gi", "person:coauthor"])
    assert journey.save_employers(store, profile("Acme Robotics"), READ_ON) == ([ME], [])
    ctx = journey.load_context(store, ME)
    assert (ctx.employer, ctx.employer_since, ctx.related) == ("Acme Robotics", "2024-03",
                                                               ["gi", "org:acme-robotics", "person:coauthor"])
    journey.save_context(store, ctx.model_copy(update={"related": [*ctx.related, "org:acme-robotics-ca"]}))  # a WARN name
    journey.save_employers(store, profile("Birch", "2026-02"), READ_ON)
    ctx = journey.load_context(store, ME)
    assert (ctx.employer, ctx.employer_since, ctx.related) == ("Birch", "2026-02", ["gi", "org:birch", "person:coauthor"])
    journey.save_employers(store, {ME: {"status": "not_returned"}}, READ_ON)  # nothing read: the employer stays
    assert journey.load_context(store, ME).employer == "Birch"
    for gone in (profile("Stealth Startup"), profile(None, status="no_current_role")):
        journey.save_employers(store, gone, READ_ON)
        ctx = journey.load_context(store, ME)
        assert (ctx.employer, ctx.employer_since, ctx.related) == ("", "", ["gi", "person:coauthor"])
    assert journey.save_employers(store, {"person:unknown": profile("Acme")[ME]}, READ_ON) == ([], ["person:unknown"])


def test_an_employer_acquisition_and_layoffs_reach_them_as_a_borrowed_pitch(store, monkeypatch):
    person(store)
    journey.save_employers(store, profile("Acme Robotics", "2024-03"), READ_ON)
    feed = rss(("Birch to acquire Acme Robotics", "Thu, 10 Sep 2026 14:00:00 GMT"),
               ("Acme Robotics lays off 40 after the deal", "Tue, 15 Sep 2026 09:30:00 GMT"))
    report = employer_news(store, feed, monkeypatch=monkeypatch)[ME]
    assert report["coverage"]["news"]["status"] == "ok" and report["coverage"]["news"]["events"] == 2
    events = {e["event_type"]: e for e in journey.view(store, ME, AS_OF) if e["subject_id"] == "org:acme-robotics"}
    assert set(events) == {"acquisition", "layoffs_reported"}
    assert (events["acquisition"]["tier"], events["acquisition"]["observed_at"]) == (2, "2026-09-10T14:00:00+00:00")
    call = journey.assess(store, ME, AS_OF, "product-designer")
    assert (call.action, call.track) == ("reach_now", "pitch")
    assert {"acquisition_closed", "warn_notice"} <= set(call.reasons) and journey.BORROWED in call.explanation
    # Before the layoffs were public, the deal alone is one reason: not now.
    assert journey.assess(store, ME, "2026-09-12T12:00:00+00:00", "product-designer").action != "reach_now"


def test_their_launch_with_an_employer_deal_is_a_pitch_and_alone_a_note(store, monkeypatch):
    person(store)
    add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "launch_announced",
                      "event_date": "2026-09-15", "date_precision": "day", "observed_at": "2026-09-15T12:00:00+00:00",
                      "source_url": "https://x.com/rin/status/1", "source_version_hash": "l1",
                      "quote": "Tiny Journal is out now", "tier": 1, "extractor": "test"})
    assert journey.assess(store, ME, AS_OF, "product-designer").track == "rapport"
    journey.save_employers(store, profile("Acme Robotics", "2024-03"), READ_ON)
    employer_news(store, rss(("Birch to acquire Acme Robotics", "Thu, 10 Sep 2026 14:00:00 GMT")), monkeypatch=monkeypatch)
    call = journey.assess(store, ME, AS_OF, "product-designer")
    assert (call.action, call.track) == ("reach_now", "pitch") and "acquisition_closed" in call.reasons


def test_an_employer_event_from_before_they_joined_is_not_theirs(store, monkeypatch):
    person(store)
    journey.save_employers(store, profile("Acme Robotics", "2026-09"), READ_ON)  # joined after the deal and the layoffs
    employer_news(store, rss(("Birch to acquire Acme Robotics", "Tue, 01 Sep 2025 14:00:00 GMT"),
                             ("Acme Robotics lays off 40", "Thu, 20 Aug 2026 09:30:00 GMT")), monkeypatch=monkeypatch)
    assert not [e for e in journey.view(store, ME, AS_OF) if e["subject_id"] == "org:acme-robotics"]
    assert journey.assess(store, ME, AS_OF, "product-designer") is None  # nothing of theirs, nothing borrowed
    ctx = journey.load_context(store, ME)  # nor does a draft on a replayed morning name a later employer
    assert (journey.employer_on(ctx, "2026-08-31T12:00:00+00:00"), journey.employer_on(ctx, AS_OF)) == ("", "Acme Robotics")
    journey.save_employers(store, profile("Acme Robotics", "2024"), READ_ON)  # a year alone counts from its end
    assert len([e for e in journey.view(store, ME, AS_OF) if e["subject_id"] == "org:acme-robotics"]) == 2


def test_a_promotion_keeps_the_day_they_joined_and_a_year_alone_counts_from_the_read_at_the_latest(store, monkeypatch):
    person(store)
    # Joined 2022, promoted 2025: LinkedIn lists the promotion as a new job at the same company.
    row = lp.roles([Person.from_dict({"person_id": ME, "linkedin_url": "https://www.linkedin.com/in/rin"})], [{
        "linkedinUrl": "https://www.linkedin.com/in/rin", "experience": [{"companyName": "Acme Robotics", "positions": [
            {"title": "Staff Designer", "startDate": "2025-06"},
            {"title": "Designer", "startDate": "2022-01", "endDate": "2025-06"}]}]}])
    assert row[ME]["current"][0] == {"title": "Staff Designer", "company": "Acme Robotics", "started": "2025-06",
                                          "joined": "2022-01"}
    journey.save_employers(store, row, READ_ON)
    employer_news(store, rss(("Birch to acquire Acme Robotics", "Mon, 01 Jul 2024 14:00:00 GMT")), monkeypatch=monkeypatch)
    assert [e["event_type"] for e in journey.view(store, ME, AS_OF) if e["subject_id"] == "org:acme-robotics"] == [
        "acquisition"]
    # "Since 2026", read on Mar 10: surely there by the read, so a June deal is theirs.
    journey.save_employers(store, profile("Acme Robotics", "2026"), "2026-03-10")
    assert journey.load_context(store, ME).employer_since == "2026-03-10"
    employer_news(store, rss(("Birch completes Acme Robotics acquisition", "Mon, 15 Jun 2026 14:00:00 GMT")),
                  monkeypatch=monkeypatch)
    assert [e["event_date"] for e in journey.view(store, ME, AS_OF) if e["subject_id"] == "org:acme-robotics"] == [
        "2026-06-15"]


def test_a_move_found_after_the_days_run_reads_the_new_employer_and_drops_the_old_one(store, monkeypatch):
    person(store)
    feeds = {"Oldco": rss(("Oldco lays off 40", "Tue, 22 Sep 2026 09:30:00 GMT")), "Newco": rss()}
    fetch = lambda url: feeds["Oldco" if "Oldco" in url else "Newco"].encode()  # noqa: E731
    monkeypatch.setattr(warn, "load", lambda fetch=None: [])
    journey.save_employers(store, profile("Oldco", "2023-01"), READ_ON)
    asyncio.run(journey.employer_sources(store, [ME], "2026-09-24T07:30:00+00:00", fetch))  # the morning run
    journey.save_employers(store, profile("Newco", "2026-09"), READ_ON)  # the profile read, later that day
    report = asyncio.run(journey.employer_sources(store, [ME], AS_OF, fetch))[ME]
    assert report["coverage"]["news"]["status"] == "empty" and "cached" not in report["coverage"]["news"]
    assert journey.load_context(store, ME).related == ["gi", "org:newco"]
    assert not [e for e in journey.view(store, ME, AS_OF) if e["subject_type"] == "org" and e["subject_id"] != "gi"]


def test_a_headline_published_after_the_day_asked_is_not_on_its_timeline(store, monkeypatch):
    person(store)
    journey.save_employers(store, profile("Acme Robotics", "2024-03"), READ_ON)
    report = employer_news(store, rss(("Acme Robotics lays off 40", "Fri, 25 Sep 2026 09:30:00 GMT")),
                           monkeypatch=monkeypatch)[ME]
    assert report["coverage"]["news"]["status"] == "empty"


def test_a_deal_called_off_voids_the_deal_from_the_day_it_was_published_whichever_read_saw_it(store, monkeypatch,
                                                                                              tmp_path):
    person(store)
    journey.save_employers(store, profile("Acme Robotics", "2024-03"), READ_ON)
    deal = ("Birch to acquire Acme Robotics", "Thu, 10 Sep 2026 14:00:00 GMT")
    off = ("Birch walks away from Acme Robotics deal", "Mon, 14 Sep 2026 09:00:00 GMT")
    cut = ("Acme Robotics lays off 40", "Tue, 15 Sep 2026 09:30:00 GMT")
    employer_news(store, rss(deal), as_of="2026-09-12T12:00:00+00:00", monkeypatch=monkeypatch)  # a first read
    employer_news(store, rss(deal, off, cut), monkeypatch=monkeypatch)  # a later one sees the deal fall through
    reasons = lambda day: journey.assess(store, ME, f"{day}T12:00:00+00:00", "product-designer").reasons  # noqa: E731
    assert reasons("2026-09-12") == ["acquisition_closed"] and "acquisition_closed" not in reasons("2026-09-16")
    # Read only after it fell through, the deal still stood on the mornings before it: a replay sees it as they did.
    fresh = Store(f"sqlite:///{tmp_path / 'fresh.sqlite'}")
    person(fresh)
    journey.save_employers(fresh, profile("Acme Robotics", "2024-03"), READ_ON)
    employer_news(fresh, rss(deal, off, cut), monkeypatch=monkeypatch)
    assert journey.assess(fresh, ME, "2026-09-12T12:00:00+00:00", "product-designer").reasons == ["acquisition_closed"]
    assert journey.assess(fresh, ME, "2026-09-16T12:00:00+00:00", "product-designer").reasons == ["warn_notice"]
    # A deal that fell through years before another is not voided by it.
    figma = [("Adobe to acquire Acme Robotics for $20 billion", "Thu, 15 Sep 2022 12:00:00 GMT"),
             ("Adobe abandons $20 billion Acme Robotics deal", "Mon, 18 Dec 2023 12:00:00 GMT"),
             ("Birch to acquire Acme Robotics", "Tue, 15 Sep 2026 14:00:00 GMT")]
    employer_news(fresh, rss(*figma), monkeypatch=monkeypatch)
    assert "acquisition_closed" in journey.assess(fresh, ME, "2026-09-20T12:00:00+00:00", "product-designer").reasons


def test_no_employer_no_news_and_a_replay_never_reads_todays_feed(store):
    person(store)
    fetched = []
    assert asyncio.run(journey.employer_sources(store, [ME], AS_OF, fetched.append)) == {}  # no employer: not asked
    assert fetched == [] and journey.assess(store, ME, AS_OF, "product-designer") is None
    ctx = journey.load_context(store, ME).model_copy(update={"employer": "Acme Robotics"})
    report = asyncio.run(journey.build_journey(ctx, AS_OF, run=journey.Run(store, replay=True, fetch=fetched.append),
                                               only={"news"}))
    assert report["coverage"]["news"] == {"status": "unavailable", "detail": "live watchlist only"} and fetched == []


def launch(store, day):
    add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "launch_announced", "event_date": day,
                      "date_precision": "day", "observed_at": f"{day}T12:00:00+00:00",
                      "source_url": f"https://x.com/rin/status/{day}", "source_version_hash": day,
                      "quote": "Tiny Journal is out now", "tier": 1, "extractor": "test"})


def test_time_in_a_seat_and_a_retention_cliff_are_listed_and_never_decide_a_call(store, monkeypatch):
    person(store)
    journey.save_employers(store, profile("Acme Robotics", "2024-03"), READ_ON)
    add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "job_started", "event_date": "2024-09-25",
                      "date_precision": "day", "observed_at": "2024-09-25T12:00:00+00:00",
                      "source_url": "https://x.com/rin/status/2", "source_version_hash": "j1",
                      "quote": "Started at Acme Robotics today", "tier": 1, "extractor": "test"})
    employer_news(store, rss(("Birch completes Acme Robotics acquisition", "Sun, 20 Sep 2025 14:00:00 GMT"),
                             ("Acme Robotics lays off 40", "Thu, 10 Sep 2026 09:30:00 GMT")), monkeypatch=monkeypatch)
    fired = {a["detector_id"] for a in journey.detect(store, ME, AS_OF, role="product-designer")}
    assert {"tenure_milestone", "retention_cliff"} <= fired  # both open: two years in, twelve months after the deal
    call = journey.assess(store, ME, AS_OF, "product-designer")
    assert call.action != "reach_now" and call.reasons == ["warn_notice"] and call.until is None
    listed = call.explanation.split("Listed, never a reason: ")[1]
    assert "a work anniversary in their current role" in listed and "a year or two after their employer's" in listed
    # Beside a fresh launch of theirs, the pitch rests on the launch and the layoffs alone; without the layoffs, a note.
    launch(store, "2026-09-15")
    call = journey.assess(store, ME, AS_OF, "product-designer")
    assert (call.action, call.track, set(call.reasons)) == ("reach_now", "pitch", {"launch", "warn_notice"})
    quiet_employer = readiness.score([a for a in journey.detect(store, ME, AS_OF, role="product-designer")
                                      if a["detector_id"] != "warn_notice"], AS_OF)
    assert (quiet_employer.action, quiet_employer.track) == ("reach_now", "rapport")


def test_an_employer_deal_makes_a_pitch_only_of_a_sign_as_fresh_as_the_stale_sign_rail(store, monkeypatch):
    assert readiness.UPGRADE_DAYS == contact.STALE_DAYS
    person(store)
    journey.save_employers(store, profile("Acme Robotics", "2024-03"), READ_ON)
    employer_news(store, rss(("Birch completes Acme Robotics acquisition", "Sat, 05 Sep 2026 14:00:00 GMT")),
                  monkeypatch=monkeypatch)
    launch(store, "2026-08-30")
    fresh = journey.assess(store, ME, "2026-09-10T12:00:00+00:00", "product-designer")
    assert (fresh.action, fresh.track) == ("reach_now", "pitch")
    stale = journey.assess(store, ME, AS_OF, "product-designer")  # the launch is 25 days old: a note, as before
    assert (stale.action, stale.track) == ("reach_now", "rapport") and "acquisition_closed" in stale.reasons


def test_an_old_deal_is_never_a_reason_on_its_fading_strength_and_one_deal_counts_once(store, monkeypatch):
    person(store)
    journey.save_employers(store, profile("The Browser Company", "2023-01"), READ_ON)
    employer_news(store, rss(("Atlassian to acquire The Browser Company for $610 million", "Mon, 08 Sep 2025 12:00:00 GMT"),
                             ("Atlassian completes acquisition of The Browser Company", "Tue, 21 Oct 2025 12:00:00 GMT")),
                  monkeypatch=monkeypatch)
    launch(store, "2026-07-20")
    # Nine and ten months on, the deal is no reason for now: their launch alone is a note.
    call = journey.assess(store, ME, "2026-07-27T12:00:00+00:00", "product-designer")
    assert (call.action, call.track, call.reasons) == ("reach_now", "rapport", ["launch"])
    assert "employer" not in call.families and "was acquired" not in call.explanation
    assert journey.assess(store, ME, "2025-11-12T12:00:00+00:00", "product-designer").reasons == []  # 22 days on


def test_reports_of_one_deal_open_together_are_one_reason_that_claims_them_all(store, monkeypatch):
    person(store)
    journey.save_employers(store, profile("Acme Robotics", "2024-03"), READ_ON)
    employer_news(store, rss(("Birch to acquire Acme Robotics", "Tue, 15 Sep 2026 14:00:00 GMT"),
                             ("Birch completes Acme Robotics acquisition", "Sat, 19 Sep 2026 14:00:00 GMT")),
                  monkeypatch=monkeypatch)
    call = journey.assess(store, ME, AS_OF, "product-designer")
    headlines = sorted((e for e in journey.view(store, ME, AS_OF) if e["event_type"] == "acquisition"),
                       key=lambda e: e["observed_at"], reverse=True)
    assert call.reasons == ["acquisition_closed"] and call.evidence["acquisition_closed"] == [e["id"] for e in headlines]
    assert call.explanation.count("a deal to buy their employer (until") == 1 and call.families["employer"] == 0.5
    # Their own post about the same deal is claimed by it too: one reason, a note, never a pitch of two.
    add_event(store, {"subject_type": "person", "subject_id": ME, "event_type": "acquisition", "event_date": "2026-09-16",
                      "date_precision": "day", "observed_at": "2026-09-16T12:00:00+00:00",
                      "source_url": "https://x.com/rin/status/9", "source_version_hash": "p9",
                      "quote": "Acme Robotics was acquired by Birch!", "tier": 1, "extractor": "test"})
    call = journey.assess(store, ME, AS_OF, "product-designer")
    assert (call.action, call.track, call.reasons) == ("reach_now", "rapport", ["acquisition_closed"])


def test_layoffs_in_the_news_count_three_weeks_and_a_warn_filing_its_months(store, monkeypatch):
    person(store)
    journey.save_employers(store, profile("Acme Robotics", "2024-03"), READ_ON)
    employer_news(store, rss(("Acme Robotics lays off 40", "Tue, 01 Sep 2026 09:30:00 GMT")), monkeypatch=monkeypatch)
    assert "warn_notice" in journey.assess(store, ME, "2026-09-20T12:00:00+00:00", "product-designer").reasons
    assert "warn_notice" not in journey.assess(store, ME, "2026-09-24T12:00:00+00:00", "product-designer").reasons
    add_event(store, {"subject_type": "org", "subject_id": "org:acme-robotics", "event_type": "warn_notice",
                      "event_date": "2026-08-01", "date_precision": "day", "observed_at": "2026-08-03T12:00:00+00:00",
                      "source_url": "https://warn.example/acme", "source_version_hash": "w1", "tier": 1,
                      "quote": "WARN notice filed 2026-08-01: 40 employees affected.", "extractor": "test"})
    assert "warn_notice" in journey.assess(store, ME, "2026-09-24T12:00:00+00:00", "product-designer").reasons
    # Open together with the headline, the filing keeps the one reason until its own close, with both as evidence.
    both = journey.assess(store, ME, "2026-09-20T12:00:00+00:00", "product-designer")
    assert both.earliest_close[:10] == "2026-11-29" and len(both.evidence["warn_notice"]) == 2


def test_a_call_off_has_a_search_of_its_own_so_it_never_crowds_out_the_deals():
    deals = rss(("Birch to acquire Acme", "Tue, 01 Sep 2026 14:00:00 GMT"))
    offs = rss(("Birch walks away from Acme deal", "Mon, 14 Sep 2026 09:00:00 GMT"))
    asked = []
    rows = news.employer(lambda url: asked.append(url) or (offs if "walks" in url else deals).encode(), "Acme")
    assert len(asked) == 2 and {r["event_type"] for r in rows} == {"acquisition", "acquisition_called_off"}
