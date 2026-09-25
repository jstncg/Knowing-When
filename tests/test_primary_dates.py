"""Primary publication timestamps stay independent of model-selected events."""

import asyncio

import httpx
import pytest

from app import providers


# Shape checked against the primary /abs/2609.22055v1 HTML, 2026-09-22.
HISTORY = """
<html><head><title>Example paper</title>
<meta name="citation_arxiv_id" content="2609.22055">
<meta name="citation_date" content="2026/09/18"></head><body>
<div class="submission-history"><h2>Submission history</h2>
From: Example Author<br><strong>[v1]</strong>
Fri, 18 Sep 2026 17:46:28 UTC (907 KB)<br>
<a href="/abs/2609.22055v2">[v2]</a>
Mon, 21 Sep 2026 11:22:33 UTC (910 KB)<br></div></body></html>
"""


def metadata(suffix="", html=HISTORY):
    return providers.primary_publication_metadata(
        "https://arxiv.org/abs/2609.22055" + suffix, html
    )


def test_unversioned_and_v1_use_first_submission_not_latest_revision():
    for suffix in ("", "v1"):
        result = metadata(suffix)
        assert result["published_at"] == "2026-09-18T17:46:28+00:00"
        assert result["date_provenance"]["version"] == 1
        assert result["date_provenance"]["quote"] == "[v1] Fri, 18 Sep 2026 17:46:28 UTC"
    assert metadata("v2")["published_at"] == "2026-09-21T11:22:33+00:00"
    assert metadata("v3") == {}


def test_meta_date_or_abstract_prose_never_substitutes_for_history():
    html = '<meta name="citation_date" content="2026/09/18"><p>Published September 18, 2026.</p>'
    assert metadata(html=html) == {}


@pytest.mark.parametrize("timestamp", [
    "Fri, 32 Sep 2026 17:46:28 UTC",  # Invalid day.
    "Thu, 18 Sep 2026 17:46:28 UTC",  # Contradictory weekday.
    "Fri, 18 Sep 2026",  # No exact timestamp.
    "Fri, 18 Sep 2026 17:46:28 PST",  # Not arXiv's documented UTC shape.
])
def test_malformed_or_ambiguous_timestamp_stays_unknown(timestamp):
    assert metadata(html=HISTORY.replace("Fri, 18 Sep 2026 17:46:28 UTC", timestamp)) == {}


def test_script_and_template_dates_cannot_supply_or_replace_publication():
    injected = HISTORY.replace(
        "<h2>Submission history</h2>",
        '<h2>Submission history</h2><script>[v1] Mon, 21 Sep 2026 11:22:33 UTC</script>'
        '<template>[v1] Mon, 21 Sep 2026 11:22:33 UTC</template>',
    )
    assert metadata(html=injected)["published_at"] == metadata()["published_at"]
    assert metadata(html='<script><div class="submission-history">[v1] Fri, 18 Sep 2026 17:46:28 UTC</div></script>') == {}


def test_duplicate_versions_or_history_sections_fail_closed():
    duplicate = HISTORY.replace("</div>", "<strong>[v1]</strong> Fri, 18 Sep 2026 17:46:28 UTC</div>")
    assert metadata(html=duplicate) == {}
    assert metadata(html=HISTORY + HISTORY) == {}


@pytest.mark.parametrize("url", [
    "https://arxiv.org.evil.test/abs/2609.22055",
    "https://example.test/abs/2609.22055",
    "https://arxiv.org/search/?query=2609.22055",
    "https://arxiv.org/pdf/2609.22055",
    "https://arxiv.org/abs/2609.22055v0",
])
def test_only_primary_arxiv_abstract_urls_are_eligible(url):
    assert providers.primary_publication_metadata(url, HISTORY) == {}


def test_primary_page_identity_must_match_requested_paper():
    assert metadata(html=HISTORY.replace('content="2609.22055"', 'content="2609.99999"')) == {}


def test_legacy_arxiv_identifier_is_supported_without_guessing_missing_history():
    html = HISTORY.replace('content="2609.22055"', 'content="math.GT/0309136"')
    result = providers.primary_publication_metadata("https://arxiv.org/abs/math.GT/0309136v1", html)
    assert result["published_at"] == "2026-09-18T17:46:28+00:00"


def test_native_document_retains_exact_primary_metadata_and_no_event_inference(monkeypatch):
    async def resolver(url):
        return "93.184.216.34"

    monkeypatch.setattr(providers, "_resolve_public", resolver)
    original_client = httpx.AsyncClient

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, text=HISTORY)

    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs
    ))
    doc = asyncio.run(providers.fetch_document("https://arxiv.org/abs/2609.22055v1"))
    assert doc["published_at"] == "2026-09-18T17:46:28+00:00"
    assert doc["event_date"] is None
    assert doc["date_provenance"]["source_url"] == doc["url"]
    assert doc["date_provenance"]["method"] == "arxiv_submission_history"
    assert doc["observed_at"] != doc["published_at"]


def test_retained_text_fallback_uses_explicit_history_and_requested_version():
    text = (
        "Title Abstract Published September 22, 2026. Submission history From: Author "
        "[v1] Fri, 18 Sep 2026 17:46:28 UTC (907 KB) "
        "[v2] Mon, 21 Sep 2026 11:22:33 UTC (910 KB) "
        "References & Citations Other paper [v1] Tue, 22 Sep 2026 11:22:33 UTC"
    )
    one = providers.primary_publication_metadata_from_text("https://arxiv.org/abs/2609.22055v1", text)
    two = providers.primary_publication_metadata_from_text("https://arxiv.org/abs/2609.22055v2", text)
    assert one["published_at"] == "2026-09-18T17:46:28+00:00"
    assert two["published_at"] == "2026-09-21T11:22:33+00:00"
    assert one["date_provenance"]["method"] == "arxiv_retained_submission_history"
    assert providers.primary_publication_metadata_from_text("https://arxiv.org/abs/2609.22055v1", text + " Submission history") == {}
    assert providers.primary_publication_metadata_from_text("https://arxiv.org/abs/2609.22055v1", "Abstract: [v1] Fri, 18 Sep 2026 17:46:28 UTC") == {}
