from timing_helpers import INVITATION, assessment, purpose

import asyncio
import json

import httpx
import pytest

from app import providers as p


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/a",
        "http://127.0.0.1",
        "http://[::1]",
        "http://169.254.169.254",
        "file:///etc/passwd",
        "https://user:pass@example.com",
        "https://example.com:8888",
        "http://foo.internal",
        "https://example.com\\@127.0.0.1",
    ],
)
def test_url_blocks_private_or_unsafe(url):
    with pytest.raises(p.ProviderError):
        p._url(url)


def fake_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        p.httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_fetch_pins_ip_preserves_sni_strips_script(monkeypatch):
    async def resolver(url):
        return "93.184.216.34"

    monkeypatch.setattr(p, "_resolve_public", resolver)

    def handler(req):
        assert req.url.host == "93.184.216.34"
        assert req.headers["Host"] == "example.com"
        assert req.extensions["sni_hostname"] == "example.com"
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>A profile</title><script>secret instructions</script><p>Alice Example built systems.</p>",
        )

    fake_client(monkeypatch, handler)
    doc = run(p.fetch_document("https://example.com/profile"))
    assert "Alice Example" in doc["text"]
    assert "secret instructions" not in doc["text"]
    assert doc["event_date"] is None
    assert doc["url"] == "https://example.com/profile"


def test_redirect_to_private_refused(monkeypatch):
    async def resolver(url):
        p._url(url)
        return "93.184.216.34"

    monkeypatch.setattr(p, "_resolve_public", resolver)
    fake_client(
        monkeypatch,
        lambda req: httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
        ),
    )
    with pytest.raises(p.ProviderError, match="public"):
        run(p.fetch_document("https://example.com"))


def test_dns_rejects_mixed_public_private(monkeypatch):
    async def scenario():
        async def addresses(*args, **kwargs):
            return [
                (2, 1, 6, "", ("93.184.216.34", 443)),
                (2, 1, 6, "", ("10.0.0.1", 443)),
            ]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", addresses)
        await p._resolve_public("https://example.com")

    with pytest.raises(p.ProviderError, match="non-public"):
        run(scenario())


def test_fetch_caps_size(monkeypatch):
    async def resolver(url):
        return "93.184.216.34"

    monkeypatch.setattr(p, "_resolve_public", resolver)
    fake_client(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"a" * (p.MAX_BYTES + 1),
        ),
    )
    with pytest.raises(p.ProviderError, match="2 MB"):
        run(p.fetch_document("https://example.com"))


def document():
    return {
        "id": "s1",
        "url": "https://example.com/alice",
        "title": "Alice Example",
        "text": "Alice Example released a world model on September 20, 2026. She worked on actions.",
        "observed_at": "2026-09-21T00:00:00+00:00",
        "event_date": None,
        "source_kind": "public_web",
    }


def test_grounding_rejects_quotes_urls_dates_and_unverified_routes():
    d = document()
    raw = {
        "evidence": [
            {
                "id": "s1",
                "url": d["url"],
                "excerpt": "Alice Example released a world model",
            },
            {"id": "fake", "url": "https://evil.com", "excerpt": "Invented"},
        ],
        "fit": [
            {"criterion": "Research", "status": "supported", "evidence_ids": ["fake"]}
        ],
        "events": [
            {
                "title": "Release",
                "date": "2026-09-20",
                "evidence_ids": ["s1"],
                "window_start": "2026-09-20T00:00:00Z",
                "window_end": "2026-09-30T00:00:00Z",
            }
        ],
        "route": {
            "status": "plausible_unconfirmed",
            "introducer": "Fake person",
            "evidence_ids": ["fake"],
        },
    }
    result = p._sanitize(raw, [d], {"criteria": ["Research"]})
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["verified"] is False
    assert result["identity_status"] == "unknown"
    assert result["fit"][0]["status"] == "unknown"
    assert result["events"][0]["date"] is None
    assert "window_start" not in result["events"][0]
    assert result["route"]["status"] == "none_found"
    raw["events"][0]["date_quote"] = d["text"]
    assert p._sanitize(raw, [d], {"criteria": []})["events"][0]["date"] == "2026-09-20"


def test_exa_published_date_is_not_event_date():
    doc = p._exa_docs(
        {
            "results": [
                {
                    "url": "https://example.com/a",
                    "title": "A",
                    "text": "A useful source",
                    "publishedDate": "2026-09-20",
                }
            ]
        },
        "controller",
    )[0]
    assert doc["event_date"] is None
    assert doc["published_at"] == "2026-09-20"


def test_research_budget_reserves_model_and_reports_limits(monkeypatch):
    calls = []

    async def fetch(url):
        return document()

    async def post(url, key, payload, provider, budget, workspace_id=None):
        budget.take()
        calls.append(provider)
        if provider == "Exa":
            return {"results": [document()] if url.endswith("/contents") else []}
        return {
            "model": "test-model",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"summary": "Proposal", "evidence": []}),
                }
            ],
        }

    monkeypatch.setattr(p, "fetch_document", fetch)
    monkeypatch.setattr(p, "_post", post)
    result = run(
        p.research(
            {"name": "Alice Example", "profile_url": "https://example.com/alice"},
            {"criteria": ["Research"]},
            {
                "anthropic_key": "test-secret",
                "exa_key": "test-exa",
                "max_calls_per_run": 3,
            },
        )
    )
    assert calls == ["Exa", "Anthropic"]
    assert result["_usage"]["provider_calls"] == 2
    assert result["_usage"]["warnings"]
    assert result["fit"][0]["status"] == "unknown"


def test_missing_key_is_actionable():
    with pytest.raises(p.ProviderError, match="Anthropic API key"):
        run(p.research({}, {}, {}))


def test_errors_never_expose_key_or_provider_body(monkeypatch):
    fake_client(
        monkeypatch, lambda req: httpx.Response(401, json={"message": "SECRET-KEY"})
    )
    with pytest.raises(p.ProviderError) as error:
        run(p._post("https://api.exa.ai/search", "SECRET-KEY", {}, "Exa", p.Budget({})))
    assert "SECRET-KEY" not in str(error.value)
    assert "401" in str(error.value)


@pytest.mark.parametrize("workspace_id", [None, "", "wrk_test-workspace"])
@pytest.mark.parametrize("operation", ["research", "extract_prospects"])
def test_anthropic_workspace_header_on_all_calls(monkeypatch, workspace_id, operation):
    requests = []

    async def fetch(url):
        return document()

    def handler(req):
        requests.append(req)
        assert req.url.host == "api.anthropic.com"
        assert req.headers["x-api-key"] == "test-key"
        assert req.headers["anthropic-version"] == "2023-06-01"
        if workspace_id:
            assert req.headers["anthropic-workspace-id"] == workspace_id
        else:
            assert "anthropic-workspace-id" not in req.headers
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}]})

    monkeypatch.setattr(p, "fetch_document", fetch)
    fake_client(monkeypatch, handler)
    settings = {"anthropic_key": "test-key", "_budget": p.Budget({})}
    if workspace_id is not None:
        settings["anthropic_workspace_id"] = workspace_id
    if operation == "research":
        run(p.research({"profile_url": document()["url"]}, {}, settings))
    else:
        run(p.extract_prospects([document()], {}, settings))
    assert len(requests) == 1


@pytest.mark.parametrize("provider", ["Exa"])
def test_workspace_header_is_not_sent_to_other_providers(monkeypatch, provider):
    def handler(req):
        assert "anthropic-workspace-id" not in req.headers
        assert "anthropic-version" not in req.headers
        return httpx.Response(200, json={})

    fake_client(monkeypatch, handler)
    run(
        p._post(
            "https://example.com", "test-key", {}, provider, p.Budget({}), "wrk_test"
        )
    )


def test_anthropic_missing_workspace_error_is_actionable_and_safe(monkeypatch):
    fake_client(
        monkeypatch,
        lambda req: httpx.Response(
            400,
            json={
                "error": {
                    "message": "Identity-linked API keys require the anthropic-workspace-id header. SECRET-KEY"
                }
            },
        ),
    )
    with pytest.raises(p.ProviderError) as error:
        run(
            p._post(
                "https://api.anthropic.com/v1/messages",
                "SECRET-KEY",
                {},
                "Anthropic",
                p.Budget({}),
            )
        )
    assert "ANTHROPIC_WORKSPACE_ID" in str(error.value)
    assert "Settings" in str(error.value)
    assert "400" in str(error.value)
    assert "SECRET-KEY" not in str(error.value)


def test_exa_search_uses_recommended_highlights_request(monkeypatch):
    def handler(req):
        assert json.loads(req.content) == {
            "query": "research",
            "type": "auto",
            "contents": {"highlights": True},
        }
        return httpx.Response(200, json={"results": []})

    fake_client(monkeypatch, handler)
    assert run(p._search("research", {}, {"exa_key": "test-key"}, p.Budget({}))) == []


def test_native_arxiv_discovery(monkeypatch):
    async def fetch(url):
        return {
            "text": '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1234.56789</id><title>Action learning</title><summary>Research</summary><published>2026-09-20T00:00:00Z</published><author><name>Alice Example</name></author></entry></feed>'
        }

    monkeypatch.setattr(p, "fetch_document", fetch)
    docs = run(p.discover({"id": "mts-research"}, {}))
    assert docs[0]["authors"] == ["Alice Example"]
    assert docs[0]["event_date"] == "2026-09-20"
    assert docs[0]["url"].startswith("https://")


def test_prospects_reject_guessed_profile_and_shared_article(monkeypatch):
    d = document()

    async def post(*args):
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "prospects": [
                                {
                                    "name": "Alice Example",
                                    "profile_url": d["url"],
                                    "source_id": "s1",
                                    "identity_excerpt": d["text"],
                                },
                                {
                                    "name": "Alice Example",
                                    "profile_url": "https://linkedin.com/in/guessed",
                                    "source_id": "s1",
                                    "identity_excerpt": d["text"],
                                },
                            ]
                        }
                    ),
                }
            ]
        }

    monkeypatch.setattr(p, "_post", post)
    prospects = run(
        p.extract_prospects([d], {"criteria": ["Research"]}, {"anthropic_key": "test"})
    )
    assert len(prospects) == 1
    assert prospects[0]["identity_status"] == "unknown"
    d["title"] = "Many researchers at an event"
    assert run(p.extract_prospects([d], {}, {"anthropic_key": "test"})) == []


def test_controller_uses_attributable_case_studies(monkeypatch):
    urls = []

    async def fetch(url):
        urls.append(url)
        return document() | {"url": url}

    monkeypatch.setattr(p, "fetch_document", fetch)
    docs = run(p.discover({"id": "controller"}, {}))
    assert urls == [
        "https://ramp.com/customers/zola",
        "https://ramp.com/customers/the-second-city",
    ]
    assert all(d["source_kind"] == "finance_vendor_case_study" for d in docs)


def test_case_study_name_resolves_to_actual_profile_with_shared_budget(monkeypatch):
    calls = []
    case = {
        "url": "https://example.com/case-study",
        "title": "Closing the books faster",
        "text": "Alice Example, Controller at Acme, rebuilt the accounting close.",
    }
    profile = {
        "url": "https://example.com/team/alice",
        "title": "Alice Example - Acme Controller",
        "text": "Alice Example is Controller at Acme, where she leads accounting.",
    }

    async def post(url, key, payload, provider, budget, workspace_id=None):
        budget.take()
        calls.append(provider)
        if provider == "Anthropic":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "prospects": [
                                    {
                                        "name": "Alice Example",
                                        "source_id": p.source_id(case["url"]),
                                        "identity_excerpt": case["text"],
                                        "employer": "Acme",
                                    }
                                ]
                            }
                        ),
                    }
                ]
            }
        return {
            "results": [case if payload["query"].startswith("Find named") else profile]
        }

    monkeypatch.setattr(p, "_post", post)
    budget = p.Budget({"max_calls_per_run": 3})
    settings = {"exa_key": "test", "anthropic_key": "test", "_budget": budget}

    async def scenario():
        sources = await p.discover(
            {"id": "controller", "title": "Global Controller"}, settings
        )
        return await p.extract_prospects(
            sources,
            {
                "id": "controller",
                "title": "Global Controller",
                "criteria": ["Accounting"],
            },
            settings,
        )

    prospects = run(scenario())
    assert calls == ["Exa", "Anthropic", "Exa"]
    assert budget.used == 3
    assert prospects[0]["profile_url"] == profile["url"]
    assert prospects[0]["identity_status"] == "unknown"
    assert len(prospects[0]["evidence"]) == 2
    assert all(not e["verified"] for e in prospects[0]["evidence"])


@pytest.mark.parametrize("ambiguous", [True, False])
def test_shared_article_rejects_ambiguous_or_missing_profiles(monkeypatch, ambiguous):
    case = document() | {
        "title": "A story about finance",
        "text": "Alice Example leads accounting at Acme.",
    }

    async def post(url, key, payload, provider, budget, workspace_id=None):
        budget.take()
        if provider == "Anthropic":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "prospects": [
                                    {
                                        "name": "Alice Example",
                                        "source_id": case["id"],
                                        "identity_excerpt": case["text"],
                                        "employer": "Acme",
                                    }
                                ]
                            }
                        ),
                    }
                ]
            }
        rows = (
            [
                {
                    "url": f"https://example.com/profile/{i}",
                    "title": "Alice Example",
                    "text": "Alice Example leads accounting at Acme.",
                }
                for i in range(2)
            ]
            if ambiguous
            else []
        )
        return {"results": rows}

    monkeypatch.setattr(p, "_post", post)
    budget = p.Budget({})
    assert (
        run(
            p.extract_prospects(
                [case],
                {"id": "controller"},
                {"anthropic_key": "test", "exa_key": "test", "_budget": budget},
            )
        )
        == []
    )
    assert budget.warnings


def test_exhausted_shared_budget_prevents_profile_resolution(monkeypatch):
    case = document() | {"title": "A shared research paper"}

    async def post(url, key, payload, provider, budget, workspace_id=None):
        budget.take()
        assert provider == "Anthropic"
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "prospects": [
                                {
                                    "name": "Alice Example",
                                    "source_id": "s1",
                                    "identity_excerpt": case["text"],
                                }
                            ]
                        }
                    ),
                }
            ]
        }

    monkeypatch.setattr(p, "_post", post)
    budget = p.Budget({"max_calls_per_run": 2})
    budget.take()  # discovery already used one call
    assert (
        run(
            p.extract_prospects(
                [case],
                {"id": "mts-research"},
                {"anthropic_key": "test", "exa_key": "test", "_budget": budget},
            )
        )
        == []
    )
    assert budget.used == 2
    assert budget.warnings


def test_native_arxiv_query_requires_topic_and_relevant_categories(monkeypatch):
    async def fetch(url):
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(url).query)["search_query"][0]
        assert "ti:world AND ti:model" in query
        assert "abs:action-conditioned" in query
        assert all(
            "cat:" + cat in query for cat in ("cs.LG", "cs.AI", "cs.CV", "cs.RO")
        )
        return {"text": '<feed xmlns="http://www.w3.org/2005/Atom" />'}

    monkeypatch.setattr(p, "fetch_document", fetch)
    assert run(p.discover({"id": "mts-research"}, {})) == []


def test_current_contents_preserves_full_text_and_partial_statuses(monkeypatch):
    good = "https://example.com/good"
    failed = "https://example.com/failed"
    missing = "https://example.com/missing"
    full_text = "Useful page content. " * 2000

    def handler(req):
        assert str(req.url) == "https://api.exa.ai/contents"
        assert json.loads(req.content) == {
            "urls": [good, failed, missing],
            "text": True,
            "maxAgeHours": 0,
            "livecrawlTimeout": 10000,
        }
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": good, "text": full_text, "publishedDate": "2026-09-20"},
                    {"url": failed, "text": "Stale content from a failed crawl"},
                ],
                "statuses": [
                    {"id": good, "status": "success", "source": "live"},
                    {
                        "id": failed,
                        "status": "error",
                        "error": {"tag": "CRAWL_TIMEOUT", "message": "SECRET"},
                    },
                ],
            },
        )

    fake_client(monkeypatch, handler)
    budget = p.Budget({})
    result = run(
        p.fetch_current_documents([good, failed, missing], {"exa_key": "test"}, budget)
    )
    assert budget.used == 1
    assert len(result["documents"]) == 1
    doc = result["documents"][0]
    assert doc["text"] == full_text
    assert doc["content_kind"] == "full_text"
    assert doc["extraction_method"] == "exa_contents"
    assert doc["truncated"] is False
    assert doc["event_date"] is None
    assert [row["status"] for row in result["statuses"]] == [
        "success",
        "error",
        "error",
    ]
    assert result["statuses"][1]["error"] == "Source crawl timed out."
    assert "SECRET" not in json.dumps(result)


def test_search_highlights_do_not_claim_full_page_baseline():
    doc = p._exa_docs(
        {
            "results": [
                {
                    "url": "https://example.com",
                    "highlights": ["One excerpt", "Another excerpt"],
                }
            ]
        },
        None,
    )[0]
    assert doc["content_kind"] == "highlights"
    assert doc["extraction_method"] == "exa_search"
    assert doc["text"] == "One excerpt Another excerpt"
    assert (
        p._exa_docs(
            {"results": [{"url": doc["url"], "highlights": ["Excerpt"]}]},
            None,
            full_text=True,
        )
        == []
    )


@pytest.mark.parametrize("limit", [2, 3, 4, 8])
def test_research_expands_sources_without_spending_claude_budget(monkeypatch, limit):
    calls = []
    profile = "https://example.com/profile"
    work = "https://example.com/work"
    full_text = "Alice Example released a model with actions. " * 1000

    def handler(req):
        body = json.loads(req.content)
        calls.append(req.url.path)
        if req.url.path == "/contents":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": url, "text": full_text, "title": "Alice Example"}
                        for url in body["urls"]
                    ]
                },
            )
        if req.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": profile, "highlights": ["Sparse profile excerpt"]},
                        {"url": work, "highlights": ["Sparse work excerpt"]},
                    ]
                },
            )
        assert req.url.path == "/v1/messages"
        packet = json.loads(body["messages"][0]["content"])["documents"]
        assert all(doc["content_kind"] == "full_text" for doc in packet)
        assert packet[0]["text"] == full_text
        proposal = {
            "evidence": [
                {
                    "id": packet[0]["id"],
                    "url": profile,
                    "excerpt": "Alice Example released a model with actions.",
                }
            ]
        }
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": json.dumps(proposal)}]}
        )

    fake_client(monkeypatch, handler)
    result = run(
        p.research(
            {"name": "Alice Example", "profile_url": profile},
            {},
            {
                "anthropic_key": "test",
                "exa_key": "test",
                "max_calls_per_run": limit,
            },
        )
    )
    assert calls[0] == "/contents"
    assert calls[-1] == "/v1/messages"
    assert len(calls) <= limit
    assert result["_usage"]["provider_calls"] == len(calls)
    assert (
        result["evidence"][0]["excerpt"]
        == "Alice Example released a model with actions."
    )
    assert all(doc["text"] == full_text for doc in result["_documents"])
    if limit >= 4:
        assert calls.count("/contents") == 2
        assert len(result["_documents"]) == 2
    assert all(
        row["status"] == "success" for row in result["_usage"]["source_statuses"]
    )


def test_full_baseline_survives_model_packet_cap_without_grounding_unseen_quote(
    monkeypatch,
):
    url = "https://example.com/long-profile"
    unseen_quote = "This sentence was beyond the model packet limit."
    full_text = "A" * 140000 + unseen_quote

    def handler(req):
        if req.url.path == "/contents":
            return httpx.Response(
                200, json={"results": [{"url": url, "text": full_text}]}
            )
        payload = json.loads(req.content)
        packet = json.loads(payload["messages"][0]["content"])["documents"]
        assert len(packet[0]["text"]) == 140000
        assert packet[0]["truncated"] is True
        proposal = {
            "evidence": [{"id": p.source_id(url), "url": url, "excerpt": unseen_quote}]
        }
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": json.dumps(proposal)}]}
        )

    fake_client(monkeypatch, handler)
    result = run(
        p.research(
            {"profile_url": url},
            {},
            {
                "anthropic_key": "test",
                "exa_key": "test",
                "max_calls_per_run": 2,
            },
        )
    )
    assert result["_documents"][0]["text"] == full_text
    assert result["_documents"][0]["truncated"] is False
    assert result["evidence"] == []
    assert any(
        "Source text capped" in warning for warning in result["_usage"]["warnings"]
    )


def test_contents_provider_failure_records_each_url_without_response_body(monkeypatch):
    urls = ["https://example.com/a", "https://example.com/b"]
    fake_client(
        monkeypatch, lambda req: httpx.Response(500, text="SECRET PROVIDER BODY")
    )
    result = run(p.fetch_current_documents(urls, {"exa_key": "test"}, p.Budget({})))
    assert result["documents"] == []
    assert [row["url"] for row in result["statuses"]] == urls
    assert all(row["status"] == "error" for row in result["statuses"])
    assert "SECRET" not in json.dumps(result)


def test_research_expands_top_hits_across_query_topics(monkeypatch):
    search_number = 0
    expanded = []

    def handler(req):
        nonlocal search_number
        payload = json.loads(req.content)
        if req.url.path == "/search":
            search_number += 1
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": f"https://example.com/topic-{search_number}/hit-{rank}",
                            "highlights": ["Useful excerpt"],
                        }
                        for rank in range(5)
                    ]
                },
            )
        if req.url.path == "/contents":
            expanded.extend(payload["urls"])
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": url, "text": "Current full text for this source."}
                        for url in payload["urls"]
                    ]
                },
            )
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}]})

    fake_client(monkeypatch, handler)
    run(
        p.research(
            {"name": "Alice Example"},
            {},
            {
                "anthropic_key": "test",
                "exa_key": "test",
                "max_calls_per_run": 8,
            },
        )
    )
    assert expanded == [
        f"https://example.com/topic-{number}/hit-0" for number in range(1, 5)
    ]


def highlight_document():
    return p._exa_docs(
        {
            "results": [
                {
                    "url": "https://example.com/alice",
                    "title": "Alice Example",
                    "highlights": [
                        "Alice Example released a world model",
                        "on September 20, 2026. She worked on actions.",
                    ],
                }
            ]
        },
        None,
    )[0]


def test_cross_highlight_evidence_quote_is_rejected():
    doc = highlight_document()
    assert len(doc["highlight_segments"]) == 2
    fabricated = "Alice Example released a world model on September 20, 2026."
    assert fabricated in doc["text"]
    raw = {"evidence": [{"id": doc["id"], "url": doc["url"], "excerpt": fabricated}]}
    assert p._sanitize(raw, [doc], {})["evidence"] == []
    raw["evidence"][0]["excerpt"] = doc["highlight_segments"][0]
    assert len(p._sanitize(raw, [doc], {})["evidence"]) == 1
    # Real full-page text remains eligible even if the same source carried highlights.
    doc["content_kind"] = "full_text"
    raw["evidence"][0]["excerpt"] = fabricated
    assert len(p._sanitize(raw, [doc], {})["evidence"]) == 1


def test_cross_highlight_date_quote_cannot_create_active_event():
    doc = highlight_document()
    raw = {
        "evidence": [
            {
                "id": doc["id"],
                "url": doc["url"],
                "excerpt": doc["highlight_segments"][0],
            }
        ],
        "events": [
            {
                "title": "Release",
                "date": "2026-09-20",
                "date_quote": doc["text"],
                "evidence_ids": [doc["id"]],
                "window_start": "2026-09-20T00:00:00Z",
                "window_end": "2026-09-30T00:00:00Z",
            }
        ],
    }
    event = p._sanitize(raw, [doc], {})["events"][0]
    assert event["date"] is None
    assert "window_start" not in event
    raw["events"][0]["date_quote"] = doc["highlight_segments"][1]
    assert p._sanitize(raw, [doc], {})["events"][0]["date"] == "2026-09-20"


def test_highlights_without_original_segments_fail_closed():
    doc = highlight_document()
    del doc["highlight_segments"]
    assert not p._grounded_quote(doc, "Alice Example released a world model")


def test_research_larger_output_budget_still_rejects_truncated_proposal(monkeypatch):
    async def fetch(url):
        return document()

    def handler(req):
        payload = json.loads(req.content)
        assert payload["max_tokens"] == 16000
        assert payload["system"] == [p.cached(p.RESEARCH_SYSTEM)]  # cached; today's date is in the user turn
        system = payload["system"][0]["text"]
        assert "at most 12 evidence entries and 4 events" in system
        assert "Avoid redundant repetition" in system
        assert "at most 130 words" in system
        assert "one low-pressure question" in system
        assert "at most 200 words" in system
        assert "Identity verification remains a human gate" in system
        return httpx.Response(
            200,
            json={
                "stop_reason": "max_tokens",
                "content": [{"type": "text", "text": "{}"}],
            },
        )

    monkeypatch.setattr(p, "fetch_document", fetch)
    fake_client(monkeypatch, handler)
    with pytest.raises(p.ProviderError, match="output limit"):
        run(
            p.research(
                {"profile_url": document()["url"]}, {}, {"anthropic_key": "test"}
            )
        )


@pytest.mark.parametrize(
    "provider,timeout", [("Anthropic", 300), ("Exa", 120)]
)
def test_provider_timeout_allows_longer_claude_generation(
    monkeypatch, provider, timeout
):
    real = httpx.AsyncClient
    seen = []

    def client(**kwargs):
        seen.append(kwargs["timeout"])
        return real(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})),
            **kwargs,
        )

    monkeypatch.setattr(p.httpx, "AsyncClient", client)
    run(p._post("https://example.com", "test", {}, provider, p.Budget({})))
    assert seen == [timeout]


@pytest.mark.parametrize("employer", ["Zola", "Unknown"])
def test_research_queries_use_unverified_identity_anchors(monkeypatch, employer):
    queries = []
    profile = "https://example.com/team/joe-horn"

    def handler(req):
        payload = json.loads(req.content)
        if req.url.path == "/contents":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": profile, "text": "Joe Horn's professional biography."}
                    ]
                },
            )
        if req.url.path == "/search":
            assert set(payload) == {"query", "type", "contents"}
            queries.append(payload["query"])
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}]})

    fake_client(monkeypatch, handler)
    run(
        p.research(
            {"name": "Joe Horn", "employer": employer, "profile_url": profile},
            {},
            {
                "anthropic_key": "test",
                "exa_key": "test",
                "research_question": "Recent work",
                "max_calls_per_run": 8,
            },
        )
    )
    assert len(queries) == 4
    assert all(profile in query and '"Joe Horn"' in query for query in queries)
    assert all(
        "unverified; not proof of current employment" in query for query in queries
    )
    if employer == "Zola":
        assert all("supplied employer hint Zola" in query for query in queries)
    else:
        assert all("supplied employer hint" not in query for query in queries)


def test_current_contents_routes_arxiv_abstract_to_native_with_date(monkeypatch):
    paper = "https://arxiv.org/abs/2609.22055v1"
    other = "https://example.com/profile"
    native_urls = []

    async def fetch(url):
        native_urls.append(url)
        return document() | {
            "id": p.source_id(url),
            "url": url,
            "text": "[Submitted on 18 Sep 2026] Alice Example released a world model.",
        }

    def handler(req):
        assert json.loads(req.content)["urls"] == [other]
        return httpx.Response(
            200,
            json={
                "results": [{"url": other, "text": "A short valid professional post."}]
            },
        )

    monkeypatch.setattr(p, "fetch_document", fetch)
    fake_client(monkeypatch, handler)
    budget = p.Budget({})
    result = run(p.fetch_current_documents([other, paper], {"exa_key": "test"}, budget))
    assert native_urls == [paper]
    assert budget.used == 2
    docs = {doc["url"]: doc for doc in result["documents"]}
    assert docs[paper]["extraction_method"] == "native"
    assert docs[other]["extraction_method"] == "exa_contents"
    quote = "[Submitted on 18 Sep 2026]"
    assert quote in docs[paper]["text"]
    proposal = p._sanitize(
        {
            "evidence": [{"id": docs[paper]["id"], "url": paper, "excerpt": quote}],
            "events": [
                {
                    "title": "Paper submitted",
                    "date": "2026-09-18",
                    "date_quote": quote,
                    "evidence_ids": [docs[paper]["id"]],
                }
            ],
        },
        result["documents"],
        {},
    )
    assert proposal["events"][0]["date"] == "2026-09-18"


def test_native_arxiv_retrieval_respects_reserved_claude_call(monkeypatch):
    urls = [f"https://arxiv.org/abs/2609.2205{i}v1" for i in range(3)]
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return document() | {"url": url}

    monkeypatch.setattr(p, "fetch_document", fetch)
    budget = p.Budget({"max_calls_per_run": 2})
    result = run(
        p.fetch_current_documents(urls, {"exa_key": "test"}, budget, reserved_calls=1)
    )
    assert fetched == urls[:1]
    assert budget.used == 1
    assert [row["status"] for row in result["statuses"]] == [
        "success",
        "error",
        "error",
    ]
    budget.take()  # The final Claude request still fits.


@pytest.mark.parametrize("use_exa", [True, False])
def test_short_ashby_extraction_is_inadequate_but_short_posts_are_valid(
    monkeypatch, use_exa
):
    job = "https://jobs.ashbyhq.com/company/job-id"
    post = "https://example.com/short-post"
    text = "Research Scientist at General Intuition"

    async def fetch(url):
        return document() | {"url": url, "text": text}

    def handler(req):
        return httpx.Response(
            200, json={"results": [{"url": url, "text": text} for url in [job, post]]}
        )

    monkeypatch.setattr(p, "fetch_document", fetch)
    fake_client(monkeypatch, handler)
    result = run(
        p.fetch_current_documents(
            [job, post], {"exa_key": "test"} if use_exa else {}, p.Budget({})
        )
    )
    assert [doc["url"] for doc in result["documents"]] == [post]
    outcomes = {row["url"]: row for row in result["statuses"]}
    assert outcomes[job]["status"] == "error"
    assert "full job description" in outcomes[job]["error"]
    assert outcomes[post]["status"] == "success"


def timing_proposal(text=None, **event_overrides):
    doc = document()
    doc["text"] += " " + INVITATION
    if text is not None:
        doc["text"] = text
    event = {
        "title": "Action world model release",
        "kind": "project_release",
        "scope": "professional",
        "date": "2026-09-20",
        "date_quote": doc["text"],
        "evidence_ids": [doc["id"]],
        "timing_action": "contact_now",
        "timing_assessment": assessment(doc["id"]),
        **purpose(doc["id"]),
        "recipient_value": "Compare her action-conditioning results with GI's action-labeled gaming setting.",
        "why_now": "The newly released results expose a concrete transfer question to discuss.",
        "why_wait": "Her release may already be superseded or she may prefer to finish follow-up experiments.",
        "falsifier": "The reported contribution belongs to a different author or has been withdrawn.",
    } | event_overrides
    raw = {
        "evidence": [{"id": doc["id"], "url": doc["url"], "excerpt": doc["text"]}],
        "events": [event],
    }
    return raw, doc


def test_contact_now_discards_every_model_window_even_when_date_is_grounded():
    raw, doc = timing_proposal(
        window_start="2026-09-22T00:00:00Z",
        window_end="2026-10-19T00:00:00Z",
    )
    event = p._sanitize(raw, [doc], {})["events"][0]
    assert event["date"] == "2026-09-20"
    assert event["timing_action"] == "contact_now"
    assert "window_start" not in event and "window_end" not in event
    assert event["career_openness"] == "unknown"
    assert event["follow_up_on"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"date": "2026-09-21"},  # Crawl date is not in the professional event quote.
        {"recipient_value": ""},
        {"why_now": "Great fit"},
        {"why_wait": ""},
        {"falsifier": ""},
        {"timing_action": "optimal_window"},
        {"timing_action": None},
        {"timing_action": []},
    ],
)
def test_unsupported_contact_recommendation_stays_watch(overrides):
    raw, doc = timing_proposal(**overrides)
    event = p._sanitize(raw, [doc], {})["events"][0]
    assert event["timing_action"] == "watch"
    assert "window_start" not in event


def test_unchanged_old_release_keeps_its_old_date_when_retrieved_today():
    raw, doc = timing_proposal(
        text="Alice released the system on September 20, 2025.",
        date="2025-09-20",
        timing_action="watch",
    )
    event = p._sanitize(raw, [doc], {})["events"][0]
    assert event["date"] == "2025-09-20"
    assert event["timing_action"] == "watch"
    assert doc["observed_at"].startswith("2026-09-21")


def test_conference_date_alone_does_not_establish_contact_now_or_followup():
    quote = (
        "On September 20, 2026, Alice announced her October 4, 2026 conference talk."
    )
    raw, doc = timing_proposal(text=quote, kind="conference_announcement")
    event = p._sanitize(raw, [doc], {})["events"][0]
    assert event["timing_action"] == "watch"
    assert event["follow_up_on"] is None


@pytest.mark.parametrize(
    "quote,expected",
    [
        (
            "On September 20, 2026, Alice wrote: Please contact me after October 4, 2026.",
            "follow_up",
        ),
        (
            "On September 20, 2026, Alice wrote: Please follow up with me on October 4, 2026.",
            "follow_up",
        ),
        # The same date forms engine.decide reads (models.day_in_quote).
        ("On September 20, 2026, Alice wrote: Please contact me after Oct 4 2026.", "follow_up"),
        ("On September 20, 2026, Alice wrote: Please contact me after October 04,\n2026.", "follow_up"),
        (
            "On September 20, 2026, Alice announced she will attend a conference on October 4, 2026.",
            "watch",
        ),
        (
            "On September 20, 2026, Alice said: I will meet conference speakers on October 4, 2026.",
            "watch",
        ),
        (
            "On September 20, 2026, Alice said: I will follow up with the team on October 4, 2026.",
            "watch",
        ),
        (
            "On September 20, 2026, Alice wrote: Do not contact me after October 4, 2026.",
            "watch",
        ),
        ("On September 20, 2026, Alice wrote: Please contact me after 14 October 2026.", "watch"),
    ],
)
def test_followup_requires_grounded_contact_instruction_not_attendance(quote, expected):
    raw, doc = timing_proposal(
        text=quote,
        timing_action="follow_up",
        follow_up_on="2026-10-04",
        follow_up_quote=quote,
        **purpose("src-test", quote),
    )
    raw["events"][0].update(purpose(doc["id"], quote))
    event = p._sanitize(raw, [doc], {})["events"][0]
    assert event["timing_action"] == expected
    assert event["follow_up_on"] == ("2026-10-04" if expected == "follow_up" else None)
    assert event["follow_up_quote"] == (quote if expected == "follow_up" else "")


def test_a_day_counts_only_as_a_whole_date_in_the_quote():
    from app.models import day_in_quote

    assert day_in_quote("2026-10-04", "after 4 Oct 2026") and day_in_quote("2026-10-04", "on October 04,\n2026")
    assert not day_in_quote("2026-10-04", "after 14 October 2026")
    assert not day_in_quote("2026-10-01", "on 2026-10-15") and not day_in_quote("2026-10-02", "until 2026-10-20")


@pytest.mark.parametrize(
    "mismatch", ["invented_quote", "wrong_date", "outside_excerpt", "cross_highlights"]
)
def test_followup_grounding_failures_do_not_schedule_contact(mismatch):
    quote = (
        "On September 20, 2026, Alice wrote: Please contact me after October 4, 2026."
    )
    raw, doc = timing_proposal(
        text=quote,
        timing_action="follow_up",
        follow_up_on="2026-10-04",
        follow_up_quote=quote,
    )
    if mismatch == "invented_quote":
        raw["events"][0]["follow_up_quote"] = (
            "Please contact me after October 4, 2026, about a new job."
        )
    elif mismatch == "wrong_date":
        raw["events"][0]["follow_up_on"] = "2026-10-05"
    elif mismatch == "outside_excerpt":
        raw["evidence"][0]["excerpt"] = "On September 20, 2026, Alice wrote:"
    else:
        doc["content_kind"] = "highlights"
        doc["highlight_segments"] = [
            "On September 20, 2026, Alice wrote:",
            "Please contact me after October 4, 2026.",
        ]
        raw["evidence"][0]["excerpt"] = doc["highlight_segments"][1]
    event = p._sanitize(raw, [doc], {})["events"][0]
    assert event["timing_action"] == "watch"
    assert event["follow_up_on"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope": "personal"},
        {"kind": "volunteer_event"},
        {"title": "PSI Climb volunteer fundraising event"},
        {"why_now": "Her charity event is finished so she has time to talk."},
    ],
)
def test_nonprofessional_activity_is_not_a_timing_event(overrides):
    raw, doc = timing_proposal(**overrides)
    assert p._sanitize(raw, [doc], {})["events"] == []


def test_professional_model_family_description_is_not_personal_activity():
    raw, doc = timing_proposal(title="Release of a new world model family")
    assert len(p._sanitize(raw, [doc], {})["events"]) == 1


def test_legacy_event_without_action_does_not_get_promoted_by_old_window():
    raw, doc = timing_proposal(
        window_start="2026-09-20T00:00:00Z", window_end="2026-09-30T00:00:00Z"
    )
    del raw["events"][0]["timing_action"]
    event = p._sanitize(raw, [doc], {})["events"][0]
    assert event["timing_action"] == "watch"
    assert "window_end" not in event


@pytest.mark.parametrize("case", ["missing", "invention", "company_context", "partial", "paper_alone"])
def test_timing_assessment_is_quote_grounded_before_contact_now(case):
    raw, doc = timing_proposal()
    event = raw["events"][0]
    if case == "missing":
        event.pop("timing_assessment")
    elif case == "invention":
        event["timing_assessment"]["waiting_cost"]["quote"] = "Tomorrow I will close all external conversations."
    elif case == "company_context":
        event["timing_assessment"]["mechanism"] = "context_only"
    elif case == "partial":
        del event["timing_assessment"]["waiting_cost"]
    else:
        doc["text"] = "Alice released the world model paper on September 20, 2026."
        raw["evidence"][0]["excerpt"] = doc["text"]
        event["date_quote"] = doc["text"]
    result = p._sanitize(raw, [doc], {})["events"][0]
    assert result["date"] == "2026-09-20"
    assert result["timing_action"] == "watch"
    assert result["timing_assessment"] is None


def test_timing_assessment_quotes_cannot_be_joined_across_highlights():
    raw, doc = timing_proposal()
    doc["content_kind"] = "highlights"
    doc["highlight_segments"] = ["Alice released the world model on September 20, 2026.", *INVITATION.split("; ")]
    # Evidence must itself be a valid single-segment quote, and the synthesized
    # assessment quote cannot cite outside that captured excerpt.
    doc["text"] = " ".join(doc["highlight_segments"])
    raw["evidence"][0]["excerpt"] = doc["highlight_segments"][0]
    raw["events"][0]["date_quote"] = doc["highlight_segments"][0]
    result = p._sanitize(raw, [doc], {})["events"][0]
    assert result["timing_action"] == "watch"
    assert result["timing_assessment"] is None


def test_a_budget_adds_up_the_tokens_anthropic_reports_and_prices_the_cache(monkeypatch):
    usage = {"input_tokens": 1200, "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 0,
             "output_tokens": 80, "cache_creation": {"ephemeral_5m_input_tokens": 2000}, "service_tier": "standard"}
    fake_client(monkeypatch, lambda req: httpx.Response(200, json={"usage": usage}))
    budget = p.Budget({})
    for _ in range(2):
        asyncio.run(p._post("https://api.anthropic.com/v1/messages", "test-key", {}, "Anthropic", budget))
    counted = {"input_tokens": 2400, "cache_creation_input_tokens": 4000, "cache_read_input_tokens": 0,
               "output_tokens": 160}
    assert budget.used == 2 and budget.tokens == counted
    asyncio.run(p._post("https://api.exa.ai/search", "test-key", {}, "Exa", budget))
    assert budget.tokens == counted  # only Anthropic's usage is tokens
    # a cache write costs a quarter more than plain input, a read a tenth of it
    assert p.usd({"input_tokens": 1_000_000}, 2.0, 10.0) == 2.0
    assert p.usd({"cache_creation_input_tokens": 1_000_000}, 2.0, 10.0) == 2.5
    assert p.usd({"cache_read_input_tokens": 1_000_000, "output_tokens": 1_000_000}, 2.0, 10.0) == pytest.approx(10.2)
