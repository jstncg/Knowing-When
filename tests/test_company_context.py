import asyncio
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import company_context as company
from app.models import iso, utcnow
from app.store import Store


@pytest.fixture(autouse=True)
def profiles(monkeypatch):
    """Invented stand-ins for the private profile list: one LinkedIn profile and eight X accounts."""
    urls = ("https://www.linkedin.com/in/gi-example", *(f"https://x.com/GIExample{n}" for n in range(8)))
    monkeypatch.setattr(company, "SEED_URLS", (company.OFFICIAL_URL, *company.OFFICIAL_ANCHOR_URLS, *urls))


@pytest.fixture
def store(tmp_path):
    value = Store(f"sqlite:///{tmp_path / 'company.sqlite'}")
    yield value
    value.engine.dispose()


def install_fetch(monkeypatch, *, texts=None, failures=None):
    calls = []
    texts = texts if texts is not None else {}
    failures = failures if failures is not None else set()

    async def fetch(urls, settings, budget, role_id=None):
        calls.append((list(urls), bool(settings.get("exa_key"))))
        for _ in range(1 if settings.get("exa_key") else len(urls)):
            budget.take()
        docs, statuses = [], []
        for url in urls:
            failed = url in failures
            statuses.append({"url": url, "status": "error" if failed else "success"})
            if not failed:
                docs.append({
                    "id": company.providers.source_id(url), "url": url,
                    "text": texts.get(url, "A public professional statement about work and research, supplied by this source."),
                    "title": "Source title", "published_at": "2026-09-20T10:00:00Z",
                    "observed_at": iso(), "source_kind": "public_web",
                    "extraction_method": "exa_contents" if settings.get("exa_key") else "native",
                })
        return {"documents": docs, "statuses": statuses}

    monkeypatch.setattr(company.providers, "fetch_current_documents", fetch)
    return calls


def reset_cursor(store):
    state = store.get(company.STATE_ID)
    store.put(company.STATE_ID, "company_context_state", {**state, "cursor": 0})


def test_seed_idempotent_and_sources_are_not_roster_assertions(store):
    first = company.seed(store)
    second = company.seed(store)
    assert first == second
    assert len(first) == len(company.SEED_URLS)
    assert first[0]["source_basis"] == "official_website"
    assert all(source["source_basis"] == "official_website_link" for source in first if source["url"] in company.OFFICIAL_ANCHOR_URLS)
    assert all(source["source_basis"] == "user_supplied_handle" for source in first if source["url"].startswith("https://x.com/"))
    assert next(source for source in first if source["url"] == "https://www.linkedin.com/in/gi-example")["source_basis"] == "public_professional_profile"
    assert "not verified employment" in company.context(store)["trust"]
    assert company.context(store)["documents"] == []


def test_bounded_rotation_includes_remaining_sources_after_failure(store, monkeypatch):
    failures = {company.SEED_URLS[2]}
    calls = install_fetch(monkeypatch, failures=failures)
    first = asyncio.run(company.refresh(store, {"exa_key": "test-secret", "max_calls_per_run": 8}))
    assert first["attempted"] == 8
    assert first["baselines"] == 7 and first["failed"] == 1 and first["changed"] == 0
    assert first["provider_calls"] == 3
    assert calls[0] == ([company.OFFICIAL_URL, company.OFFICIAL_ANCHOR_URLS[0]], False)
    assert calls[1][1] is True
    second = asyncio.run(company.refresh(store, {"exa_key": "test-secret", "max_calls_per_run": 8}))
    assert any(company.SEED_URLS[-1] in urls for urls, _ in calls[2:])
    assert second["baselines"] == len(company.SEED_URLS) - company.BATCH_SIZE
    third = asyncio.run(company.refresh(store, {"exa_key": "test-secret", "max_calls_per_run": 8}))
    assert third["failed"] == 1  # Failed sources remain in the rotation.
    assert "test-secret" not in str(company.status(store))
    assert "test-secret" not in str(store.all("company_source"))


def test_repeated_content_not_new_change_and_failure_retains_stale_document(store, monkeypatch):
    texts, failures = {}, set()
    install_fetch(monkeypatch, texts=texts, failures=failures)
    settings = {"max_calls_per_run": 1}
    first = asyncio.run(company.refresh(store, settings))
    assert first["baselines"] == 1
    reset_cursor(store)
    repeat = asyncio.run(company.refresh(store, settings))
    assert repeat["unchanged"] == 1 and repeat["changed"] == 0
    texts[company.OFFICIAL_URL] = "A changed public professional statement announcing a new research program and scope."
    reset_cursor(store)
    change = asyncio.run(company.refresh(store, settings))
    assert change["changed"] == 1
    from app.timeline import timeline
    gi = timeline(store, "gi", "2100-01-01T00:00:00Z")
    assert [e["event_type"] for e in gi] == ["gi_source_first_seen", "gi_source_changed"]
    assert gi[1]["event_date"] is None and "new research program" in gi[1]["quote"]
    before = deepcopy(store.get(company._key(company.OFFICIAL_URL)))
    failures.add(company.OFFICIAL_URL)
    reset_cursor(store)
    failure = asyncio.run(company.refresh(store, settings))
    after = store.get(company._key(company.OFFICIAL_URL))
    assert failure["failed"] == 1
    assert after["document"] == before["document"]
    assert after["last_success_at"] == before["last_success_at"]
    assert after["change_count"] == 1
    doc = company.context(store)["documents"][0]
    assert doc["stale"] and not doc["usable_for_current_claims"]


@pytest.mark.parametrize("exa_key", ["", "test-key"])
def test_one_call_limit_is_respected_without_starving_sources(store, monkeypatch, exa_key):
    install_fetch(monkeypatch)
    settings = {"exa_key": exa_key, "max_calls_per_run": 1}
    for _ in range(len(company.SEED_URLS)):
        result = asyncio.run(company.refresh(store, settings))
        assert result["provider_calls"] <= 1
        assert result["attempted"] <= 8
        assert result["failed"] == 0
    assert len(company.context(store)["documents"]) == len(company.SEED_URLS)


def test_due_after_six_hours_and_context_is_bounded_with_publication_metadata(store, monkeypatch):
    fixed = utcnow()
    monkeypatch.setattr(company, "utcnow", lambda: fixed)
    texts = {url: "Public professional research description. " * 1_000 for url in company.SEED_URLS}
    install_fetch(monkeypatch, texts=texts)
    assert company.due(store)
    asyncio.run(company.refresh(store, {"exa_key": "test-key", "max_calls_per_run": 8}))
    asyncio.run(company.refresh(store, {"exa_key": "test-key", "max_calls_per_run": 8}))
    assert not company.due(store)
    context = company.context(store)
    assert sum(len(doc["text"]) for doc in context["documents"]) <= 40_000
    assert all(doc["published_at"] == "2026-09-20T10:00:00Z" for doc in context["documents"])
    assert all(doc["id"] and doc["company_source_id"] for doc in context["documents"])
    assert all(doc["source_kind"] == "company_public_context" for doc in context["documents"])
    assert "text" not in company.status(store)["sources"][0]
    assert "document" not in company.status(store)["sources"][0]
    monkeypatch.setattr(company, "utcnow", lambda: fixed + timedelta(hours=7))
    assert company.due(store)
    assert company.status(store)["stale_or_missing"] == len(company.SEED_URLS)


def test_access_wall_is_failure_not_fresh_context_and_exception_is_sanitized(store, monkeypatch):
    install_fetch(monkeypatch, texts={company.OFFICIAL_URL: "Log in to X. Sign up or log in to read the latest posts and replies."})
    result = asyncio.run(company.refresh(store, {"max_calls_per_run": 1}))
    assert result["failed"] == 1
    assert company.context(store)["documents"] == []

    async def broken(*args, **kwargs):
        raise RuntimeError("secret-provider-key-in-error")

    monkeypatch.setattr(company.providers, "fetch_current_documents", broken)
    reset_cursor(store)
    result = asyncio.run(company.refresh(store, {"max_calls_per_run": 1}))
    assert result["failed"] == 1
    assert "secret-provider-key" not in str(company.status(store))
    assert "secret-provider-key" not in str(store.all("company_source"))


def test_extraction_method_change_resets_baseline_without_false_change(store, monkeypatch):
    install_fetch(monkeypatch)
    settings = {"exa_key": "test-key", "max_calls_per_run": 8}
    asyncio.run(company.refresh(store, settings))
    selected_url = company.OFFICIAL_ANCHOR_URLS[1]
    prior = store.get(company._key(selected_url))
    assert prior["document"]["extraction_method"] == "exa_contents"
    state = store.get(company.STATE_ID)
    store.put(company.STATE_ID, "company_context_state", {
        **state, "cursor": company.SEED_URLS.index(selected_url),
    })
    result = asyncio.run(company.refresh(store, {"max_calls_per_run": 1}))
    assert result["baselines"] == 1 and result["changed"] == 0
    assert store.get(company._key(selected_url))["change_count"] == 0


def test_exhausted_shared_budget_never_starts_another_call(store, monkeypatch):
    calls = install_fetch(monkeypatch)
    budget = company.providers.Budget({"max_calls_per_run": 1})
    budget.take()
    result = asyncio.run(company.refresh(store, {"exa_key": "test-key", "_budget": budget}))
    assert result["attempted"] == result["provider_calls"] == 0
    assert calls == [] and budget.used == budget.limit
    assert not company.due(store)  # No busy-loop retries when a run has no budget.
