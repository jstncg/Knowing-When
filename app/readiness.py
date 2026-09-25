"""Readiness: stack detector activations into one score and one recommended action.

Signals decay after their window closes. A recently closed window still counts as a reason while another is open, but
only one per reason: old closed windows of one mechanism never add up to a reason. The strongest counts; the rest add
no strength but still carry their evidence, so they make no other reason independent. Their employer's deal or layoffs
counts only while open (EMPLOYER_NEWS).

A reason is one mechanism (one detector) whose evidence no stronger reason already carries: a postdoc end and a PI
leaving are two, an acquisition and the retention cliff it sets are one. The paper mechanisms are one reason, new work:
a preprint and its acceptance are the same work, a conversation opener rather than two reasons to move.

Acting needs an open window. "Reach out now" needs two independent reasons, or one that is enough alone: the person
said it (self), a GI human heard them say they are open (private), or a forced move (a fixed-term end, their own layoff
or departure, or their employer closing). That is a pitch.

An early sign in their own words (work in progress, a technical ask, a submission not yet public) is enough on its own
for a note about their work, not a pitch. So is a public moment of the last month (their paper out, their own launch,
or their own post that their company was acquired), whatever the role; it is never a pitch, even with drift or with
another such moment, and a launch still waits out launch week (hold_imminent_launch). An early sign with a sign they
would listen (a career, employer or pattern reason, or drift toward GI's field) is a pitch. Drift alone (topic drift,
new field contacts, a posting burst) is never a reach: it has nothing to write about. Any other single weak reason is
not now.

Holds (calendar quiet periods, recent promotion, short tenure, equity refresh, imminent launch) delay a call that would
otherwise reach until the last of them lifts. verify_first is only for a named fact to check: a role-claim hold (an
announced next role, or role claims that cannot both be true) over an open window, or an open signal that carries its
own check (a late filing) when the reasons alone would not reach.

Nothing here is a probability: confidence is a label from the same rules, not a measured rate.
"""

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field

from .detectors import HEADLINE_DAYS, OPT_OUT_HOLDS, VERIFY_HOLDS, clusters, day_of, observed_day
from .models import parse_time
from .timeline import DETECTORS

# Days for a closed window's strength to halve. Career moves take a month or
# two to decide, so a departure or milestone stays warm for ~45 days. Work
# (a paper) is a conversation opener that goes stale in weeks. Employer
# shocks (a deal, a WARN notice) play out over quarters. GI-side attention is
# the most perishable. A stated date is a plan and fades like a career signal.
# A private note is human judgement and is trusted longest. An early sign (an
# ask, work in progress) is answered by someone else within weeks; drift
# toward GI's field lasts about as long as a paper does.
HALF_LIFE_DAYS = {"career": 45, "work": 30, "employer": 60, "gi": 21, "self": 45, "private": 90, "reason": 14,
                  "pattern": 45, "drift": 30}
DEFAULT_HALF_LIFE_DAYS = 30
SUPPRESSORS = {"calendar", "holds"}
SELF_EVIDENT = {"self", "private"}  # families where one open window is enough on its own
FORCED = {"placement_end", "own_departure", "company_closure"}  # forced moves, enough on their own
OPENERS = {"reason"}  # one open window is enough for a note about their work, never a pitch alone
NEWS = {"paper_v1", "paper_accepted", "launch", "acquired"}  # a public moment: a note, never a pitch alone
NEWS_DAYS = 30  # a public moment opens a note for about a month after it (app/happened.py)
# What a note about a public moment quotes: the person's own paper, launch or acquisition news, never a talk or an
# acquirer's filing.
NEWS_TYPES = {"paper_v1", "publication", "paper_accepted", "launch_announced", "project_release", "acquisition"}
SUPPORT = {"drift"}  # counts toward a pitch with an opener, never toward a reach without one
LISTENING = {"career", "employer", "pattern", *SUPPORT}  # signs they would listen: with an opener, a pitch
# An employer's record (a deal, layoffs) makes a pitch of their own paper or launch only while it is this fresh: as
# old as the stale-sign rail allows (contact.STALE_DAYS), past which the card is checked first anyway.
UPGRADE_DAYS = HEADLINE_DAYS
# Their employer's deal or layoffs is a reason only while its window is open, never on the fading strength after it,
# and its open reports are one event: one window (the newest's), one strength, the evidence of all.
EMPLOYER_NEWS = {"acquisition_closed", "warn_notice"}
# Time in a seat, an acquisition's retention cliff, the pattern that came before their past moves and a path like
# people GI hired: listed on a call, never a reason, never what makes a note a pitch, never a date to wait for, never a
# window the moment planner plans. GI does not predict who will leave (Justin's rule).
CONTEXT_ONLY = {"tenure_milestone", "retention_cliff", "own_precedent", "similar_path"}
REASON_OF = {"paper_v1": "new_work", "paper_accepted": "new_work", "paper_talk": "new_work"}  # else the detector id
MOMENTS = {"new_work", "launch", "acquired"}  # the NEWS reasons: one reason when deciding a pitch
REASON_FLOOR = 0.25   # stacked strength at which a mechanism counts as a reason
SINGLE_REASON_CAP = 0.5  # readiness on one reason that is not enough alone never exceeds this
HOLD_FACTOR = 0.3  # readiness under an active hold

STRONG = 0.7  # readiness at which a converged, unheld call is high confidence

# Plain words for each reason and hold, for everything a person reads: the explanation, the card, the screen.
LABELS = {
    "tenure_milestone": "a work anniversary in their current role", "exec_departure": "an executive left their employer",
    "pi_departure": "the head of their lab left", "team_exodus": "several colleagues left their employer",
    "coauthor_departure": "a close coauthor left their employer", "placement_end": "their fixed-term role is ending",
    "own_departure": "they left or are leaving their job", "company_closure": "their employer is closing",
    "retention_cliff": "a year or two after their employer's acquisition", "new_work": "new published work",
    "paper_v1": "a new preprint", "paper_accepted": "a paper accepted", "paper_talk": "a talk on their paper",
    "launch": "something they launched", "acquired": "they posted that their company was acquired",
    "rhythm_change": "a change in how often they publish", "own_precedent": "the pattern that came before their past moves",
    "work_in_progress": "work in progress they posted", "technical_ask": "a public ask GI could answer",
    "just_submitted": "a submission not yet public", "topic_drift": "their posts turning to GI's topics",
    "new_field_contact": "new contacts in GI's field", "posting_burst": "a burst of posting",
    "similar_path": "a path like people GI hired", "associate_joined": "a close colleague joined a lab like GI",
    "acquisition_closed": "a deal to buy their employer", "warn_notice": "layoffs at their employer",
    "auditor_change": "their employer changed auditors", "late_filing": "their employer filed its accounts late",
    "grant_end": "their grant is ending", "gi_citation": "GI's work cites theirs", "gi_attention": "GI engaged with their work",
    "self_stated_availability": "they said they are available", "stated_follow_up": "they named a date to talk",
    "private_note": "a GI teammate's note", "short_tenure": "under a year in their current role",
    "recent_promotion": "a recent promotion", "equity_refresh": "a recent equity grant",
    "retention_grant": "a retention grant not yet vested", "imminent_launch": "a launch in the next few weeks",
    "not_looking": "they said they are not looking", "accepted_next_role": "they announced their next role",
    "announced_next_role": "someone else says they have a next role",
    "conflicting_role_claims": "their role claims conflict", "quarter_close": "their quarter close",
    "audit_season": "their audit season", "conference_deadline": "a conference deadline",
    "conference_attending": "a conference they are attending",
}


def label(name):
    """A reason, detector or hold in plain words; an unknown id reads with spaces, never underscores."""
    return LABELS.get(name, name.replace("_", " "))

Action = Literal["reach_now", "verify_first", "watch_until", "respect_follow_up", "quiet"]


class Readiness(BaseModel):
    as_of: str
    score: float = Field(ge=0, le=1)
    action: Action
    until: str | None = None
    families: dict[str, float]
    # Under reach_now: "pitch" (the role is fair to raise) or "rapport" (a note that opens with their work).
    track: Literal["pitch", "rapport"] | None = None
    # Each reason's evidence event ids, in the order of reasons: what the card lists and a draft quotes.
    evidence: dict[str, list[str]] = Field(default_factory=dict)
    reasons: list[str]  # independent reasons, strongest first: detector ids, or new_work for the paper mechanisms
    holds: list[str]
    open_windows: list[str]
    earliest_close: str | None
    # The earliest-closing open signal window, as the ping reports it.
    window: dict | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    falsifiers: list[str] = Field(default_factory=list)
    explanation: str


def _at(activation, as_of):
    """Strength as of the date: full inside the window, decayed after it, none before it."""
    opens, closes = activation.get("window_open"), activation.get("window_close")
    if opens and opens > as_of:
        return 0.0
    if not closes or closes >= as_of:
        return activation["strength"]
    days = (parse_time(as_of) - parse_time(closes)) / timedelta(days=1)
    return activation["strength"] * 0.5 ** (days / HALF_LIFE_DAYS.get(activation["family"], DEFAULT_HALF_LIFE_DAYS))


def _is_open(activation, as_of):
    return _at(activation, as_of) == activation["strength"] > 0


def dedupe(activations, events=()):
    """Overlapping windows of one detector citing the same underlying event count once (the strongest wins).

    Without ``events`` only identical ids match; with them, reports of one
    event (same subject and type, dates within 2 days) match too. Separate
    windows from one event, such as the 12- and 24-month retention cliffs, stay.
    """
    cluster_of = {e["id"]: i for i, group in enumerate(clusters(events)) for e in group}
    kept = {}
    for a in sorted(activations, key=lambda a: (a["strength"], not a.get("code_only")), reverse=True):
        same = kept.setdefault((a["detector_id"], frozenset(cluster_of.get(i, i) for i in a["evidence_event_ids"])), [])
        if not any(a["window_open"] <= b["window_close"] and b["window_open"] <= a["window_close"] for b in same):
            same.append(a)  # of equal strength, one also in a post is kept over the GitHub code's copy
    return [a for same in kept.values() for a in same]


def _fresh(members, as_of):
    """One reason's strength as of the day: its open windows stacked with only the strongest of its closed ones (its
    newest, nearly always). Three posts saying the same thing months ago are one faded window, never a reason
    together; one closed window counts as it would alone."""
    def closed(a):
        return bool(a["window_close"]) and a["window_close"] < as_of
    faded = [_at(a, as_of) for a in members if closed(a)]
    return _stack([_at(a, as_of) for a in members if not closed(a)] + ([max(faded)] if faded else []))


def _stack(strengths):
    total = 1.0
    for s in strengths:
        total *= 1 - s
    return round(1 - total, 4)


def _reasons(signals, as_of):
    """Reasons at the floor whose evidence no stronger reason already carries: {reason: strength}.

    Evidence is compared by id after dedupe, so scoring deduped activations
    without the events (journey.assess) gives the same result.
    """
    by_reason = {}
    for a in signals:
        by_reason.setdefault(REASON_OF.get(a["detector_id"], a["detector_id"]), []).append(a)
    ranked = sorted(((_stack(_at(a, as_of) for a in members), reason, members)
                     for reason, members in by_reason.items()), key=lambda r: r[0], reverse=True)
    reasons, claimed = {}, set()
    for strength, reason, members in ranked:
        evidence = {i for a in members if _at(a, as_of) > 0 for i in a["evidence_event_ids"]}
        if strength >= REASON_FLOOR and evidence - claimed:
            claimed |= evidence  # as when all its windows stacked: its old windows free no other reason
            if (fresh := _fresh(members, as_of)) >= REASON_FLOOR:
                reasons[reason] = fresh
    return dict(sorted(reasons.items(), key=lambda kv: kv[1], reverse=True))  # strongest first, as counted


def _counted(signals, as_of):
    """The reasons a call counts (_reasons): a reason to write only while its window is open, since nobody writes
    about a stale post."""
    opened = {a["detector_id"] for a in signals if a["family"] in OPENERS and _is_open(a, as_of)}
    family_of = {REASON_OF.get(a["detector_id"], a["detector_id"]): a["family"] for a in signals}
    return {r: v for r, v in _reasons(signals, as_of).items() if family_of[r] not in OPENERS or r in opened}


def _code_alone(signals, as_of):
    """The early signs, drift and papers read only from GitHub items that are not releases (a push, a pull request, an
    issue, a comment; a paper counts as read from one unless the item links it), when nothing they could back up is
    open. A push can back up a post or a release but never makes the call by itself (Justin, 2026-09-24). What it
    backs up is a fresh early sign in a post, a public moment of the last NEWS_DAYS (a release, a paper) or something
    enough alone (what they said, a forced move); alone, it is listed and decides nothing."""
    code = [a for a in signals if a.get("code_only") and (a["family"] in OPENERS | SUPPORT
                                                          or REASON_OF.get(a["detector_id"]) == "new_work")]
    fresh = (parse_time(as_of) - timedelta(days=NEWS_DAYS)).isoformat()
    backed = any(_is_open(a, as_of) and (a["family"] in OPENERS or _enough_alone(a["detector_id"], a["family"])
                                         or a["detector_id"] in NEWS and (a["window_open"] or "") >= fresh)
                 for a in signals if not any(a is c for c in code))
    return [] if backed else code


def listed_only(activations, as_of, events=()):
    """The activations a call lists and never counts, as score reads them: the signs in CONTEXT_ONLY, and an early sign,
    drift or paper on GitHub code with nothing beside it that it could back up (_code_alone). The moment planner never
    plans their windows."""
    as_of = parse_time(as_of).isoformat()
    rest = dedupe(_employer_news([a for a in activations if a["detector_id"] not in CONTEXT_ONLY], as_of), events)
    code = _code_alone([a for a in rest if a["family"] not in SUPPRESSORS], as_of)
    return [a for a in activations if a["detector_id"] in CONTEXT_ONLY or any(a is c for c in code)]


def _enough_alone(detector_id, family):
    return family in SELF_EVIDENT or detector_id in FORCED


def always_counts(detector_id, family):
    """What is enough alone (what the person said, what GI heard, forced moves), early signs, public moments (NEWS),
    drift and holds count for every role; a role's compiled spec (config/role-specs) chooses among the other
    detectors."""
    return (family == "holds" or family in OPENERS | SUPPORT or detector_id in NEWS
            or _enough_alone(detector_id, family))


def opener(call):
    """The event a note quotes: the newest evidence of the strongest fresh reason to write, or None."""
    return next((ids[0] for r, ids in call.evidence.items() if ids and DETECTORS.get(r, ("",))[0] in OPENERS), None)


def news(call, events):
    """With no early sign to quote, the public moment a note quotes: the newest event behind the call whose type is
    in NEWS_TYPES and that happened in the last NEWS_DAYS, from ``events`` (the view, or the events behind the
    call); else None."""
    if opener(call):
        return None
    since = (parse_time(call.as_of) - timedelta(days=NEWS_DAYS)).date()
    behind = {i for ids in call.evidence.values() for i in ids}
    day = lambda e: day_of(e) or observed_day(e)  # noqa: E731  the day its window opens, as score dates it
    moments = [e for e in events or () if e.get("id") in behind and e.get("event_type") in NEWS_TYPES
               and day(e) >= since]
    return max(moments, key=day)["id"] if moments else None


def for_role(activations, watch):
    """The activations of the detectors a role watches, plus those that count for every role; None keeps all."""
    if watch is None:
        return activations
    return [a for a in activations if a["detector_id"] in watch or always_counts(a["detector_id"], a["family"])]


def score(activations, as_of, events=(), notes_only=frozenset()):
    """Readiness as of a date. ``events`` (the view the activations came from) enables dedupe. Signs in
    ``notes_only`` (live calls: journey.notes_only) can open a note about their work but never count toward a pitch;
    the replay and the scorecard pass none. Signs in CONTEXT_ONLY are listed in the explanation and decide nothing.
    Their employer's news (EMPLOYER_NEWS) counts only while open, once."""
    as_of = parse_time(as_of).isoformat()
    context = [a for a in activations if a["detector_id"] in CONTEXT_ONLY and _is_open(a, as_of)]
    activations = dedupe(_employer_news([a for a in activations if a["detector_id"] not in CONTEXT_ONLY], as_of), events)
    signals = [a for a in activations if a["family"] not in SUPPRESSORS]
    code = _code_alone(signals, as_of)
    signals = [a for a in signals if not any(a is c for c in code)]

    families = {}
    for a in signals:
        families.setdefault(a["family"], []).append(_at(a, as_of))
    families = {f: _stack(v) for f, v in families.items() if _stack(v) > 0}
    holding = [a for a in activations if _is_open(a, as_of) and (a["family"] in SUPPRESSORS or a["holds"])]
    holds = sorted({h for a in holding for h in (a["holds"] or [a["detector_id"]])})
    open_now = sorted((a for a in signals if _is_open(a, as_of)), key=lambda a: a["window_close"] or "")
    opened = [a for a in open_now if a["family"] in OPENERS]
    opener = bool(opened)
    family_of = {REASON_OF.get(a["detector_id"], a["detector_id"]): a["family"] for a in signals}
    reasons = _counted(signals, as_of)
    others = [r for r in reasons if family_of[r] not in OPENERS]
    upgrade_since = (parse_time(as_of) - timedelta(days=UPGRADE_DAYS)).isoformat()
    newest = {}
    for a in (a for a in signals if (a["window_open"] or "") <= as_of):  # a talk announced ahead is not fresh news
        r = REASON_OF.get(a["detector_id"], a["detector_id"])
        newest[r] = max(newest.get(r, ""), a["window_open"] or "")

    def converges(counted, opens):
        # Public moments (paper, launch, acquisition post) count as one reason toward a pitch: never a pitch together.
        # Beside an employer's record, their own paper or launch counts only while fresh (UPGRADE_DAYS).
        employer = any(family_of[r] == "employer" for r in counted)
        kept = [r for r in counted if not (employer and r in MOMENTS and newest[r] < upgrade_since)]
        return (len({"moment" if r in MOMENTS else r for r in kept if family_of[r] not in SUPPORT}) >= 2
                or any(_enough_alone(a["detector_id"], a["family"]) for a in open_now)
                or opens and any(family_of[r] in LISTENING for r in counted))

    converged = converges([r for r in others if r not in notes_only],
                          any(a["detector_id"] not in notes_only for a in opened))
    kept_to_a_note = not converged and converges(others, opener)  # a pitch only on signs kept to notes
    fresh = parse_time(as_of) - timedelta(days=NEWS_DAYS)
    news = any(a["detector_id"] in NEWS and a["window_open"] and parse_time(a["window_open"]) >= fresh
               for a in open_now)
    rapport = (opener or news) and not converged
    upcoming = sorted((a for a in signals if a["window_open"] and a["window_open"] > as_of), key=lambda a: a["window_open"])
    stated = [a for a in upcoming if a["family"] in SELF_EVIDENT]

    readiness = _stack(reasons.values())
    if not converged:
        readiness = min(readiness, SINGLE_REASON_CAP)
    if holds:
        readiness = round(readiness * HOLD_FACTOR, 4)

    verify = [a for a in holding if set(a["holds"]) & set(VERIFY_HOLDS)]
    checks = [a for a in open_now if a.get("falsifier")]  # signals that name their own fact to check
    until = None
    if set(holds) & set(OPT_OUT_HOLDS):
        action = "quiet"
    elif stated:
        action, until = "respect_follow_up", stated[0]["window_open"]
    elif verify and open_now:
        action = "verify_first"
    elif open_now and (converged or rapport) and holds:
        action, until = "watch_until", max(a["window_close"] for a in holding)
    elif open_now and (converged or rapport):
        action = "reach_now"
    elif checks:  # one reason, not enough alone, but it names what to find out
        action = "verify_first"
    elif upcoming:
        action, until = "watch_until", upcoming[0]["window_open"]
    elif "short_tenure" in holds:  # they just started a job: nothing before their first year (or a later hold) is out
        action, until = "watch_until", max(a["window_close"] for a in holding)
    else:
        action = "quiet"

    earliest_close = open_now[0]["window_close"] if open_now else None
    confidence = ("high" if action == "reach_now" and readiness >= STRONG else
                  "medium" if action == "reach_now" else "low")
    from .moments import falsifiers_for  # the planner's per-mechanism table; moments imports this module

    falsifiers = [a["falsifier"] for a in verify + checks if a.get("falsifier")]
    falsifiers += [text for a in open_now for text, _ in falsifiers_for(a["detector_id"])]
    # What a note quotes: within a reason, what is open before what has closed, then their words before GitHub code;
    # a reason that has only code open goes after one with their words (opener), so a push never opens a note a post
    # could, and a stale post never beats an open push.
    rank = lambda a: (_is_open(a, as_of), not a.get("code_only"), a["window_open"] or "")  # noqa: E731
    ranked = [a for a in sorted(signals, key=rank, reverse=True) if _at(a, as_of) > 0]
    top = {}
    for a in ranked:
        top.setdefault(REASON_OF.get(a["detector_id"], a["detector_id"]), a)
    return Readiness(
        as_of=as_of, score=readiness, action=action, until=until, families=families,
        track=("pitch" if converged else "rapport") if action == "reach_now" else None, reasons=list(reasons),
        evidence={r: list(dict.fromkeys(i for a in ranked if REASON_OF.get(a["detector_id"], a["detector_id"]) == r
                                        for i in a["evidence_event_ids"]))
                  for r in sorted(reasons, key=lambda r: bool(top.get(r, {}).get("code_only")))}, holds=holds,
        open_windows=[a["detector_id"] for a in open_now], earliest_close=earliest_close,
        window={"opens": open_now[0]["window_open"], "closes": earliest_close,
                "detector_id": open_now[0]["detector_id"]} if open_now else None,
        confidence=confidence, falsifiers=list(dict.fromkeys(falsifiers)),
        explanation=_explain(as_of, open_now, list(reasons), holds, earliest_close, action, until, upcoming, stated,
                             rapport, kept_to_a_note, context, [a for a in code if _is_open(a, as_of)]),
    )


def _day(value):
    return value[:10] if value else None


def _employer_news(activations, as_of):
    """The activations with their employer's deal or layoffs (EMPLOYER_NEWS) counted only while open, never on the
    fading strength after a window closes. Each detector's open ones merge into one: open from the newest, until the
    latest close, at the strongest strength, with every report's evidence, so no other reason claims a report of the
    same deal (their own post that their company was acquired, say). One not yet open stays as it is: a date to wait
    for."""
    groups = {}
    for a in activations:
        if a["detector_id"] in EMPLOYER_NEWS and _is_open(a, as_of):
            groups.setdefault(a["detector_id"], []).append(a)
    merged = {}
    for detector_id, group in groups.items():
        group = sorted(group, key=lambda a: a["window_open"] or "", reverse=True)
        merged[detector_id] = {**group[0], "strength": max(a["strength"] for a in group),
                               "window_close": max((a["window_close"] for a in group), key=lambda c: c or "9999"),
                               "evidence_event_ids": list(dict.fromkeys(i for a in group for i in a["evidence_event_ids"]))}
    out = []
    for a in activations:
        if a["detector_id"] not in EMPLOYER_NEWS or (a["window_open"] or "") > as_of:
            out.append(a)
        elif a["detector_id"] in merged:
            out.append(merged.pop(a["detector_id"]))
    return out


def _explain(as_of, open_now, reasons, holds, earliest_close, action, until, upcoming, stated, rapport,
             kept_to_a_note=False, context=(), code=()):
    parts = []
    if open_now:
        listed = "; ".join(f"{label(a['detector_id'])} (until {_day(a['window_close']) or 'no set end'})" for a in open_now)
        parts.append(f"Open on {_day(as_of)}: {listed}.")
        parts.append(f"{len(reasons)} independent reason{'' if len(reasons) == 1 else 's'}"
                     + (f": {'; '.join(label(r) for r in reasons)}." if reasons else "."))
        if earliest_close:
            parts.append(f"Earliest window closes {_day(earliest_close)}.")
    else:
        parts.append(f"No window open on {_day(as_of)}.")
    if holds:
        parts.append("Holding back: " + "; ".join(label(h) for h in holds) + ".")
    if action == "respect_follow_up":
        parts.append(f"A GI teammate's note says not until {_day(until)}." if stated[0]["family"] == "private"
                     else f"The person named a date; follow up from {_day(until)}.")
    elif action == "watch_until" and until:
        parts.append(f"Watch until {_day(until)}" + (", when the last hold lifts." if holds
                                                      else f", when the next window opens ({label(upcoming[0]['detector_id'])})."))
    elif action == "verify_first" and set(holds) & set(VERIFY_HOLDS):
        parts.append("Their current or next role is unconfirmed: verify it before reaching out.")
    elif action == "verify_first":
        parts.append("One reason only, and it names a fact to check before reaching out.")
    elif set(holds) & set(OPT_OUT_HOLDS):
        parts.append("They asked not to be contacted; watch for that to change.")
    elif action == "quiet" and open_now:
        parts.append("One reason only: not enough to reach out on its own.")
    elif rapport and kept_to_a_note:
        parts.append("The signs that would make this a pitch are early signs the noise test has not shown to come "
                     "before a move: a note that opens with their work.")
    elif rapport:
        parts.append("A reason to write about their work, and no sign yet that they would listen to a pitch: "
                     "a note that opens with their work.")
    if context:
        parts.append("Listed, never a reason: " + "; ".join(dict.fromkeys(label(a["detector_id"]) for a in context)) + ".")
    if code:
        parts.append("Listed, never a reason by itself (GitHub code, with no fresh post, release or paper beside it): "
                     + "; ".join(dict.fromkeys(label(a["detector_id"]) for a in code)) + ".")
    return " ".join(parts)
