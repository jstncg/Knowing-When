"""The daily run's call log: a chain that shows each line came first, and the forward test scored from it."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import call_log
from app.timeline import add_event

START = datetime(2026, 9, 25, 7, 30, tzinfo=timezone.utc)  # the log's first line


def add(store, subject, kind, day, url):
    add_event(store, {"subject_type": "person", "subject_id": subject, "event_type": kind, "event_date": day,
                      "date_precision": "day", "observed_at": f"{day}T12:00:00+00:00", "source_url": url,
                      "source_version_hash": url, "quote": "x", "tier": 1, "extractor": "test"})


def call(day, person="ada", action="quiet", early=False):
    """A line as call_log.line writes it, ``day`` days after the log's first."""
    return {"at": (START + timedelta(days=day)).isoformat(), "commit": "0123456789ab", "person": person,
            "subject_id": f"mts-research:{person}", "role": "mts-research", "action": action, "track": None,
            "reasons": [], "evidence": {}, "window": None, "until": None, "detectors": [], "early": early}


def daily(person="ada", days=range(120), early=()):
    """One line a morning on ``days``, an early reach on each day in ``early``."""
    return [call(d, person, *(("reach_now", True) if d in early else ())) for d in days]


def logged(tmp_path, calls):
    call_log.append(tmp_path / "calls.jsonl", sorted(calls, key=lambda c: c["at"]))  # as the mornings came
    return call_log.read(tmp_path / "calls.jsonl")


def moment(day, person="ada", kind="paper"):
    return {"person": person, "date": (START + timedelta(days=day)).date().isoformat(), "kind": kind,
            "source_url": f"https://arxiv.org/abs/{person}-{day}"}


def test_a_line_changed_or_taken_out_later_breaks_the_chain_from_there(tmp_path):
    path = tmp_path / "calls.jsonl"
    call_log.append(path, [call(0), call(0, "bo")])
    call_log.append(path, [call(1), call(1, "bo")])  # the next run chains on
    assert call_log.intact(call_log.read(path)) == 4
    texts = path.read_text().splitlines()
    edited = json.loads(texts[1]) | {"action": "reach_now", "early": True}
    path.write_text("\n".join([texts[0], json.dumps(edited), *texts[2:]]) + "\n")
    assert call_log.intact(call_log.read(path)) == 1
    path.write_text("\n".join([texts[0], *texts[2:]]) + "\n")  # a line taken out
    assert call_log.intact(call_log.read(path)) == 1
    path.write_text("\n".join([texts[0], "not json", *texts[2:]]) + "\n")
    assert call_log.intact(call_log.read(path)) == 1
    with pytest.raises(ValueError, match="the chain breaks at line 2 of 4"):
        call_log.append(path, [call(2)])  # nothing more is written after a break
    assert call_log.head(path) == {"lines": 4, "intact": 1, "head": json.loads(texts[0])["hash"], "at": call(0)["at"]}


def test_a_torn_write_or_a_morning_filled_in_later_stops_the_log_and_says_so(tmp_path):
    path = tmp_path / "calls.jsonl"
    call_log.append(path, [call(0), call(0, "bo")])
    # A morning filled in later would read posts pulled after it: refused, and a line added by hand breaks the chain.
    for late in ([call(-1)], [call(2), call(1)]):
        with pytest.raises(ValueError, match="earlier than the line before it"):
            call_log.append(path, late)
    backdated = {**call(-1), "prev": json.loads(path.read_text().splitlines()[-1])["hash"]}
    backdated["hash"] = call_log._hash(backdated)
    assert call_log.intact(call_log.read(path) + [backdated]) == 2
    # A write cut off part way leaves a torn last line: nothing more is written after it, and each run says why.
    text = path.read_text()
    path.write_text(text[:-40])
    with pytest.raises(ValueError, match="the chain breaks at line 2 of 2"):
        call_log.append(path, [call(1)])
    assert path.read_text() == text[:-40]
    path.write_text(text[:-1])  # whole, but its newline lost: the next line would be glued onto it
    with pytest.raises(ValueError, match="no newline"):
        call_log.append(path, [call(1)])


def test_a_line_is_the_call_before_the_gate_and_says_whether_it_is_an_early_reach(store):
    add(store, "mts-research:ada", "work_in_progress", "2026-09-20", "https://x.com/ada/1")
    add(store, "mts-research:dee", "self_stated_availability", "2026-09-20", "https://x.com/dee/1")
    at = START.isoformat()
    ada = call_log.line(store, "mts-research:ada", "mts-research", at, "0123456789ab")
    assert (ada["person"], ada["action"], ada["early"], ada["commit"]) == ("ada", "reach_now", True, "0123456789ab")
    assert "work_in_progress" in ada["detectors"] and ada["evidence"] and ada["reasons"] and ada["track"]
    # An "open to work" post makes a reach on news anyone can see: never an early reach.
    dee = call_log.line(store, "mts-research:dee", "mts-research", at, "0123456789ab")
    assert (dee["action"], dee["early"]) == ("reach_now", False)
    nobody = call_log.line(store, "mts-research:cy", "mts-research", at, "0123456789ab")
    assert (nobody["action"], nobody["early"], nobody["detectors"]) == (None, False, [])


def test_only_the_first_line_per_person_and_role_a_day_is_written(store, tmp_path):
    path, people = tmp_path / "calls.jsonl", [SimpleNamespace(subject_id="mts-research:ada", role="mts-research")]
    assert call_log.log(path, store, people, START.isoformat(), "0123456789ab") == 1
    assert call_log.log(path, store, people, (START + timedelta(hours=3)).isoformat(), "0123456789ab") == 0
    assert call_log.log(path, store, people, (START + timedelta(days=1)).isoformat(), "0123456789ab") == 1
    assert call_log.intact(call_log.read(path)) == 2


def test_a_moment_is_seen_coming_only_by_an_early_line_8_to_56_days_before_it(tmp_path):
    lines = logged(tmp_path, daily("ada", early={80}) + daily("bo", early={93}) + daily("cy", early={43})
                   + daily("dee", early={92}) + daily("eve", early={44}))
    result = call_log.score(lines, [moment(110, "ada"), *(moment(100, p) for p in ("bo", "cy", "dee", "eve"))])
    seen = {w["person"]: w["lead_days"] for w in result["windows"] if w["moment"]}
    # bo's is 7 days before (the last week, never counted); cy's 57, outside the 8 weeks.
    assert seen == {"ada": 30, "bo": None, "cy": None, "dee": 8, "eve": 56}
    assert (result["moments"], result["seen_coming"], result["broken"], result["a_test"]) == (5, 3, False, False)


def test_only_early_reaches_count_and_only_before_a_release_launch_or_paper(tmp_path):
    lines = logged(tmp_path, [call(d, "ada", "reach_now", False) for d in range(120)] + daily("bo", early={80}))
    result = call_log.score(lines, [moment(100, "ada"), moment(100, "bo", kind="job")])
    assert (result["moments"], result["seen_coming"]) == (1, 0)  # bo's job news is not a moment here


def test_a_window_counts_only_when_every_counted_morning_was_logged(tmp_path):
    # ada missed one morning inside the 8 weeks before their paper; dee's paper came before the log had 8 weeks of
    # them. cy joined the log 30 days after its first day: their stretches start from their own first morning.
    lines = logged(tmp_path, daily("ada", [d for d in range(120) if d != 60], early={80})
                   + daily("dee", early={10}) + daily("cy", range(30, 150)))
    result = call_log.score(lines, [moment(100, "ada"), moment(40, "dee")])
    assert (result["moments"], result["seen_coming"], result["incomplete"], result["missing_mornings"]) == (0, 0, 1, 1)
    assert [(w["person"], w["start"], w["missing"]) for w in result["windows"]] == [
        ("ada", "2026-11-08", ["2026-11-24"]), ("cy", "2026-10-25", [])]


def test_ordinary_stretches_are_the_scorecards_and_an_early_line_in_one_is_a_false_alarm(tmp_path):
    # 120 mornings with no moment: one 8-week stretch with the 8 weeks after it inside the log. An early line in its
    # last week is not counted, as the replay never looks there.
    lines = logged(tmp_path, daily("ada", early={20}) + daily("bo", early={50}))
    result = call_log.score(lines, [])
    assert (result["ordinary_stretches"], result["fired_with_nothing_after"], result["p"]) == (2, 1, None)
    # A moment inside a stretch or the 8 weeks after it leaves no ordinary stretch; with both kinds there is a p.
    both = call_log.score(lines, [moment(100, "ada")])
    assert (both["moments"], both["ordinary_stretches"]) == (1, 1) and both["p"] is not None


def test_a_broken_chain_is_reported_as_broken_and_never_scored(tmp_path):
    logged(tmp_path, daily("ada", early={80}))
    texts = (tmp_path / "calls.jsonl").read_text().splitlines()
    texts[5] = json.dumps(json.loads(texts[5]) | {"early": True})
    lines = [json.loads(t) for t in texts]
    assert call_log.score(lines, [moment(110, "ada")]) == {"lines": 120, "intact": 5, "broken": True}
