import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile

from app import contact, happened, routing, today
from app.sources import x_follows
from app.store import Store

ROOT = Path(__file__).resolve().parents[1]
LINKS = ["https://x.com/ada_example", "https://github.com/ada-example"]
POSTS = ROOT / "tests" / "fixtures" / "posts"
spec = importlib.util.spec_from_file_location("replay_drill", ROOT / "scripts" / "replay_drill.py")
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


def test_the_drill_cards_each_invented_person_once_and_no_rail_gives_way():
    report = drill.main(["--demo", "--days", "21"])
    cards = [(c["as_of"][:10], c["name"]) for c in report["cards"]]
    assert cards == [("2026-09-06", "Noa Brandt"), ("2026-09-09", "Mara Quill")]
    assert report["rails_failures"] == [] and all(c["rails"] == [] for c in report["cards"])
    assert all(0 < c["lag_hours"] <= 24 for c in report["cards"])  # a daily pull: within a day of the post
    assert report["summary"]["held_person_mornings"]["Sent on <day>"] > 0  # the next mornings, the ledger holds who was carded
    assert report["summary"]["cards_not_replay_clean"] == 0 and all(c["not_replay_clean"] == "" for c in report["cards"])


def test_the_drill_never_writes_to_the_store_it_copies_and_leaves_no_copy(tmp_path, monkeypatch):
    real = Store(f"sqlite:///{tmp_path}/timelines.sqlite")
    routing.seed(real, json.loads((POSTS / "timeline.json").read_text()))
    for entry in json.loads((POSTS / "ledger.json").read_text()):
        contact.record(real, **entry)
    contact.record(real, "person:someone", "identity", at="2026-09-14T00:00:00+00:00", links=LINKS)  # backdated in the copy
    before = sorted(json.dumps(e, sort_keys=True) for e in real.all("contact"))
    monkeypatch.setattr(today, "TIMELINES", tmp_path / "timelines.sqlite")
    monkeypatch.setattr(happened, "PROFILES", tmp_path / "none.json")
    monkeypatch.setattr(x_follows, "FOLLOWS", tmp_path / "none.json")
    made = []
    real_temp = tempfile.TemporaryDirectory

    def temp(*a, **kw):
        made.append(real_temp(*a, **kw))
        return made[-1]

    monkeypatch.setattr(tempfile, "TemporaryDirectory", temp)
    report = drill.main(["--days", "21", "--end", "2026-09-15", "--out", str(tmp_path / "out"), "--sheet", "5",
                         "--team", str(ROOT / "tests/fixtures/routing/team.json"),
                         "--contacts", str(POSTS / "contacts.json")])
    assert len(report["cards"]) == 2  # the drill ran, and recorded both cards, in its copy
    assert sorted(json.dumps(e, sort_keys=True) for e in real.all("contact")) == before
    assert made and not Path(made[0].name).exists()  # the copy is gone
    [json_file] = (tmp_path / "out").glob("drill-2026-09-15-21d-*.json")  # the end, the days and the run's time
    assert json_file.with_name(json_file.stem + "-sheet.md").exists()


def test_an_opt_out_in_the_web_apps_ledger_holds_the_drills_people_as_it_does_route_py(tmp_path, monkeypatch):
    web = Store(f"sqlite:///{tmp_path}/pilot.sqlite")
    contact.record(web, "X902", "never", at="2026-08-01T00:00:00+00:00")  # Noa, in the other ledger
    real = Store(f"sqlite:///{tmp_path}/timelines.sqlite")
    routing.seed(real, json.loads((POSTS / "timeline.json").read_text()))
    for entry in json.loads((POSTS / "ledger.json").read_text()):
        contact.record(real, **entry)
    monkeypatch.setattr(contact, "LEDGERS", {"the web app": tmp_path / "pilot.sqlite",
                                             "the timing run": tmp_path / "timelines.sqlite"})
    monkeypatch.setattr(today, "TIMELINES", tmp_path / "timelines.sqlite")
    monkeypatch.setattr(happened, "PROFILES", tmp_path / "none.json")
    monkeypatch.setattr(x_follows, "FOLLOWS", tmp_path / "none.json")
    report = drill.main(["--days", "21", "--end", "2026-09-15", "--out", str(tmp_path / "out"), "--sheet", "0",
                         "--team", str(ROOT / "tests/fixtures/routing/team.json"),
                         "--contacts", str(POSTS / "contacts.json")])
    assert [c["name"] for c in report["cards"]] == ["Mara Quill"]  # Noa asked, in the web app, not to be contacted
    assert contact.LEDGERS["the timing run"] == tmp_path / "timelines.sqlite"  # put back after the run


def test_follows_count_from_the_day_they_were_read(tmp_path, monkeypatch):
    seen = []
    route = routing.route

    def spy(*a, follows=None, **kw):
        seen.append((a[2][:10], sorted(follows)))
        return route(*a, follows=follows, **kw)

    monkeypatch.setattr(routing, "route", spy)
    monkeypatch.setattr(x_follows, "load", lambda path: {"mts:a": {"read_on": "2026-09-10"}, "mts:b": {}})
    monkeypatch.setattr(happened, "load_profiles", lambda path: {})
    monkeypatch.setattr(today, "TIMELINES", tmp_path / "timelines.sqlite")
    real = Store(f"sqlite:///{tmp_path}/timelines.sqlite")
    routing.seed(real, json.loads((POSTS / "timeline.json").read_text()))
    drill.main(["--days", "3", "--end", "2026-09-11", "--out", str(tmp_path / "out"), "--sheet", "0"])
    assert {day: who for day, who in seen} == {"2026-09-09": [], "2026-09-10": ["mts:a"], "2026-09-11": ["mts:a"]}


def test_a_moment_with_no_card_says_why_and_whether_its_month_began_before_the_drill_or_is_still_open():
    demo = ["--demo", "--fixtures", str(ROOT / "tests/fixtures/demo")]
    open_ = drill.main([*demo, "--days", "21"])
    [lena] = open_["moments"]
    assert (lena["name"], lena["kind"], lena["outcome"]) == ("Lena Okafor", "open_to_work", "still open")
    # Why: her calls over the moment's days the drill saw, each run of mornings once.
    assert [(w["from"], w["to"]) for w in lena["why"]] == [("2026-09-01", "2026-09-09"), ("2026-09-10", "2026-09-15")]
    assert lena["why"][0]["state"].startswith("reach_now, held: Confirm it's them before any reach")
    assert lena["why"][1]["state"].startswith("verify_first")
    done = drill.main([*demo, "--days", "50", "--end", "2026-10-15"])
    assert [m["outcome"] for m in done["moments"]] == ["missed"]  # held to confirm it's them: a miss to fix
    assert [done["summary"][k] for k in ("moments_missed", "moments_held_by_design", "moments_without_a_card")] == [1, 0, 1]
    late = drill.main([*demo, "--days", "20", "--end", "2026-10-15"])  # its 30 days began before the first morning
    assert [m["outcome"] for m in late["moments"]] == ["began before the drill"]


def test_a_moment_two_pages_report_counts_once_with_one_reason(tmp_path):
    shutil.copytree(ROOT / "tests/fixtures/demo", tmp_path / "demo")
    timeline = json.loads((tmp_path / "demo/timeline.json").read_text())
    li = "https://www.linkedin.com/posts/lenaokafor-example_1"
    said = [e for e in timeline["events"] if e["source_url"] == "https://x.com/lenaokafor_example/status/9001"]
    timeline["events"] += [{**e, "event_type": "linkedin_post" if e["event_type"] == "x_post" else e["event_type"],
                            "source_url": li, "quote": e["quote"].replace("I'm looking", "Now looking")}
                           for e in said]  # the same news, on LinkedIn the same day
    (tmp_path / "demo/timeline.json").write_text(json.dumps(timeline))
    [lena] = drill.main(["--demo", "--fixtures", str(tmp_path / "demo"), "--days", "21"])["moments"]
    assert lena["kind"] == "open_to_work" and set(lena["urls"]) == {said[0]["source_url"], li}
    assert lena["reason"] == "miss: who they are is not confirmed"  # held to confirm it's them, then check first


def test_a_moment_a_later_morning_dates_differently_is_the_same_moment():
    from types import SimpleNamespace as H
    moments = {("p", "job_change", "2026-09-12"): {"date": "2026-09-12", "event_ids": ["e1"],
                                                   "urls": ["https://x.test/1"]}}
    same = moments[("p", "job_change", "2026-09-12")]
    assert drill.same_moment(moments, "p", H(kind="job_change", date="2026-09", event_id="e1", source_url="")) is same
    assert drill.same_moment(moments, "p", H(kind="job_change", date="2026-10-31", event_id="e9",
                                             source_url="https://x.test/1")) is same  # the day it names, same page
    assert drill.same_moment(moments, "p", H(kind="job_change", date="2026-09", event_id="e7", source_url="")) is same
    assert drill.same_moment(moments, "p", H(kind="job_change", date="2026-09-20", event_id="e7", source_url="")) is None
    assert drill.same_moment(moments, "p", H(kind="paper", date="2026-09-12", event_id="e1", source_url="")) is None
    assert drill.same_moment(moments, "q", H(kind="job_change", date="2026-09-12", event_id="e1", source_url="")) is None


def test_each_uncarded_moment_gets_one_reason_by_design_or_a_miss():
    def why(*states):
        return [{"from": "2026-09-01", "to": "2026-09-01", "state": s} for s in states]

    assert drill.reason(why("watch_until 2026-10-01"), carded_before=True) == "by design: already on a card"
    assert drill.reason(why("reach_now, held: Carded on 2026-06-24: one card per person, ever."), False) == "by design: already on a card"
    assert drill.reason(why("watch_until 2027-01-01 (holds: conflicting_role_claims, short_tenure)"), False) == \
        "by design: first-year hold after a job move"
    assert drill.reason(why("reach_now, held: The draft fails a check (x)"), False) == "miss: the draft failed its checks"
    assert drill.reason(why("reach_now, held: Confirm it's them before any reach"), False) == \
        "miss: who they are is not confirmed"
    assert drill.reason(why("verify_first (holds: announced_next_role)"), False) == \
        "by design: held (announced_next_role)"  # the call's own check, not who they are
    assert drill.reason(why("verify_first"), False) == "miss: never reach now (verify_first)"
    assert drill.reason(why("reach_now, held: Followed up on 2026-08-01 with no reply: quiet until 2026-10-30."),
                        False) == "by design: already on a card"
    assert drill.reason(why("quiet (holds: hold_not_looking)"), False) == "by design: held (hold_not_looking)"
    assert drill.reason(why("reach_now, held: Past this week's cap of 4 cards for the role."), False) == \
        "by design: Past this week's cap of 4 cards for the role."
    assert drill.reason(why("reach_now, held: They asked us to wait until 2026-10-01."), False) == \
        "by design: They asked us to wait until <day>."
    assert drill.reason(why("no call (nothing of theirs in view)", "watch_until 2026-10-01", "watch_until 2026-10-02"),
                        False) == "miss: never reach now (watch_until)"
    assert drill.reason([], False) == "miss: no morning in the drill saw it"


def two_sites(tmp_path):
    """The demo people, with Lena on two sites, so an identity record could tie her."""
    shutil.copytree(ROOT / "tests/fixtures/demo", tmp_path / "demo")
    timeline = json.loads((tmp_path / "demo/timeline.json").read_text())
    for ctx in timeline["contexts"]:
        if ctx["subject_id"] == "D904":
            ctx["anchors"]["github"] = "https://github.com/lenaokafor-example"
    (tmp_path / "demo/timeline.json").write_text(json.dumps(timeline))
    return ["--demo", "--fixtures", str(tmp_path / "demo"), "--days", "21"]


def checks(monkeypatch, passes, problems=()):
    """Every draft passes its checks, or fails them with these problems."""
    real = routing.outreach.check
    monkeypatch.setattr(routing.outreach, "check",
                        lambda *a, **kw: {**real(*a, **kw), "passes": passes, "problems": list(problems)})


def test_a_draft_that_fails_a_check_is_never_on_the_sheet_and_the_report_says_which_check(monkeypatch, tmp_path):
    # Lena is held only to confirm who she is, but her draft fails a check, so she is not on the sheet.
    checks(monkeypatch, False, ["a made-up problem"])
    report = drill.main(two_sites(tmp_path))
    assert report["held_only_for_identity"] == [] and report["summary"]["people_ever_reach_now"] == 2
    posts = drill.main(["--demo", "--days", "21"])
    assert posts["cards"] == [] and posts["summary"]["draft_check_problems"] == {"a made-up problem": 2}
    # Each held draft once per person and text, with what it said and the check it failed, to read by hand.
    held = posts["held_drafts"]
    assert sorted(d["name"] for d in held) == sorted({d["name"] for d in held}) and len(held) == 2
    assert all(d["failed"] == ["a made-up problem"] and d["body"].startswith("Hey ") and d["mornings"] >= 1
               and d["first"] <= d["last"] for d in held)
    assert sum(d["mornings"] for d in held) == sum(n for why, n in posts["summary"]["held_person_mornings"].items()
                                                   if why.startswith("The draft fails a check"))


def test_a_reach_held_only_to_confirm_who_they_are_goes_on_the_sheet_blind_to_why(monkeypatch, tmp_path):
    checks(monkeypatch, True)
    report = drill.main(two_sites(tmp_path))
    assert [c["name"] for c in report["held_only_for_identity"]] == ["Lena Okafor"]
    assert report["held_only_for_identity"][0]["held"].startswith("Confirm it's them")
    assert report["summary"]["held_only_for_identity"] == 1
    sheet = drill.rating_sheet(report["cards"] + report["held_only_for_identity"], 20)
    assert "## 1. Lena Okafor" in sheet and "Confirm it's them" not in sheet


def test_one_site_on_file_is_counted_and_a_record_tying_it_to_another_site_would_clear_it(monkeypatch):
    checks(monkeypatch, True)
    report = drill.main(["--demo", "--fixtures", str(ROOT / "tests/fixtures/demo"), "--days", "21"])
    assert [c["name"] for c in report["held_only_for_identity"]] == ["Lena Okafor"]  # read on X alone
    assert report["summary"]["held_for_identity_with_one_site"] == 1


def real_run(tmp_path, monkeypatch):
    """The invented people in a store of their own, run as the Mac runs the drill (not --demo), writing to out/."""
    real = Store(f"sqlite:///{tmp_path}/timelines.sqlite")
    routing.seed(real, json.loads((POSTS / "timeline.json").read_text()))
    for entry in json.loads((POSTS / "ledger.json").read_text()):
        contact.record(real, **entry)
    monkeypatch.setattr(today, "TIMELINES", tmp_path / "timelines.sqlite")
    monkeypatch.setattr(happened, "PROFILES", tmp_path / "none.json")
    monkeypatch.setattr(x_follows, "FOLLOWS", tmp_path / "none.json")
    return lambda: drill.main(["--days", "21", "--end", "2026-09-15", "--out", str(tmp_path / "out"), "--sheet", "5",
                               "--team", str(ROOT / "tests/fixtures/routing/team.json"),
                               "--contacts", str(POSTS / "contacts.json")])


def test_a_run_never_overwrites_an_earlier_runs_files_and_says_what_changed(tmp_path, monkeypatch, capsys):
    stamp = "drill-2026-09-15-21d-20260924T030000Z"
    (tmp_path / f"{stamp}.json").write_text("the earlier run")
    (tmp_path / f"{stamp}-2-sheet.md").write_text("another earlier run")
    assert drill.claim(tmp_path, stamp) == tmp_path / f"{stamp}-3"
    assert drill.claim(tmp_path, "drill-2026-09-15-42d-20260924T030000Z").name == "drill-2026-09-15-42d-20260924T030000Z"
    run = real_run(tmp_path, monkeypatch)
    first = run()
    assert "No earlier run of the same people and mornings in this folder to compare with." in capsys.readouterr().out
    second = run()
    out = capsys.readouterr().out
    assert len(list((tmp_path / "out").glob("*.json"))) == 2 and len(list((tmp_path / "out").glob("*-sheet.md"))) == 2
    assert "Cards: 2 to 2. Gained: none. Lost: none.\n  Holds, in person-mornings: no change." in out
    assert f"This run: {second["commit"]}, " in out and first["commit"] == second["commit"]  # "unknown" without git


def test_what_changed_names_the_people_gained_lost_and_moved_and_the_holds_that_moved():
    def report(cards, held, carded, uncarded, early=None, split=None):
        return {"end": "2026-09-23", "run_at": "2026-09-24T03:00:00+00:00", "commit": "af229da", "seconds": 40,
                "cards": [{"name": n, "as_of": f"2026-{d}T13:00:00+00:00"} for n, d in cards],
                "summary": {"mornings": 120, "cards": len(cards), "held_person_mornings": held,
                            "moments_carded": carded, "moments_without_a_card": uncarded,
                            **({} if early is None else {"moments_carded_early": early}),
                            **({} if split is None else {"moments_missed": split[0], "moments_held_by_design": split[1]})}}
    before = report([("Ada", "06-24"), ("Bo", "07-27")], {"Identity": 96, "Sent on <day>": 18}, 7, 10, early=1,
                    split=(6, 4))
    after = report([("Bo", "07-20"), ("Cy", "07-07")], {"Identity": 0, "Sent on <day>": 18, "Stale": 3}, 9, 8, early=2,
                   split=(1, 7))
    lines = drill.changes(before, after)
    assert lines[1] == "Since the last run (2026-09-24 03:00, af229da, 120 mornings to 2026-09-23):"
    assert lines[2] == "  Cards: 2 to 2. Gained: Cy (07-07). Lost: Ada (06-24). Moved: Bo (07-27 to 07-20)."
    assert lines[3] == "  Holds, in person-mornings: Identity: 96 to 0; Stale: 0 to 3."
    assert lines[4] == "  Public moments: 7 carded to 9, 1 carded early to 2, 6 missed to 1, 4 held by design to 7."
    assert lines[5] == "This run: af229da, 40 s, 120 mornings to 2026-09-23."
    unsplit = report([("Ada", "06-24")], {}, 7, 10, early=1)  # a run from before a hold by design was told apart
    assert drill.changes(unsplit, after)[4] == ("  Public moments: 7 carded to 9, 1 carded early to 2, 10 with no card "
                                                "to 8 (now 1 missed and 7 held by design; the earlier run did not split "
                                                "them).")
    old = report([("Ada", "06-24")], {}, 7, 10)  # a run from before "carded" meant a card that cites the moment
    assert drill.changes(old, after)[4].startswith("  Public moments: 7 carded to 9 (the earlier run counted any card "
                                                   "near a moment, not one that cites it), 2 carded early, 10 with no "
                                                   "card to 8 (now 1 missed")


def test_what_changed_compares_only_with_a_run_of_the_same_people_and_mornings(tmp_path):
    def run(name, people, file="__none__", end="2026-09-23", mornings=120):
        report = {"end": end, "summary": {"mornings": mornings, "people": people, "cards": 0}}
        report |= {} if file == "__none__" else {"people_file": file}
        (path := tmp_path / f"drill-{name}.json").write_text(json.dumps(report))
        written = len(list(tmp_path.glob("*.json")))
        os.utime(path, (written, written))  # written in this order, whatever the clock's resolution
        return report

    this = {"end": "2026-09-23", "people_file": None, "summary": {"mornings": 120, "people": 38}}
    run("old", 38)  # from before the people file was kept: it compares on the counts alone
    assert drill.latest(tmp_path, this)["file"] == "drill-old.json"
    run("watchlist", 16, "research/private/early-signals/people.json")
    run("shorter", 38, None, mornings=42)
    run("earlier-end", 38, None, end="2026-09-15")
    assert drill.latest(tmp_path, this)["file"] == "drill-old.json"  # never the watchlist, fewer mornings or another end
    run("same", 38, None)
    assert drill.latest(tmp_path, this)["file"] == "drill-same.json"
    assert drill.latest(tmp_path, {**this, "people_file": "other.json"})["file"] == "drill-old.json"
    assert drill.latest(tmp_path, {**this, "summary": {"mornings": 120, "people": 5}}) is None


def test_held_only_for_identity_means_the_gate_clears_once_identity_is_confirmed(monkeypatch, tmp_path):
    checks(monkeypatch, True)
    real = contact.check

    def stale_too(history, now, *a, **kw):  # a second ask the gate would make once identity is confirmed
        found = real(history, now, *a, **kw)
        if found.state == "clear" and any(e["kind"] == "identity" for e in history):
            return contact.Clearance(state="check_first", reason="The newest sign is over three weeks old")
        return found

    monkeypatch.setattr(contact, "check", stale_too)
    assert drill.main(two_sites(tmp_path))["held_only_for_identity"] == []


def test_the_sheet_holds_each_persons_first_card_and_prefers_a_card_to_one_held():
    card = {"subject_id": "mts:ada", "name": "Ada", "role": "mts", "as_of": "2026-09-10T13:00:00+00:00", "trigger": [],
            "why_now": "", "channel": "email", "sender": "Noor", "subject_line": "", "draft": "Hey Ada", "route_in": {},
            "held": ""}
    sheet = drill.rating_sheet([{**card, "as_of": "2026-09-01T13:00:00+00:00", "held": "Confirm it's them"},
                                card, {**card, "as_of": "2026-09-20T13:00:00+00:00"}], 20)
    assert sheet.count("## ") == 1 and "as of 2026-09-10" in sheet


def test_a_card_resting_on_a_repo_or_quoting_its_description_is_flagged_as_not_replay_clean():
    repo = "https://github.com/ada-example/tidewire"
    def row(i, kind, quote, at, url=repo):
        return {"id": i, "subject_id": "mts:ada", "event_type": kind, "quote": quote, "source_url": url,
                "source_version_hash": None, "observed_at": at}
    rows = {e["id"]: e for e in [
        row("repo", "github_repo", "Created ada-example/tidewire: A renderer for game clips (Rust)", "2026-05-22T10:00:00Z"),
        row("tag", "work_in_progress", "A renderer for game clips", "2026-05-22T10:00:00Z"),  # read from the repo
        row("push", "github_activity", "Pushed to ada-example/tidewire", "2026-09-01T10:00:00Z"),
        row("wip", "work_in_progress", "Pushed to ada-example/tidewire", "2026-09-01T10:00:00Z"),  # read from the push
        row("post", "x_post", "Shipping tidewire's clip decoder this week", "2026-09-03T10:00:00Z",
            "https://x.com/ada_example/status/1"),
        row("star", "github_star", "Starred someone/engine", "2026-09-04T10:00:00Z", "https://github.com/someone/engine")]}
    repos, as_of = [rows["repo"]], "2026-09-10T13:00:00+00:00"

    def card(cites, body="Hey Ada, saw your tidewire commits on GitHub.", model_cites=(), listed=()):
        call = type("Call", (), {"evidence": {"work_in_progress": [*cites, *listed]}})()
        return type("Result", (), {"subject_id": "mts:ada", "call": call, "signals": [{"id": i} for i in cites],
                                   "outreach": {"body": body, "cites": list(model_cites)}})()

    assert drill.replay_clean(card(["tag"]), as_of, rows, repos) == drill.ON_A_REPO  # dated by the repo's creation
    assert drill.replay_clean(card(["post"], listed=["tag"]), as_of, rows, repos) == ""  # listed, not shown: no weight
    assert drill.replay_clean(card(["post", "tag"]), as_of, rows, repos) == drill.ON_A_REPO  # shown second, still shown
    assert drill.replay_clean(card(["post"], model_cites=["repo"]), as_of, rows, repos) == drill.ON_A_REPO
    quoted = 'Hey Ada, saw your tidewire commits on GitHub. "A renderer for game clips" really caught my eye.'
    assert drill.replay_clean(card(["wip"], quoted), as_of, rows, repos) == drill.REPO_WORDS  # today's description
    spaced = [{**rows["repo"], "quote": "Created ada-example/tidewire: A renderer for\u00a0game  clips (Rust)"}]
    assert drill.replay_clean(card(["wip"], quoted), as_of, rows, spaced) == drill.REPO_WORDS  # however it is spaced
    assert drill.replay_clean(card(["wip"], "Hey Ada, congrats on the new paper!"), as_of, rows, repos) == ""
    for cites in (["wip"], ["post"], ["star"], []):
        assert drill.replay_clean(card(cites), as_of, rows, repos) == ""  # dated when done, no description quoted
    assert drill.replay_clean(card(["tag"], quoted), "2026-05-01T13:00:00+00:00", rows, repos) == ""  # not yet made


def test_the_report_counts_the_cards_a_replay_cant_vouch_for_and_the_sheet_marks_them(monkeypatch):
    monkeypatch.setattr(drill, "replay_clean", lambda result, as_of, rows, repos: drill.ON_A_REPO)
    report = drill.main(["--demo", "--days", "21"])
    assert report["summary"]["cards_not_replay_clean"] == len(report["cards"]) == 2
    sheet = drill.rating_sheet(report["cards"], 20)
    assert sheet.count(f"*{drill.ON_A_REPO}: a replay can't vouch for it.*") == 2


def test_the_drill_hides_a_replay_cases_posts_to_gis_own_account_never_a_watched_persons(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/t.sqlite")
    to_gi = "Congrats on the round!\n\nIn reply to @gen_intuition: Our round"
    routing.seed(store, {"contexts": [], "events": [
        {"subject_id": sid, "event_type": kind, "event_date": "2026-06-25", "source_url": f"https://x.com/{sid}/1",
         "quote": to_gi} for sid in ("case", "watched") for kind in ("x_post", "technical_ask")]})
    people = tmp_path / "people.json"
    people.write_text(json.dumps([
        {"person_id": "case", "role": "mts-research", "since": "2025-07-01", "until": "2026-07-06",
         "moments": [{"date": "2026-07-06", "kind": "job", "source_url": "https://x.com/gen_intuition/status/1"}]},
        {"person_id": "watched", "role": "mts-research", "since": "2025-07-01", "until": "2026-09-24"}]))
    hidden = drill.replay_cases_to_gi(store, people)
    assert {e["subject_id"] for e in drill.Cached(store, hidden).all("timeline_event")} == {"watched"}
    assert drill.replay_cases_to_gi(store, tmp_path / "none.json") == frozenset()


def test_a_card_is_a_rails_failure_when_carded_twice_or_with_no_identity():
    store = Store("sqlite:///:memory:")
    result = type("Result", (), {"subject_id": "mts:ada", "role": {"id": "mts"},
                                 "call": type("Call", (), {"holds": []})()})()
    as_of = "2026-09-10T13:00:00+00:00"
    assert drill.rails(result, store, as_of, {}, []) == ["no identity entry"]
    contact.record(store, "mts:ada", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    assert drill.rails(result, store, as_of, {contact.person_key("mts:ada"): "2026-09-03"}, []) == ["carded twice (first on 2026-09-03)"]
    week = [{"role": "mts"}] * contact.WEEKLY_CAP
    assert drill.rails(result, store, as_of, {}, week) == [f"over {contact.WEEKLY_CAP} cards for the role in 7 days"]
    week = [{"role": "backend"}] * contact.TOTAL_CAP
    assert drill.rails(result, store, as_of, {}, week) == [f"over {contact.TOTAL_CAP} cards across roles in 7 days"]


def test_the_rating_sheet_leaves_off_the_engines_confidence_and_label():
    cards = drill.main(["--demo", "--days", "21"])["cards"]
    sheet = drill.rating_sheet(cards, 20)
    assert sheet == drill.rating_sheet(cards, 20)  # seeded: the same order every time
    assert sheet.count("**Your call:**") == len(cards)
    for word in ("score", "confidence", "Early sign", "Just happened", "early_sign"):
        assert word not in sheet.split("don't sway you.", 1)[1], word


def test_a_sheet_card_for_someone_now_on_gis_team_is_marked_a_replay(tmp_path, monkeypatch):
    team = json.loads((ROOT / "tests/fixtures/routing/team.json").read_text())
    team["members"].append({"name": "Mara Quill", "title": "GI: MTS", "x_handle": "MaraQuill_Example"})
    (tmp_path / "team.json").write_text(json.dumps(team))
    real_run(tmp_path, monkeypatch)
    report = drill.main(["--days", "21", "--end", "2026-09-15", "--out", str(tmp_path / "out"), "--sheet", "5",
                         "--team", str(tmp_path / "team.json"), "--contacts", str(POSTS / "contacts.json")])
    assert {c["name"]: c["on_gi_team"] for c in report["cards"]} == {"Noa Brandt": False, "Mara Quill": True}
    sheet = next((tmp_path / "out").glob("*-sheet.md")).read_text()
    mara = sheet[sheet.index("Mara Quill"):]
    assert mara.split("\n")[2] == f"*{drill.ON_TEAM}*" and sheet.count(drill.ON_TEAM) == 1
    lab = [{"name": "Someone Else", "source_url": "https://noabrandt.example/"}]  # a member's page that is not a profile
    assert not drill.on_team(Store(f"sqlite:///{tmp_path}/timelines.sqlite"), "X902", lab)



def test_with_redraft_the_model_redoes_a_failing_free_draft_within_the_drills_cap(monkeypatch):
    from app import providers

    checks(monkeypatch, False, ["a made-up problem"])  # every free draft fails
    budget, written, given = providers.Budget({"max_calls_per_run": 30}), [], set()

    def drafter(settings, spend, store, model, as_of, max_usd=None, leave_out=()):
        given.add((max_usd, leave_out))

        def draft(name, *a, **kw):
            if max_usd < 0.5:  # outreach.write's cap: a draft whose worst case doesn't fit is never asked for
                raise drill.outreach.OverCap(f"this run's cap (${max_usd:.2f}, 30 calls) is spent")
            spend.take()
            written.append((name, as_of[:10]))
            return {"subject": "s", "body": f"Hey {name}", "note": "", "cites": [], "model": model,
                    "checks": {"passes": True, "problems": [], "name_swap": {"anchors": []}}}
        return draft

    monkeypatch.setattr(drill.outreach, "drafter", drafter)
    monkeypatch.setattr(drill, "writer_for", lambda args: ({"anthropic_key": "k"}, budget))
    report = drill.main(["--demo", "--days", "21", "--redraft"])
    assert [(c["as_of"][:10], c["name"]) for c in report["cards"]] == [("2026-09-06", "Noa Brandt"),
                                                                         ("2026-09-09", "Mara Quill")]
    assert written and report["summary"]["model_drafts"]["calls"] == len(written) == budget.used
    assert given == {(1.0, ("diamond",))}  # one cap for the whole drill, and never the DIAMOND fact
    assert report["summary"]["model_drafts"]["cap_is"] == "per drill"
    # Past the drill's cap nothing more is tried, and the failing drafts stay held.
    written.clear()
    monkeypatch.setattr(drill, "writer_for", lambda args: ({"anthropic_key": "k"}, providers.Budget({})))
    capped = drill.main(["--demo", "--days", "21", "--redraft", "--max-usd", "0.10"])
    assert capped["cards"] == [] and not written
    assert capped["summary"]["model_drafts"]["not_tried"][0].endswith("not tried: this run's cap ($0.10, 30 calls) is spent")


def test_a_model_error_holds_that_card_and_the_drill_carries_on(monkeypatch):
    from app import providers

    checks(monkeypatch, False, ["a made-up problem"])

    def drafter(settings, spend, store, model, as_of, max_usd=None, leave_out=()):
        def draft(name, *a, **kw):
            spend.take()
            if name == "Noa Brandt":  # a rate limit, a server error or a refused answer: what it spent still counts
                raise providers.ProviderError("Anthropic returned HTTP 529. Try again later.")
            return {"subject": "s", "body": f"Hey {name}", "note": "", "cites": [], "model": model,
                    "checks": {"passes": True, "problems": [], "name_swap": {"anchors": []}}}
        return draft

    monkeypatch.setattr(drill.outreach, "drafter", drafter)
    monkeypatch.setattr(drill, "writer_for", lambda args: ({"anthropic_key": "k"}, providers.Budget({})))
    report = drill.main(["--demo", "--days", "21", "--redraft"])
    assert [(c["as_of"][:10], c["name"]) for c in report["cards"]] == [("2026-09-09", "Mara Quill")]
    failed = report["summary"]["model_drafts"]["failed"]
    assert failed and all(f.endswith("Noa Brandt's model draft failed, so the card stays held: Anthropic returned "
                                      "HTTP 529. Try again later.") for f in failed)


def test_redraft_refuses_a_model_its_cap_cannot_price(capsys):
    import pytest

    with pytest.raises(SystemExit):
        drill.main(["--demo", "--days", "21", "--redraft", "--model", "claude-other"])
    assert drill.outreach.UNPRICED in capsys.readouterr().err


def test_with_redraft_and_no_model_key_the_drill_runs_free_and_says_so(monkeypatch, capsys):
    import sys
    import types

    from app import providers

    posts = types.ModuleType("posts")
    posts._paid_settings = lambda calls, model: ({"anthropic_key": ""}, providers.Budget({}))
    monkeypatch.setitem(sys.modules, "posts", posts)
    report = drill.main(["--demo", "--days", "21", "--redraft"])
    assert report["summary"]["model_drafts"] == "off: " + drill.NO_KEY and len(report["cards"]) == 2
    assert capsys.readouterr().out.count("Model drafts are off") == 1
