"""The early-sign replay: 8 weeks before each public moment against the same people's other 8-week stretches."""

import asyncio
from datetime import date

from app import extraction, journey, providers, readiness
from app import scorecard as sc
from app.detectors import REPLAY_TYPES
from app.sources import apify, social
from app.timeline import add_event

PAPER = "https://arxiv.org/abs/2604.00001"


def person(subject, moments=(), since="2025-06-01", until="2026-08-01"):
    return sc.Person(subject_id=subject, role="mts-research", since=since, until=until,
                           moments=[sc.Moment(date=d, kind="paper", source_url=u) for d, u in moments])


def add(store, subject, kind, day, url, observed=None, quote="x"):
    add_event(store, {"subject_type": "person", "subject_id": subject, "event_type": kind, "event_date": day,
                      "date_precision": "day", "observed_at": observed or f"{day}T12:00:00+00:00", "source_url": url,
                      "source_version_hash": url, "quote": quote, "tier": 1, "extractor": "test"})


def run(view, subject, as_of):
    return journey.detect(view, subject, as_of)


def test_windows_are_the_8_weeks_before_each_moment_and_the_other_stretches_nothing_followed():
    a = person("A", [("2026-06-01", PAPER)])
    assert [(s.isoformat(), e.isoformat(), bool(m)) for s, e, m in sc.stretches(a)] == [
        ("2026-04-06", "2026-06-01", True),
        # After the 150-day warm-up; 18 February leads up to the paper, and 15 April's next 8 weeks pass the pull.
        ("2025-10-29", "2025-12-24", False), ("2025-12-24", "2026-02-18", False),
    ]
    assert len(sc.stretches(person("B"))) == 3
    too_early = person("C", [("2025-12-01", PAPER)])  # its 8 weeks start inside the warm-up
    assert not any(m for _, _, m in sc.stretches(too_early))
    # A release three weeks after a paper: the person was already in the news, so only the paper's window counts.
    two = person("D", [("2026-05-01", PAPER), ("2026-05-22", "https://github.com/d/release")])
    assert [(s.isoformat(), m.date) for s, _, m in sc.stretches(two) if m] == [("2026-03-06", "2026-05-01")]


def test_the_replay_looks_weekly_and_never_in_the_last_week_before_the_moment():
    days = sc.weekly(date(2026, 4, 6), date(2026, 6, 1))
    assert (len(days), days[0], days[-1]) == (7, "2026-04-12T23:59:59+00:00", "2026-05-24T23:59:59+00:00")


def test_a_moments_own_source_is_never_in_view_even_when_mis_stamped(store):
    add(store, "A", "publication", "2026-06-01", PAPER, observed="2026-03-01T00:00:00+00:00")
    add(store, "A", "x_post", "2026-03-02", "https://x.com/a/1")
    read = sc.ReadOnce(store)
    assert {r["source_url"] for r in read.hiding({PAPER}).all("timeline_event")} == {"https://x.com/a/1"}
    assert len(read.all("timeline_event")) == 2


def test_noise_test_and_scorecard_on_invented_people(store):
    # A shows work in progress three weeks before their paper; B shows it once when nothing followed;
    # D says they are open to work three weeks before job news: a reach, but on news the crowd sees too.
    add(store, "A", "work_in_progress", "2026-05-10", "https://x.com/a/2")
    add(store, "B", "work_in_progress", "2026-01-05", "https://x.com/b/1")
    add(store, "D", "self_stated_availability", "2026-05-10", "https://x.com/d/1")
    add(store, "A", "publication", "2026-06-01", PAPER, observed="2026-03-01T00:00:00+00:00")  # hidden, see above
    people = [person("A", [("2026-06-01", PAPER)]), person("B"), person("D", [("2026-06-01", "https://d.example/job")])]
    observations = sc.replay_moments(store, people, readiness.score, run=run)
    assert len(observations) == (3 + 3 + 3) * 7
    assert not any(a["detector_id"] == "paper_v1" for o in observations for a in o["activations"])

    sign = sc.noise(observations)["signs"]["work_in_progress"]
    assert (sign["before_moment"], sign["before_moment_windows"], sign["ordinary"], sign["ordinary_windows"]) == (1, 2, 1, 7)
    assert sign["lift"] == 3.5 and round(sign["p"], 3) == 0.417 and not sign["kept"]  # one case is not evidence

    card = sc.scorecard(observations)
    assert (card["engine"]["seen_coming"], card["engine"]["lead_weeks"]["values"]) == (1, [3.1])
    assert (card["engine"]["fired_with_nothing_after"], card["engine"]["ordinary_stretches"]) == (1, 7)
    assert card["any_reach"]["seen_coming"] == 2  # D's open-to-work reach is counted there, never as seen coming
    assert 0 <= card["random_timing"]["seen_rate"] <= 1 and card["obvious_news"]["lead_weeks"] == 0

    without = sc.scorecard(sc.rescored(observations, readiness.score, {"work_in_progress"}))
    assert (without["engine"]["seen_coming"], without["engine"]["fired_with_nothing_after"]) == (0, 0)


def test_random_timing_deals_the_engines_own_weeks_to_random_windows():
    def window(subject, moment, hits):
        return [{"subject_id": subject, "window": ["2026-04-06", "2026-06-01"], "moment": moment,
                 "as_of": f"2026-04-{12 + 7 * k:02d}T23:59:59+00:00" if k < 3 else f"2026-05-{7 * k - 18:02d}T23:59:59+00:00",
                 "action": "reach_now" if hit else "quiet", "early": hit,
                 "readiness": {"reasons": ["technical_ask"] if hit else []}} for k, hit in enumerate(hits)]
    reached = [True, True, False, False, False, False, False]
    observations = window("A", {"date": "2026-06-01"}, reached) + [
        o for s in "BCD" for o in window(s, None, [False] * 7)]
    card = sc.scorecard(observations, draws=400)
    assert card["engine"]["seen_coming"] == 1 and card["engine"]["false_alarm_rate"] == 0
    assert abs(card["random_timing"]["seen_rate"] - 0.25) < 0.08  # one run of reaches in four windows


def test_a_reach_the_news_makes_by_itself_is_not_early_whatever_signs_come_with_it(store):
    # E posts "open to work" and work in progress the same week: the crowd saw the first, so it is not seen coming.
    add(store, "E", "self_stated_availability", "2026-05-10", "https://x.com/e/1")
    add(store, "E", "work_in_progress", "2026-05-10", "https://x.com/e/2")
    add(store, "F", "work_in_progress", "2026-05-10", "https://x.com/f/1")
    people = [person(s, [("2026-06-01", f"https://{s}.example/job")]) for s in "EF"]
    card = sc.scorecard(sc.replay_moments(store, people, readiness.score, run=run))
    assert (card["engine"]["seen_coming"], card["any_reach"]["seen_coming"]) == (1, 2)


def test_two_moments_on_one_day_are_one_window():
    two = person("G", [("2026-06-01", PAPER), ("2026-06-01", "https://github.com/g/release")])
    assert [m.source_url for _, _, m in sc.stretches(two) if m] == [PAPER]


def test_the_replay_reads_posts_and_stars_but_not_repos_pushes_or_what_was_read_from_them(store):
    repo = "https://github.com/a/wm"
    add(store, "A", "github_repo", "2026-05-10", repo, quote="Created a/wm: code for our paper")
    add(store, "A", "work_in_progress", "2026-05-10", repo, quote="Created a/wm: code for our paper")
    add(store, "A", "github_star", "2026-05-11", "https://github.com/b/diamond", quote="Starred b/diamond")
    seen = []
    sc.replay_moments(store, [person("A", [("2026-06-01", PAPER)])], readiness.score,
                      run=lambda view, subject, as_of: seen.append({e["event_type"] for e in view.all("timeline_event")}) or [])
    assert seen[-1] == {"github_star"}


def tweets(handle, days):
    return [{"id": f"{handle}{n}", "url": f"https://x.com/{handle}/status/{n}", "author": {"userName": handle},
             "fullText": "x", "createdAt": f"{day}T12:00:00Z"} for n, day in enumerate(days)]


def test_a_pull_is_complete_from_since_unless_a_cap_may_have_cut_it_or_a_run_failed(store):
    many = social.Person("A", x_handle="ann", since="2025-06-01", until="2026-06-01")
    few = social.Person("B", x_handle="bo", linkedin_url="https://www.linkedin.com/in/bo", since="2025-06-01",
                        until="2026-06-01")
    raw = {apify.X_SEARCH: tweets("ann", [f"2026-{m:02d}-{d:02d}" for m in (3, 4, 5) for d in range(1, 21)])
           + tweets("bo", ["2025-12-01"]),
           "failed": [{"actor": apify.LINKEDIN_POSTS, "input": {"targetUrls": [few.linkedin_url]}, "error": "x"}],
           "asked": {"A": ["2025-06-01", "2026-06-01"], "B": ["2025-09-23", "2026-06-01"]}}  # the pull's own windows
    other = social.Person("C", x_handle="cy", since="2025-06-01", until="2026-06-01")  # in another role's pull
    found = sc.pull_coverage([many, few, other], raw, per_person=400)
    assert found == {"A": {"x": ["2026-03-01", "2026-06-01"]},  # B's pull asked from later than the people file says
                     "B": {"x": ["2025-09-23", "2026-06-01"], "linkedin_posts": [None, "2026-06-01"]}}
    # A pull that did not record its windows says nothing sure about anyone, unless they are given.
    unrecorded = {k: v for k, v in raw.items() if k != "asked"}
    assert sc.pull_coverage([few], unrecorded, per_person=400) == {}
    assert sc.pull_coverage([few], unrecorded, per_person=400, asked=raw["asked"]) == {"B": found["B"]}
    # The pull's own cap wins over the one ingest is given: B's one post under a cap of 1 may have hit it.
    assert sc.pull_coverage([few], {**raw, "per_person": 1}, per_person=400)["B"]["x"][0] == "2025-12-01"
    sc.save_coverage(store, found)
    sc.save_coverage(store, {"A": {"x": ["2026-09-20", ""]}})  # a later short pull does not undo the full one
    sc.save_coverage(store, sc.pull_coverage([many, few, other], {}))  # nor does a pull that never asked about them
    records = sc.coverage(store)
    until = lambda s: person(s, until="2026-06-01")  # noqa: E731
    assert [sc.complete(until(s), records.get(s)) for s in "ABC"] == [("2026-03-01", True), (None, True), ("", True)]
    # A pull that stopped short of the person's until says nothing about the months after it.
    sc.save_coverage(store, {"A": {"x": ["2025-06-01", "2025-09-01"]}})
    assert sc.complete(until("A"), sc.coverage(store)["A"])[0] == "2026-03-01"
    # A record older code saved (a bare day, or the people file's window) reads as unrecorded until ingest replaces it.
    for old in ("2025-06-01", [["2024-10-16", "2026-06-01"]]):
        store.put("pull-coverage:D", "pull_coverage", {"person_id": "D", "platforms": {"x": old}})
        assert "D" not in sc.coverage(store)
        sc.save_coverage(store, {"D": {"x": ["2026-01-01", "2026-06-01"]}})
        assert sc.coverage(store)["D"] == {"x": [["2026-01-01", "2026-06-01"]]}
    # A's first 150 days after March are its warm-up: no window is complete; B's LinkedIn failed: none at all.
    assert sc.stretches(person("A", [("2026-06-01", PAPER)]), complete_from="2026-03-01") == []
    add(store, "B", "work_in_progress", "2026-05-10", "https://x.com/bo/status/9")
    assert sc.replay_moments(store, [person("B", [("2026-06-01", PAPER)])], readiness.score, run=run) == []


def test_scored_people_have_a_moment_are_not_set_aside_and_post_once_a_month(store):
    for n in range(12):
        add(store, "A", "x_post", f"2026-{1 + n % 5:02d}-{1 + n:02d}", f"https://x.com/a/{n}", quote=f"post {n}")
    add(store, "Q", "x_post", "2026-02-01", "https://x.com/q/1")
    add(store, "Q", "x_post", "2026-08-02", "https://x.com/q/late")  # after the pull ends: not counted
    people = [person("A", [("2026-06-01", PAPER)]), person("Q", [("2026-06-01", PAPER)]), person("W"),
              sc.Person(subject_id="J", role="mts-research", since="2025-06-01", until="2026-06-01",
                        moments=[sc.Moment(date="2026-06-01", kind="job", source_url="https://gi.example")],
                        unscored="joined months before the announcement")]
    sc.save_coverage(store, {s: {"x": "2025-06-01"} for s in "AQJ"})
    scored, skipped = sc.scoreable(store, people + [person("N", [("2026-06-01", PAPER)])])
    assert skipped.pop("N").startswith("no pull coverage record")
    assert [p.subject_id for p in scored] == ["A"]
    assert skipped == {"Q": "too quiet: 1 posts, comments and replies in the pull", "W": "watchlist",
                       "J": "unscored: joined months before the announcement"}


def test_stars_that_ran_out_leave_the_replay_and_posts_keep_their_windows(store):
    add(store, "A", "github_star", "2026-05-11", "https://github.com/b/diamond", quote="Starred b/diamond")
    add(store, "A", "x_post", "2026-05-12", "https://x.com/a/1")
    sc.save_coverage(store, {"A": {"x": "2025-06-01", "github_stars": "2026-01-01"}})
    assert sc.complete(person("A"), sc.coverage(store)["A"]) == ("2025-06-01", False)
    seen = []
    sc.replay_moments(store, [person("A", [("2026-06-01", PAPER)])], readiness.score,
                      run=lambda view, subject, as_of: seen.append({e["event_type"] for e in view.all("timeline_event")}) or [])
    assert len(seen) == 21 and seen[-1] == {"x_post"}


def test_github_events_of_a_retired_source_are_never_read_or_replayed(store):
    star = {"subject_type": "person", "subject_id": "A", "event_type": "github_star", "event_date": "2026-05-11",
            "date_precision": "day", "observed_at": "2026-05-11T12:00:00+00:00", "source_url": "https://github.com/b/wm",
            "source_version_hash": "s1", "quote": "Starred b/wm: code for our paper", "tier": 1}
    add_event(store, {**star, "extractor": "github_v1"})  # today's description in the quote
    add_event(store, {**star, "event_type": "gi_topic", "extractor": "claude_extract_v1:posts:x"})
    seen = []
    sc.replay_moments(store, [person("A", [("2026-06-01", PAPER)])], readiness.score,
                      run=lambda view, subject, as_of: seen.append({e["event_type"] for e in view.all("timeline_event")}) or [])
    assert seen[-1] == set()
    assert extraction.plan(store, "A", REPLAY_TYPES)[0] == []  # nor read


def test_a_star_a_retired_source_quotes_the_same_way_is_read_and_replayed_once(model, store, settings):
    star = {"subject_type": "person", "subject_id": "A", "event_type": "github_star", "event_date": "2026-05-11",
            "date_precision": "day", "observed_at": "2026-05-11T12:00:00+00:00", "source_url": "https://github.com/b/wm",
            "source_version_hash": "s1", "quote": "Starred b/wm", "tier": 1}
    for source in ("github_v1", "github_v2"):  # a repo with no description: one item under both
        add_event(store, {**star, "extractor": source})
    model.reply({"events": [{"post": 1, "event_type": "gi_topic", "event_date": None, "about_subject": True,
                             "quote": "Starred b/wm"}]})
    subject, as_of = {"type": "person", "id": "A", "name": "A"}, "2026-09-15T00:00:00+00:00"
    asyncio.run(extraction.read_posts(store, subject, role="mts-research", settings=settings, types=REPLAY_TYPES,
                                      budget=providers.Budget({"max_calls_per_run": 1}), as_of=as_of))
    live_only = sc.unreplayed(store.all("timeline_event"))
    todo, expected = extraction.audit(store, subject, role="mts-research", settings=settings, types=REPLAY_TYPES,
                                      as_of=as_of)
    held = {e["id"] for e in extraction.post_readings(store, "A") if extraction.item(e) not in live_only}
    assert todo == [] and len(expected) == 1 and held == expected  # what posts.py readings checks
    seen = []
    sc.replay_moments(store, [person("A", [("2026-06-01", PAPER)])], readiness.score,
                      run=lambda view, subject, as_of: seen.append(sorted(e["event_type"] for e in view.all("timeline_event"))) or [])
    assert seen[-1] == ["gi_topic", "github_star"]


def test_a_replay_never_sees_a_post_answering_gis_own_account_nor_what_was_read_from_it(store):
    said = "Congrats on the round, the demo clips look great."
    add(store, "A", "x_post", "2026-05-20", "https://x.com/a/1", quote=f"{said}\n\nIn reply to @gen_intuition: Our round")
    add(store, "A", "technical_ask", "2026-05-20", "https://x.com/a/1", quote=said)  # the reader's tag on it
    add(store, "A", "x_reply", "2026-05-21", "https://x.com/a/2", quote="Agreed\n\nIn reply to @gen_intuition")
    add(store, "A", "linkedin_post", "2026-05-22", "https://linkedin.com/posts/a-3", quote="Proud\n\nIn reply to "
        "General Intuition: We are hiring")
    add(store, "A", "x_post", "2026-05-22", "https://x.com/a/6", quote="In reply to @gen_intuition: Our round")  # no words
    add(store, "A", "x_reply", "2026-05-23", "https://x.com/a/4", quote="Nice\n\nIn reply to @gen_intuition_fan")
    add(store, "A", "x_post", "2026-05-24", "https://x.com/a/5", quote="Reading @gen_intuition's paper today")
    seen = []
    sc.replay_moments(store, [person("A", [("2026-06-01", PAPER)])], readiness.score,
                      run=lambda view, subject, as_of: seen.append({e["source_url"] for e in view.all("timeline_event")}) or [])
    assert seen[-1] == {"https://x.com/a/4", "https://x.com/a/5"}  # someone else's account, or their own words
    assert sc.unreplayed(store.all("timeline_event")) == set()  # posts.py still reads and checks them


def test_scored_people_have_windows_on_both_sides(store):
    for n in range(12):
        add(store, "A", "x_post", f"2026-0{1 + n % 5}-{1 + n:02d}", f"https://x.com/a/{n}", quote=f"post {n}")
    sc.save_coverage(store, {"A": {"x": "2025-10-01"}})  # capped: only the 8 weeks before the moment are complete
    scored, skipped = sc.scoreable(store, [person("A", [("2026-06-01", PAPER)])])
    assert not scored and skipped["A"].startswith("one-sided: 1 window(s) before a moment, 0 ordinary")


def test_halves_are_seeded_disjoint_and_cover_everyone():
    people = [person(f"P{i}") for i in range(9)]
    one, other = sc.split(people, seed=0)
    assert (len(one), len(other), one & other, one | other) == (4, 5, set(), {p.subject_id for p in people})
    assert sc.split(people, seed=0) == (one, other) and sc.split(people, seed=1) != (one, other)


def test_fisher_matches_the_hypergeometric_tail():
    assert round(sc.fisher_greater(5, 5, 1, 19), 5) == 0.00884
    assert sc.fisher_greater(0, 4, 0, 10) == 1.0


def test_a_people_file_that_writes_a_missing_account_as_null_loads_everywhere(monkeypatch, tmp_path):
    import json
    import os
    import subprocess
    import sys

    from app import today
    from app.store import Store
    from scripts import posts

    row = {"person_id": "mts-research:ada", "role": "mts-research", "since": "2026-01-01", "until": None,
           "name": "Ada Example", "x_handle": None, "linkedin_url": None, "github": None, "unscored": None,
           "moments": []}
    path = tmp_path / "people.json"
    path.write_text(json.dumps({"people": [row, {**row, "person_id": "mts-research:bo", "name": None,
                                                 "x_handle": "@bo_example", "github": "bo-example"}]}))
    ada, bo = sc.load_moments(path)
    assert (ada.x_handle, ada.linkedin_url, ada.github, ada.unscored, ada.anchors()) == ("", "", "", "", {})
    assert bo.anchors() == {"x": "https://x.com/bo_example", "github": "https://github.com/bo-example"}
    db = tmp_path / "timelines.sqlite"
    posts.save_people(Store(f"sqlite:///{db}"), [ada, bo])  # what read and happened save first
    monkeypatch.setattr(today, "TIMELINES", db)  # Today's calls on the live store
    monkeypatch.setattr(today, "TEAM", tmp_path / "no-team.json")
    monkeypatch.setattr(today, "CONTACTS", tmp_path / "no-contacts.json")
    assert {c["name"] for c in today.calls("live")["calls"]} == {"Ada Example", "mts-research:bo"}
    run = subprocess.run([sys.executable, "scripts/route.py", "--people", str(path), "--out", str(tmp_path / "out"),
                          "--team", str(tmp_path / "no-team.json"), "--contacts", str(tmp_path / "no-contacts.json"),
                          "--profiles", str(tmp_path / "no-profiles.json")],
                         env={**os.environ, "TIMELINES_DB": str(db)}, capture_output=True, text=True)
    assert run.returncode == 0 and "0 of 2 people are reach_now" in run.stdout, run.stderr
