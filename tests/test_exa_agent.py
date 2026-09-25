import asyncio
import copy
import json

import httpx
import pytest

from app import exa_agent as exa
from app.models import Candidate
from app.providers import ProviderError

ROLE = {
    "id": "role-1",
    "title": "Research engineer",
    "criteria": ["Published systems research"],
    "manager_notes": "Evidence of building useful systems",
}
URL = "https://example.com/people/alice"


def mock_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        exa.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


def completed():
    return {
        "id": "agent_run_test",
        "status": "completed",
        "stopReason": "schema_satisfied",
        "costDollars": {"total": 1.21},
        "output": {
            "structured": {
                "candidates": [
                    {
                        "name": "Alice Example",
                        "profile_url": URL,
                        "employer": "Unverified employer",
                        "location": "Unverified city",
                        "why_fit": "Published a relevant systems project.",
                        "source_urls": [URL],
                        "evidence_remarks": "Project attributed to Alice.",
                        "timing_hint": "Unknown",
                    }
                ]
            },
            "grounding": [
                {
                    "field": "structured.candidates[0].profile_url",
                    "confidence": "high",
                    "citations": [{"url": URL, "title": "Alice's public work"}],
                }
            ],
        },
    }


def test_create_one_bounded_run_with_allowlisted_inputs(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert str(request.url) == exa.API_URL
        assert body["effort"] == "auto"
        assert body["budget"] == {"maxCostDollars": 5}
        assert body["outputSchema"]["properties"]["candidates"]["maxItems"] == 6
        assert body["input"] == {"exclusion": [{"profile_url": URL}]}
        assert "Published systems research" in body["query"]
        assert "private" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "id": "agent_run_new",
                "status": "queued",
                "request": {"hidden": "private echo"},
            },
        )

    mock_http(monkeypatch, handler)
    result = asyncio.run(
        exa.create_run(
            {**ROLE, "contact_history": "private"},
            {"exa_key": "test", "private": "private"},
            [{"profile_url": URL, "note": "private"}, "http://localhost/private", URL],
        )
    )
    assert result["id"] == "agent_run_new"
    assert result["status"] == "queued"
    assert "request" not in result
    assert len(requests) == 1


@pytest.mark.parametrize("value", [0, 101, "invalid", float("nan"), float("inf")])
def test_invalid_budget_rejected_before_paid_creation(value):
    with pytest.raises(ProviderError, match="budget"):
        asyncio.run(
            exa.create_run(ROLE, {"exa_key": "test", "exa_run_budget": value}, [])
        )


@pytest.mark.parametrize(
    "status", ["queued", "running", "completed", "failed", "cancelled"]
)
def test_poll_is_one_get_and_preserves_lifecycle(monkeypatch, status):
    requests = []
    fixture = {
        **completed(),
        "status": status,
        "error": {"message": "secret provider body"},
    }

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url).endswith("/agent_run_test")
        return httpx.Response(200, json=fixture)

    mock_http(monkeypatch, handler)
    result = asyncio.run(exa.poll_run("agent_run_test", {"exa_key": "test"}))
    assert result["status"] == status
    assert result["costDollars"] == {"total": 1.21}
    assert result["output"]["grounding"] == fixture["output"]["grounding"]
    assert "secret" not in json.dumps(result)
    assert len(requests) == 1


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_noncompleted_is_never_empty_success(status):
    with pytest.raises(ProviderError, match=status):
        exa.parse_completed({**completed(), "status": status}, ROLE)


def test_empty_complete_differs_from_failed_and_bad_schema():
    fixture = completed()
    fixture["output"]["structured"]["candidates"] = []
    assert exa.parse_completed(fixture, ROLE) == []
    fixture["output"]["structured"] = None
    with pytest.raises(ProviderError, match="structured"):
        exa.parse_completed(fixture, ROLE)


def test_grounded_discovery_cannot_produce_ping_or_fabricated_quote():
    fixture = completed()
    proposal = exa.parse_completed(fixture, ROLE)[0]
    Candidate.model_validate(proposal)
    assert proposal["profile_url"] == URL
    assert proposal["identity_status"] == "unknown"
    assert proposal["employer"] == proposal["location"] == "Unknown"
    assert proposal["fit"][0]["status"] == "unknown"
    assert proposal["events"] == []
    assert proposal["draft"]["body"] == ""
    assert proposal["evidence"][0]["verified"] is False
    assert "No verbatim source excerpt" in proposal["evidence"][0]["excerpt"]
    assert proposal["_documents"][0]["text"] == ""
    assert proposal["_usage"]["grounding"] == fixture["output"]["grounding"]


@pytest.mark.parametrize(
    "citation_url",
    [
        "https://example.com/unrelated",
        "http://127.0.0.1/private",
        "https://user:pass@example.com",
        "file:///private",
    ],
)
def test_unsupported_or_unsafe_source_urls_rejected(citation_url):
    fixture = completed()
    fixture["output"]["grounding"][0]["citations"][0]["url"] = citation_url
    if citation_url != "https://example.com/unrelated":
        fixture["output"]["structured"]["candidates"][0]["profile_url"] = citation_url
        fixture["output"]["structured"]["candidates"][0]["source_urls"] = [citation_url]
    with pytest.raises(ProviderError, match="grounding"):
        exa.parse_completed(fixture, ROLE)


def test_ungrounded_profile_uses_cited_original_source_anchor():
    fixture = completed()
    fixture["output"]["structured"]["candidates"][0]["profile_url"] = (
        "https://linkedin.com/in/invented"
    )
    proposal = exa.parse_completed(fixture, ROLE)[0]
    assert proposal["profile_url"] == URL
    assert "invented" not in json.dumps(proposal)


def test_other_candidates_grounding_cannot_be_reused():
    fixture = completed()
    fixture["output"]["grounding"][0]["field"] = "structured.candidates[1].name"
    with pytest.raises(ProviderError, match="grounding"):
        exa.parse_completed(fixture, ROLE)


@pytest.mark.parametrize(
    "field",
    [
        "structured.candidates[0].name",
        "output.structured.candidates[0].name",
        "/candidates/0/name",
        "candidates.0.name",
    ],
)
def test_grounding_field_variants(field):
    fixture = completed()
    fixture["output"]["grounding"][0]["field"] = field
    assert len(exa.parse_completed(fixture, ROLE)) == 1


def test_budget_stop_has_visible_warning_even_with_no_rows():
    fixture = completed()
    fixture["stopReason"] = "budget_reached"
    checked = exa._checked_run(fixture)
    assert checked["warnings"] == [exa.BUDGET_WARNING]
    assert exa.parse_completed(checked, ROLE)[0]["_usage"]["warnings"] == [
        exa.BUDGET_WARNING
    ]
    checked["output"]["structured"]["candidates"] = []
    assert exa.parse_completed(checked, ROLE) == []
    assert exa.BUDGET_WARNING in checked["warnings"]


def test_parse_preserves_original_grounding():
    fixture = completed()
    original = copy.deepcopy(fixture["output"])
    exa.parse_completed(fixture, ROLE)
    assert fixture["output"] == original


def test_http_errors_never_expose_provider_response(monkeypatch):
    mock_http(
        monkeypatch, lambda request: httpx.Response(401, text="secret response body")
    )
    with pytest.raises(ProviderError, match="HTTP 401") as error:
        asyncio.run(exa.poll_run("agent_run_test", {"exa_key": "test"}))
    assert "secret" not in str(error.value)


def test_ambiguous_create_is_not_retried(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("sensitive timeout detail")

    mock_http(monkeypatch, handler)
    with pytest.raises(ProviderError, match="may have succeeded remotely") as error:
        asyncio.run(exa.create_run(ROLE, {"exa_key": "test"}, []))
    assert "sensitive" not in str(error.value)
    assert len(requests) == 1


def test_run_id_cannot_change_host_or_path():
    with pytest.raises(ProviderError, match="run ID"):
        asyncio.run(exa.poll_run("agent_run_x/../../secrets", {"exa_key": "test"}))
