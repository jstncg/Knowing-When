"""GTM's accounts list and ops' Monday brief share the contact ledger and nothing else: a sales note never
comes back as an ops follow-up, and the pause stops the accounts list as it stops every card."""

import json

import pytest

from app import accounts, contact, inbox, today
from tests.test_accounts import Poster, simulation  # noqa: F401  (simulation is a fixture)


def test_a_sales_note_is_never_an_ops_follow_up(simulation):  # noqa: F811
    store = today._demo_store()[0]
    contact.record(store, "account:driftwire:lee-example", "sent", at="2026-09-07T12:00:00+00:00", team="sales",
                   by="Luis Example")
    contact.record(store, "X950", "sent", at="2026-09-07T12:00:00+00:00", team="recruiting", by="Dana Kest")
    as_of = "2026-09-15T23:59:59+00:00"
    assert [pid for pid, _ in contact.follow_ups_due(store, as_of)] == ["X950"]
    assert [pid for pid, _ in contact.follow_ups_due(store, as_of, teams=("sales",))] == \
        ["account:driftwire:lee-example"]
    week = inbox.week("simulation")
    assert "X950" in [f["subject_id"] for f in week["follow_ups"]]
    assert "driftwire" not in json.dumps(week) and "driftwire" not in json.dumps(inbox.card(week))


def test_of_two_notes_at_the_same_time_the_follow_up_belongs_to_the_one_recorded_last(simulation):  # noqa: F811
    store = today._demo_store()[0]
    for pid, teams in (("X951", ("recruiting", "sales")), ("X952", ("sales", "recruiting"))):
        for team in teams:
            contact.record(store, pid, "sent", at="2026-09-07T12:00:00+00:00", team=team, by="Dana Kest")
    due = contact.follow_ups_due(store, "2026-09-15T23:59:59+00:00")
    assert [pid for pid, _ in due] == ["X952"] and "(recruiting)" in due[0][1]  # check() names the same note


def test_the_accounts_list_posts_nothing_while_sending_is_paused(simulation):  # noqa: F811
    (store, as_of), _, _ = accounts.workspace("simulation")
    rows = accounts.page("simulation")["accounts"]
    contact.pause(by="Justin", why="the demo")
    poster = Poster()
    with pytest.raises(ValueError, match="Sending is paused"):
        accounts.send(store, rows, "https://hooks.example.org/x", now=as_of, client=poster)
    assert poster.posted == [] and not [e for e in store.all("contact") if e["kind"] == "pinged" and e["team"] == "sales"]
    contact.resume()
    assert accounts.send(store, rows, "https://hooks.example.org/x", now=as_of, client=poster)
