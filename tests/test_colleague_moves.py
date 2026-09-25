"""Colleague and PI moves read from a person's own OpenAlex coauthor links, and the leakage guards."""

from itertools import count

from app import detectors, timeline

ME = "A1"
HOME, LAB = "Example University", "Frontier Lab"
_ids = count()


def work(day, own, coauthors, title="A paper"):
    """affiliation_seen per own institution and coauthor_link per (name, id, places, position), all seen on `day`."""
    url = f"https://doi.org/10.0/{day}-{title}"
    base = dict(subject_type="person", subject_id=ME, event_date=day, date_precision="day",
                observed_at=f"{day}T00:00:00+00:00", source_url=url, source_version_hash="h", tier=1,
                extractor="openalex_works")
    out = [timeline.TimelineEvent(event_type="affiliation_seen", quote=f"{inst} on '{title}'", **base) for inst in own]
    out += [timeline.TimelineEvent(event_type="coauthor_link", **base,
                                   quote=f"{name} ({cid}) on '{title}' | at {'; '.join(places)} | {position}")
            for name, cid, places, position in coauthors]
    return [{**e.model_dump(), "id": f"ev:{next(_ids)}"} for e in out]


PI = ("Pat Chen", "A3", [HOME], "last")  # a senior author who stays, so the subject is not last


def fired(events, as_of):
    out = {}
    for a in detectors.detect(events, ME, f"{as_of}T12:00:00+00:00"):
        out.setdefault(a["detector_id"], []).append(a)
    return out


def test_a_colleague_leaving_the_shared_institution_opens_a_window_from_the_paper_date():
    events = work("2025-01-10", [HOME], [("Sam Poe", "A2", [HOME], "middle"), PI]) + \
             work("2025-03-01", [HOME], [("Sam Poe", "A2", [LAB], "middle"), PI], "Later")
    move = fired(events, "2025-03-15")["coauthor_departure"][0]
    assert move["window_open"].startswith("2025-03-01") and len(move["evidence_event_ids"]) == 2
    assert "pi_departure" not in fired(events, "2025-03-15")
    # Before the second paper was public the move is invisible.
    visible = [e for e in events if e["observed_at"] <= "2025-02-28T12:00:00+00:00"]
    assert "coauthor_departure" not in fired(visible, "2025-02-28")


def test_the_senior_author_moving_is_a_pi_departure():
    events = work("2025-01-10", [HOME], [("Pat Chen", "A3", [HOME], "last")]) + \
             work("2025-02-20", [HOME], [("Pat Chen", "A3", ["Other Lab"], "last")], "Later")
    got = fired(events, "2025-03-01")
    assert got["pi_departure"][0]["window_close"].startswith("2025-06-20") and "coauthor_departure" not in got


def test_the_persons_own_move_is_the_outcome_and_never_a_signal():
    events = work("2025-01-10", [HOME], [("Sam Poe", "A2", [HOME], "middle")]) + \
             work("2025-03-01", [LAB], [("Sam Poe", "A2", [LAB], "middle")], "At the new lab")
    assert not {"coauthor_departure", "pi_departure", "team_exodus"} & set(fired(events, "2025-03-15"))


def test_two_colleagues_leaving_within_60_days_is_an_exodus():
    events = work("2025-01-10", [HOME], [("Sam Poe", "A2", [HOME], "middle"), ("Lee Wu", "A4", [HOME], "first"), PI]) + \
             work("2025-02-01", [HOME], [("Sam Poe", "A2", [LAB], "middle"), PI], "B") + \
             work("2025-03-15", [HOME], [("Lee Wu", "A4", ["Another Co"], "first"), PI], "C")
    exodus = fired(events, "2025-03-20")["team_exodus"][0]
    assert exodus["window_open"].startswith("2025-03-15")


def test_no_signal_from_outside_collaborators_or_links_without_places():
    outside = work("2025-01-10", [HOME], [("Ann Roe", "A5", ["Other U"], "middle")]) + \
              work("2025-03-01", [HOME], [("Ann Roe", "A5", [LAB], "middle")], "Later")
    assert "coauthor_departure" not in fired(outside, "2025-03-15")
    old_format = [{**e, "quote": e["quote"].split(" | ")[0]} for e in
                  work("2025-01-10", [HOME], [("Sam Poe", "A2", [HOME], "middle")]) +
                  work("2025-03-01", [HOME], [("Sam Poe", "A2", [LAB], "middle")], "Later")]
    assert "coauthor_departure" not in fired(old_format, "2025-03-15")


def test_a_january_1_work_counts_no_move_since_the_date_may_stand_for_the_year():
    events = work("2024-10-01", [HOME], [("Sam Poe", "A2", [HOME], "middle"), PI]) + \
             work("2025-01-01", [HOME], [("Sam Poe", "A2", [LAB], "middle"), PI], "Year only")
    assert "coauthor_departure" not in fired(events, "2025-01-15")
    dated = [{**e, "event_date": "2025-01-02"} if e["event_date"] == "2025-01-01" else e for e in events]
    assert "coauthor_departure" in fired(dated, "2025-01-15")


def test_juniors_leaving_a_last_author_are_churn_but_the_pi_rule_stands():
    """A professor (last author) whose students graduate and leave is not about to move."""
    students = work("2025-01-10", [HOME], [("Sam Poe", "A2", [HOME], "first"), ("Lee Wu", "A4", [HOME], "middle")]) + \
               work("2025-02-01", [HOME], [("Sam Poe", "A2", [LAB], "first")], "B") + \
               work("2025-03-15", [HOME], [("Lee Wu", "A4", ["Another Co"], "first")], "C")
    assert not {"coauthor_departure", "team_exodus"} & set(fired(students, "2025-03-20"))
    pi = work("2025-01-10", [HOME], [("Pat Chen", "A3", [HOME], "last")]) + \
         work("2025-02-20", [HOME], [("Pat Chen", "A3", ["Other Lab"], "last")], "Later")
    assert "pi_departure" in fired(pi, "2025-03-01")
