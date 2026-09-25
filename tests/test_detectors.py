"""Detectors over synthetic timelines: one per detector, plus the as-of leakage contract."""

from itertools import count
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import detectors, happened, timeline
from app.store import Store

ME, ORG, CFO, PEER = "person:me", "org:acme", "jane-doe|acme", "person:peer"
_ids = count()


def ev(subject, event_type, event_date=None, observed=None, quote="x", tier=3, **extra):
    """An in-memory event as the store would return it; extra fields (topic, title) stay for pure detect()."""
    precision = {4: "year", 7: "month", 10: "day"}.get(len(event_date or ""))
    record = timeline.TimelineEvent(
        subject_type="org" if subject.startswith("org:") or subject == "gi" else "person", subject_id=subject,
        event_type=event_type, event_date=event_date, date_precision=precision,
        observed_at=observed or (f"{event_date}T00:00:00+00:00" if precision == "day" else "2026-01-01T00:00:00+00:00"),
        quote=quote, tier=tier, extractor="test").model_dump()
    return {**record, **extra, "id": f"ev:{next(_ids)}"}


def fire(events, as_of="2026-09-22", subject=ME):
    """Last activation per detector; use ``fires`` when one detector emits several."""
    return {a["detector_id"]: a for a in detectors.detect(events, subject, f"{as_of}T12:00:00+00:00")}


def fires(events, detector_id, as_of="2026-09-22", subject=ME):
    return [a for a in detectors.detect(events, subject, f"{as_of}T12:00:00+00:00") if a["detector_id"] == detector_id]


def open_at(activation, day):
    return activation["window_open"][:10] <= day <= activation["window_close"][:10]


# ------------------------------------------------------------------ career

def test_tenure_milestones_from_role_start_and_own_typical_tenure():
    history = [ev(ME, "job_started", "2019-01-01"), ev(ME, "job_started", "2021-07-01"),
               ev(ME, "job_started", "2024-01-01"), ev(ME, "job_started", "2025-09-01")]
    fired = fires(history, "tenure_milestone")
    assert [a["strength"] for a in fired] == [0.4, 0.5]  # the 1-year mark is open, 2 years is within a year, 4 is not
    assert open_at(fired[0], "2026-09-22") and fired[0]["evidence_event_ids"] == [history[-1]["id"]]
    typical = fires(history, "tenure_milestone", as_of="2028-05-01")
    assert [a["strength"] for a in typical] == [0.6]  # median gap of ~2.5 years, the person's own rhythm


def test_tenure_milestone_ignores_year_precision_starts():
    assert "tenure_milestone" not in fire([ev(ME, "affiliation_change", "2025")])


def test_a_fixed_term_role_has_no_tenure_milestone_or_short_tenure_hold():
    student = ev(ME, "job_started", "2026-06", quote="Student Researcher, Example Lab (June 2026 to mid-October 2026)")
    interim = ev(ME, "job_started", "2026-01-05", quote="Controller, Atlas")
    ends = ev(ME, "placement_end_expected", "2026-12-31", quote="The engagement runs through December 31, 2026.")
    phd = ev(ME, "job_started", "2026-02", quote="PhD candidate, Example University")
    for events in ([student], [interim, ends], [phd]):
        fired = fire(events, as_of="2026-09-22")
        assert "tenure_milestone" not in fired and "hold_short_tenure" not in fired
    assert "hold_short_tenure" in fire([interim])  # the same start without a stated end is under a year in


def test_a_job_start_seen_only_on_linkedin_holds_the_first_year_and_feeds_nothing_else():
    seen = "2026-09-20T00:00:00+00:00"  # the day the profile was read
    start = ev(ME, "profile_job_started", "2026-03", observed=seen, quote="Staff Engineer at Brambleworld since 2026-03")
    fired = fire([start])
    assert fired["hold_short_tenure"]["holds"] == ["short_tenure"] and set(fired) == {"hold_short_tenure"}
    assert "hold_short_tenure" not in fire([start], as_of="2027-03-02")  # a year in
    older = ev(ME, "profile_job_started", "2024-06", observed=seen, quote="Staff Engineer at Brambleworld since 2024-06")
    assert fire([older]) == {}  # two years in the seat is listed from the profile, never a reason to reach
    newer = fire([ev(ME, "job_started", "2023-01-01"), start])  # the latest start, from either source, holds
    assert newer["hold_short_tenure"]["evidence_event_ids"] == [start["id"]]


def test_a_profile_start_known_only_by_its_year_holds_until_they_are_surely_a_year_in():
    start = ev(ME, "profile_job_started", "2026", observed="2026-09-24T00:00:00+00:00",
               quote="Member of Technical Staff at Example AI since 2026")
    hold = fire([start], as_of="2026-09-25")["hold_short_tenure"]
    assert hold["window_close"][:10] == "2027-09-24"  # it may have begun the day the profile was read
    assert "hold_short_tenure" in fire([start], as_of="2027-06-01") and "hold_short_tenure" not in fire([start], as_of="2027-09-25")
    assert "hold_short_tenure" not in fire([ev(ME, "job_started", "2026")])  # a year alone from other sources still does not hold
    again = {**start, "id": "ev:reread", "observed_at": "2027-03-01T00:00:00+00:00"}  # the same job, read again later
    assert fire([start, again], as_of="2027-03-02")["hold_short_tenure"]["window_close"][:10] == "2027-09-24"


def test_a_profile_hold_lifts_when_a_later_read_shows_the_job_ended_and_a_bare_year_never_cancels_it():
    start = ev(ME, "profile_job_started", "2026-03", observed="2026-09-01T00:00:00+00:00", quote="Engineer at A since 2026-03")
    ended = ev(ME, "profile_job_ended", "2026-09", observed="2026-09-20T00:00:00+00:00", quote="Engineer at A, 2026-03 to 2026-09")
    assert "hold_short_tenure" in fire([start]) and "hold_short_tenure" not in fire([start, ended])
    # LinkedIn lags: an end read before a start was seen never lifts it.
    early_end = ev(ME, "profile_job_ended", "2026-08", observed="2026-09-20T00:00:00+00:00", quote="Old job, to 2026-08")
    new_job = ev(ME, "profile_job_started", "2026-09", observed="2026-10-05T00:00:00+00:00", quote="New job since 2026-09")
    assert "hold_short_tenure" in fire([early_end, new_job], as_of="2026-10-06")
    page = ev(ME, "job_started", "2026-08-15", observed="2026-09-25T00:00:00+00:00", quote="Engineer, B")
    assert "hold_short_tenure" in fire([early_end, page], as_of="2026-09-26")
    bare = ev(ME, "affiliation_change", "2026", quote="Example University: years [2026]")
    assert fire([bare, start])["hold_short_tenure"]["evidence_event_ids"] == [start["id"]]


def test_github_releases_a_build_made_are_not_launches():
    assert detectors.automated_release("Released rules-20260811-0932 of rin/scraper")
    assert detectors.automated_release("Released nightly of a/b: Nightly build")
    assert not detectors.automated_release("Released v1.0 of rin/tidebrowser: Tidebrowser 1.0")
    assert not detectors.automated_release("Released 2026.09.13 of a/b")
    assert not detectors.automated_release("Released v20260920 of me/x: Brambleworld 2.0")  # a person named it
    # A time of day or a timestamp in the tag is a build whatever the release is named (a live card's was "Rules…").
    assert detectors.automated_release("Released rules-20260811-0932 of rin/scraper: Rules…")
    assert detectors.automated_release("Released 2026-09-13-1200 of a/b: Brambleworld")
    assert detectors.automated_release("Released 1726212345 of a/b: Brambleworld")
    # A date alone is a build only when the name adds nothing to the tag.
    assert detectors.automated_release("Released rules-20260913 of a/b: Rules")
    assert detectors.automated_release("Released v20260920 of me/x: Release v20260920")
    assert not detectors.automated_release("Released rules-20260913 of a/b: Rules for 140 more sites")
    assert detectors.automated_release("Released build-42 of a/b") and detectors.automated_release("Released v2.0-ci of a/b")
    for built in ("rules-20260811-093214 of me/x: Rules update", "20260811T093214Z of me/x: Rules update",  # seconds
                  "1726212345123 of a/b: Brambleworld", "nightly-2026-09-13 of a/b: Bramble", "build42 of a/b",
                  "2026-09-13 of a/b: Nightly build", "v20260913 of a/b: CI build", "canary.3 of a/b",
                  "2026-09-13 of a/b: Nightly build for 2026-09-13", "v20260913 of a/b: Build #42"):
        assert detectors.automated_release(f"Released {built}"), built
    for hand in ("v1.2.0 of a/b: v1.2.0", "v1.0-rc1 of a/b", "cinema-1.0 of a/b", "v3.1 of a/b: Rebuild",
                 "2026-09-13 of a/b: Brambleworld 2.0", "v20260920 of me/x: Brambleworld 20260920, multiplayer",
                 "v20260920 of me/x: 世界模型", "2026091301 of me/x: Brambleworld",  # a name in any script; a date + count
                 "build-tools-v1.2.0 of a/b", "ci-helper-v0.3 of a/b", "@me/ci-utils@1.0.0 of a/b",  # packages
                 "snapshot-testing-v2.0.0 of a/b"):
        assert not detectors.automated_release(f"Released {hand}"), hand


def test_a_build_release_and_its_reading_are_never_a_launch_a_hold_or_a_moment():
    url = "https://github.com/me/scraper/releases/tag/rules-20260811-0932"
    build = ev(ME, "github_activity", "2026-09-13", quote="Released rules-20260811-0932 of me/scraper: Rules update",
               source_url=url)
    read = ev(ME, "project_release", "2026-09-13", quote="Rules update for scraper", source_url=url)
    for as_of in ("2026-09-10", "2026-09-16", "2026-09-30"):  # before, in and after launch week
        assert {"launch", "hold_imminent_launch"}.isdisjoint(fire([build, read], as_of=as_of)), as_of
    assert happened.recent([build, read], ME, "2026-09-16T12:00:00+00:00") == []
    hand = ev(ME, "project_release", "2026-09-13", quote="Released v1.0 of me/tidebrowser: Tidebrowser 1.0",
              source_url="https://github.com/me/tidebrowser/releases/tag/v1.0")
    assert {"launch", "hold_imminent_launch"} <= set(fire([build, read, hand], as_of="2026-09-16"))
    assert [h.kind for h in happened.recent([build, read, hand], ME, "2026-09-16T12:00:00+00:00")] == ["launch"]
    repo = ev(ME, "github_repo", "2026-09-01", quote="Created me/scraper", source_url="https://github.com/me/scraper")
    odd = {**build, "source_url": repo["source_url"]}  # a build with no release page of its own
    assert detectors.made_by_hand([odd, repo]) == [repo]  # never takes the repo's other events with it


def test_only_a_github_release_is_a_launch_and_a_repo_they_created_is_never_a_sign_on_its_own():
    url, observed = "https://github.com/me/lantern", "2026-09-10T08:00:00+00:00"
    repo = ev(ME, "github_repo", "2026-09-10", observed=observed, source_url=url, source_version_hash="h1",
              quote="Created me/lantern: Lantern — a tiny clip viewer for the terminal (Rust)")
    read = ev(ME, "project_release", "2026-09-10", observed=observed, source_url=url, source_version_hash="h1",
              quote=repo["quote"])
    wip, ask = ({**read, "event_type": kind} for kind in ("work_in_progress", "technical_ask"))
    built = detectors.as_built([repo, read, wip, ask])  # a new repo, whatever the reader called it: nothing yet
    assert built == [repo] and fire(built, as_of="2026-09-16") == {}
    assert happened.recent(built, ME, "2026-09-16T12:00:00+00:00") == []
    said = ev(ME, "work_in_progress", "2026-09-11", source_url="https://x.com/me/status/2", quote="building Lantern")
    assert detectors.as_built([repo, read, said]) == [repo, said]  # their post about it is its own item
    other = {**read, "source_version_hash": "h2"}  # read from another version of the page: not this item's reading
    assert detectors.as_built([repo, other]) == [repo, other]
    tag = "https://github.com/me/lantern/releases/tag/v1.0"
    release = ev(ME, "github_activity", "2026-09-12", observed="2026-09-12T08:00:00+00:00", source_url=tag,
                 quote="Released v1.0 of me/lantern: Lantern 1.0")
    shipped = ev(ME, "project_release", "2026-09-12", observed="2026-09-12T08:00:00+00:00", source_url=tag,
                 quote="Lantern 1.0")
    assert detectors.as_built([release, shipped]) == [release, shipped]
    assert "launch" in fire([release, shipped], as_of="2026-09-16")
    assert [h.kind for h in happened.recent([release, shipped], ME, "2026-09-16T12:00:00+00:00")] == ["launch"]
    pushed = ev(ME, "github_activity", "2026-09-12", source_url=url, quote="Pushed to me/lantern: add seek bar")
    sign = ev(ME, "work_in_progress", "2026-09-12", source_url=url, quote="add seek bar")
    assert detectors.as_built([pushed, sign]) == [pushed, sign]  # work on it is still a sign
    for quote, kind, becomes in [("Pushed to me/lantern: add seek bar", "github_activity", "work_in_progress"),
                                 ("Opened pull request on them/engine: Faster step", "github_activity", "work_in_progress"),
                                 ("Released v2 of them/engine: out now\n\nIn reply to them/engine: v2", "github_activity",
                                  "work_in_progress"),  # a comment that says so is not the release
                                 ("Forked them/engine", "github_activity", None),
                                 ("Starred them/engine", "github_star", None)]:
        src = ev(ME, kind, "2026-09-12", source_url="https://github.com/them/engine", quote=quote)
        readings = detectors.as_built([src, ev(ME, "launch_announced", "2026-09-12", source_url=src["source_url"],
                                               quote=quote)])
        assert [e["event_type"] for e in readings[1:]] == ([becomes] if becomes else []), quote
    post = ev(ME, "launch_announced", "2026-09-12", source_url="https://x.com/me/status/1", quote="Lantern is out!")
    assert detectors.as_built([post]) == [post]  # a post keeps its reading


def test_their_words_in_a_github_item_are_never_the_feeds_framing():
    gh = "https://github.com/me/lantern"
    words = {
        "Created me/lantern: A clip viewer (Rust)": "A clip viewer",
        "Created me/lantern: A clip viewer (beta)": "A clip viewer (beta)",  # their words, not a language
        "Created me/lantern (Jupyter Notebook)": "",
        "Pushed to me/lantern: fix seek; add captions": "fix seek; add captions",
        "Pushed to me/lantern": "",
        "Opened pull request on them/engine: Faster physics step": "Faster physics step",
        "Opened pull request on them/engine:": "",  # a pull request with no title
        "Review_requested pull request on them/engine: Faster step": "Faster step",
        "Released v1.0 of me/lantern: Lantern 1.0": "Lantern 1.0",
        "Starred them/engine": "",
        "Forked them/engine": "",
        "Nice work!\n\nIn reply to them/engine: Faster physics": "Nice work!",
    }
    for quote, said in words.items():
        assert detectors.their_words({"event_type": "github_activity", "quote": quote, "source_url": gh}) == said, quote
    post = {"event_type": "x_post", "quote": "Created me/lantern: my new viewer", "source_url": "https://x.com/me/status/1"}
    assert detectors.their_words(post) == post["quote"]  # a post's words are all theirs


def test_exec_departure_is_another_person_leaving():
    left = ev(CFO, "officer_departure", "2026-09-01", quote="Jane Doe resigned as Chief Financial Officer")
    assert "exec_departure" not in fire([left], subject=CFO)
    fired = fire([left])["exec_departure"]
    assert fired["family"] == "career" and open_at(fired, "2026-09-22") and fired["window_close"][:10] == "2026-11-30"


def test_two_reports_of_one_departure_are_one_activation():
    reports = [ev(CFO, "officer_departure", "2026-09-01"), ev(CFO, "officer_departure", "2026-09-02")]
    fired = fires(reports, "exec_departure")
    assert len(fired) == 1 and sorted(fired[0]["evidence_event_ids"]) == sorted(e["id"] for e in reports)


def test_pi_departure():
    assert fire([ev(ME, "pi_departure", "2026-08-01")])["pi_departure"]["window_close"][:10] == "2026-11-29"


def test_team_exodus_needs_two_departures_at_one_org_within_60_days():
    first = ev("a|acme", "officer_departure", "2026-07-01")
    assert "team_exodus" not in fire([first, ev("b|other", "officer_departure", "2026-07-20")])
    assert "team_exodus" not in fire([first, ev("b|acme", "officer_departure", "2026-09-15")])
    fired = fire([first, ev("b|acme", "job_ended", "2026-08-20")])["team_exodus"]
    assert fired["window_open"][:10] == "2026-08-20" and len(fired["evidence_event_ids"]) == 2


def test_coauthor_departure_reads_coauthor_ids_from_links():
    link = ev(ME, "coauthor_link", "2025-03-01", quote="Sam Peer (A123) on 'Scaling laws'")
    moved = ev("A123", "job_ended", "2026-09-01")
    assert "coauthor_departure" not in fire([moved])
    assert fire([link, moved])["coauthor_departure"]["evidence_event_ids"] == [moved["id"]]


def test_placement_end_opens_90_days_before():
    fired = fire([ev(ME, "placement_end_expected", "2026-12-01")])["placement_end"]
    assert fired["window_open"][:10] == "2026-09-02" and fired["window_close"][:10] == "2026-12-31"


def test_retention_cliff_at_12_and_24_months():
    deal = ev(ORG, "acquisition_closed", "2025-09-15")
    cliffs = fires([deal], "retention_cliff")
    assert [a["strength"] for a in cliffs] == [0.5, 0.6]
    assert open_at(cliffs[0], "2026-09-22") and cliffs[1]["window_open"][:10] == "2027-08-16"


# -------------------------------------------------------------------- work

@pytest.mark.parametrize("event_type, detector_id, closes", [
    ("paper_v1", "paper_v1", "2026-10-16"), ("paper_accepted", "paper_accepted", "2026-10-31"),
    ("gi_citation", "gi_citation", "2026-10-31"), ("gi_attention", "gi_attention", "2026-10-01"),
])
def test_single_event_windows(event_type, detector_id, closes):
    fired = fire([ev(ME, event_type, "2026-09-01")])[detector_id]
    assert fired["window_open"][:10] == "2026-09-01" and fired["window_close"][:10] == closes


def test_paper_talk_brackets_the_date():
    fired = fire([ev(ME, "paper_talk", "2026-10-01")])["paper_talk"]
    assert (fired["window_open"][:10], fired["window_close"][:10]) == ("2026-09-17", "2026-10-15")


def test_rhythm_change_quiet_and_burst():
    monthly = [ev(ME, "paper_v1", f"2025-{m:02d}-10") for m in range(1, 13)]
    assert "rhythm_change" not in fire(monthly, as_of="2026-01-20")
    # Posting cadence is posting_burst's: a LinkedIn silence is not a paper silence.
    posts = [ev(ME, "linkedin_post", f"2025-{m:02d}-10") for m in range(1, 13)]
    assert "rhythm_change" not in fire(posts, as_of="2026-09-22")
    quiet = fire(monthly, as_of="2026-09-22")["rhythm_change"]
    assert quiet["strength"] == 0.3 and quiet["window_open"][:10] == "2026-03-13"  # three median gaps after the last post
    burst = fire(monthly + [ev(ME, "paper_v1", f"2026-09-{d:02d}") for d in (2, 9, 16)])["rhythm_change"]
    assert burst["strength"] == 0.4 and burst["window_open"][:10] == "2026-09-02"


def test_own_precedent_recurs_before_a_move():
    history = [ev(ME, "paper_v1", "2020-09-01"), ev(ME, "job_started", "2021-01-01"),
               ev(ME, "paper_v1", "2023-08-01"), ev(ME, "job_started", "2024-01-01")]
    assert "own_precedent" not in fire(history, as_of="2026-01-01")
    fired = fire(history + [ev(ME, "paper_v1", "2026-08-01")])["own_precedent"]
    assert fired["window_open"][:10] == "2026-08-01" and len(fired["evidence_event_ids"]) == 3


# ---------------------------------------------------------------- employer

@pytest.mark.parametrize("event_type, closes", [
    ("acquisition_closed", "2027-02-28"), ("warn_notice", "2026-12-30"),
    ("auditor_change", "2026-12-30"), ("late_filing", "2026-12-30"),
])
def test_employer_events_reach_the_person_view(event_type, closes):
    fired = fire([ev(ORG, event_type, "2026-09-01")])[event_type]
    assert fired["family"] == "employer" and fired["window_close"][:10] == closes


def test_grant_end_opens_90_days_before_expected_end():
    fired = fire([ev(ME, "grant_end_expected", "2026-12-01")])["grant_end"]
    assert fired["window_open"][:10] == "2026-09-02"


# ---------------------------------------------------------------------- gi

# -------------------------------------------------------------------- self

def test_self_stated_availability_opens_when_seen_and_runs_past_the_stated_date():
    fired = fire([ev(ME, "self_stated_availability", "2026-11", observed="2026-09-01T00:00:00+00:00",
                     quote="available from November")])["self_stated_availability"]
    assert (fired["window_open"][:10], fired["window_close"][:10], fired["strength"]) == ("2026-09-01", "2026-12-31", 0.9)


def test_contact_constraint_opens_on_the_named_date():
    fired = fire([ev(ME, "contact_constraint", "2026-10-04", observed="2026-09-01T00:00:00+00:00",
                     quote="please reach out after the 4 October deadline")])["stated_follow_up"]
    assert fired["family"] == "self" and fired["window_open"][:10] == "2026-10-04"
    # "After" or "until" a whole month or year opens on the 1st after it; "in December" opens December 1.
    for date, quote, opens in (("2027-03", "happy to talk after March 2027", "2027-04-01"),
                               ("2026-12", "busy until December 2026", "2027-01-01"),
                               ("2026", "reach out after 2026", "2027-01-01"),
                               ("2026-12", "let's talk in December 2026", "2026-12-01")):
        fired = fire([ev(ME, "contact_constraint", date, observed="2026-09-01T00:00:00+00:00", quote=quote)])
        assert fired["stated_follow_up"]["window_open"][:10] == opens, quote


# ----------------------------------------------------------------- private

def test_a_private_note_fires_only_when_its_author_typed_it_and_the_newest_counts():
    def note(event_type, day="2026-09-10", observed="2026-09-10T18:00:00+00:00"):
        return ev(ME, event_type, day, observed=observed, tier=0, quote="Told a friend she is looking.")
    fired = fire([note("note_open")])["private_note"]
    assert fired["family"] == "private" and fired["window_open"][:10] == "2026-09-10"
    assert fired["window_close"][:10] == "2026-12-09"
    assert "private_note" not in fire([note("private_note")])  # untyped is context, whatever it says
    # A wait note opens on its day, however far past the one-year lookahead.
    assert fire([note("note_wait", "2028-01-01")])["private_note"]["window_open"][:10] == "2028-01-01"
    # "Looking now" replaces an older "not until 2028", and the other way round.
    later = note("note_open", "2026-09-12", observed="2026-09-12T18:00:00+00:00")
    assert [a["window_open"][:10] for a in fires([note("note_wait", "2028-01-01"), later], "private_note")] == ["2026-09-12"]
    later = note("note_wait", "2028-01-01", observed="2026-09-12T18:00:00+00:00")
    assert [a["window_open"][:10] for a in fires([note("note_open"), later], "private_note")] == ["2028-01-01"]


# ---------------------------------------------------------------- calendar

def test_conference_holds():
    holds = fires([ev(ME, "conference_deadline", "2026-10-01"), ev(ME, "conference_attending", "2026-12-08")],
                  "calendar_quiet_conference")
    assert [h["holds"] for h in holds] == [["conference_deadline"], ["conference_attending"]]
    assert [h["window_open"][:10] for h in holds] == ["2026-09-10", "2026-12-05"]


def test_quarter_close_hold_for_finance_roles_only():
    assert "calendar_quiet_close" not in fire([ev(ME, "job_started", "2024-01-01", quote="Staff Engineer")], as_of="2026-10-05")
    finance = [ev(ME, "officer_appointment", "2024-01-01", quote="appointed Controller", title="Controller")]
    hold = fires(finance, "calendar_quiet_close", as_of="2026-10-05")[0]
    assert hold["holds"] == ["quarter_close"] and open_at(hold, "2026-10-05")
    with_10k = finance + [ev(ORG, "annual_report_filed", "2026-03-15")]
    season = [a for a in fires(with_10k, "calendar_quiet_close", as_of="2027-01-20") if a["holds"] == ["audit_season"]]
    # The audit ends when the 10-K is filed, so the hold lifts the day after, and the filing is its evidence.
    assert season and season[0]["window_close"][:10] == "2027-03-16" and with_10k[-1]["id"] in season[0]["evidence_event_ids"]
    leap = finance + [ev(ORG, "annual_report_filed", "2024-02-29")]  # its anniversary is 28 February in other years
    season = [a for a in fires(leap, "calendar_quiet_close", as_of="2025-01-20") if a["holds"] == ["audit_season"]]
    assert season and (season[0]["window_open"][:10], season[0]["window_close"][:10]) == ("2024-12-15", "2025-03-01")


# ------------------------------------------------------------------- holds

def test_recent_promotion_and_short_tenure_from_crustdata_changes():
    promoted = ev(ME, "profile_change", "2026-08-01", quote='[{"field": "basic_profile.current_title", "from": "A", "to": "B"}]')
    fired = fire([promoted])
    assert fired["hold_recent_promotion"]["holds"] == ["recent_promotion"] and "hold_short_tenure" not in fired
    started = ev(ME, "profile_change", "2026-08", quote='[{"field": "experience.employment_details.current", "type": "added"}]')
    fired = fire([started])
    assert fired["hold_short_tenure"]["window_close"][:10] == "2027-08-01" and "hold_recent_promotion" not in fired
    assert "hold_short_tenure" not in fire([ev(ME, "job_started", "2025-01-01")])


def test_role_claim_holds_carry_the_falsifier():
    announced = ev(ME, "role_announced", observed="2026-09-20T00:00:00+00:00",
                   quote="Incoming Research Scientist at Example Lab")
    fired = fires([announced], "hold_role_claim")
    assert [a["holds"] for a in fired] == [["announced_next_role"]] and open_at(fired[0], "2026-09-22")
    assert fired[0]["window_close"][:10] == "2027-03-19"  # undated: 180 days past sighting
    assert fired[0]["falsifier"].startswith("Confirm whether the announced role was accepted")
    # With a stated start, or on their own page, the person or the new employer said it: a plain hold.
    joining = ev(ME, "role_announced", "2027-01", observed="2026-08-15T00:00:00+00:00", quote="In January 2027 I will join Y")
    own_page = {**ev(ME, "role_announced", observed="2026-09-20T00:00:00+00:00", quote="I am joining Y"), "tier": 1}
    for claim, closes in ((joining, "2028-01-01"), (own_page, "2027-09-20")):
        [held] = fires([claim], "hold_role_claim")
        assert held["holds"] == ["accepted_next_role"] and held["window_close"][:10] == closes and not held["falsifier"]
    # Saying they are looking at the same time or later contradicts even their own announcement: check it.
    still_looking = ev(ME, "self_stated_availability", observed="2026-09-21T00:00:00+00:00", quote="seeking full-time roles")
    [held] = fires([own_page, still_looking], "hold_role_claim")
    assert held["holds"] == ["announced_next_role"] and "It conflicts with 'seeking full-time roles'" in held["falsifier"]
    looking = ev(ME, "self_stated_availability", observed="2026-09-01T00:00:00+00:00", quote="seeking full-time roles")
    started = ev(ME, "job_started", "2026-09-15", quote="Research Scientist, Example Lab")
    assert [a["holds"] for a in fires([looking, started], "hold_role_claim")] == [["conflicting_role_claims"]]
    older = ev(ME, "job_started", "2024-01-15", quote="Engineer, Old Co")
    assert fires([looking, older], "hold_role_claim") == []  # a past role is not a conflicting claim


def page(event):
    """The event as page extraction records it."""
    return {**event, "extractor": "claude_extract_v1"}


def test_own_layoff_or_departure_and_company_closure_are_forced_moves():
    laid_off = page(ev(ME, "layoff", "2026-09-15", quote="I was laid off from Example Lab on September 15, 2026."))
    [left] = fires([laid_off], "own_departure")
    assert (left["window_open"][:10], left["window_close"][:10], left["strength"]) == ("2026-09-15", "2026-12-14", 0.6)
    leaving = page(ev(ME, "job_ended", "2026-10-31", observed="2026-09-20T00:00:00+00:00", quote="My last day is October 31, 2026."))
    assert fires([leaving], "own_departure")[0]["window_open"][:10] == "2026-09-20"  # on the market once they say so
    closing = page(ev(ME, "company_closure", "2026-12-31", observed="2026-09-10T00:00:00+00:00",
                      quote="Example Co is winding down operations by December 31, 2026."))
    assert fires([closing], "company_closure")[0]["window_open"][:10] == "2026-09-10"
    # Departures from filings and page history are not read: in the replay they are the outcome.
    assert fires([ev(ME, "job_ended", "2026-09-15"), ev(ME, "officer_departure", "2026-09-15")], "own_departure") == []


def test_an_ended_role_has_no_short_tenure_or_close_hold_and_new_scope_holds_like_a_promotion():
    hired = ev(ME, "job_started", "2026-03-01", quote="Controller, Example Co")
    laid_off = page(ev(ME, "layoff", "2026-09-15", quote="I was laid off from Example Co."))
    assert "hold_short_tenure" in fire([hired]) and "hold_short_tenure" not in fire([hired, laid_off])
    assert fires([hired], "calendar_quiet_close", as_of="2026-10-05")
    assert fires([hired, laid_off], "calendar_quiet_close", as_of="2026-10-05") == []
    scope = ev(ME, "new_responsibility", "2026-08-01", quote="Now leading the world-models team")
    assert fire([scope])["hold_recent_promotion"]["holds"] == ["recent_promotion"]


def test_extracted_page_events_reach_the_detectors_that_read_them():
    fired = fire([ev(ME, "acquisition", "2026-08-01", quote="Acme was acquired by Globex"),
                  ev(ME, "publication", "2026-09-10", quote="Published 'World models' at TMLR"),
                  ev(ME, "talk_or_conference", "2026-10-01", quote="Talk at the Example Workshop")])
    assert {"acquisition_closed", "paper_v1", "paper_talk"} <= fired.keys()
    assert fires([ev(ME, "acquisition", "2025-10-01")], "retention_cliff", as_of="2026-09-22")  # the 12-month cliff
    [held] = fires([ev(ME, "project_release", "2026-10-05", quote="We release the model on October 5")],
                   "hold_imminent_launch")
    assert held["holds"] == ["imminent_launch"] and open_at(held, "2026-09-22")


def test_equity_refresh_and_launch_holds():
    fired = fire([ev(ME, "equity_refresh", "2026-09-01"), ev(ME, "launch_announced", "2026-10-10")])
    assert fired["hold_equity_refresh"]["holds"] == ["equity_refresh"]
    assert fired["hold_imminent_launch"]["window_open"][:10] == "2026-09-10"
    [grant] = fires([ev(ME, "retention_or_commitment", "2026-08-20")], "hold_equity_refresh")
    assert grant["holds"] == ["retention_grant"] and grant["window_close"][:10] == "2027-06-21"  # 60 days before a 1-year vest
    at_employer = fires([ev(ORG, "launch_announced", "2026-10-01")], "hold_imminent_launch")
    assert [a["holds"] for a in at_employer] == [["imminent_launch"]]
    assert fires([ev("gi", "launch_announced", "2026-10-01")], "hold_imminent_launch") == []  # GI's own launch


def test_not_looking_holds_until_they_say_otherwise():
    optout = ev(ME, "contact_constraint", observed="2026-09-05T00:00:00+00:00", quote="Not looking; no recruiters please.")
    [held] = fires([optout], "hold_not_looking")
    assert held["holds"] == ["not_looking"] and held["window_close"][:10] == "2027-09-05"
    looking = ev(ME, "self_stated_availability", observed="2026-11-01T00:00:00+00:00", quote="Now open to new roles.")
    assert fires([optout, looking], "hold_not_looking", as_of="2026-12-01")[0]["window_close"][:10] == "2026-11-01"
    assert fires([ev(ME, "contact_constraint", "2026-10-04", quote="after 4 October")], "hold_not_looking") == []


def test_a_later_mention_at_the_old_employer_conflicts_with_a_move():
    old = ev(ME, "profile_change", "2020-02-03", quote='[{"field": "experience.employment_details.current", "to": "Controller, Northgate"}]')
    moved = ev(ME, "profile_change", "2026-09-01", quote='[{"field": "experience.employment_details.current", "to": "Controller, Beacon Corp."}]')
    still = ev(ME, "other_professional", "2026-09-15", quote="Northgate reported results, said Controller F, its controller.")
    [held] = fires([old, moved, still], "hold_role_claim")
    assert held["holds"] == ["conflicting_role_claims"] and held["evidence_event_ids"] == [moved["id"], still["id"]]
    assert held["falsifier"].startswith("Confirm where they work now")
    farewell = ev(ME, "other_professional", "2026-09-15", quote="Controller F left Northgate to join Beacon.")
    assert fires([old, moved, farewell], "hold_role_claim") == []  # names the new employer too


# ---------------------------------------------------------- as-of contract

def test_stale_windows_are_not_emitted():
    assert "paper_v1" not in fire([ev(ME, "paper_v1", "2025-01-01")])


def test_view_and_leakage_through_the_store(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 't.sqlite'}")
    paper = timeline.add_event(store, ev(ME, "paper_accepted", "2026-06-01"))
    late = timeline.add_event(store, ev(ORG, "warn_notice", "2026-06-15", observed="2026-08-30T00:00:00+00:00"))
    early = detectors.view(store, ME, "2026-07-01T00:00:00+00:00", related=[ORG])
    assert [e["id"] for e in early] == [paper["id"]]
    assert {a["detector_id"] for a in detectors.detect(early, ME, "2026-07-01T00:00:00+00:00")} == {"paper_accepted"}
    later = detectors.view(store, ME, "2026-09-01T00:00:00+00:00", related=[ORG])
    assert {e["id"] for e in later} == {paper["id"], late["id"]}

    @timeline.detector("cheat", "test")
    def cheat(events, subject_id, as_of):
        return [{"strength": 1, "evidence_event_ids": [late["id"]]}]

    try:
        with pytest.raises(timeline.LeakageError):
            detectors.detect(early, ME, "2026-07-01T00:00:00+00:00")
    finally:
        timeline.DETECTORS.pop("cheat")


def post(event):
    """The event as the post reader records it, from the person's own post."""
    return {**event, "extractor": "claude_extract_v1:posts:2026-09-23.5:abc123def456:a1b2c3:small"}


def call(events, as_of="2026-09-22"):
    from app import readiness  # readiness imports detectors

    return readiness.score(detectors.detect(events, ME, f"{as_of}T12:00:00+00:00"), f"{as_of}T12:00:00+00:00", events)


def test_a_layoff_or_last_day_in_their_own_post_is_a_forced_move_and_ends_the_first_year_hold():
    hired = ev(ME, "job_started", "2026-03-02", quote="Research Engineer, Example Robotics")
    laid_off = post(ev(ME, "layoff", "2026-09-12", observed="2026-09-14T00:00:00+00:00",
                       quote="Friday was my last day at Example Robotics; the team was let go."))
    [left] = fires([laid_off], "own_departure")
    assert (left["window_open"][:10], left["window_close"][:10]) == ("2026-09-12", "2026-12-11")  # 90 days on
    reach = call([hired, laid_off])
    assert (reach.action, reach.track, reach.holds) == ("reach_now", "pitch", [])
    assert call([hired]).holds == ["short_tenure"]  # without the post, the first year holds
    # Another source's departure (page history, a stranger's post read as x_read) is still not their own word.
    assert fires([{**laid_off, "extractor": "x_read"}], "own_departure") == []
    assert fires([{**laid_off, "extractor": "claude_extract_v1:postscript"}], "own_departure") == []


def test_their_own_newer_im_open_beats_the_first_year_and_launch_week_holds():
    hired = ev(ME, "job_started", "2026-03-02", quote="Research Scientist, Example Games")
    said = post(ev(ME, "self_stated_availability", None, observed="2026-09-15T00:00:00+00:00",
                   quote="Six months in and I'm open to new roles in world models."))
    assert (call([hired, said]).action, call([hired, said]).holds) == ("reach_now", [])
    earlier = post(ev(ME, "self_stated_availability", "2026-06-01", observed="2025-06-01T00:00:00+00:00",
                      quote="Available for full-time roles from June 2026."))  # open still, but the new job is newer
    assert (call([hired, earlier], as_of="2026-03-20").action, call([hired, earlier], as_of="2026-03-20").holds) == \
        ("watch_until", ["short_tenure"])
    assert call([hired, said], as_of="2026-12-01").holds == ["short_tenure"]  # its 60 days are over: the hold is back
    assert call([hired, said], as_of="2026-11-13").holds == [] and call([hired, said], as_of="2026-11-14").holds
    page_line = {**said, "extractor": "claude_extract_v1"}  # a page's line is dated by when it was fetched, not said
    assert call([hired, page_line]).holds == ["short_tenure"]
    far = post(ev(ME, "self_stated_availability", "2027-06-01", observed="2026-09-15T00:00:00+00:00",
                  quote="Open to new roles from June 2027."))
    assert call([hired, far], as_of="2026-12-01").holds == ["short_tenure"]  # recent means said in the last 60 days
    launch = ev(ME, "launch_announced", "2026-09-18", quote="Launching Example Engine today")
    assert fire([launch])["hold_imminent_launch"]["holds"] == ["imminent_launch"]
    assert "hold_imminent_launch" not in fire([launch, said])  # said 3 days before it: open beats launch week
    old = {**said, "observed_at": "2026-08-10T00:00:00+00:00"}  # said 39 days before it, and still open
    assert "hold_imminent_launch" in fire([launch, old])


def test_im_open_never_beats_an_opt_out_a_note_a_named_next_role_or_the_contact_gate():
    from app import contact

    hired = ev(ME, "job_started", "2026-03-02", quote="Research Scientist, Example Games")
    said = post(ev(ME, "self_stated_availability", None, observed="2026-09-15T00:00:00+00:00",
                   quote="I'm open to new roles in world models."))
    no = post(ev(ME, "contact_constraint", None, observed="2026-09-18T00:00:00+00:00",
                 quote="Not looking right now, please no recruiters."))
    assert call([hired, said, no]).action == "quiet"  # a later opt-out wins
    note = {**ev(ME, "note_open", "2026-09-15", quote="Told me she'd consider a move", tier=0), "extractor": "private_note"}
    assert call([hired, note]).action == "watch_until"  # a GI person's note is not their own word
    joining = ev(ME, "role_announced", "2027-01-10", observed="2026-09-01T00:00:00+00:00",
                 quote="Incoming Research Scientist at Example Lab")
    assert call([joining, said]).action == "verify_first"  # a named next role and "I'm open": a fact to check
    reach = call([hired, said])
    sent = contact.Entry(person_id="P", kind="sent", at="2026-08-20T12:00:00+00:00").model_dump()
    gate = contact.check([sent], "2026-09-22T12:00:00+00:00", reach)
    assert gate.state == "hold" and gate.reason.startswith("No reply since")  # the contact rails still hold
