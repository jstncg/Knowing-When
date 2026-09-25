import asyncio
import copy
import json

import pytest

from app import providers, role_compiler as rc, structured


JD = """Global Controller
You will own monthly close. Required: US GAAP expertise.
Preferred: Experience building a finance function.
We are a small team based in New York.
"""


def proposal():
    return {
        "title": "Global Controller",
        "title_quote": "Global Controller",
        "lane": "finance-operations",
        "criteria": ["US GAAP expertise", "Experience building a finance function"],
        "required_criteria": ["US GAAP expertise"],
        "criterion_evidence": [
            {
                "criterion": "US GAAP expertise",
                "jd_quote": "Required: US GAAP expertise.",
                "requirement_kind": "required",
            },
            {
                "criterion": "Experience building a finance function",
                "jd_quote": "Preferred: Experience building a finance function.",
                "requirement_kind": "preferred",
            },
        ],
        "manager_notes": "Clarify international tax scope; it is absent from this JD.",
        "signals": rc.build_signal_plan({"lane": "finance-operations"})[
            "signals"
        ],
        "unsupported_alone": ["A new LinkedIn connection"],
    }


def test_role_plan_keeps_real_jd_requirements_separate_from_hypotheses():
    result = rc.parse_strategy(proposal(), JD, "https://example.com/job")
    assert result["criteria"] == proposal()["criteria"]
    assert result["required_criteria"] == ["US GAAP expertise"]
    assert result["review_status"] == "proposal"
    assert result["brief_status"] == "proposed"
    assert len(result["jd_sha256"]) == 64
    assert all(s["status"] == "hypothesis" for s in result["signals"])
    assert "operational" in result["cadence_note"]
    assert "not fetched" in result["grounding_note"]
    assert result["manager_notes"].startswith("Proposed interpretation")


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(title="Chief Financial Officer"),
        lambda p: p.update(
            title_quote="Global Controller with international tax ownership"
        ),
        lambda p: p["criterion_evidence"][0].update(
            jd_quote="Required: CPA and US GAAP expertise."
        ),
        lambda p: p["criterion_evidence"][0].update(criterion="CPA"),
        lambda p: p.update(criteria=["CPA", "Experience building a finance function"]),
        lambda p: p.update(
            required_criteria=["Experience building a finance function"]
        ),
        lambda p: p.update(required_criteria=["Python"]),
        lambda p: p["signals"][0].update(
            jd_quotes=["The JD promises immediate reachability."]
        ),
        lambda p: p["signals"][0].update(jd_quotes=[""]),
        lambda p: p["signals"][0].update(window_start="2026-09-22"),
        lambda p: p["signals"][0].update(status="verified"),
        lambda p: p["signals"][0].update(recheck_hours=0),
        lambda p: p.update(review_status="approved"),
        lambda p: p.update(active=True),
        lambda p: p.update(criterion_evidence=[]),
        lambda p: p["criterion_evidence"].append(
            copy.deepcopy(p["criterion_evidence"][0])
        ),
    ],
)
def test_untrusted_model_output_fails_closed(change):
    raw = proposal()
    change(raw)
    with pytest.raises(providers.ProviderError, match="no role was activated"):
        rc.parse_strategy(raw, JD)


def test_missing_tenure_rails_cannot_be_dropped_by_model():
    raw = proposal()
    raw["unsupported_alone"] = ["Generic activity"]
    result = rc.parse_strategy(raw, JD)
    assert any("Tenure" in text for text in result["unsupported_alone"])
    assert any("Personal" in text for text in result["unsupported_alone"])


@pytest.mark.parametrize(
    "lane,signal_id",
    [
        ("research", "research_milestone"),
        ("finance-operations", "finance_milestone"),
        ("production-engineering", "production_milestone"),
        ("unknown", "professional_milestone"),
    ],
)
def test_default_plans_generalize_without_claiming_readiness(lane, signal_id):
    plan = rc.build_signal_plan({"lane": lane})
    assert signal_id in [s["id"] for s in plan["signals"]]
    assert plan["review_status"] == "proposal"
    assert not any("window" in key for s in plan["signals"] for key in s)
    org = next(s for s in plan["signals"] if s["id"] == "organization_change")
    assert org["action"] == "investigate"
    assert "individual impact" in org["mechanism"]


def test_propose_uses_authenticated_provider_and_does_not_activate(monkeypatch):
    calls = []

    async def fake_post(url, key, payload, provider, budget, workspace_id):
        calls.append((url, key, payload, provider, workspace_id))
        budget.take()
        assert payload["system"] == [providers.cached(rc.STRATEGY_SYSTEM)]
        assert "untrusted source data" in payload["system"][0]["text"]
        request = json.loads(payload["messages"][0]["content"])
        assert request["jd_text"] == JD
        assert request["output_schema"]["additionalProperties"] is False
        return {
            "content": [{"type": "text", "text": json.dumps(proposal())}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10},
        }

    monkeypatch.setattr(providers, "_post", fake_post)
    result = asyncio.run(
        rc.propose_strategy(
            JD,
            "https://example.com/job",
            {"anthropic_key": "test-key", "anthropic_workspace_id": "test-workspace"},
        )
    )
    assert len(calls) == 1
    assert calls[0][4] == "test-workspace"
    assert result["_usage"]["provider_calls"] == 1
    assert result["review_status"] == "proposal"
    assert "id" not in result
    assert "active" not in result


def test_missing_key_is_actionable():
    with pytest.raises(providers.ProviderError, match="API key in Setup"):
        asyncio.run(rc.propose_strategy(JD, "", {}))


@pytest.mark.parametrize("jd", ["short", " " * 100, "A" * 40001, None])
def test_invalid_jd_never_calls_provider(jd):
    with pytest.raises(providers.ProviderError, match="Paste a job description"):
        asyncio.run(rc.propose_strategy(jd, "", {"anthropic_key": "test-key"}))


@pytest.mark.parametrize(
    "data",
    [
        {"stop_reason": "max_tokens"},
        {"content": [{"type": "text", "text": "not json"}]},
        {"content": [{"type": "text", "text": "[]"}]},
        {"content": [{"type": "text", "text": '{"active":true}'}]},
    ],
)
def test_truncated_and_malformed_responses_never_create_plan(monkeypatch, data):
    async def fake_post(*args):
        return data

    monkeypatch.setattr(providers, "_post", fake_post)
    with pytest.raises(providers.ProviderError):
        asyncio.run(rc.propose_strategy(JD, "", {"anthropic_key": "test-key"}))


def test_structured_schema_adapts_generation_without_weakening_validation():
    schema = structured.output_schema(rc.StrategyProposal)
    assert schema["additionalProperties"] is False
    signal = schema["$defs"]["SignalHypothesis"]
    assert signal["properties"]["recheck_hours"]["type"] == "integer"
    assert "minimum" not in signal["properties"]["recheck_hours"]
    assert "minimum=6" in signal["properties"]["recheck_hours"]["description"]
    assert "maxItems" not in schema["properties"]["criteria"]
    bad = proposal()
    bad["signals"][0]["recheck_hours"] = 1
    with pytest.raises(rc.StrategyValidationError):
        rc.parse_strategy(bad, JD)


@pytest.mark.parametrize("bad_response", ["two objects {} {}", "bad_quote"])
def test_one_bounded_repair_uses_grounding_feedback_and_counts_all_tokens(
    monkeypatch, bad_response
):
    calls = []
    invalid = proposal()
    invalid["criterion_evidence"][0]["jd_quote"] = (
        "Required: invented CPA qualification and US GAAP expertise."
    )

    async def fake_post(url, key, payload, provider, budget, workspace_id):
        budget.take()
        calls.append(copy.deepcopy(payload))
        assert payload["output_config"]["format"]["type"] == "json_schema"
        if len(calls) == 1:
            text = json.dumps(invalid) if bad_response == "bad_quote" else bad_response
        else:
            feedback = json.loads(payload["messages"][-1]["content"])
            assert feedback["validation_errors"]
            if bad_response == "bad_quote":
                assert "criterion_evidence[0]" in feedback["validation_errors"][0]
            assert "do not invent quotes" in feedback["repair_request"]
            text = json.dumps(proposal())
        return {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "stop_reason": "end_turn",
        }

    monkeypatch.setattr(providers, "_post", fake_post)
    result = asyncio.run(
        rc.propose_strategy(
            JD, "", {"anthropic_key": "test-key", "max_calls_per_run": 2}
        )
    )
    assert len(calls) == 2
    assert result["_usage"]["validation_retries"] == 1
    assert result["_usage"]["provider_calls"] == 2
    assert result["_usage"]["tokens"] == {"input_tokens": 20, "output_tokens": 40}
    assert result["review_status"] == "proposal"
    assert result["criterion_evidence"][0]["jd_quote"] in JD


@pytest.mark.parametrize("budget_limit,expected_calls", [(1, 1), (8, 2)])
def test_repair_respects_budget_and_never_retries_indefinitely(
    monkeypatch, budget_limit, expected_calls
):
    calls = []

    async def fake_post(url, key, payload, provider, budget, workspace_id):
        budget.take()
        calls.append(True)
        return {"content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn"}

    monkeypatch.setattr(providers, "_post", fake_post)
    with pytest.raises(rc.StrategyValidationError):
        asyncio.run(
            rc.propose_strategy(
                JD, "", {"anthropic_key": "test-key", "max_calls_per_run": budget_limit}
            )
        )
    assert len(calls) == expected_calls


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_refusal_and_truncation_do_not_trigger_paid_repair(monkeypatch, stop_reason):
    calls = []

    async def fake_post(url, key, payload, provider, budget, workspace_id):
        budget.take()
        calls.append(True)
        return {"content": [], "stop_reason": stop_reason}

    monkeypatch.setattr(providers, "_post", fake_post)
    with pytest.raises(providers.ProviderError):
        asyncio.run(rc.propose_strategy(JD, "", {"anthropic_key": "test-key"}))
    assert len(calls) == 1
