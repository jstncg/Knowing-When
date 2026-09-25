"""Drop in a JD, get a role the engine runs: preview (two model calls, nothing written), accept (config/roles.json and
the role's spec, where every path reads roles), then the engine's call and card for people listed with it."""

import asyncio
import json
import re
import shutil

import pytest
from fastapi.testclient import TestClient

from app import contact, extraction, journey, main, pay, readiness, role_compiler as rc, role_drop, routing, today
from app import scorecard as sc
from app.store import Store

SAMPLE = role_drop.SAMPLE
JD = (SAMPLE / "jd.txt").read_text()
AS_OF = "2026-09-15T23:59:59+00:00"
ID = "data-engineer"


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A copy of config/ in place of the real one for every reader of roles and specs, so a test never touches the
    tracked files (a reader left out here would read the real ones and miss the dropped role)."""
    copy = tmp_path / "config"
    shutil.copytree(role_drop.ROLES.parent, copy)
    for module, name, path in ((role_drop, "ROLES", copy / "roles.json"), (rc, "SPECS", copy / "role-specs"),
                               (routing, "ROLES", copy / "roles.json"), (sc, "ROLES", copy / "roles.json"),
                               (extraction, "ROLES", copy / "roles.json"), (extraction, "SPECS", copy / "role-specs"),
                               (contact, "CONFIG", copy)):
        monkeypatch.setattr(module, name, path)
    return copy


def answer(model):
    """The sample JD's hand-written answers, as the model's replies: the brief, then the spec."""
    for name in ("strategy", "compiled"):
        model.reply(json.loads((SAMPLE / f"{name}.json").read_text()))


def people(store):
    """Two invented people listed for the dropped role: one who says what they are building on its topics, and one
    who says the same a month into a new job."""
    routing.seed(store, {"contexts": [
        {"subject_id": f"{ID}:ada", "name": "Ada Lind", "role": ID, "anchors": {"x": "https://x.com/adalind_example"}},
        {"subject_id": f"{ID}:bo", "name": "Bo Sato", "role": ID, "anchors": {"x": "https://x.com/bosato_example"}},
    ], "events": [
        *({"subject_id": f"{ID}:{who}", "event_type": kind, "event_date": "2026-09-10", "quote": quote,
           "source_url": f"https://x.com/{who}_example/status/1"}
          for who in ("ada", "bo") for kind, quote in (
              ("x_post", "Been hacking on a Ray pipeline that labels gameplay video clips for training. Early "
                         "results: 3x faster than our Spark jobs."),
              ("work_in_progress", "Been hacking on a Ray pipeline that labels gameplay video clips for training."))),
        {"subject_id": f"{ID}:bo", "event_type": "job_started", "event_date": "2026-08-10",
         "quote": "Started as a data engineer at Streamly", "source_url": "https://x.com/bo_example/status/0"},
    ]})


def test_a_dropped_jd_becomes_a_role_the_engine_runs(config, model, settings, store, tmp_path, capsys):
    answer(model)
    previewed = asyncio.run(role_drop.preview(JD, settings))
    assert len(model.calls) == 2  # the brief, then the spec: no repair was needed
    assert previewed["clash"] == "" and previewed["role"]["id"] == ID and not previewed["sample"]
    spec = rc.RoleConfig.model_validate(previewed["spec"])  # the manifest's parts only
    assert {d.id for d in spec.detectors} <= set(rc.DETECTOR_FEEDS)
    signs = {s["means"] for s in previewed["watch"]["signs"]}
    assert "Layoffs at their employer: a WARN notice, or a headline that reports them." in signs
    assert not signs & {d["means"] for d in rc.manifest()["detectors"] if d["id"] in readiness.CONTEXT_ONLY}  # selected, never a reason
    assert "gameplay video datasets" in previewed["watch"]["topics"]
    assert "Authorized referral records" in previewed["watch"]["gaps"]
    assert previewed["cost"]["input_tokens"] == 10  # the stub's usage for the brief's call
    assert rc.watched(ID) is None and ID not in contact.role_prefixes()  # nothing written yet

    role = role_drop.accept(previewed, locations=["New York City"])
    assert role["jd_url"] == "" and role["locations"] == ["New York City"] and role["implementation_stage"] == "dropped"
    assert rc.watched(ID) == {d.id for d in spec.detectors}
    assert contact.person_key(f"{ID}:ada") == "ada"  # the role list reads again when config changes

    people(store)
    ada, bo = (journey.load_context(store, f"{ID}:{who}") for who in ("ada", "bo"))
    call = journey.assess(store, ada.subject_id, AS_OF, ID)
    assert call.action == "reach_now"
    assert journey.assess(store, bo.subject_id, AS_OF, ID).action == "watch_until"  # a first year in the job holds
    shown = today.ping(store, ada, call, AS_OF, [], [])
    assert shown["role"] == {"id": ID, "title": "Data Engineer", "jd_url": "", "pay": pay.NONE}
    assert shown["draft"]["body"] and "Data Engineer role" in shown["draft"]["body"]
    assert "()" not in shown["draft"]["body"]  # no post's link, so none in brackets

    listed = tmp_path / "people.json"
    listed.write_text(json.dumps([{"subject_id": f"{ID}:ada", "role": ID, "since": "2026-01-01"},
                                  {"subject_id": "x:cy", "role": "no-such-role", "since": "2026-01-01"}]))
    assert [p.subject_id for p in sc.load_moments(listed)] == [f"{ID}:ada"]  # the unknown role is left out, not fatal
    assert "no-such-role" in capsys.readouterr().err

    assert role_drop.remove(ID)["id"] == ID
    assert rc.watched(ID) is None and ID not in {r["id"] for r in role_drop.roles()}
    assert (config / "roles.json").read_bytes() == (main.ROOT / "config/roles.json").read_bytes()  # its own format


@pytest.mark.parametrize("change", [
    {"criteria": []}, {"criteria": "Strong Python and SQL"}, {"criteria": [{"text": "SQL"}]},
    {"required_criteria": ["Invented criterion"]}, {"required_criteria": "Strong Python and SQL"},
    {"title": "Data Wrangler"}, {"title": 7}, {"jd_sha256": None}, {"locations": "New York City"}, {"preview": []},
])
def test_a_malformed_role_is_refused_before_anything_is_written(config, change):
    previewed = role_drop.sample() | {"sample": False}
    before = role_drop.ROLES.read_bytes()
    offices, preview = change.pop("locations", ["New York City"]), change.pop("preview", None)
    with pytest.raises(ValueError):
        role_drop.accept({**previewed, "role": {**previewed["role"], **change}} if preview is None else preview,
                         locations=offices)
    assert role_drop.ROLES.read_bytes() == before and not rc.spec_path(ID).exists()


def test_the_entry_is_built_from_the_role_fields_only(config):
    previewed = role_drop.sample() | {"sample": False}
    role = role_drop.accept({**previewed, "role": {**previewed["role"], "manager_preferences": "x", "extra": 1,
                                                    "lane": "wizardry", "required_criteria": []}})
    assert "extra" not in role and role["manager_preferences"] == [] and role["lane"] == "other"
    main.seed_role(dict(role))  # the web app's copy takes it, even with fewer than two required criteria


def test_the_preview_never_says_time_in_a_seat_counts(config, model):
    previewed = role_drop.sample()  # its spec selects the tenure and retention-cliff detectors, which readiness only lists
    assert {"tenure_milestone", "retention_cliff"} <= {d["id"] for d in previewed["spec"]["detectors"]}
    shown = {s["means"] for s in previewed["watch"]["signs"]}
    assert not shown & {d["means"] for d in rc.manifest()["detectors"] if d["id"] in readiness.CONTEXT_ONLY}
    assert "never a reason" in previewed["watch"]["every_role"]


def test_a_sign_that_cannot_fire_for_the_role_never_shows_as_counting():
    """A sale role's spec, as a real read returned one: it selected a sign no source reads (someone close to them
    joined GI) and the quarter-close hold, which holds only people with a finance title."""
    spec = role_drop.sample()["spec"]
    spec = {**spec, "detectors": [*spec["detectors"],
                                  {"id": "associate_joined", "reason": "A former co-seller joining GI is a route."},
                                  {"id": "calendar_quiet_close", "reason": "Quota quarter-close is the seller's busy window."}],
            "gaps": [*spec["gaps"], rc.DETECTOR_FEEDS["associate_joined"].gap()]}
    means = {d["id"]: d["means"] for d in rc.manifest()["detectors"]}
    sale = role_drop.watch(spec, {"title": "Founding Account Executive", "lane": "other"})
    finance = role_drop.watch(spec, {"title": "Head of Finance", "lane": "other"})  # the title says finance
    assert role_drop.watch(spec, {"title": "Controller", "lane": "finance-operations"})["signs"] == finance["signs"]
    assert not {s["means"] for s in sale["signs"]} & {means["associate_joined"], means["calendar_quiet_close"]}
    assert f"{means['associate_joined']} No source reads it yet." in sale["gaps"]
    assert not [g for g in sale["gaps"] if "(associate_joined)" in g]  # its meaning, not the manifest's id
    assert any("finance title" in g for g in sale["gaps"])  # nothing holds at a seller's quarter end: said, not shown
    assert means["calendar_quiet_close"] in {s["means"] for s in finance["signs"]}
    assert not any("finance title" in g for g in finance["gaps"])
    unlisted = role_drop.watch({**spec, "gaps": []}, {"title": "Founding Account Executive", "lane": "other"})
    assert f"{means['associate_joined']} No source reads it yet." in unlisted["gaps"]  # said even when the spec left it out


def test_what_is_missing_before_calls_is_said_in_plain_words(config):
    gi = next(r for r in role_drop.roles() if r["id"] == "backend")["jd_url"]
    said = {link: " ".join(role_drop.before({"jd_url": link})) for link in
            ("", gi, "https://jobs.ashbyhq.com/featherlessai/9fea9bef-1f32-4659-8042-7a8f5ba324a1")}
    for text in said.values():
        assert not re.search(r"signs_for|pay_bands|\.py\b|golden|pre-registered|unvalidated|worked test|replay", text), text
        assert "signed Justin" in text  # the default sender, by name
    assert "no link" in said[""] and "next time" in said[gi]
    assert "only from GI's own job posts" in said["https://jobs.ashbyhq.com/featherlessai/9fea9bef-1f32-4659-8042-7a8f5ba324a1"]
    assert not re.search(r"repairs|token caps|list price", role_drop.PRICE)
    assert "forced" not in role_drop.EVERY_ROLE and "config" not in role_drop.clash(
        {"id": "backend", "title": "Backend", "jd_url": ""})
    assert role_drop.watch({"detectors": [], "gaps": ["gap: needs adapter for conference_deadline events "
                                                      "(calendar_quiet_conference)"]}, {})["gaps"] == [
        "Conference deadline events"]  # a model's gap line, without its ids, starting as a sentence does
    gaps = ["referrals (internal)", "posts by @some_handle", "arXiv preprints", "iOS builds", "github.com stars"]
    assert role_drop.watch({"detectors": [], "gaps": gaps}, {})["gaps"] == [
        "Referrals (internal)", "Posts by @some_handle", "arXiv preprints", "iOS builds", "github.com stars"]  # names keep their case


def test_the_preview_never_says_a_pattern_from_their_past_moves_counts():
    """A founding AE's spec, as a real read returned one, selected the sign that the events before someone's earlier
    moves are recurring: a move predicted from their own past, which readiness only lists."""
    spec = role_drop.sample()["spec"]
    spec = {**spec, "detectors": [*spec["detectors"],
                                  {"id": "own_precedent", "reason": "A seller's past moves followed a pattern now recurring."}]}
    means = next(d["means"] for d in rc.manifest()["detectors"] if d["id"] == "own_precedent")
    sale = role_drop.watch(spec, {"title": "Founding Account Executive", "lane": "other"})
    assert means not in {s["means"] for s in sale["signs"]}
    assert f"{readiness.label('own_precedent')} are never a reason" in sale["every_role"]


def test_the_preview_never_offers_a_path_like_past_hires_as_counting_once_a_source_reads_it():
    """A spec that selected the path like people GI hired, and named the source it lacks. Failure modes: it shows as a
    sign; its gap says no source reads it yet, as if it would count once one did; the line for every role leaves it
    out of what is never a reason."""
    spec = role_drop.sample()["spec"]
    spec = {**spec, "detectors": [*spec["detectors"], {"id": "similar_path", "reason": "Looks like our past hires."}],
            "gaps": [*spec["gaps"], rc.DETECTOR_FEEDS["similar_path"].gap()]}
    means = next(d["means"] for d in rc.manifest()["detectors"] if d["id"] == "similar_path")
    sale = role_drop.watch(spec, {"title": "Founding Account Executive", "lane": "other"})
    assert means not in {s["means"] for s in sale["signs"]}
    assert not [g for g in sale["gaps"] if means in g or "similar" in g]
    assert readiness.label("similar_path") in sale["every_role"].split("are never a reason")[0]

def test_the_sample_previews_without_a_call_and_never_becomes_a_role(config, model):
    previewed = role_drop.sample()  # the model stub fails on any call
    assert previewed["sample"] and previewed["role"]["title"] == "Data Engineer" and previewed["jd_text"] == JD
    with pytest.raises(ValueError, match="invented"):
        role_drop.accept(previewed)
    assert ID not in {r["id"] for r in role_drop.roles()}


def test_a_clash_with_a_role_on_file_is_refused(config):
    previewed = role_drop.sample() | {"sample": False}
    backend = next(r for r in role_drop.roles() if r["id"] == "backend")
    same_title = {**previewed["role"], "title": backend["title"], "id": role_drop.slug(backend["title"])}
    assert "already a role" in role_drop.clash(same_title)
    with pytest.raises(FileExistsError, match="already a role"):
        role_drop.accept(previewed, jd_url=backend["jd_url"])  # the same post
    assert ID not in {r["id"] for r in role_drop.roles()}
    with pytest.raises(ValueError, match="configured by hand"):
        role_drop.remove("backend")


@pytest.fixture
def client(monkeypatch, tmp_path, settings):
    monkeypatch.setattr(main, "store", Store(f"sqlite:///{tmp_path / 'web.sqlite'}"))
    monkeypatch.setattr(main, "settings", lambda: settings)
    main.seed_role(next(r for r in role_drop.roles() if r["id"] == "backend"))
    web = TestClient(main.app, raise_server_exceptions=False)  # no lifespan: no seeds, monitor or worker
    yield web
    web.close()


def test_the_web_drop_previews_then_writes_the_spec(config, client, model):
    assert client.post("/api/roles/preview", json={"sample": True}).json()["sample"]
    answer(model)
    previewed = client.post("/api/roles/preview", json={"jd_text": JD}).json()
    assert len(model.calls) == 2 and previewed["role"]["id"] == ID
    added = client.post("/api/roles/accept", json={"preview": previewed, "jd_url": "https://example.com/jobs/de",
                                                   "locations": ["New York City"]})
    assert added.status_code == 200, added.text
    said = " ".join(added.json()["next"])  # what the page says once it's added: no one watched, how, and when
    assert "Nobody is watched for this role yet" in said and "under this role" in said and "each morning" in said
    assert ID not in said  # the role's id is for the people file, not for the page
    assert (rc.SPECS / f"{ID}.json").exists() and rc.watched(ID)
    assert client.post("/api/roles/accept", json={"preview": previewed}).status_code == 409  # already added
    shown = {r["id"]: r for r in client.get("/api/state").json()["roles"]}
    assert shown[ID]["jd_url"] == "https://example.com/jobs/de" and shown[ID]["watch"]["topics"]
    assert shown["backend"]["watch"]["signs"]  # every role with a spec shows what it watches
    assert client.request("DELETE", "/api/roles/backend", json={}).status_code == 422
    role_drop.remove(ID)  # as scripts/roles.py remove does, while the app runs: only the page's copy is left
    assert client.request("DELETE", f"/api/roles/{ID}", json={}).status_code == 200
    assert ID not in {r["id"] for r in client.get("/api/state").json()["roles"]} and rc.watched(ID) is None
