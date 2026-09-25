"""Helpers shared by the paper-record sources (arXiv, OpenReview, OpenAlex)."""

from ..timeline import TimelineEvent
from .http import content_hash, decode


def fetch_text(fetch, url: str) -> tuple[str, str]:
    """Body as text plus the sha256 of the raw bytes, the event's source_version_hash."""
    body = fetch(url)
    return decode(body), content_hash(body)


def person_event(subject_id: str, event_type: str, event_date: str, precision: str, observed_at: str,
                 source_url: str, version_hash: str, quote: str, extractor: str) -> dict:
    """Tier-1 event for a dated public record.

    observed_at is the record's own public timestamp, never the fetch time:
    app.timeline filters replay on observed_at, so a fetch-time stamp would
    hide every historical record from an as_of view.
    """
    return TimelineEvent(
        subject_type="person", subject_id=subject_id, event_type=event_type,
        event_date=event_date, date_precision=precision, observed_at=observed_at,
        source_url=source_url, source_version_hash=version_hash, quote=quote[:300],
        tier=1, extractor=extractor,
    ).model_dump()
