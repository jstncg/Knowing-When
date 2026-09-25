"""The screen says how fresh its data is, warns when a pull is old or broken, and claims no win over random timing
that luck would explain. Invented logs and reports only."""

import json
from datetime import datetime, timezone

import pytest

from app import inbox, slack, today, weekly
from app.store import Store

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _log(tmp_path, *rows, extra=""):
    log = tmp_path / "daily-runs.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in rows) + extra)
    return log


def test_the_simulation_says_its_data_is_invented():
    fresh = today.freshness("simulation")
    assert "Nothing here is real" in fresh["line"] and not fresh["warning"] and not fresh["stale"]


def test_a_pull_yesterday_is_fresh_and_says_when(tmp_path):
    log = _log(tmp_path, {"at": "2026-09-23T11:30:00+00:00", "status": "ok", "pulled": "2026-09-23"})
    fresh = today.freshness("live", now=NOW, log=log)
    assert fresh["pulled"] == "2026-09-23" and not fresh["stale"] and fresh["warning"] is None
    assert fresh["line"] == ("Data pulled 23 September 2026 (yesterday) by the daily run. "
                             "Last daily run 23 Sep 11:30 UTC: ok.")


def test_old_data_warns_that_a_quiet_list_may_be_a_broken_pull(tmp_path):
    log = _log(tmp_path, {"at": "2026-09-20T11:30:00+00:00", "status": "ok", "pulled": "2026-09-20"},
               {"at": "2026-09-21T11:30:00+00:00", "status": "partial", "notes": ["x search came back at its cap"]},
               extra="not json\n")  # a routine partial, and no full pull since
    fresh = today.freshness("live", now=NOW, log=log)
    assert fresh["stale"] and "4 days old" in fresh["warning"] and "not a quiet week" in fresh["warning"]
    assert "(4 days ago)" in fresh["line"] and "partial" in fresh["line"]


def test_a_failed_or_paused_last_run_is_said_even_when_the_data_is_recent(tmp_path):
    failed = _log(tmp_path, {"at": "2026-09-23T11:30:00+00:00", "status": "ok", "pulled": "2026-09-23"},
                  {"at": "2026-09-24T11:30:00+00:00", "status": "partial", "failed": ["apify~linkedin"]})
    fresh = today.freshness("live", now=NOW, log=failed)
    assert not fresh["stale"] and "went wrong: apify~linkedin failed" in fresh["warning"]
    paused = _log(tmp_path, {"at": "2026-09-24T11:30:00+00:00", "status": "ok", "pulled": "2026-09-24"},
                  {"at": "2026-09-24T11:40:00+00:00", "status": "paused", "usd": 0.0})
    assert "paused" in today.freshness("live", now=NOW, log=paused)["warning"]


def test_without_a_log_it_reads_the_newest_event_and_with_nothing_it_says_so(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 's.sqlite'}")
    missing = tmp_path / "none.jsonl"
    fresh = today.freshness("live", store, now=NOW, log=missing)
    assert fresh["line"] == "No pull on record." and fresh["stale"] and "Nothing has been pulled yet" in fresh["warning"]
    store.put("e1", "timeline_event", {"subject_id": "P1", "quote": "x"})
    fresh = today.freshness("live", store, now=NOW, log=missing)
    assert fresh["pulled"] == store.all("timeline_event")[0]["updated_at"][:10]  # when it was written
    assert "newest event in the store" in fresh["line"]
    assert "a door note or a reading counts too" in fresh["warning"] or fresh["stale"]  # a guess never reads as fresh


def test_a_malformed_pull_day_is_skipped_not_a_crash(tmp_path):
    log = _log(tmp_path, {"at": "garbage", "status": "ok", "pulled": "not a day"})
    fresh = today.freshness("live", now=NOW, log=log)
    assert fresh["line"].startswith("No pull on record.") and "Last daily run garbage: ok." in fresh["line"]


def test_a_hand_edited_log_never_takes_a_page_down(tmp_path):
    log = _log(tmp_path, {"at": "2026-09-23T11:30:00+00:00", "status": "ok", "pulled": "2026-09-23"},
               {"at": 1727000000, "status": "partial", "failed": "x", "error": "KeyError"}, extra="[1, 2]\n")
    fresh = today.freshness("live", now=NOW, log=log)
    assert "Last daily run 1727000000: partial." in fresh["line"] and fresh["warning"].startswith(
        "The last daily run (1727000000) went wrong: error KeyError.")
    bad = tmp_path / "bytes.jsonl"
    bad.write_bytes(b"\xff\xfe not text")
    assert today.freshness("live", now=NOW, log=bad)["line"] == "No pull on record."


def test_todays_calls_carry_the_line():
    today._demo_store.cache_clear()
    try:
        assert "Nothing here is real" in today.calls("simulation")["fresh"]["line"]
    finally:
        today._demo_store.cache_clear()


ITEM = {"area": "Hiring", "title": "Reach Ada", "why": "a paper", "where": "Today's calls", "due": None,
        "overdue": False, "way_in": None, "cost": None, "act": None}
STALE = {"line": "Data pulled 20 September 2026 (4 days ago) by the daily run.", "stale": True,
         "warning": "The data is 4 days old.", "pulled": "2026-09-20"}


def test_the_brief_card_leads_with_a_stale_warning_and_says_when_it_was_pulled():
    week = {"week_of": "2026-09-21", "as_of": "2026-09-24", "source": "research/private/social/timelines.sqlite",
            "decisions": [ITEM], "fresh": STALE}
    blocks = inbox.card(week)["blocks"]
    assert blocks[1]["text"]["text"] == "*Check the pull first.* The data is 4 days old."
    assert "Data pulled 20 September 2026" in blocks[-1]["elements"][0]["text"]
    invented = inbox.card({**week, "source": "Invented people, as of 15 September 2026"})["blocks"]
    assert "Check the pull first" not in json.dumps(invented) and "Invented people and numbers." in json.dumps(invented)


def test_a_quiet_home_tab_says_how_fresh_the_data_is(monkeypatch):
    monkeypatch.setattr(weekly, "brief", lambda mode: {"missing": None, "week_of": "2026-09-21", "as_of": "2026-09-24",
                                                       "source": "live", "decisions": [], "fresh": STALE})
    text = json.dumps(slack.home("live"))
    assert "Nothing to decide this week. Data pulled 20 September 2026" in text
    assert "Check the pull first.* The data is 4 days old." in text


@pytest.mark.parametrize("seen, alarms, low", [(4, 5, False), (9, 1, True)])
def test_the_scorecard_gets_the_chance_its_gap_over_random_timing_is_luck(tmp_path, seen, alarms, low):
    engine = {"moments": 10, "seen_coming": seen, "ordinary_stretches": 19, "fired_with_nothing_after": alarms}
    (tmp_path / "2026-09-23-scorecard.json").write_text(json.dumps({
        "noise": {"signs": {}}, "scorecard": {"all_signs": {"engine": engine, "any_reach": engine},
                                              "kept_signs": {"engine": engine}}}))
    report = today.latest_scorecard(tmp_path)
    p = report["scorecard"]["all_signs"]["engine"]["p"]
    assert (p < 0.05) is low and report["scorecard"]["kept_signs"]["engine"]["p"] == p
    if not low:
        assert round(p, 2) == 0.36  # the live scorecard's 4 of 10 against 5 of 19


def test_the_monday_post_leads_with_a_stale_warning_and_says_when_it_was_pulled():
    week = {"week_of": "2026-09-21", "as_of": "2026-09-24", "source": "research/private/social/timelines.sqlite",
            "decisions": [{**ITEM, "thread": None}], "fresh": STALE}
    blocks = weekly.post(week)["blocks"]
    assert blocks[1]["text"]["text"] == "*Check the pull first.* The data is 4 days old."
    assert "Data pulled 20 September 2026" in blocks[-1]["elements"][0]["text"]
    assert "Check the pull first" not in json.dumps(weekly.post({**week, "fresh": {**STALE, "warning": None}}))
    with_fix = weekly.post({**week, "fresh": BROKEN})["blocks"][1]["text"]["text"]
    assert with_fix.endswith("On the Mac: `tail -n 3 research/private/social/daily-runs.jsonl`")


class Poster:
    """The Slack app's poster, recording each card."""

    def __init__(self):
        self.cards = []

    def post(self, card, route=None, test=False, thread=None):
        self.cards.append(card)
        return {"ts": str(len(self.cards))}


def test_a_broken_pull_is_named_with_the_one_command_that_checks_or_fixes_it(tmp_path):
    log = tmp_path / "daily-runs.jsonl"
    assert today.freshness("live", now=NOW, log=log)["fix"] == "uv run python scripts/daily.py --dry-run"
    stale = today.freshness("live", now=NOW, log=_log(tmp_path, {"at": "2026-09-20T11:30:00+00:00", "status": "ok",
                                                                "pulled": "2026-09-20"}))
    assert stale["broken"] == "stale" and stale["fix"] == f"tail -n 3 {log}"
    paused = _log(tmp_path, {"at": "2026-09-24T11:30:00+00:00", "status": "ok", "pulled": "2026-09-24"},
                  {"at": "2026-09-24T11:40:00+00:00", "status": "paused"})
    assert today.freshness("live", now=NOW, log=paused)["fix"] == f"rm {tmp_path / 'daily.pause'}"
    fine = _log(tmp_path, {"at": "2026-09-24T11:30:00+00:00", "status": "ok", "pulled": "2026-09-24"})
    assert today.freshness("live", now=NOW, log=fine)["broken"] is None
    store = Store(f"sqlite:///{tmp_path / 's.sqlite'}")
    store.put("e1", "timeline_event", {"subject_id": "P1", "quote": "x"})
    guessed = today.freshness("live", store, now=parse(store), log=tmp_path / "none.jsonl")
    assert guessed["warning"] and guessed["broken"] is None  # a guess is warned about on the pages, never posted


def parse(store):
    from app.models import parse_time
    return parse_time(store.all("timeline_event")[0]["updated_at"])


BROKEN = {**STALE, "broken": "stale", "why": "2026-09-20", "fix": "tail -n 3 research/private/social/daily-runs.jsonl"}


def test_the_pull_line_says_what_is_wrong_and_the_command_and_a_quiet_week_says_nothing():
    card = weekly.pull_alert(BROKEN)
    assert card["text"] == ("Check the pull: The data is 4 days old. To check, on the Mac: "
                            "tail -n 3 research/private/social/daily-runs.jsonl")
    assert len(card["blocks"]) == 1 and "`tail -n 3 research/private/social/daily-runs.jsonl`" in json.dumps(card)
    assert weekly.pull_alert({**STALE, "broken": None}) is None and weekly.pull_alert({}) is None
    assert "To resume" in weekly.pull_alert({**BROKEN, "broken": "paused", "fix": "rm daily.pause"})["text"]


def test_the_daily_line_is_said_once_per_problem_and_again_for_a_new_one(tmp_path):
    to, state = Poster(), tmp_path / "pull-alert.json"
    assert weekly.alert_once(BROKEN, to, state).startswith("Check the pull:")
    assert weekly.alert_once(BROKEN, to, state) is None  # the same problem, the next morning
    assert weekly.alert_once({**BROKEN, "broken": "failed", "why": "apify~linkedin failed"}, to, state)  # a new one
    assert len(to.cards) == 2
    assert weekly.alert_once({**BROKEN, "broken": None}, to, state) is None and not state.exists()  # fixed: forgotten
    assert weekly.alert_once(BROKEN, to, state) and len(to.cards) == 3


THURSDAY = NOW.date()  # not a month's first Monday: no one-pager


def test_a_monday_with_nothing_to_decide_posts_only_when_the_pull_is_broken(monkeypatch, tmp_path):
    monkeypatch.setattr(today, "PULL_ALERT", tmp_path / "pull-alert.json")
    quiet = {"missing": None, "week_of": "2026-09-21", "as_of": "2026-09-24", "source": "live", "decisions": []}
    monkeypatch.setattr(weekly, "week", lambda mode, now: ({**quiet, "fresh": {**STALE, "broken": None}}, {}))
    to = Poster()
    assert weekly.publish(to, "live", THURSDAY) is False and to.cards == []  # a plain quiet week: silence
    monkeypatch.setattr(weekly, "week", lambda mode, now: ({**quiet, "fresh": BROKEN}, {}))
    assert weekly.publish(to, "live", THURSDAY) is True and to.cards[0]["text"].startswith("Check the pull:")
    assert weekly.publish(to, "live", THURSDAY) is False and len(to.cards) == 1  # the daily run said it, or Monday did
    assert weekly.publish(to, "simulation", THURSDAY) is False  # the invented week never says the pull is broken


def test_the_daily_run_posts_the_line_once_and_never_while_sending_is_paused(tmp_path, monkeypatch):
    import importlib.util
    from app import contact
    spec = importlib.util.spec_from_file_location("daily_check", today.ROOT / "scripts" / "daily.py")
    daily = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daily)
    to = Poster()
    monkeypatch.setattr(daily.slack, "load", lambda: {"SLACK_BOT_TOKEN": "", "SLACK_CHANNEL": ""})
    monkeypatch.setattr(daily.slack, "poster", lambda settings: to)
    store_url = f"sqlite:///{tmp_path / 't.sqlite'}"
    _log(tmp_path, {"at": "2026-09-01T11:30:00+00:00", "status": "ok", "pulled": "2026-09-01"})
    monkeypatch.setattr(contact, "paused", lambda: {"at": "2026-09-20T00:00:00+00:00", "by": "Justin", "why": "a check"})
    assert "not posted: Sending is paused" in daily.pull_check(tmp_path, store_url) and to.cards == []
    monkeypatch.setattr(contact, "paused", lambda: None)
    assert daily.pull_check(tmp_path, store_url).startswith("Pull check posted: Check the pull: The data is")
    assert daily.pull_check(tmp_path, store_url) == "Pull check: stale, already said" and len(to.cards) == 1
    _log(tmp_path, {"at": iso_now(), "status": "ok", "pulled": iso_now()[:10]})
    assert daily.pull_check(tmp_path, store_url) == "Pull check: nothing broken." and len(to.cards) == 1


def iso_now():
    from app.models import iso
    return iso()


def _day(n, **run):
    return {"at": f"2026-09-{n}T11:30:00+00:00", **run}


def test_the_same_problem_keeps_one_reason_and_a_routine_partial_is_no_problem(tmp_path):
    def on(n, *rows):
        return today.freshness("live", now=datetime(2026, 9, n, 12, tzinfo=timezone.utc), log=_log(tmp_path, *rows))
    ok = _day(20, status="ok", pulled="2026-09-20")
    routine = [on(n, ok, *[_day(d, status="partial", pulled=f"2026-09-{d}", notes=["1 card kept but not sent"])
                           for d in range(21, n + 1)]) for n in (21, 22)]
    assert all(f["broken"] is None and f["warning"] is None for f in routine)  # ordinary days: nothing to say
    failing = [on(n, ok, *[_day(d, status="partial", failed=["apify~linkedin"]) for d in range(21, n + 1)])
               for n in (21, 22)]
    assert {(f["broken"], f["why"]) for f in failing} == {("failed", "an Apify actor failed")}  # one problem, said once
    assert "apify~linkedin failed" in failing[0]["warning"]  # the details, in the warning
    flaky = [on(22, ok, _day(21, status="partial", failed=failed)) for failed in (["ada-gh"], ["bo-gh"])]
    assert {f["why"] for f in flaky} == {"a GitHub fetch failed"}  # a different person's fetch: the same problem
    paused = [on(n, ok, *[_day(d, status="paused") for d in range(21, n + 1)]) for n in (21, 23, 25)]
    assert {(f["broken"], f["why"], f["fix"]) for f in paused} == {("paused", "paused", f"rm {tmp_path / 'daily.pause'}")}
    assert on(25, ok, _day(25, status="partial", notes=["pull skipped: its worst case is more than what is left"]))[
        "why"] == "pull skipped"


def test_a_guessed_day_never_hides_a_failed_run_and_door_notes_never_make_a_new_problem(tmp_path):
    from app.models import iso
    store = Store(f"sqlite:///{tmp_path / 's.sqlite'}")
    store.put("e1", "timeline_event", {"subject_id": "P1", "quote": "x"})
    written = parse(store)
    failing = _log(tmp_path, {"at": iso(written), "status": "partial", "failed": ["apify~linkedin"]})
    assert today.freshness("live", store, now=written, log=failing)["broken"] == "failed"
    paused = _log(tmp_path, {"at": iso(written), "status": "paused"})
    assert today.freshness("live", store, now=written, log=paused)["broken"] == "paused"
    later = written.replace(year=written.year + 1)
    old = today.freshness("live", store, now=later, log=tmp_path / "none.jsonl")
    store.put("e2", "timeline_event", {"subject_id": "P1", "quote": "a door note"})
    assert (old["broken"], old["why"]) == ("stale", "guessed")
    assert today.freshness("live", store, now=later.replace(day=later.day), log=tmp_path / "none.jsonl")["why"] == \
        "guessed"


def _main(tmp_path, monkeypatch, *argv, run=None):
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("daily_main", today.ROOT / "scripts" / "daily.py")
    daily = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daily)
    checked, people = [], tmp_path / "people.json"
    people.write_text("[]")
    monkeypatch.setattr(daily.social_pull, "PRIVATE", tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)  # conftest's web store: an older job's would stop the run (test_contact)
    monkeypatch.setattr(daily, "pull_check", lambda folder, url: checked.append(folder) or "Pull check: checked.")
    monkeypatch.setattr(daily, "run", run or (lambda *a, **k: None))
    monkeypatch.setattr(sys, "argv", ["daily.py", "--people", str(people), *argv])
    return daily, checked


def test_the_daily_run_checks_the_pull_after_every_posting_run_even_a_failed_one(tmp_path, monkeypatch):
    daily, checked = _main(tmp_path, monkeypatch, "--post")
    daily.main()
    assert checked == [tmp_path]

    def boom(*a, **k):
        raise RuntimeError("the run's own error")
    daily, checked = _main(tmp_path, monkeypatch, "--post", run=boom)
    with pytest.raises(RuntimeError, match="the run's own error"):
        daily.main()
    assert checked == [tmp_path]
    daily, checked = _main(tmp_path, monkeypatch, "--post", "--people", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit):
        daily.main()
    assert checked == [tmp_path]  # no people file is a broken pull too


def test_no_check_on_a_dry_run_a_run_by_hand_or_while_another_run_is_going(tmp_path, monkeypatch):
    for argv in (("--post", "--dry-run"), ()):
        daily, checked = _main(tmp_path, monkeypatch, *argv)
        daily.main()
        assert checked == []

    def busy(*a, **k):
        raise SystemExit(daily.BUSY)
    daily, checked = _main(tmp_path, monkeypatch, "--post", run=busy)
    with pytest.raises(SystemExit):
        daily.main()
    assert checked == []  # the run going now checks for itself
