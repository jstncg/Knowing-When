import re
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """Model output that must not carry undeclared keys."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        dt.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None
        else dt.astimezone(timezone.utc)
    )


def public_url(value: str) -> str:
    u = urlsplit(value)
    if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password:
        raise ValueError("Use an http(s) public profile or evidence URL")
    return value


def canonical_url(value: str) -> str:
    u = urlsplit(public_url(value))
    return urlunsplit(
        (
            "https",
            (u.hostname or "").lower().removeprefix("www."),
            u.path.rstrip("/"),
            "",
            "",
        )
    )


class Evidence(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    url: str
    title: str = Field(default="", max_length=1000)
    excerpt: str = Field(min_length=1, max_length=2000)
    additional_excerpts: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(default_factory=list, max_length=12)
    observed_at: str = Field(default_factory=iso)
    last_checked_at: str | None = None
    event_date: str | None = None
    published_at: str | None = None
    verified: bool = False
    source_kind: str = "public_web"
    _url = field_validator("url")(public_url)

    @field_validator("observed_at")
    @classmethod
    def observation_time(cls, value):
        return iso(parse_time(value))

    @field_validator("last_checked_at")
    @classmethod
    def checked_time(cls, value):
        return iso(parse_time(value)) if value else None

    @field_validator("event_date")
    @classmethod
    def date_value(cls, value):
        if value:
            from datetime import date

            date.fromisoformat(value)
        return value


class Fit(BaseModel):
    criterion: str
    status: Literal["supported", "unknown", "contradicted"] = "unknown"
    evidence_ids: list[str] = Field(default_factory=list)


class TimingGround(BaseModel):
    """A proposed interpretation and its inspectable source, never proof of intent."""

    statement: str = Field(min_length=15, max_length=2000)
    evidence_id: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=12, max_length=2000)


class TimingAssessment(BaseModel):
    mechanism: Literal[
        "explicit_interest",
        "active_work_need",
        "professional_transition",
        "time_bounded_access",
        "context_only",
        "unknown",
    ]
    person_impact: TimingGround
    opportunity: TimingGround
    waiting_cost: TimingGround


def evidence_contains_quote(evidence: dict, quote: str, *, normalize=False) -> bool:
    """Match within one retained excerpt; never concatenate disjoint segments."""
    if not isinstance(quote, str) or not quote:
        return False
    excerpts = [evidence.get("excerpt", ""), *evidence.get("additional_excerpts", [])]
    if normalize:
        quote = " ".join(quote.casefold().split())
        excerpts = [" ".join(text.casefold().split()) for text in excerpts if isinstance(text, str)]
    return any(isinstance(text, str) and quote in text for text in excerpts)


DAY_FORMS = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y")


def day_in_quote(day: str | None, quote: str) -> bool:
    """The quote writes out this YYYY-MM-DD day ("2026-10-04", "October 4, 2026", "4 Oct 2026") as a
    whole date, never inside a longer one ("4 October" in "14 October"): a mechanical check."""
    try:
        dt = datetime.strptime(day or "", "%Y-%m-%d")
    except ValueError:
        return False
    text = " ".join(str(quote).casefold().split())
    forms = {dt.strftime(form).casefold() for form in DAY_FORMS}
    forms |= {re.sub(r"\b0(\d)\b", r"\1", form) for form in forms}
    return any(re.search(rf"(?<!\d){re.escape(form)}(?!\d)", text) for form in forms)


def follow_up_supported(event: dict, evidence: list[dict]) -> bool:
    """A source-backed day to wait for: one cited quote names the follow-up day.

    Grounding, not semantic proof that the person asked; a reviewer still reads the source.
    providers._sanitize also requires a model's quote to ask to be contacted then.
    """
    quote = event.get("follow_up_quote") or ""
    return day_in_quote(event.get("follow_up_on"), quote) and any(
        evidence_contains_quote(e, quote, normalize=True) for e in evidence)


def contact_purpose_supported(event: dict, evidence: list[dict]) -> bool:
    """Citation check only; a human must judge whether it supports this purpose."""
    if event.get("contact_purpose") not in {"rapport", "hiring"}:
        return False
    try:
        ground = TimingGround.model_validate(event.get("purpose_reason"))
    except (ValueError, TypeError):
        return False
    return ground.evidence_id in event.get("evidence_ids", []) and any(
        e["id"] == ground.evidence_id and evidence_contains_quote(e, ground.quote)
        for e in evidence
    )


def timing_assessment_supported(event: dict, evidence: list[dict]) -> bool:
    """Validate citation grounding, not semantic entailment or reachability.

    Free prose and model confidence cannot satisfy this check. A reviewer must
    still decide whether the quoted facts actually support each interpretation.
    """
    try:
        assessment = TimingAssessment.model_validate(event.get("timing_assessment"))
    except (ValueError, TypeError):
        return False
    if assessment.mechanism in {"unknown", "context_only"}:
        return False
    excerpts = {e["id"]: e for e in evidence}
    event_ids = set(event.get("evidence_ids", []))
    return all(
        ground.evidence_id in event_ids
        and ground.evidence_id in excerpts
        and evidence_contains_quote(excerpts[ground.evidence_id], ground.quote)
        for ground in (
            assessment.person_impact, assessment.opportunity, assessment.waiting_cost
        )
    )


class Event(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    kind: str = "professional_update"
    title: str = Field(min_length=1, max_length=2000)
    date: str | None = None
    date_basis: Literal["published_event_date", "observed_change_date", "source_statement_date"] = (
        "published_event_date"
    )
    date_source_id: str | None = None
    date_caveat: str = "Event date must be supported by the cited source."
    timing_action: Literal["contact_now", "verify_first", "watch", "follow_up"] = "watch"
    contact_purpose: Literal["rapport", "hiring", "unassessed"] = "unassessed"
    purpose_reason: TimingGround | None = None
    timing_assessment: TimingAssessment | None = None
    recipient_value: str = ""
    follow_up_on: str | None = None
    follow_up_quote: str = ""
    # Entailment and supersession results from verify.kill_pass.
    kill_pass: dict | None = None
    why_now: str = ""
    why_wait: str = ""
    falsifier: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    conversation_score: int = Field(default=0, ge=0, le=3)
    career_openness: Literal["unknown", "possible", "explicit"] = "unknown"
    confidence: Literal["low", "medium", "high"] = "low"

    @field_validator("date", "follow_up_on")
    @classmethod
    def day(cls, value):
        if value:
            from datetime import date

            date.fromisoformat(value)
        return value


class Route(BaseModel):
    status: Literal["none_found", "plausible_unconfirmed", "verified"] = "none_found"
    description: str = "none found"
    introducer: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class Draft(BaseModel):
    subject: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=10000)
    sender_function: str = "Relevant senior leader"
    sender_name: str = ""
    channel: Literal["email", "linkedin", "x", "introduction"] = "email"
    source_ids: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    profile_url: str
    role_id: str
    mode: Literal["live", "simulation"] = "live"
    summary: str = Field(default="", max_length=8000)
    employer: str = "Unknown"
    location: str = "Unknown"
    identity_status: Literal["unknown", "verified", "conflict"] = "unknown"
    fit: list[Fit] = Field(default_factory=list, max_length=50)
    evidence: list[Evidence] = Field(default_factory=list, max_length=100)
    events: list[Event] = Field(default_factory=list, max_length=50)
    route: Route = Field(default_factory=Route)
    draft: Draft = Field(default_factory=Draft)
    _url = field_validator("profile_url")(public_url)

    @model_validator(mode="after")
    def references(self):
        evidence = {e.id for e in self.evidence}
        if len(evidence) != len(self.evidence):
            raise ValueError("Evidence IDs must be unique within a person’s record")
        if len({e.id for e in self.events}) != len(self.events):
            raise ValueError("Event IDs must be unique within a person’s record")
        for record in [*self.fit, *self.events, self.route]:
            if not set(record.evidence_ids).issubset(evidence):
                raise ValueError(
                    "Evidence references must point to evidence in this record"
                )
        if not set(self.draft.source_ids).issubset(evidence):
            raise ValueError("Draft citations must point to evidence in this record")
        return self


class Action(BaseModel):
    action: Literal["snooze", "dismiss", "opt_out", "mark_sent", "reopen"]
    note: str = Field(default="", max_length=4000)
    until: str | None = None


class Review(BaseModel):
    revision: int
    note: str = Field(default="", max_length=4000)
    identity_confirmed: bool
    evidence_confirmed: bool
    fit_confirmed: bool
