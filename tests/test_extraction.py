import asyncio

import pytest

from app import extraction, providers
from app.timeline import TimelineEvent

PAGE = (
    "Dana Reyes joined Northwind as VP Finance on March 3, 2026. "
    "She previously led accounting at Contoso. "
    "Northwind laid off 40 staff in 2025. "
    "She will speak at CFO Summit in June 2026."
)
SUBJECT = {"type": "person", "id": "dana-reyes", "name": "Dana Reyes"}
URL = "https://example.com/news/dana"


def run(coro):
    return asyncio.run(coro)


def extract(model, store, settings, **kwargs):
    return run(extraction.extract_events(
        PAGE, URL, "2026-09-22T10:00:00+00:00", SUBJECT,
        settings=settings, store=store, **kwargs,
    ))


def test_events_are_grounded_quotes_with_dates_at_stated_precision(model, store, settings):
    model.reply({"events": [
        {"event_type": "job_started", "event_date": "2026-03-03", "about_subject": True,
         "quote": "Dana Reyes joined Northwind as VP Finance on March 3, 2026."},
        {"event_type": "talk_or_conference", "event_date": "2026-06", "about_subject": True,
         "quote": "She will speak at CFO Summit in June 2026."},
        # Company event, not about the subject.
        {"event_type": "layoff", "event_date": "2025", "about_subject": False,
         "quote": "Northwind laid off 40 staff in 2025."},
        # Quote not in the page: dropped.
        {"event_type": "promotion", "event_date": "2026-04-01", "about_subject": True,
         "quote": "Dana was promoted to CFO on April 1, 2026."},
        # Date not stated in the quote: event kept, date dropped.
        {"event_type": "job_ended", "event_date": "2025-12-31", "about_subject": True,
         "quote": "She previously led accounting at Contoso."},
    ]})
    events = extract(model, store, settings)
    assert all(isinstance(e, TimelineEvent) for e in events)
    by_type = {e.event_type: e for e in events}
    assert set(by_type) == {"job_started", "talk_or_conference", "job_ended"}
    assert (by_type["job_started"].event_date, by_type["job_started"].date_precision) == ("2026-03-03", "day")
    assert (by_type["talk_or_conference"].event_date, by_type["talk_or_conference"].date_precision) == ("2026-06", "month")
    assert by_type["job_ended"].event_date is None and by_type["job_ended"].date_precision is None
    first = by_type["job_started"]
    assert first.source_version_hash == extraction.source_hash(PAGE)
    assert first.extractor == "claude_extract_v1" and first.tier == 2
    assert first.subject_id == "dana-reyes" and first.source_url == URL
    payload = model.calls[0]["payload"]
    assert payload["model"] == "claude-sonnet-5"
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] <= 4000
    assert payload["output_config"]["format"]["type"] == "json_schema"


def test_same_source_version_is_never_extracted_twice(model, store, settings):
    model.reply({"events": [
        {"event_type": "job_started", "event_date": "2026-03-03", "about_subject": True,
         "quote": "Dana Reyes joined Northwind as VP Finance on March 3, 2026."},
    ]})
    first = extract(model, store, settings)
    second = extract(model, store, settings, tier=1)
    assert len(model.calls) == 1
    assert [e.quote for e in first] == [e.quote for e in second]
    assert second[0].tier == 1  # Cache holds the grounded rows; caller fields still apply.
    model.reply({"events": []})
    changed = run(extraction.extract_events(
        PAGE + " Updated.", URL, "2026-09-23T10:00:00+00:00", SUBJECT,
        settings=settings, store=store,
    ))
    assert changed == [] and len(model.calls) == 2  # A new version is a new call.


def test_metadata_published_date_may_date_a_statement(model, store, settings):
    model.reply({"events": [
        {"event_type": "self_stated_availability", "event_date": "2026-09-01", "about_subject": True,
         "quote": "She previously led accounting at Contoso."},
    ]})
    events = extract(model, store, settings, published_at="2026-09-01T08:00:00Z")
    assert events[0].event_date == "2026-09-01"


def test_output_cap_or_bad_json_accepts_nothing(model, store, settings):
    model.reply({"events": []}, stop_reason="max_tokens")
    with pytest.raises(providers.ProviderError):
        extract(model, store, settings)
    assert store.all("extraction") == []


def test_fetched_person_pages_land_on_the_timeline_once(model, store, settings):
    from app.timeline import timeline

    model.reply({"events": [{"event_type": "job_started", "event_date": "2026-03-03", "about_subject": True,
                             "quote": "Dana Reyes joined Northwind as VP Finance on March 3, 2026."}]})
    docs = [
        {"url": URL, "text": PAGE, "observed_at": "2026-09-22T10:00:00+00:00", "content_kind": "full_text"},
        {"url": "https://example.com/h", "text": "snippet", "observed_at": "2026-09-22T10:00:00+00:00",
         "content_kind": "highlights"},
        {"url": "https://gi.example/", "text": "GI page", "observed_at": "2026-09-22T10:00:00+00:00",
         "source_kind": "company_public_context"},
    ]
    candidate = {"person_id": "dana-reyes", "name": "Dana Reyes", "profile_url": URL}
    for _ in range(2):  # second pass hits the extraction cache: no new model call
        report = run(extraction.record_documents(docs, candidate, settings=settings, store=store,
                                                 budget=providers.Budget(settings)))
    assert report == {"pages": 1, "events": 1, "errors": []}
    assert len(model.calls) == 1
    [event] = timeline(store, "dana-reyes", "2100-01-01T00:00:00Z")
    assert (event["event_type"], event["event_date"], event["tier"]) == ("job_started", "2026-03-03", 1)
