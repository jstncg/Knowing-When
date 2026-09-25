"""Ping field 05: confidence, and what would prove the call wrong.

The High / Medium / Low word is readiness's rule, not a measurement: high when the reasons behind a reach add up to
readiness.STRONG or more on hand-set weights. What this module adds is the measurement, when there is one: how often
the sign behind the call came in the 8 weeks before a real moment for people in the same role, against those
people's ordinary 8-week stretches, and how often the engine's reach-now fired in a stretch nothing followed: the
scorecard run (scripts/posts.py scorecard). Until a run covers the role, it says "not measured yet", and the card
says the level rests on the rule alone (routing._sure).

What would prove it wrong is a list of dated claims the engine checks later (``status``):
- news: a call on an early sign says public news follows within 8 weeks (a release, launch, paper or job news).
  None by then means a false alarm, the scorecard's own definition. A call on news already out gets no such claim.
- follow_up: work in progress is real work, so more of it shows up within 3 weeks. Nothing more on it by then
  means it was a passing post.
- person: the mechanism's other falsifiers (app.moments); a person checks them when the first window closes.
"""

from datetime import date, timedelta

from . import moments, outreach, readiness, scorecard
from .detectors import READ_TYPES, REPLY_CONTEXT, made_by_hand
from .models import parse_time
from .readiness import Readiness
from .role_compiler import role_id as config_role_id

MIN_MOMENTS = 3  # fewer stretches before a moment for the role than this, and a rate says nothing
FOLLOW_UP = timedelta(days=21)
FOLLOWS = ("work_in_progress",)  # work being built: more of it should show
# What the scorecard counts as the moment: the news everyone sees.
NEWS_TYPES = ("paper_v1", "publication", "paper_accepted", "project_release", "launch_announced", "job_started",
              "role_announced", "affiliation_change", "job_ended", "layoff")
SAME_WORK = 2  # content words a later item shares with the work to count as more of it
PASSING = {text for kind in FOLLOWS for text, _ in moments.FALSIFIERS.get(kind, [])}  # what follow_up dates


def _pct(part, whole):
    return f"{part} of {whole} ({round(100 * part / whole)}%)" if whole else f"{part} of 0"


def _day(value):
    """A date at day, month or year precision, as its first day."""
    return date.fromisoformat({4: f"{value}-01-01", 7: f"{value}-01"}.get(len(value), value[:10]))


def sign_of(call: Readiness):
    """The sign that timed the call: the reason behind the event the draft quotes, else its first early sign, else
    its strongest reason."""
    quoted = readiness.opener(call)
    return next((r for r in call.reasons if quoted in call.evidence.get(r, [])),
                next((r for r in call.reasons if r in scorecard.EARLY_SIGNS), call.reasons[0] if call.reasons else None))


def confidence(call: Readiness, role_id, report=None):
    """{measured, text, sign, ...counts}: the role's rate for the sign behind the call, from a scorecard report."""
    sign = sign_of(call)
    observations = [o for o in (report or {}).get("observations", []) if config_role_id(o["role"]) == role_id]
    subjects = {o["subject_id"] for o in observations}
    row = scorecard.noise(observations, subjects)["signs"].get(sign) if observations else None
    if not row or row["before_moment_windows"] < MIN_MOMENTS:
        seen = row["before_moment_windows"] if row else 0
        return {"measured": False, "sign": sign, "text": f"Not measured yet: the scorecard has {seen} stretch"
                f"{'' if seen == 1 else 'es'} before a moment for this role, and needs {MIN_MOMENTS}."}
    early = sign in scorecard.EARLY_SIGNS  # the scorecard's engine row counts only reaches on an early sign
    card = scorecard.scorecard(observations, subjects, draws=0)["engine" if early else "any_reach"]
    # The noise test's keep rule, for any sign: more common before a moment, and not by chance.
    beats = bool(row["before_moment"]) and row["p"] is not None and row["p"] < scorecard.KEEP_P \
        and (row["lift"] is None or row["lift"] >= scorecard.KEEP_LIFT)
    p = f", p={row['p']:.2f}" if row["p"] is not None else ""
    lift = f" ({row['lift']:.1f} times as often{p})" if row["lift"] is not None else f" ({p[2:]})" if p else ""
    ordinary = (f"and in {_pct(row['ordinary'], row['ordinary_windows'])} of their ordinary 8-week stretches{lift}"
                if row["ordinary_windows"] else "and there were no ordinary stretches to compare against")
    text = (f"Measured on {len(subjects)} people for this role: this kind of sign came before "
            f"{_pct(row['before_moment'], row['before_moment_windows'])} of their public moments (a paper, launch or "
            f"job news, within 8 weeks), {ordinary}. "
            + ("" if beats else missed(row["before_moment"], row["ordinary_windows"], row["lift"], row["p"]) + ". ")
            + (f"Reaching out on it would have been a false alarm in "
               f"{_pct(card['fired_with_nothing_after'], card['ordinary_stretches'])} of ordinary stretches."
               if card["ordinary_stretches"] else "The false-alarm rate is unknown."))
    return {"measured": True, "beats_ordinary": beats, "sign": sign, "text": text,
            "before_moment": [row["before_moment"], row["before_moment_windows"]],
            "ordinary": [row["ordinary"], row["ordinary_windows"]], "lift": row["lift"], "p": row["p"],
            "false_alarms": [card["fired_with_nothing_after"], card["ordinary_stretches"]]}


TIMES = "twice" if scorecard.KEEP_LIFT == 2 else f"{scorecard.KEEP_LIFT:g} times"


def bar(than=""):
    """The noise test's keep rule in words, for a sign that missed it: "twice as often, at p under 0.05"."""
    return f"{TIMES} as often{than}, at p under {scorecard.KEEP_P:g}"


def missed(before, ordinary_windows, lift, p, card=False):
    """Which bar a sign that doesn't count missed, in words, never one it met: too few cases (p), not often enough
    (the lift), both, or nothing to measure against. ``card``: the card's clause, which gives p itself and calls an
    ordinary stretch a quiet one; else a sentence after the counts, which give p already."""
    often = lift is None or lift >= scorecard.KEEP_LIFT
    sure = p is not None and p < scorecard.KEEP_P
    if not before:
        said = "never before a moment, so it doesn't count as a sign"
    elif not ordinary_windows or p is None:
        said = ("no quiet stretches to compare it against yet, so " if card else "so ") + "it doesn't count as a sign"
    elif often and not sure:
        said = (f"too few cases to be sure yet (p={p:.2f}, and the bar is p under {scorecard.KEEP_P:g})" if card
                else f"too few cases to be sure yet: the bar is p under {scorecard.KEEP_P:g}")
    elif sure and not often:
        said = f"not {TIMES} as often as in {'a quiet' if card else 'an ordinary'} stretch, the bar for counting it as a sign"
    else:
        said = (f"not enough to count it as a sign (the bar is {bar(' as in a quiet stretch')})" if card
                else f"not enough to count it as a sign: the bar is {bar()}")
    return said if card else said[:1].upper() + said[1:]


def falsifiers(call: Readiness, opener=None):
    """Dated claims that would prove the call wrong: [{claim, by, kind, since, work}]. ``opener`` is the event the
    draft quotes (readiness.opener's), when the call has one. A call on news already out predicts no news, so it
    gets no news claim."""
    made = parse_time(call.as_of).date()
    news_by = made + scorecard.STRETCH
    out = [{"kind": "news", "since": made.isoformat(), "by": news_by.isoformat(),
            "claim": f"No public news by {news_by} (a release, launch, paper or job news): the call was a false alarm."}
           ] if any(r in scorecard.EARLY_SIGNS for r in call.reasons) else []
    if opener and opener["event_type"] in FOLLOWS:
        day = _day(opener["event_date"] or call.as_of[:10])
        work = opener["quote"].partition(REPLY_CONTEXT)[0]
        out.append({"kind": "follow_up", "since": day.isoformat(), "by": (day + FOLLOW_UP).isoformat(), "work": work,
                    "claim": f"Nothing more on this work by {day + FOLLOW_UP}: it was a passing post."})
    closes = (call.earliest_close or call.as_of)[:10]
    for text in call.falsifiers:
        if not (opener and opener["event_type"] in FOLLOWS and text in PASSING):
            out.append({"kind": "person", "since": made.isoformat(), "by": closes, "claim": f"{text} (check on {closes})."})
    return out


def _same_work(event, work):
    shared = set(outreach.terms(event["quote"].partition(REPLY_CONTEXT)[0])) & set(outreach.terms(work))
    return len(shared - outreach.STOP) >= SAME_WORK


def status(falsifier, events, as_of):
    """'call held' (what the call expected happened), 'call wrong' (the date passed without it), 'open', or 'ask a
    person'.
    ``events`` is the person's view as of ``as_of``; an event counts by when it became public."""
    if falsifier["kind"] == "person":
        return "ask a person"
    since, by, today = falsifier["since"], falsifier["by"], parse_time(as_of).date().isoformat()
    public = [e for e in made_by_hand(events) if since < e["observed_at"][:10] <= by]  # a build is no news
    if falsifier["kind"] == "news":
        hit = any(e["event_type"] in NEWS_TYPES for e in public)
    else:
        hit = any(e["event_type"] in READ_TYPES and e["quote"].partition(REPLY_CONTEXT)[0] != falsifier["work"]
                  and _same_work(e, falsifier["work"]) for e in public)
    return "call held" if hit else "call wrong" if today > by else "open"
