"""The daily run on invented people: one combined X search, the month's cap, the log line, the pause file, and the
morning list posted only by a real run given --post (the launchd job's) and not paused."""

import importlib.util
import itertools
import json
import math
import os
import plistlib
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import call_log, contact, journey
from app.sources import apify, warn
from app.sources.social import Person
from app.store import Store

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 20, 14, 30, tzinfo=timezone.utc)  # tweets are refused if later than the real clock
PEOPLE = [
    {"person_id": "mts-research:ada-example", "name": "Ada Example", "role": "mts-research", "since": "2026-01-01",
     "x_handle": "ada_example", "linkedin_url": "https://www.linkedin.com/in/ada-example", "github": "ada-example"},
    {"person_id": "backend:bo-example", "name": "Bo Example", "role": "backend", "since": "2026-01-01",
     "x_handle": "bo_example"},
    {"person_id": "backend:cy-never", "name": "Cy Never", "role": "backend", "since": "2026-01-01",
     "x_handle": "cy_never"},
    {"person_id": "backend:eve-until", "name": "Eve Until", "role": "backend", "since": "2026-01-01",
     "until": "2026-09-01", "x_handle": "eve_until"},  # a past window with no moment: still not live
    {"person_id": "mts-research:dee-past", "name": "Dee Past", "role": "mts-research", "since": "2025-01-01",
     "until": "2025-09-01", "x_handle": "dee_past",
     "moments": [{"date": "2025-08-01", "kind": "paper", "source_url": "https://example.org/p", "what": "a paper"}]},
]


def tweet(handle, n, day="2026-09-19"):
    return {"id": f"{handle}-{n}", "url": f"https://x.com/{handle}/status/{n}", "createdAt": f"{day}T0{n}:00:00Z",
            "fullText": f"invented tweet {n} by {handle}", "author": {"userName": handle}}


LINKEDIN_POST = {"author": {"publicIdentifier": "ada-example", "linkedinUrl": "https://www.linkedin.com/in/ada-example"},
                 "postedAt": {"date": "2026-09-19T09:00:00Z"}, "content": "An invented post about world models.",
                 "linkedinUrl": "https://www.linkedin.com/posts/ada-example-1"}


class FakeApify:
    def __init__(self, items):
        self.items, self.runs, self.charged = items, [], []

    async def run(self, actor, actor_input, *, max_items, max_usd, keep=True):
        self.runs.append((actor, actor_input, max_items, max_usd))
        found = self.items.get(actor, [])
        failed = isinstance(found, Exception)
        self.charged.append({"actor": actor, "run": f"r{len(self.runs)}", "status": "FAILED" if failed else "SUCCEEDED",
                             "usd": 0.001, "events": {} if failed else {"result": len(found)}})
        if failed:
            raise found
        return found


@pytest.fixture(autouse=True)
def utc_clock():
    """The Monday rule reads the machine's own clock, so every test here runs on UTC, wherever it runs."""
    was = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if was is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = was
    time.tzset()


@pytest.fixture
def daily(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("daily_script", ROOT / "scripts" / "daily.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.fake = FakeApify({apify.X_SEARCH: [tweet("ada_example", 1), tweet("bo_example", 2)],
                             apify.LINKEDIN_POSTS: [LINKEDIN_POST]})
    module.routed, module.reads = [], []
    monkeypatch.setattr(module.social_pull, "PRIVATE", tmp_path)
    monkeypatch.setattr(module.social_pull, "key", lambda name: "token")
    monkeypatch.setattr(module.apify, "Client", lambda token: module.fake)
    monkeypatch.setattr(module.github, "fetch", lambda login, per_person, token: {"repos": [], "starred": [], "events": []})

    def read(store, jobs, max_calls, model, since):
        module.reads.append(([s["id"] for s, _, _ in jobs], max_calls, model, since))
        return SimpleNamespace(used=max_calls, tokens={"input_tokens": 4000 * max_calls, "cache_creation_input_tokens": 1000 * max_calls,
                                                              "cache_read_input_tokens": 3000 * max_calls, "output_tokens": 300 * max_calls})
    monkeypatch.setattr(module.posts, "read", read)

    module.posted = "Posted the morning list with 1 card in its thread.\n"  # what route.py --send says

    def route(args, **kwargs):
        if args[0] == "git":  # the commit the call log names
            return subprocess.CompletedProcess(args, 0, stdout="0123456789ab\n", stderr="")
        module.routed.append((args, kwargs["env"]["TIMELINES_DB"]))
        return subprocess.CompletedProcess(args, 0, stdout=(module.posted if "--send" in args else "") +
                                           "Kept quiet: ...\n1 of 2 people are reach_now as of 2026-09-25; "
                                           "1 carded, 0 kept quiet; 0 more had a public moment in the last month\n", stderr="")
    monkeypatch.setattr(module.subprocess, "run", route)
    (tmp_path / "people.json").write_text(json.dumps(PEOPLE))
    contact.record(store_of(tmp_path), "backend:cy-never", "never")  # asked to be left alone, so not even read
    monkeypatch.setitem(module.starts.FILES, "live", tmp_path / "starts.json")  # no New starts unless a test writes it
    monkeypatch.setattr(contact, "PAUSE", tmp_path / "sending-paused.json")  # sending is on unless a test pauses it
    monkeypatch.setattr(module, "slack_target", lambda: "as the Slack app, each card in the list's thread")
    return module


def store_of(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'timelines.sqlite'}")


def run(daily, tmp_path, **kwargs):  # as the launchd job runs it: --post
    return daily.run(tmp_path / "people.json", tmp_path, f"sqlite:///{tmp_path / 'timelines.sqlite'}",
                     now=kwargs.pop("now", NOW), post=kwargs.pop("post", True), **kwargs)


def lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "daily-runs.jsonl").read_text().splitlines()]


def test_one_combined_x_search_covers_every_handle_and_splits_only_when_too_long():
    people = [Person(f"p{i}", x_handle=f"handle_{i:02d}") for i in range(3)]
    [one] = apify.x_together_inputs(people, "2026-09-24", 150)
    assert one == {"searchTerms": ["(from:handle_00 OR from:handle_01 OR from:handle_02) since:2026-09-24"],
                   "sort": "Latest", "maxItems": 150}
    many = apify.x_together_inputs([Person(f"p{i}", x_handle=f"long_handle_{i:03d}") for i in range(40)], "2026-09-24", 150)
    assert len(many) > 1 and all(len(s["searchTerms"][0]) <= apify.X_QUERY_CHARS for s in many)
    assert sorted(h for s in many for h in s["searchTerms"][0][1:].split(")")[0].replace("from:", "").split(" OR ")) == \
        [f"long_handle_{i:03d}" for i in range(40)]
    runs = apify.together_runs(people + [Person("li", linkedin_url="https://www.linkedin.com/in/ada-example")],
                               "2026-09-24", 150, 5)
    assert [(a, cap, round(worst, 4)) for a, _, cap, worst in runs] == [(apify.X_SEARCH, 150, 0.06),
                                                                         (apify.LINKEDIN_POSTS, 5, 0.01)]
    assert runs[1][1]["postedLimitDate"] == "2026-09-24"
    # Each tweet in a combined search lands on its author's timeline, and only theirs.
    events, rejected = apify.x_events([replace_since(p) for p in people],
                                      [tweet("handle_01", 1), tweet("someone_else", 2)])
    assert [e["subject_id"] for e in events] == ["p1"] and rejected["not_asked_author"] == 1


def replace_since(person):
    return Person(person.person_id, x_handle=person.x_handle, since="2026-09-17")


def test_a_run_pulls_reads_routes_and_logs_one_line_with_its_cost(daily, tmp_path):
    line = run(daily, tmp_path)
    [(actor_x, x_input, x_cap, _), (actor_li, li_input, _, _)] = sorted(daily.fake.runs, key=lambda r: r[0] != apify.X_SEARCH)
    # The watchlist only: the past-moment person, the one with an end date and the one who said never are neither
    # searched nor read.
    assert actor_x == apify.X_SEARCH and x_input["searchTerms"] == ["(from:ada_example OR from:bo_example) since:2026-09-17"]
    assert x_cap == daily.X_CAP and actor_li == apify.LINKEDIN_POSTS
    assert li_input["targetUrls"] == ["https://www.linkedin.com/in/ada-example"]
    rows = [(e["subject_id"], e["event_type"]) for e in store_of(tmp_path).all("timeline_event")]
    assert sorted(rows) == [("backend:bo-example", "x_post"), ("mts-research:ada-example", "linkedin_post"),
                            ("mts-research:ada-example", "x_post")]
    assert store_of(tmp_path).all("pull_coverage") == []  # a shared cap says nothing about one person's coverage
    [(read_ids, calls, model, since)] = daily.reads
    assert sorted(read_ids) == ["backend:bo-example", "mts-research:ada-example"] and calls == 2
    assert sorted(r["subject_id"] for r in store_of(tmp_path).all("person_context")) == sorted(read_ids)
    assert since == "2026-09-13T14:30:00+00:00"
    [(args, db)] = daily.routed
    assert args[-6:] == ["--people", str(tmp_path / "people.json"), "--redraft", "--max-usd", "1.00", "--send"]
    assert db.endswith("timelines.sqlite")
    assert not {"--draft", "--inbox", "--demo", "--as-of", "--top"} & set(args)  # never the demo, a brief or --draft
    assert line["slack"] == "Posted the morning list with 1 card in its thread."
    assert (line["draft_calls"], line["draft_usd"]) == (0, 0.0)  # every free draft passed: no model draft
    # X at its 50-tweet minimum, one LinkedIn post, a start-fee allowance per run, and the reading's tokens.
    pull = 50 * 0.0004 + 1 * 0.002 + 2 * daily.RUN_FEE_USD
    reading = ((8000 + 1.25 * 2000 + 0.1 * 6000) * 2.0 + 600 * 10.0) / 1e6  # cache writes and reads priced
    assert line == lines(tmp_path)[0]
    assert line["status"] == "ok" and line["usd"] == pytest.approx(pull + reading, abs=1e-4)
    assert line["month_usd"] == line["usd"] and line["pulled"] == "2026-09-20" and line["since"] == "2026-09-17"
    assert line["new_events"] == 3 and line["read_calls"] == 2 and line["route"].startswith("1 of 2 people")
    assert line["read_tokens"] == {"input_tokens": 8000, "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 6000,
                                   "output_tokens": 600}  # the reading's usage, cache use included
    # What Apify's own run records say each run cost goes beside the estimate, to check the prices against.
    assert sorted((c["actor"], c["events"]["result"]) for c in line["apify_reported"]) == \
        [(apify.X_SEARCH, 2), (apify.LINKEDIN_POSTS, 1)]
    # The next run starts from the day this one pulled, and the month adds up.
    second = run(daily, tmp_path, now=datetime(2026, 9, 21, 14, 30, tzinfo=timezone.utc))
    assert [i["searchTerms"][0][-16:] for a, i, *_ in daily.fake.runs[2:] if a == apify.X_SEARCH] == ["since:2026-09-20"]
    assert second["month_usd"] == pytest.approx(line["usd"] + second["usd"], abs=1e-4)


def test_the_scheduled_run_logs_each_watched_persons_first_call_of_the_day_on_one_chain(daily, tmp_path, monkeypatch,
                                                                                         capsys):
    first = run(daily, tmp_path)
    again = run(daily, tmp_path, now=datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc))  # the day's first line counts
    run(daily, tmp_path, now=datetime(2026, 9, 21, 14, 30, tzinfo=timezone.utc))
    by_hand = run(daily, tmp_path, now=datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc), post=False)
    logged = call_log.read(tmp_path / "calls.jsonl")
    assert (first["calls_logged"], again["calls_logged"]) == (2, 0) and "calls_logged" not in by_hand
    assert call_log.intact(logged) == 4
    assert [(c["at"][:10], c["person"], c["role"], c["commit"]) for c in logged] == [
        (day, person, role, "0123456789ab") for day in ("2026-09-20", "2026-09-21")
        for person, role in (("ada-example", "mts-research"), ("bo-example", "backend"))]
    monkeypatch.setattr(sys, "argv", ["daily.py", "head"])
    daily.main()
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == call_log.head(tmp_path / "calls.jsonl") | \
        {"lines": 4, "intact": 4, "head": logged[-1]["hash"]}


def test_the_call_log_names_a_checkout_with_changes_since_its_commit_and_never_guesses(daily, monkeypatch):
    for said, code, named in (("0123456789ab-dirty\n", 0, "0123456789ab-dirty"), ("fatal: not a git repository", 128,
                                                                                    "unknown")):
        monkeypatch.setattr(daily.subprocess, "run", lambda args, s=said, c=code, **kw: subprocess.CompletedProcess(
            args, c, stdout=s, stderr=""))
        assert daily.commit() == named


def test_a_call_log_that_cannot_be_written_is_a_note_and_the_run_goes_on(daily, tmp_path, monkeypatch):
    def full(path, calls):
        raise OSError("no space left on device")
    monkeypatch.setattr(daily.call_log, "append", full)
    line = run(daily, tmp_path)
    assert line["calls_logged"] == 0 and line["status"] == "partial" and daily.routed
    assert line["notes"] == ["call log not written: OSError: no space left on device"]
    assert line["slack"] == "Posted the morning list with 1 card in its thread."


def test_model_drafts_are_charged_to_the_month_capped_at_what_is_left_and_only_cached_when_nothing_is(daily, tmp_path,
                                                                                                       monkeypatch):
    said = "Ada Example's model draft failed, so the card stays held: Anthropic returned HTTP 402.\n" \
           "0 of 2 people are reach_now as of 2026-09-20; 0 carded, 0 kept quiet; 0 more had a public moment in the " \
           "last month; 3 model calls, $0.210"
    monkeypatch.setattr(daily.subprocess, "run", lambda args, **kw: (daily.routed.append((args, "")), subprocess.
                        CompletedProcess(args, 0, stdout=said + "\n", stderr=""))[1])
    line = run(daily, tmp_path)
    assert (line["draft_calls"], line["draft_usd"]) == (3, 0.21) and daily.drafted("no calls") == (0, 0.0)
    assert "1 model draft failed, so those cards stay held: why is in daily-output.log" in line["notes"]
    assert line["usd"] > 0.21 and line["month_usd"] == line["usd"]  # drafts count in the month's cap
    spent(tmp_path, daily.CAP_USD - 0.30)  # under a dollar left: route.py's cap is what is left, in whole cents down
    daily.routed.clear()
    line = run(daily, tmp_path, now=datetime(2026, 9, 21, 14, 30, tzinfo=timezone.utc))
    args = daily.routed[-1][0]
    left = 0.30 - (line["usd"] - line["draft_usd"])  # after this run's pulls and reading
    assert float(args[args.index("--max-usd") + 1]) == math.floor(left * 100) / 100 and "--redraft" in args
    spent(tmp_path, daily.CAP_USD)  # nothing left: a draft already written is free, so route.py still reads those
    daily.routed.clear()
    line = run(daily, tmp_path, now=datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc))
    assert daily.routed[-1][0][-4:-1] == ["--redraft", "--max-usd", "0.00"]
    assert any(n.startswith("model drafts: only ones already written") for n in line["notes"])


def spent(tmp_path, usd):
    (tmp_path / "daily-runs.jsonl").write_text(json.dumps({"at": "2026-09-10T07:30:00+00:00", "usd": usd}) + "\n"
                                               + json.dumps({"at": "2026-08-31T07:30:00+00:00", "usd": 9.0}) + "\n"
                                               + "not a run\n[]\n")


def test_the_month_cap_skips_the_pull_when_its_worst_case_does_not_fit(daily, tmp_path):
    spent(tmp_path, 5.95)  # August's spend is not this month's; September's is
    line = run(daily, tmp_path)
    assert daily.fake.runs == [] and daily.reads == []
    assert line["status"] == "partial" and line["usd"] == 0 and line["month_usd"] == 5.95 and "pulled" not in line
    assert any(n.startswith("pull skipped") for n in line["notes"])
    assert daily.routed  # routing is free, so it still runs on what the timeline holds


def test_the_month_cap_skips_reading_that_does_not_fit_after_the_pull(daily, tmp_path):
    spent(tmp_path, 5.90)  # the pull's worst case ($0.09) fits; after it, a week's reading at its worst does not
    line = run(daily, tmp_path)
    assert len(daily.fake.runs) == 2 and daily.reads == [] and line["read_calls"] == 0
    assert any(n.startswith("read skipped for 2 people") for n in line["notes"]) and line["pulled"] == "2026-09-20"
    assert line["month_usd"] <= 6.0


def test_the_pause_file_stops_everything_but_the_log_line(daily, tmp_path):
    (tmp_path / "daily.pause").write_text("")
    line = run(daily, tmp_path)
    assert line["status"] == "paused" and line["usd"] == 0
    assert daily.fake.runs == daily.reads == daily.routed == store_of(tmp_path).all("timeline_event") == []


def test_a_run_that_fails_still_logs_what_it_spent_and_the_mornings_calls(daily, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("invented")
    monkeypatch.setattr(daily.posts, "read", broken)
    with pytest.raises(RuntimeError):
        run(daily, tmp_path)
    [line] = lines(tmp_path)
    # A morning missing from the call log would drop every open window, so it is logged on what the store holds.
    assert line["calls_logged"] == 2 and call_log.intact(call_log.read(tmp_path / "calls.jsonl")) == 2
    # The pull at its cost and the reading at its worst case, which it may have spent before failing: two calls,
    # each up to its 8,000-token answer.
    pull = 50 * 0.0004 + 0.002 + 2 * daily.RUN_FEE_USD
    assert line["status"] == "failed" and line["error"] == "RuntimeError" and line["pulled"] == "2026-09-20"
    assert line["usd"] - pull > 2 * 0.08


def test_a_failed_actor_run_counts_at_its_worst_and_the_next_run_asks_again(daily, tmp_path):
    daily.fake.items[apify.X_SEARCH] = RuntimeError("invented")
    (tmp_path / "daily-runs.jsonl").write_text(json.dumps({"at": "2026-09-19T14:30:00+00:00", "usd": 0.0,
                                                           "pulled": "2026-09-19"}) + "\n")
    line = run(daily, tmp_path)
    assert line["status"] == "partial" and line["failed"] == [apify.X_SEARCH] and "pulled" not in line
    assert sorted((c["actor"], c["status"]) for c in line["apify_reported"]) == \
        [(apify.X_SEARCH, "FAILED"), (apify.LINKEDIN_POSTS, "SUCCEEDED")]  # a failed run may still have charged
    # X at its worst case, the LinkedIn post, two start fees, and one week read (Ada's LinkedIn post).
    assert line["usd"] == pytest.approx(0.06 + 0.002 + 2 * daily.RUN_FEE_USD + ((4000 + 1.25 * 1000 + 0.1 * 3000) * 2.0 + 300 * 10.0) / 1e6, abs=1e-4)
    run(daily, tmp_path, now=datetime(2026, 9, 21, 14, 30, tzinfo=timezone.utc))
    assert [i["searchTerms"][0][-16:] for a, i, *_ in daily.fake.runs if a == apify.X_SEARCH] == \
        ["since:2026-09-19"] * 2  # the same days, asked again


def test_a_github_failure_changes_neither_the_cost_nor_where_the_next_run_starts(daily, tmp_path, monkeypatch):
    def gone(login, per_person, token):
        raise daily.httpx.ConnectError("offline")
    monkeypatch.setattr(daily.github, "fetch", gone)
    line = run(daily, tmp_path)
    assert line["failed"] == ["ada-example"] and line["status"] == "partial" and line["pulled"] == "2026-09-20"
    assert line["usd"] == pytest.approx(50 * 0.0004 + 0.002 + 2 * daily.RUN_FEE_USD + ((8000 + 1.25 * 2000 + 0.1 * 6000) * 2.0 + 600 * 10.0) / 1e6,
                                        abs=1e-4)


def test_a_search_that_fills_its_cap_says_what_may_be_missing(daily, tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "X_CAP", 3)
    daily.fake.items[apify.X_SEARCH] = [tweet("ada_example", 1, "2026-09-20"), tweet("ada_example", 2, "2026-09-20"),
                                        tweet("someone_else", 3, "2026-09-20")]  # a stray still fills the cap
    line = run(daily, tmp_path)
    assert line["status"] == "partial" and line["pulled"] == "2026-09-20"  # nothing later asks for the older ones
    assert "X search came back with 3 of its cap of 3: anything before 2026-09-20 may be missing" in line["notes"]


def test_an_employer_a_profile_read_found_has_its_news_read_free_each_run(daily, tmp_path, monkeypatch):
    journey.save_context(store_of(tmp_path), journey.PersonContext(
        subject_id="mts-research:ada-example", name="Ada Example", employer="Acme Robotics", employer_since="2024-03"))
    feed = ("<rss><channel><item><title>Birch to acquire Acme Robotics - Wire</title><link>https://example.org/n/1</link>"
            "<pubDate>Tue, 15 Sep 2026 14:00:00 GMT</pubDate></item></channel></rss>")
    asked = []
    monkeypatch.setattr(daily, "Fetcher", lambda refresh: lambda url: asked.append(url) or feed.encode())
    monkeypatch.setattr(warn, "load", lambda fetch=None: [])
    line = run(daily, tmp_path)
    assert line["status"] == "ok" and line["new_events"] == 3 and len(asked) == 2  # two free searches, apart from the pull
    assert [e["event_type"] for e in store_of(tmp_path).all("timeline_event")
            if e["subject_id"] == "org:acme-robotics"] == ["acquisition"]

    def offline(url):
        raise daily.httpx.ConnectError("offline")
    monkeypatch.setattr(daily, "Fetcher", lambda refresh: offline)
    line = run(daily, tmp_path, now=datetime(2026, 9, 21, 14, 30, tzinfo=timezone.utc))
    assert line["status"] == "partial" and "employer sources failed for some employers: news" in line["notes"]


def test_a_run_with_no_one_to_pull_says_so(daily, tmp_path):
    (tmp_path / "people.json").write_text(json.dumps([PEOPLE[-1]]))  # only a past moment
    line = run(daily, tmp_path)
    assert line["status"] == "partial" and line["notes"][0].startswith("no one to pull") and daily.fake.runs == []


def test_one_run_at_a_time(daily, tmp_path):
    import fcntl
    with (tmp_path / "daily.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="Another daily run"):
            run(daily, tmp_path)
    assert daily.fake.runs == [] and not (tmp_path / "daily-runs.jsonl").exists()
    run(daily, tmp_path)  # once it is let go, the next run goes ahead
    assert len(lines(tmp_path)) == 1


def test_where_a_run_starts(daily):
    assert daily.since_day([{"pulled": ""}], date(2026, 9, 25)) == date(2026, 9, 22)  # first run: LOOKBACK_DAYS
    assert daily.since_day([{"pulled": "2026-09-24"}], date(2026, 9, 25)) == date(2026, 9, 24)
    assert daily.since_day([{"pulled": "2026-09-01"}], date(2026, 9, 25)) == date(2026, 9, 18)  # MAX_DAYS at most


def searched(daily):
    return " ".join(i["searchTerms"][0] for a, i, *_ in daily.fake.runs if a == apify.X_SEARCH)


def test_someone_gi_hired_is_neither_pulled_nor_read_until_the_hire_ends(daily, tmp_path):
    store = store_of(tmp_path)
    contact.record(store, "backend:bo-example", "hired", at="2026-09-10T09:00:00+00:00", by="New starts")
    contact.record(store, "mts-research:ada-example", "hired", at="2026-08-01T09:00:00+00:00", by="New starts",
                   until="2026-09-15")  # taken off before the start: a candidate again
    run(daily, tmp_path)
    assert "from:ada_example" in searched(daily) and "bo_example" not in searched(daily)
    [(read_ids, *_)] = daily.reads
    assert read_ids == ["mts-research:ada-example"]


def test_a_hire_on_new_starts_is_held_before_the_pull_but_not_by_a_dry_run(daily, tmp_path):
    hire = {"id": "bo", "name": "Bo Example", "role_id": "backend", "office": "New York City", "start": "2026-10-05",
            "offer_on": "2026-09-18", "subject_id": "backend:bo-example", "items": {}}
    (tmp_path / "starts.json").write_text(json.dumps({"hires": [hire]}))
    run(daily, tmp_path, dry_run=True)
    assert not any(e["kind"] == "hired" for e in contact.history(store_of(tmp_path), "backend:bo-example"))
    assert daily.fake.runs == []
    run(daily, tmp_path)
    assert [e["by"] for e in contact.history(store_of(tmp_path), "backend:bo-example") if e["kind"] == "hired"] == \
        ["New starts"]
    assert "from:ada_example" in searched(daily) and "bo_example" not in searched(daily)


def test_the_dry_run_leaves_out_a_new_starts_hire_as_the_run_will(daily, tmp_path, capsys):
    hire = {"id": "bo", "name": "Bo Example", "role_id": "backend", "office": "New York City", "start": "2026-10-05",
            "offer_on": "2026-09-18", "subject_id": "backend:bo-example", "items": {}}
    (tmp_path / "starts.json").write_text(json.dumps({"hires": [hire]}))
    run(daily, tmp_path, dry_run=True)
    out = capsys.readouterr().out
    assert "from:ada_example" in out and "bo_example" not in out


def test_new_starts_that_cannot_be_read_is_said_in_the_log(daily, tmp_path):
    (tmp_path / "starts.json").write_text("{not json")
    line = run(daily, tmp_path)
    assert line["status"] == "partial" and line["notes"][0].startswith("New starts could not be read")


def test_a_pause_on_sending_saves_the_cards_and_posts_nothing(daily, tmp_path, capsys):
    contact.pause("Justin", "checking the list first")
    run(daily, tmp_path, dry_run=True)
    [said] = [x for x in capsys.readouterr().out.splitlines() if "posts nothing" in x]
    assert "posts nothing: Sending is paused" in said and said.endswith("first.")
    line = run(daily, tmp_path)
    [(args, _)] = daily.routed
    assert "--send" not in args and line["route"].startswith("1 of 2 people")  # the cards are still saved
    assert line["slack"].startswith("not posted: Sending is paused") and line["status"] == "ok"


def test_on_a_monday_the_monday_post_carries_the_reaches_so_the_run_posts_no_list(daily, tmp_path, capsys):
    monday = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)  # Monday wherever the Mac is, UTC-12 to UTC+11
    run(daily, tmp_path, now=monday, dry_run=True)
    assert "posts nothing: Monday: the Monday post carries this week's reach cards." in capsys.readouterr().out
    line = run(daily, tmp_path, now=monday)
    [(args, _)] = daily.routed
    assert "--send" not in args and line["route"].startswith("1 of 2 people")  # the cards are still saved
    assert line["slack"] == "not posted: Monday: the Monday post carries this week's reach cards"
    assert line["status"] == "ok"
    run(daily, tmp_path, now=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc))  # Tuesday: the list again
    assert "--send" in daily.routed[-1][0]


def test_the_monday_rule_asks_the_clock_when_it_routes_not_when_the_run_began(daily, tmp_path, monkeypatch):
    times = itertools.chain([datetime(2026, 9, 20, 23, 58, tzinfo=timezone.utc)],  # begun Sunday, just before midnight
                            itertools.repeat(datetime(2026, 9, 21, 0, 5, tzinfo=timezone.utc)))  # routed on Monday
    monkeypatch.setattr(daily, "utcnow", lambda: next(times))
    line = daily.run(tmp_path / "people.json", tmp_path, f"sqlite:///{tmp_path / 'timelines.sqlite'}", post=True)
    assert line["at"].startswith("2026-09-20") and line["slack"].startswith("not posted: Monday")


def test_a_morning_with_no_one_new_posts_nothing(daily, tmp_path):
    daily.posted = "Nothing new to reach: nothing posted.\n"
    line = run(daily, tmp_path)
    assert line["slack"] == "Nothing new to reach: nothing posted." and line["status"] == "ok"


def test_a_run_without_post_and_the_dry_run_never_post(daily, tmp_path, capsys):
    assert run(daily, tmp_path, dry_run=True) is None and daily.routed == []
    assert "posts the morning list as the Slack app" in capsys.readouterr().out
    run(daily, tmp_path, dry_run=True, post=False)
    assert "posts nothing: no --post" in capsys.readouterr().out
    line = run(daily, tmp_path, post=False)
    [(args, _)] = daily.routed
    assert "--send" not in args and line["slack"] == "not posted: no --post" and line["status"] == "ok"


def test_a_route_that_fails_without_posting_says_why(daily, tmp_path, monkeypatch):
    monkeypatch.setattr(daily.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(
        args, 1, stdout="", stderr="Traceback ...\nKeyError: 'invented'\n"))
    line = run(daily, tmp_path, post=False)
    assert line["status"] == "partial" and line["notes"] == ["route.py failed: KeyError: 'invented'"]


def test_the_dry_run_says_where_the_list_would_go_and_never_names_a_token(daily, monkeypatch):
    daily = importlib.util.module_from_spec(spec := importlib.util.spec_from_file_location("d", daily.__file__))
    spec.loader.exec_module(daily)  # the real slack_target, not the fixture's
    settings = {"SLACK_BOT_TOKEN": "xoxb-invented", "SLACK_CHANNEL": "C0INVENTED", "SLACK_APP_TOKEN": ""}
    monkeypatch.setattr(daily.slack, "load", lambda: settings)
    assert daily.slack_target() == "as the Slack app, each card in the list's thread"
    settings["SLACK_CHANNEL"] = ""
    monkeypatch.setattr(daily, "dotenv_values", lambda path: {})
    monkeypatch.setenv("SLACK_ROUTING_WEBHOOK", "https://hooks.slack.example/T/B/invented")
    assert daily.slack_target().startswith("through the webhook") and "hooks" not in daily.slack_target()
    monkeypatch.delenv("SLACK_ROUTING_WEBHOOK")
    assert daily.slack_target().startswith("nowhere: Slack is not set up")


def test_a_post_that_fails_is_logged_with_route_pys_reason_and_the_cards_still_saved(daily, tmp_path, monkeypatch):
    routes = daily.subprocess.run

    def no_slack(args, **kwargs):
        if "--send" not in args:
            return routes(args, **kwargs)
        daily.routed.append((args, kwargs["env"]["TIMELINES_DB"]))
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="--send needs the Slack app (SLACK_BOT_TOKEN and "
                                           "SLACK_CHANNEL) or SLACK_ROUTING_WEBHOOK\n")
    monkeypatch.setattr(daily.subprocess, "run", no_slack)
    line = run(daily, tmp_path)
    assert ["--send" in args for args, _ in daily.routed] == [True, False]  # then routed again, posting nothing
    assert line["status"] == "partial" and line["route"].startswith("1 of 2 people")
    assert line["slack"].startswith("not posted: --send needs the Slack app")
    assert line["notes"] == ["route.py failed: " + line["slack"].removeprefix("not posted: ")]


def test_a_route_that_fails_after_posting_says_it_posted_and_never_posts_again(daily, tmp_path, monkeypatch):
    routes = daily.subprocess.run

    def late(args, **kwargs):
        if "--send" not in args:
            return routes(args, **kwargs)
        daily.routed.append((args, kwargs["env"]["TIMELINES_DB"]))
        return subprocess.CompletedProcess(args, 1, stdout="Posted the morning list with 1 card in its thread.\n",
                                           stderr="Traceback ...\nKeyError: 'invented'\n")
    monkeypatch.setattr(daily.subprocess, "run", late)
    line = run(daily, tmp_path)
    assert ["--send" in args for args, _ in daily.routed] == [True, False]
    assert line["slack"] == "Posted the morning list with 1 card in its thread." and line["status"] == "partial"
    assert line["notes"] == ["route.py failed after posting: KeyError: 'invented'"]


def test_cards_kept_but_not_sent_mark_the_run(daily, tmp_path):
    daily.posted = ("Not sent: Bo Example: Slack refused the card: 400 invalid_blocks\n"
                    "Posted the morning list, but none of its 1 card went: see Not sent above.\n")
    line = run(daily, tmp_path)
    assert line["slack"].startswith("Posted the morning list, but none") and line["status"] == "partial"
    assert line["notes"] == ["1 card kept but not sent: why is in daily-output.log"]


def test_route_py_says_a_list_went_even_when_none_of_its_cards_did(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("route_script", ROOT / "scripts" / "route.py")
    route = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(route)
    monkeypatch.setattr(contact, "PAUSE", tmp_path / "sending-paused.json")
    monkeypatch.setattr(route, "target", lambda: "https://hooks.slack.example/T/B/invented")

    def refused(store, kept, to, now=None, test=False, held=None):
        held.extend((r, "Slack refused the card: 400 invalid_blocks") for r in kept)
        return []
    monkeypatch.setattr(route.routing, "send_morning", refused)
    monkeypatch.setattr(sys, "argv", ["route.py", "--demo", "--send"])
    route.main()
    out = capsys.readouterr().out
    assert "Posted the morning list, but none of its" in out and "Nothing new to reach" not in out


def test_route_py_still_says_what_its_send_did_in_the_words_the_log_reads(daily):
    assert all(words in (ROOT / "scripts" / "route.py").read_text() for words in daily.POSTED)


def test_a_dry_run_spends_and_logs_nothing(daily, tmp_path, capsys):
    assert run(daily, tmp_path, dry_run=True) is None
    assert daily.fake.runs == daily.reads == daily.routed == [] and not (tmp_path / "daily-runs.jsonl").exists()
    assert not (tmp_path / "calls.jsonl").exists()
    assert store_of(tmp_path).all("person_context") == []
    assert "worst case $0.090" in capsys.readouterr().out  # X 150 x $0.0004, LinkedIn 5 x $0.002, two start fees


def test_the_launchd_job_runs_this_checkout_daily_and_posts(daily, tmp_path):
    job = plistlib.loads(daily.plist(tmp_path / "people.json", tmp_path).encode())
    assert job["StartCalendarInterval"] == {"Hour": 7, "Minute": 30} and job["WorkingDirectory"] == str(ROOT)
    assert job["ProgramArguments"] == [sys.executable, str(ROOT / "scripts/daily.py"), "--people",
                                       str(tmp_path / "people.json"), "--post"]  # the job posts; a run by hand does not
    assert job["StandardOutPath"] == str(tmp_path / "daily-output.log")
    assert job["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"


def test_the_launchd_job_keeps_the_folder_and_the_timing_store_it_was_made_with(daily, tmp_path, monkeypatch):
    monkeypatch.setenv("SOCIAL_DIR", str(tmp_path))
    monkeypatch.delenv("TIMELINES_DB")  # conftest's
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@host/db")  # the web app's: never in the job
    monkeypatch.chdir(tmp_path.parent)
    job = plistlib.loads(daily.plist(tmp_path / "people.json", Path(tmp_path.name)).encode())  # a relative folder
    assert job["EnvironmentVariables"] == {"PYTHONUNBUFFERED": "1", "SOCIAL_DIR": str(tmp_path)}  # no password URL
    assert job["StandardOutPath"] == str(tmp_path / "daily-output.log")
    monkeypatch.setenv("TIMELINES_DB", "t.sqlite")
    monkeypatch.setattr(daily.today, "TIMELINES", Path("t.sqlite"))  # as the job's run would resolve it
    env = plistlib.loads(daily.plist(tmp_path / "people.json", tmp_path).encode())["EnvironmentVariables"]
    assert env["TIMELINES_DB"] == str(tmp_path.parent / "t.sqlite") and "DATABASE_URL" not in env
