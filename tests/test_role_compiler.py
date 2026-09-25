import asyncio
import copy
import json
import re

import pytest

from app import detectors, journey, providers, readiness, role_compiler as rc, timeline
from app.timeline import DETECTORS as REGISTRY

FINANCE_JD = """Global Controller
You will own monthly close, US GAAP reporting, multi-entity consolidation and audits.
Required: 10+ years including Big 4 public accounting. Preferred: finance automation.
"""
RESEARCH_JD = """Member of Technical Staff
Research on action policies and world models. Show exceptional attributable work:
papers, benchmarks or systems. Required: deep learning research experience.
"""
RECRUITER_JD = """Senior Recruiter, AI Research & Engineering
Own full-cycle recruiting for researchers and engineers; build sourcing pipelines
in the ATS; partner with hiring managers. Required: 5+ years technical recruiting.
"""


def hypotheses(lane):
    return rc.build_signal_plan({"lane": lane})["signals"]


def choice(id):
    return {"id": id, "reason": f"{id} follows from the JD and mechanism."}


FINANCE = {
    "archetype": "non_publishing_operations",
    "detectors": [
        choice("exec_departure"), choice("acquisition_closed"), choice("retention_cliff"),
        choice("warn_notice"), choice("auditor_change"), choice("late_filing"),
        choice("calendar_quiet_close"), choice("hold_recent_promotion"),
        choice("self_stated_availability"), choice("private_note"),
    ],
    "answer_key": {
        "query": "SEC 8-K item 5.02 appointments naming Controller, Chief Accounting Officer or VP Finance",
        "sources": ["sec", "exa"],
    },
    "hypotheses": [
        {"id": "explicit_invitation", "detector_ids": ["private_note"]},
        {"id": "organization_change", "detector_ids": ["acquisition_closed", "warn_notice", "exec_departure"]},
        {"id": "warm_route", "gap": "gap: needs adapter for authorized referral records"},
        {"id": "finance_milestone", "detector_ids": ["self_stated_availability"]},
    ],
    "gaps": ["gap: needs adapter for private-company finance leadership changes"],
}

RESEARCH = {
    "archetype": "publishing_research",
    "detectors": [
        choice("paper_v1"), choice("paper_accepted"), choice("paper_talk"),
        choice("coauthor_departure"), choice("pi_departure"), choice("grant_end"),
        choice("placement_end"), choice("gi_citation"), choice("calendar_quiet_conference"),
        choice("hold_short_tenure"), choice("private_note"),
    ],
    "answer_key": {
        "query": "Lab and team join announcements and arXiv affiliation changes for world-model researchers",
        "sources": ["arxiv", "openalex", "exa"],
    },
    "hypotheses": [
        {"id": "explicit_invitation", "detector_ids": ["private_note"]},
        {"id": "organization_change", "detector_ids": ["pi_departure", "grant_end"]},
        {"id": "warm_route", "detector_ids": ["coauthor_departure"]},
        {"id": "research_milestone", "detector_ids": ["paper_v1", "paper_accepted", "paper_talk"]},
    ],
    "gaps": [],
}

RECRUITER = {
    "archetype": "non_publishing_operations",
    "detectors": [
        choice("tenure_milestone"), choice("team_exodus"), choice("warn_notice"),
        choice("self_stated_availability"), choice("private_note"),
        choice("hold_short_tenure"), choice("hold_equity_refresh"),
    ],
    "answer_key": {
        "query": "LinkedIn profile changes announcing a new Senior Recruiter or Talent Lead at AI labs",
        "sources": ["crustdata_person"],
    },
    "hypotheses": [
        {"id": "explicit_invitation", "detector_ids": ["private_note"]},
        {"id": "organization_change", "detector_ids": ["team_exodus", "warn_notice"]},
        {"id": "warm_route", "gap": "gap: needs adapter for authorized referral records"},
        {"id": "professional_milestone", "gap": "gap: needs adapter for recruiting community talks"},
    ],
    "gaps": ["gap: needs adapter for ATS or recruiting-platform activity"],
}

SETTINGS = {"anthropic_key": "test-key", "max_calls_per_run": 4}


def model(monkeypatch, *responses):
    """Answer each compiler call with the next response; record the requests."""
    calls, queue = [], list(responses)

    async def fake_post(url, key, payload, provider, budget, workspace_id):
        budget.take()
        calls.append(json.loads(payload["messages"][0]["content"]))
        assert payload["output_config"]["format"]["schema"]["properties"]["detectors"]
        body = queue.pop(0)
        return {"content": [{"type": "text", "text": body if isinstance(body, str) else json.dumps(body)}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 1}}

    monkeypatch.setattr(providers, "_post", fake_post)
    return calls


def compile(jd, lane, *responses, monkeypatch):
    model(monkeypatch, *responses)
    return asyncio.run(rc.compile_role(jd, "role-" + lane, SETTINGS, hypotheses(lane)))


def test_manifest_matches_the_registry_and_journey_adapters():
    m = rc.manifest()
    assert set(rc.DETECTOR_FEEDS) == set(REGISTRY)
    assert all(d["family"] == REGISTRY[d["id"]][0] and d["means"] for d in m["detectors"])
    assert {s["id"] for s in m["sources"]} == set(journey.ADAPTERS) | {"research_extraction", "manual_note"}
    blind = {d["id"] for d in m["detectors"] if not d["sources"]}
    assert blind == {"calendar_quiet_conference", "similar_path", "associate_joined"}  # the last two: trend lane
    assert all(rc.DETECTOR_FEEDS[d].gap() for d in blind)
    assert set(detectors.OWN_END_TYPES) == {*rc.CONSUMES["own_departure"], *rc.CONSUMES["company_closure"]}
    assert set(rc.SOURCES["manual_note"].produces) == set(timeline.NOTE_TYPES.values())
    assert set(rc.CONSUMES["private_note"]) == {t for kind, t in timeline.NOTE_TYPES.items() if kind}  # untyped is context
    assert rc.DETECTOR_FEEDS["warn_notice"].sources() == ["warn", "news"]
    assert rc.DETECTOR_FEEDS["acquired"].sources() == ["research_extraction"]  # their own post, never employer news
    assert rc.DETECTOR_FEEDS["team_exodus"].sources() == ["sec", "openalex", "wayback", "research_extraction"]
    always = {d["id"] for d in m["detectors"] if d["always"]}
    assert {"private_note", "placement_end", "own_departure", "hold_not_looking", *readiness.NEWS} <= always
    assert not always & {"paper_talk", "exec_departure", "calendar_quiet_close"}


def test_the_compiler_is_told_which_signs_are_only_ever_listed():
    """Its prompt says every sign it selects counts for the role. Time in a seat, a retention cliff and the pattern
    before their past moves never do (readiness.CONTEXT_ONLY), so the manifest marks them and the prompt says so."""
    listed = {d["id"] for d in rc.manifest()["detectors"] if d["listed_only"]}
    assert listed == set(readiness.CONTEXT_ONLY) and "own_precedent" in listed
    assert "listed_only" in rc.COMPILER_SYSTEM


# Wording that says a move follows a sign: GI does not predict who will leave.
MOVE_FOLLOWS = re.compile(r"about to move|new positions|often follow|after them|brings? their own (?:controller|people|team)|"
                          r"free to (?:move|leave|go)|\bmoves? at\b|weigh a move|stops costing|leaving is free|"
                          r"re-evaluate at|future is open|strands the|stay for|on the market|likely to (?:move|leave)|"
                          r"follow them", re.I)


def test_no_sign_is_worded_as_a_move_that_follows():
    """The Role briefs page shows each sign's meaning and why the role watches it, for every role and the sample JD.
    Failure modes: a spec's reason says a move follows (a group about to move, partners pulled after them, looking for
    new positions, a new leader bringing their own people, moving at an anniversary or a cliff, a team they would stay
    for); a sign's meaning says so (collaborators often follow, the group's future is open, leaving is free after a
    cliff); the sample JD's stand-in answer says so; the compiler is not told, so a new JD's spec brings it back."""
    from app import role_drop

    specs = [*sorted(rc.SPECS.glob("*.json")), role_drop.SAMPLE / "compiled.json"]
    for path in specs:
        for d in json.loads(path.read_text())["detectors"]:
            assert not MOVE_FOLLOWS.search(d["reason"]), (path.name, d["id"], d["reason"])
    for d in rc.manifest()["detectors"]:
        assert not MOVE_FOLLOWS.search(d["means"]), (d["id"], d["means"])
    shown = [role_drop.watch(role_drop.spec_of(r["id"]), r) for r in role_drop.roles() if role_drop.spec_of(r["id"])]
    for page in [*shown, role_drop.sample()["watch"]]:
        text = [*(s[k] for s in page["signs"] for k in ("means", "why")), page["every_role"], page["holds"], *page["gaps"]]
        assert not [t for t in text if MOVE_FOLLOWS.search(t)]
    assert "never that a move follows" in rc.COMPILER_SYSTEM


def test_unknown_detector_is_rejected():
    raw = copy.deepcopy(FINANCE)
    raw["detectors"].append(choice("vibes_detector"))
    with pytest.raises(rc.StrategyValidationError) as info:
        rc.compile_output(raw, "r", [h["id"] for h in hypotheses("finance-operations")])
    assert any("vibes_detector" in issue and "detectors" in issue for issue in info.value.issues)


@pytest.mark.parametrize("change, fragment", [
    (lambda r: r["detectors"][2].update(params={"months": [12]}), "Extra inputs"),
    (lambda r: r["detectors"].append(choice("warn_notice")), "unique"),
    (lambda r: r["hypotheses"].pop(), "exactly once"),
    (lambda r: r["hypotheses"][0].update(detector_ids=["paper_v1"]), "unselected"),
    (lambda r: r["hypotheses"][0].update(detector_ids=[]), "not both or neither"),
    (lambda r: r["hypotheses"][2].update(gap="we cannot see referrals"), "needs adapter for"),
    (lambda r: r["gaps"].append("missing: referrals"), "needs adapter for"),
    (lambda r: r["answer_key"].update(sources=["linkedin_scraper"]), "answer_key"),
    (lambda r: r.update(archetype="wizardry"), "archetype"),
    (lambda r: r.update(window_start="2026-10-01"), "window_start"),
])
def test_anything_outside_the_manifest_is_rejected(change, fragment):
    raw = copy.deepcopy(FINANCE)
    change(raw)
    with pytest.raises(rc.StrategyValidationError) as info:
        rc.compile_output(raw, "r", [h["id"] for h in hypotheses("finance-operations")])
    assert any(fragment in issue for issue in info.value.issues), info.value.issues


def test_finance_jd_enables_filings_and_close_calendar(monkeypatch):
    config = compile(FINANCE_JD, "finance-operations", FINANCE, monkeypatch=monkeypatch).model_dump()
    assert config["archetype"] == "non_publishing_operations"
    assert config["sources"] == ["sec", "warn", "news", "crustdata_person", "wayback", "exa",
                                 "research_extraction", "manual_note"]
    assert config["answer_key"]["sources"] == ["sec", "exa"]
    assert not {"arxiv", "openreview", "nsf"} & set(config["sources"])
    assert config["calendars"] == ["calendar_quiet_close"]
    assert config["gaps"] == ["gap: needs adapter for private-company finance leadership changes",
                              "gap: needs adapter for authorized referral records"]
    assert config["manifest_version"] == rc.MANIFEST_VERSION


def test_research_jd_enables_paper_sources_and_flags_the_blind_calendar(monkeypatch):
    config = compile(RESEARCH_JD, "research", RESEARCH, monkeypatch=monkeypatch).model_dump()
    assert config["archetype"] == "publishing_research"
    assert {"arxiv", "openreview", "openalex", "openalex_citations", "nsf", "exa"} <= set(config["sources"])
    assert "crustdata_company" not in config["sources"]
    assert config["calendars"] == ["calendar_quiet_conference"]
    assert config["gaps"] == [
        "gap: needs adapter for conference_deadline or conference_attending events (calendar_quiet_conference)"]


def test_recruiter_jd_keeps_model_and_hypothesis_gaps_in_order(monkeypatch):
    config = compile(RECRUITER_JD, "other", RECRUITER, monkeypatch=monkeypatch).model_dump()
    assert config["gaps"] == [
        "gap: needs adapter for ATS or recruiting-platform activity",
        "gap: needs adapter for authorized referral records",
        "gap: needs adapter for recruiting community talks",
    ]
    assert "crustdata_company" in config["sources"]


def test_one_repair_then_fail_closed(monkeypatch):
    unmapped = copy.deepcopy(FINANCE)
    unmapped["hypotheses"].pop()
    calls = model(monkeypatch, unmapped, FINANCE)
    config = asyncio.run(rc.compile_role(FINANCE_JD, "r", SETTINGS, hypotheses("finance-operations")))
    assert config.role_id == "r" and len(calls) == 2
    assert "exactly once" in json.dumps(calls[1]["validation_errors"])
    assert [h["id"] for h in calls[1]["previous_output"]["hypotheses"]] == [h["id"] for h in unmapped["hypotheses"]]
    assert "manifest" in calls[0] and calls[0]["hypotheses"][0]["id"] == "explicit_invitation"

    calls = model(monkeypatch, unmapped, unmapped)
    with pytest.raises(rc.StrategyValidationError):
        asyncio.run(rc.compile_role(FINANCE_JD, "r", SETTINGS, hypotheses("finance-operations")))
    assert len(calls) == 2

    calls = model(monkeypatch, unmapped, FINANCE)
    with pytest.raises(rc.StrategyValidationError):
        asyncio.run(rc.compile_role(FINANCE_JD, "r", {**SETTINGS, "max_calls_per_run": 1}, hypotheses("finance-operations")))
    assert len(calls) == 1


def test_output_outside_the_schema_is_refused_without_a_repair(monkeypatch):
    invented = copy.deepcopy(FINANCE)
    invented["detectors"][0]["id"] = "vibes_detector"
    for body in (invented, "not json"):
        calls = model(monkeypatch, body, FINANCE)
        with pytest.raises(providers.ProviderError, match="invalid object"):
            asyncio.run(rc.compile_role(FINANCE_JD, "r", SETTINGS, hypotheses("finance-operations")))
        assert len(calls) == 1


def test_missing_key_never_calls_provider(monkeypatch):
    calls = model(monkeypatch)
    with pytest.raises(providers.ProviderError, match="API key"):
        asyncio.run(rc.compile_role(FINANCE_JD, "r", {}, []))
    assert calls == []


def test_side_by_side_prints_one_column_per_role(monkeypatch):
    configs = [compile(jd, lane, out, monkeypatch=monkeypatch).model_dump() for jd, lane, out in (
        (FINANCE_JD, "finance-operations", FINANCE), (RESEARCH_JD, "research", RESEARCH),
        (RECRUITER_JD, "other", RECRUITER))]
    text = rc.side_by_side(configs)
    first = text.splitlines()[0]
    assert first.count(" | ") == 2 and "role-research" in first
    assert "(calendar_quiet_conference)" in text and "referral" in text


@pytest.mark.parametrize("role_id", ["mts-research", "controller", "backend", "product-designer"])
def test_committed_specs_pass_the_current_manifest(role_id):
    """A spec is what readiness counts for its role: its choices must still be valid against today's registry.

    Derived fields (sources, blind-detector gaps) may drift as adapters land; only a new, reviewed spec replaces one.
    """
    spec = json.loads(rc.spec_path(role_id).read_text())
    role = next(r for r in json.loads((rc.SPECS.parent / "roles.json").read_text())["roles"] if r["id"] == role_id)
    raw = {k: spec[k] for k in rc.CompiledRole.model_fields if k != "gaps" and k in spec}
    hypothesis_ids = [s["id"] for s in rc.build_signal_plan(role)["signals"]]
    assert rc.compile_output(raw, role_id, hypothesis_ids).detectors == rc.RoleConfig.model_validate(spec).detectors
    assert rc.watched(role_id) == {d["id"] for d in spec["detectors"]}


def test_watched_maps_role_names_and_falls_back_to_everything():
    assert rc.watched("mts") == rc.watched("mts-research") and "paper_v1" in rc.watched("mts")
    assert "exec_departure" not in rc.watched("mts") and "exec_departure" in rc.watched("controller")
    assert rc.watched(None) is rc.watched("recruiter") is rc.watched("../roles") is None


def test_a_role_counts_its_detectors_and_what_counts_for_everyone():
    def ev(subject, event_type, day):
        return {**timeline.TimelineEvent(
            subject_type="person", subject_id=subject, event_type=event_type, event_date=day, date_precision="day",
            observed_at=f"{day}T00:00:00+00:00", quote="x", tier=3, extractor="test").model_dump(),
            "id": f"ev:{subject}:{event_type}"}

    me = "jo-roe|acme"
    events = [ev(me, "paper_v1", "2026-09-01"), ev("cfo-doe|acme", "officer_departure", "2026-09-05"),
              ev(me, "self_stated_availability", "2026-09-10")]

    def fired(role):
        return {a["detector_id"] for a in journey.detect(None, me, "2026-09-15T00:00:00+00:00", events, role)}

    assert fired(None) == {"paper_v1", "exec_departure", "self_stated_availability"}
    assert fired("mts") == {"paper_v1", "self_stated_availability"}
    assert fired("controller") == {"paper_v1", "exec_departure", "self_stated_availability"}  # a paper out: every role
    assert readiness.for_role([], frozenset()) == []
