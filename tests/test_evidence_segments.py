"""Disjoint quotes remain separate and cannot fabricate a contiguous quotation."""

import pytest
from pydantic import ValidationError

from app.models import Evidence, evidence_contains_quote, contact_purpose_supported, follow_up_supported, timing_assessment_supported
from app.providers import _sanitize

FIRST = "My research concerns robust world models."
SECOND = "I welcome conversations about a new research role."
FOLLOW = "Please contact me after October 4, 2026."


def source(highlights=False):
    return {"id": "one", "url": "https://example.test/person", "text": FIRST + " " + SECOND + " " + FOLLOW,
            "observed_at": "2026-09-22T12:00:00Z", "title": "Professional statement", "source_kind": "public_web",
            "content_kind": "highlights" if highlights else "full_text",
            "highlight_segments": [FIRST, SECOND, FOLLOW] if highlights else []}


def normalized(highlights=False, excerpts=None):
    doc = source(highlights)
    rows = [{"id": "one", "url": doc["url"], "excerpt": quote} for quote in (excerpts or [FIRST, SECOND, FOLLOW])]
    return _sanitize({"evidence": rows}, [doc], {"criteria": []})["evidence"]


def ground(quote=SECOND):
    return {"statement": "This is a proposed interpretation requiring human review.", "evidence_id": "one", "quote": quote}


def test_valid_duplicate_sources_keep_distinct_quotes_without_stitching():
    evidence = normalized()
    assert len(evidence) == 1
    assert evidence[0]["excerpt"] == FIRST
    assert evidence[0]["additional_excerpts"] == [SECOND, FOLLOW]
    Evidence.model_validate(evidence[0])
    assert evidence_contains_quote(evidence[0], SECOND)
    assert not evidence_contains_quote(evidence[0], FIRST + " " + SECOND)


def test_purpose_and_timing_checks_can_use_one_additional_excerpt():
    event = {"contact_purpose": "hiring", "purpose_reason": ground(), "evidence_ids": ["one"],
             "timing_assessment": {"mechanism": "explicit_interest", "person_impact": ground(FIRST),
                                   "opportunity": ground(), "waiting_cost": ground(FOLLOW)}}
    assert contact_purpose_supported(event, normalized())
    assert timing_assessment_supported(event, normalized())
    event["purpose_reason"] = ground(FIRST + " " + SECOND)
    event["timing_assessment"]["opportunity"] = event["purpose_reason"]
    assert not contact_purpose_supported(event, normalized())
    assert not timing_assessment_supported(event, normalized())


def test_highlights_never_accept_cross_segment_quote_even_when_joined_source_contains_it():
    cross = FIRST + " " + SECOND
    evidence = normalized(highlights=True, excerpts=[FIRST, cross, SECOND])
    assert evidence[0]["additional_excerpts"] == [SECOND]
    assert not evidence_contains_quote(evidence[0], cross)
    assert evidence_contains_quote(evidence[0], SECOND)


def test_duplicate_quotes_and_ungrounded_quotes_do_not_accumulate():
    evidence = normalized(excerpts=[FIRST, FIRST, SECOND, SECOND, "A completely invented assertion."])
    assert evidence[0]["additional_excerpts"] == [SECOND]


def test_follow_up_checks_one_additional_excerpt_not_cross_segment_text():
    event = {"follow_up_on": "2026-10-04", "follow_up_quote": FOLLOW}
    assert follow_up_supported(event, normalized())
    event["follow_up_quote"] = SECOND + " " + FOLLOW
    assert not follow_up_supported(event, normalized())


def test_excerpt_bounds_are_enforced_in_candidate_model():
    evidence = normalized()[0]
    with pytest.raises(ValidationError):
        Evidence.model_validate({**evidence, "additional_excerpts": ["x" * 2001]})
    with pytest.raises(ValidationError):
        Evidence.model_validate({**evidence, "additional_excerpts": ["x"] * 13})
