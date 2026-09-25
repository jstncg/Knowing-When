"""Confidence from the scorecard, and dated falsifiers the engine checks later, over invented observations."""

from app import ping, readiness

CALL = readiness.Readiness(as_of="2026-09-15T12:00:00+00:00", score=0.5, action="reach_now", families={},
                           reasons=["work_in_progress", "topic_drift"], holds=[], open_windows=[],
                           earliest_close="2026-09-26T00:00:00+00:00", track="pitch",
                           falsifiers=["It was a passing post: nothing more on the work within three weeks",
                                       "Their next posts return to their old topics"], explanation="")
OPENER = {"id": "o", "event_type": "work_in_progress", "event_date": "2026-09-05",
          "quote": "Training a diffusion world model on Doom gameplay this week"}


def window(subject, role, start, end, moment, signs):
    return {"subject_id": subject, "role": role, "window": [start, end], "moment": moment, "as_of": f"{end}T00:00:00",
            "action": "reach_now" if signs else "quiet", "readiness": {"reasons": signs},
            "early": bool(signs),  # scorecard.scored: a reach on early signs alone (work_in_progress is one)
            "activations": [{"detector_id": s, "window_open": start} for s in signs]}


def report():
    """Four MTS people: the sign came before 3 of 4 moments and in none of 8 ordinary stretches."""
    rows = []
    for n in range(4):
        who = f"p{n}"
        rows.append(window(who, "mts-research", "2026-03-01", "2026-04-26", {"date": "2026-04-26"},
                           ["work_in_progress"] if n < 3 else []))
        rows += [window(who, "mts-research", day, day, None, []) for day in ("2026-01-01", "2026-06-01")]
    rows.append(window("d0", "product-designer", "2026-03-01", "2026-04-26", {"date": "2026-04-26"}, []))
    return {"observations": rows}


def test_confidence_is_not_measured_without_a_run_or_enough_moments_for_the_role():
    assert ping.confidence(CALL, "mts-research")["text"].startswith("Not measured yet")
    designer = ping.confidence(CALL, "product-designer", report())
    assert not designer["measured"] and "1 stretch before a moment" in designer["text"]


def test_confidence_is_the_roles_measured_rate_for_the_sign_behind_the_call():
    got = ping.confidence(CALL, "mts-research", report())
    assert got["measured"] and got["beats_ordinary"] and got["sign"] == "work_in_progress"
    assert got["before_moment"] == [3, 4] and got["ordinary"] == [0, 8]
    assert "3 of 4 (75%)" in got["text"] and "0 of 8 (0%)" in got["text"]


def test_a_sign_not_shown_to_predict_says_what_that_means_and_never_reads_as_a_contradiction():
    # 2 of 4 moments against 2 of 8 ordinary stretches: twice as often, the lift's bar met, but too few cases.
    rows = []
    for n in range(4):
        who = f"p{n}"
        rows.append(window(who, "mts-research", "2026-03-01", "2026-04-26", {"date": "2026-04-26"},
                           ["work_in_progress"] if n < 2 else []))
        rows += [window(who, "mts-research", day, day, None, ["work_in_progress"] if n < 1 else [])
                 for day in ("2026-01-01", "2026-06-01")]
    got = ping.confidence(CALL, "mts-research", {"observations": rows})
    assert got["measured"] and not got["beats_ordinary"] and round(got["lift"], 1) == 2.0
    assert "Too few cases to be sure yet: the bar is p under 0.05." in got["text"] and "twice" not in got["text"]
    assert "better than an ordinary week" not in got["text"]
    # 12 of 15 moments against 20 of 45 ordinary stretches: 1.8 times as often at p under 0.05, short only of the
    # lift. It must name the bar it missed, never read as predicting nothing.
    rows = []
    for n in range(15):
        who = f"q{n}"
        rows.append(window(who, "mts-research", "2026-03-01", "2026-04-26", {"date": "2026-04-26"},
                           ["work_in_progress"] if n < 12 else []))
        rows += [window(who, "mts-research", day, day, None, ["work_in_progress"] if 3 * n + i < 20 else [])
                 for i, day in enumerate(("2026-01-01", "2026-06-01", "2026-08-01"))]
    got = ping.confidence(CALL, "mts-research", {"observations": rows})
    assert (got["before_moment"], got["ordinary"], round(got["lift"], 1)) == ([12, 15], [20, 45], 1.8) and got["p"] < 0.05
    assert not got["beats_ordinary"] and "(1.8 times as often, p=0.02). Not twice as often as in an ordinary " \
        "stretch, the bar for counting it as a sign." in got["text"]
    # 2 of 3 moments against 0 of 6 ordinary stretches (a live card): far more than twice as often, short only on p.
    # It must say there are too few cases, never name the lift it met.
    rows = []
    for n in range(3):
        who = f"b{n}"
        rows.append(window(who, "mts-research", "2026-03-01", "2026-04-26", {"date": "2026-04-26"},
                           ["work_in_progress"] if n < 2 else []))
        rows += [window(who, "mts-research", day, day, None, []) for day in ("2026-01-01", "2026-06-01")]
    got = ping.confidence(CALL, "mts-research", {"observations": rows})
    assert (got["before_moment"], got["ordinary"], got["lift"]) == ([2, 3], [0, 6], None) and not got["beats_ordinary"]
    assert "(p=0.08). Too few cases to be sure yet: the bar is p under 0.05." in got["text"]
    assert "twice" not in got["text"]


def test_falsifiers_are_dated_claims():
    wrong = ping.falsifiers(CALL, OPENER)
    assert [(f["kind"], f["by"]) for f in wrong] == [("news", "2026-11-10"), ("follow_up", "2026-09-26"),
                                                     ("person", "2026-09-26")]
    assert wrong[2]["claim"] == "Their next posts return to their old topics (check on 2026-09-26)."
    assert [f["kind"] for f in ping.falsifiers(CALL)] == ["news", "person", "person"]


def post(kind, day, quote):
    return {"event_type": kind, "observed_at": f"{day}T12:00:00+00:00", "quote": quote}


def test_status_checks_the_news_and_the_follow_up_by_their_dates():
    news, follow_up, person = ping.falsifiers(CALL, OPENER)
    assert ping.status(news, [], "2026-10-01T00:00:00+00:00") == "open"
    assert ping.status(news, [], "2026-11-11T00:00:00+00:00") == "call wrong"
    assert ping.status(news, [post("job_started", "2026-10-20", "Joined Pine Lab")], "2026-11-11T00:00:00") == "call held"
    more = post("x_post", "2026-09-20", "Doom world model now runs at 20 fps, diffusion sampler rewritten")
    other = post("x_post", "2026-09-20", "Great coffee in Lisbon")
    assert ping.status(follow_up, [more], "2026-09-21T00:00:00") == "call held"
    assert ping.status(follow_up, [other], "2026-09-27T00:00:00") == "call wrong"
    assert ping.status(person, [], "2026-09-27T00:00:00") == "ask a person"


def test_a_release_a_build_made_is_not_the_news_that_holds_a_call():
    news = ping.falsifiers(CALL, OPENER)[0]
    build = post("project_release", "2026-10-20", "Released rules-20261020-0514 of me/scraper: Rules update")
    assert ping.status(news, [build], "2026-11-11T00:00:00") == "call wrong"
    launch = post("project_release", "2026-10-20", "Released v1.0 of me/scraper: Scraper 1.0")
    assert ping.status(news, [launch], "2026-11-11T00:00:00") == "call held"
