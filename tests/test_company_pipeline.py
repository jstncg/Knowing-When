import asyncio
import json

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import company_context, main, providers
from app.fixtures import scenarios
from test_sequence_api import client, ok


def enrolled():
    roles = {r["id"].removeprefix("role:"): {**r, "id": r["id"].removeprefix("role:")} for r in main.store.all("role")}
    c = scenarios(roles)[0]
    c["mode"] = "live"
    saved = main.save_candidate(c)
    main.store.put("watch:" + saved["id"], "watch", {
        "candidate_id": saved["id"], "mode": "live", "status": "active",
        "role_id": saved["role_id"], "brief_version": roles[saved["role_id"]]["brief_version"],
    })
    return saved


def test_company_refresh_requests_deduplicate_and_status_has_no_raw_text(client):
    first = ok(client.post("/api/company-context/refresh", json={}))
    second = ok(client.post("/api/company-context/refresh", json={}))
    assert first["id"] == second["id"]
    status = ok(client.get("/api/company-context"))
    assert first["id"] in status["pending_jobs"]
    assert all("document" not in source for source in status["sources"])


def test_company_change_never_fans_out_paid_research(client, monkeypatch):
    enrolled()
    async def refresh(*args):
        return {"baselines": 0, "changed": 1}
    monkeypatch.setattr(company_context, "refresh", refresh)
    asyncio.run(main.execute_job({"kind": "company_refresh", "mode": "live"}))
    assert main.store.all("job") == []
    assert main.store.all("review_alert") == []


def test_shared_gi_sources_reach_grounded_model_without_refetch(monkeypatch):
    seen = []
    person = {"id": "person", "url": "https://example.test/person", "text": "A professional biography of the known person.", "observed_at": "2026-09-21T12:00:00Z"}
    gi = {"id": "gi", "url": "https://example.test/gi-report", "text": "A public report, not a promise of private access.", "observed_at": "2026-09-21T12:00:00Z", "source_kind": "company_public_context"}
    async def fetch(urls, *args, **kwargs):
        seen.extend(urls)
        return {"documents": [person], "statuses": []}
    async def post(url, key, payload, *args):
        packet = json.loads(payload["messages"][0]["content"])
        assert {d["id"] for d in packet["documents"]} == {"person", "gi"}
        return {"content": [{"type": "text", "text": json.dumps({"events": [], "evidence": []})}]}
    monkeypatch.setattr(providers, "fetch_current_documents", fetch)
    monkeypatch.setattr(providers, "_post", post)
    asyncio.run(providers.research({"profile_url": person["url"]}, {}, {
        "anthropic_key": "synthetic", "max_calls_per_run": 2, "company_documents": [gi],
    }))
    assert seen == [person["url"]]
