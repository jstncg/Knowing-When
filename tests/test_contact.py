"""The contact gate and ledger, over invented people: every rail the design lists, on the Slack path and the web app's."""

import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import types

import httpx
import pytest

from app import contact, journey, main, providers, routing, today
from app.engine import decide, review_hash
from app.models import iso
from app.readiness import Readiness
from app.store import Store
from tests.test_api import client, create, review, state_candidate  # noqa: F401  (client is a fixture)
from tests.test_engine_adversarial import NOW, ready_candidate, role

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
AS_OF = "2026-09-15T00:00:00+00:00"
LINKS = ["https://rinvale.example/", "https://github.com/rinvale-example"]


@pytest.fixture
def store(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'c.sqlite'}")
    routing.seed(s, json.loads((FIXTURES / "routing" / "timeline.json").read_text()))
    return s


def entry(kind, day, **fields):
    return contact.Entry(person_id="P", kind=kind, at=f"{day}T12:00:00+00:00", **fields).model_dump()


def call(score=0.6, evidence=("e1",), track="pitch"):
    return Readiness(as_of=AS_OF, score=score, action="reach_now", families={}, reasons=["r"], holds=[],
                     open_windows=[], earliest_close=None, explanation="", evidence={"r": list(evidence)}, track=track)


def slack(seen=None):
    seen = [] if seen is None else seen
    return httpx.Client(transport=httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(200)))


def rin(store, as_of=AS_OF):
    return routing.route(store, "A900", as_of, team=[])


def test_never_holds_the_person_for_every_role_and_every_path(store):
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    contact.record(store, "A900", "never", at="2026-09-02T00:00:00+00:00", role_id="backend")
    r = rin(store)
    assert r.gate.state == "hold" and "every role" in r.gate.reason
    with pytest.raises(ValueError, match="not cleared"):
        routing.send(store, r, "https://hooks.slack.example/x", client=slack())
    later = contact.check(contact.history(store, "A900"), "2031-01-01T00:00:00+00:00", call(), [])
    assert later.state == "hold"  # for good
    assert decide(ready_candidate(), role(), contact.history(store, "A900"), now=NOW)["state"] == "suppressed"


def test_an_opt_out_or_a_send_in_either_ledger_holds_every_path(store, tmp_path, monkeypatch):
    web = Store(f"sqlite:///{tmp_path / 'pilot.sqlite'}")  # the web app's ledger; the store fixture is the timing run's
    monkeypatch.setattr(contact, "LEDGERS", {"the web app": tmp_path / "pilot.sqlite", "the timing run": tmp_path / "c.sqlite"})
    for pid in ("person:live:rin", "person:simulation:rin"):  # the web app knows Rin by the GitHub profile the engine reads
        web.put(f"candidate:{pid}", "candidate", {"person_id": pid, "mode": pid.split(":")[1],
                                                   "profile_url": "https://www.github.com/RinVale-Example?tab=repositories"})
    web.put("candidate:bo", "candidate", {"person_id": "person:live:bo", "mode": "live", "profile_url": "https://bo.example"})
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    contact.record(store, "A900", "sent", at="2026-09-10T00:00:00+00:00", by="Dana")  # marked sent from the Slack card

    seen = contact.history(web, "person:live:rin")
    assert [(e["kind"], e["ledger"]) for e in seen] == [("sent", "the timing run")]  # identity stays where it was
    assert contact.check(seen, AS_OF).state == "hold"
    assert contact.history(web, "person:simulation:rin") == [] and contact.history(web, "person:live:bo") == []

    contact.record(web, "person:live:rin", "never", at="2026-09-12T00:00:00+00:00")  # the web app's opt-out
    r = rin(store)
    assert r.gate.state == "hold" and "every role" in r.gate.reason
    assert [e.get("ledger") for e in contact.history(store, "A900")] == [None, None, "the web app"]
    with pytest.raises(ValueError, match="not cleared"):
        routing.send(store, r, "https://hooks.slack.example/x", client=slack())
    assert [e["kind"] for e in contact.history(web, "person:live:rin")] == ["sent", "never"]  # nothing was copied

    elsewhere = Store(f"sqlite:///{tmp_path / 'demo.sqlite'}")  # a demo's own store is neither ledger: reads nothing across
    routing.seed(elsewhere, json.loads((FIXTURES / "routing" / "timeline.json").read_text()))
    assert contact.history(elsewhere, "A900") == []
    contact.record(web, "A901", "never", at="2026-09-12T00:00:00+00:00")  # a note in the web app's own ledger
    assert [e["kind"] for e in contact.history(store, "mts-research:A901")] == ["never"]  # the same key holds
    assert contact.unreadable(store) is None and contact.unreadable(elsewhere) is None
    contact.record(store, "A902", "sent", at="2026-08-01T00:00:00+00:00")
    assert [pid for pid, _ in contact.follow_ups_due(store, AS_OF)] == ["A902"]  # due, while the ledger reads
    for leftover in tmp_path.glob("pilot.sqlite*"):  # the file and its write-ahead log
        leftover.unlink()
    (tmp_path / "pilot.sqlite").write_bytes(b"not a database, " * 64)
    contact._opened.clear()
    broken = rin(store).gate  # a ledger that can't be read holds everyone, and the page still loads
    assert broken.state == "hold" and broken.reason.startswith("The web app's contact ledger can't be read")
    assert contact.unreadable(store) == broken.reason  # said once, whoever is on the page
    assert contact.follow_ups_due(store, AS_OF) == []  # a follow-up is a contact too: it waits
    assert contact.unreadable(elsewhere) is None  # a demo's store reads nothing across
    for leftover in tmp_path.glob("pilot.sqlite*"):
        leftover.unlink()
    contact._opened.clear()
    odd = Store(f"sqlite:///{tmp_path / 'pilot.sqlite'}")  # opens, but a row is not a ledger entry
    odd.put("contact:odd", "contact", {"kind": "sent", "at": "2026-09-12T00:00:00+00:00"})
    odd.put("person-context:odd", "person_context", {"subject_id": "odd", "anchors": None})
    assert rin(store).gate.reason == contact.unreadable(store) == \
        "The web app's contact ledger can't be read (KeyError), so no one is cleared until it's fixed."


def test_an_account_in_talks_holds_its_people_on_the_web_apps_pages_too(store, tmp_path, monkeypatch):
    web = Store(f"sqlite:///{tmp_path / 'pilot.sqlite'}")
    monkeypatch.setattr(contact, "LEDGERS", {"the web app": tmp_path / "pilot.sqlite", "the timing run": tmp_path / "c.sqlite"})
    web.put("candidate:person:live:rin", "candidate", {"person_id": "person:live:rin", "mode": "live",
                                                       "profile_url": "https://www.github.com/RinVale-Example"})
    contact.record(store, "A900", "partner_staff", at="2026-09-10T00:00:00+00:00", team="sales", by="Nora",
                   note=contact.talks_note("Birchwood Lab", "birchwood-lab"))  # accounts.in_talks, in the timing run
    gate = contact.check(contact.history(web, "person:live:rin"), AS_OF)
    assert gate.state == "hold" and "a founder decides" in gate.reason and "Nora (sales)" in gate.reason


def test_an_accepted_offer_in_either_ledger_holds_every_path_until_it_is_ended(store, tmp_path, monkeypatch):
    web = Store(f"sqlite:///{tmp_path / 'pilot.sqlite'}")
    monkeypatch.setattr(contact, "LEDGERS", {"the web app": tmp_path / "pilot.sqlite", "the timing run": tmp_path / "c.sqlite"})
    web.put("candidate:person:live:rin", "candidate", {"person_id": "person:live:rin", "mode": "live",
                                                       "profile_url": "https://www.github.com/RinVale-Example"})
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    contact.record(store, "A900", "hired", at="2026-09-10T00:00:00+00:00", by="New starts")  # New starts, timing run
    gate = contact.check(contact.history(web, "person:live:rin"), AS_OF)
    assert gate.state == "hold" and "Accepted GI's offer" in gate.reason  # the web app's pages hold them too
    contact.record(web, "person:live:rin", "wrong_person", at="2026-09-12T00:00:00+00:00")
    assert "Accepted GI's offer" in contact.check(contact.history(web, "person:live:rin"), AS_OF).reason  # still held
    contact.record(store, "A900", "hired", at="2026-09-13T00:00:00+00:00", until="2026-09-13", by="New starts")
    assert contact.check(contact.history(web, "person:live:rin"), AS_OF).state == "check_first"  # ended: the doubt
    contact.record(web, "A901", "hired", at="2026-09-10T00:00:00+00:00")  # and the other way: a web ledger's entry
    assert contact.check(contact.history(store, "mts-research:A901"), AS_OF).state == "hold"


def test_a_link_matches_its_profile_across_country_sites_and_page_queries():
    assert contact.tied(["https://uk.linkedin.com/in/Rin-Vale/?miniProfileUrn=x", "https://x.com/rin?s=21&t=abc",
                         "https://github.com/rin?tab=repositories"],
                        ["https://www.linkedin.com/in/rin-vale", "https://twitter.com/rin", "https://github.com/rin"]) == \
        {"linkedin.com", "x.com", "github.com"}  # a share link's query is the same page
    assert contact.tied(["https://scholar.google.co.uk/citations?user=abc"],
                        ["https://scholar.google.com/citations?user=xyz"]) == set()  # two people, one page path
    assert contact.tied(["https://scholar.google.co.uk/citations?user=abc&hl=en"],
                        ["https://scholar.google.com/citations?user=abc"]) == {"scholar.google.com"}


def test_a_card_whose_draft_fails_a_check_never_goes_out(store):
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    r = rin(store)
    assert r.gate.state == "clear" and r.outreach["checks"]["passes"]
    bad = r.model_copy(update={"outreach": {**r.outreach, "checks": {**r.outreach["checks"], "passes": False,
                                                                      "problems": ["fails the name-swap test"]}}})
    seen = []
    with pytest.raises(ValueError, match="draft fails a check \\(fails the name-swap test\\), so no card goes out"):
        routing.send(store, bad, "https://hooks.slack.example/x", now=AS_OF, client=slack(seen))
    assert seen == [] and "pinged" not in [e["kind"] for e in contact.history(store, "A900")]
    routing.send(store, r, "https://hooks.slack.example/x", now=AS_OF, client=slack(seen))
    assert len(seen) == 1


def test_a_card_goes_out_once_and_never_again_however_new_or_late_the_reason(store):
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    seen = []
    first = rin(store)
    assert first.gate.state == "clear"
    routing.send(store, first, "https://hooks.slack.example/x", now=AS_OF, client=slack(seen))
    assert len(seen) == 1 and [e["kind"] for e in contact.history(store, "A900")] == ["identity", "pinged"]

    again = rin(store, "2026-09-16T00:00:00+00:00")  # the next day's run: same reasons
    assert again.gate.state == "hold" and "one card per person, ever" in again.gate.reason
    with pytest.raises(ValueError):
        routing.send(store, again, "https://hooks.slack.example/x", client=slack(seen))
    assert len(seen) == 1
    later = "2027-01-01T00:00:00+00:00"  # past the 90 quiet days: still no second card
    assert routing.sendable(store, [first], later) == []
    with pytest.raises(ValueError, match="one card per person"):
        routing.send(store, first, "https://hooks.slack.example/x", now=later, client=slack(seen))
    assert len(seen) == 1

    ping = contact.history(store, "A900")[-1]
    pinged = [entry("pinged", "2026-09-15", evidence=ping["evidence"], score=ping["score"])]
    new = [*ping["evidence"], "fresh"]
    for day in ("2026-10-01", "2026-12-15", "2027-06-01"):  # a new, stronger reason, and past the 90 quiet days
        held = contact.check(pinged, f"{day}T00:00:00+00:00", call(ping["score"] + 0.1, new), role_id="mts-research")
        assert held.state == "hold" and held.reason == "On a card from 2026-09-15: one card per person, ever.", day
    # Only their own answer reopens it: a wait they asked for, now over.
    asked = [*pinged, entry("not_now", "2026-09-20", until="2026-12-01")]
    assert contact.check(asked, "2026-11-01T00:00:00+00:00", call(0.9, new)).state == "hold"
    assert contact.check(asked, "2026-12-02T00:00:00+00:00", call(0.9, new)).state == "clear"
    # The web app has no call to compare: the person is on a card, so it holds.
    assert decide(ready_candidate(), role(), pinged, now=NOW)["state"] == "suppressed"


def test_one_follow_up_after_a_week_then_quiet():
    sent = [entry("sent", "2026-09-01", by="Dana")]
    assert "waiting for a reply" in contact.check(sent, "2026-09-04T00:00:00+00:00").reason
    due = contact.check(sent, "2026-09-09T00:00:00+00:00")
    assert due.state == "hold" and due.follow_up_due and "one follow-up is due from Dana" in due.reason
    followed = [*sent, entry("follow_up", "2026-09-09")]
    quiet = contact.check(followed, "2026-11-01T00:00:00+00:00", call())
    assert quiet.state == "hold" and not quiet.follow_up_due and "quiet until 2026-12-08" in quiet.reason
    assert contact.check(followed, "2026-12-09T00:00:00+00:00", call()).state == "clear"


def test_follow_ups_due_lists_only_the_unanswered(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'f.sqlite'}")
    for person, kinds in {"A": ["sent"], "B": ["sent", "replied"], "C": ["sent", "follow_up"]}.items():
        for kind in kinds:
            contact.record(s, person, kind, at="2026-09-01T00:00:00+00:00")
    assert [p for p, _ in contact.follow_ups_due(s, "2026-09-10T00:00:00+00:00")] == ["A"]


def test_a_reply_holds_and_a_dated_wait_holds_until_its_day_then_invites():
    replied = [entry("sent", "2026-09-01"), entry("replied", "2026-09-03", by="Dana")]
    assert "In conversation since 2026-09-03 with Dana" in contact.check(replied, "2027-06-01T00:00:00+00:00").reason
    wait = [entry("sent", "2026-09-01"), entry("not_now", "2026-09-05", until="2026-10-15")]
    assert "wait until 2026-10-15" in contact.check(wait, "2026-10-14T00:00:00+00:00", call()).reason
    # They asked for that day, so the 90 days after the note do not apply; the next card follows the usual rules.
    assert contact.check(wait, "2026-10-15T00:00:00+00:00", call()).state == "clear"
    after = [*wait, entry("pinged", "2026-10-15", evidence=["e1"], score=0.6)]
    assert contact.check(after, "2026-10-16T00:00:00+00:00", call()).state == "hold"
    # A card from before their answer does not count against what they asked for.
    answered = [entry("pinged", "2026-09-01", evidence=["e1"], score=0.6), *wait[1:], entry("sent", "2026-10-15")]
    assert contact.check(answered, "2027-02-15T00:00:00+00:00", call()).state == "clear"


def test_one_open_ask_across_teams_and_the_hold_names_who_owns_it():
    sales = [entry("sent", "2026-09-10", team="sales", by="Omar")]
    assert contact.check(sales, "2026-09-12T00:00:00+00:00").reason == "Sent on 2026-09-10 by Omar (sales): waiting for a reply."
    invite = [entry("invited", "2026-09-15", team="events", by="Nora", until="2026-09-24", note="Games night")]
    held = contact.check(invite, "2026-09-24T00:00:00+00:00", call())
    assert held.state == "hold" and held.reason.startswith("Invited to Games night on 2026-09-24 by Nora (events)")
    assert contact.check(invite, "2026-09-25T00:00:00+00:00", call()).state == "clear"  # the evening happened
    partner = [entry("partner_staff", "2026-09-01", team="sales", by="Omar")]
    assert "a founder decides" in contact.check(partner, "2027-06-01T00:00:00+00:00", call()).reason
    left = [entry("partner_staff", "2026-09-01", team="sales", until="2026-12-01")]
    assert contact.check(left, "2026-12-01T00:00:00+00:00", call()).state == "clear"
    with pytest.raises(ValueError, match="event's day"):
        entry("invited", "2026-09-15", team="events")
    with pytest.raises(ValueError):
        entry("sent", "2026-09-15", team="legal")


def test_a_dated_wait_also_goes_on_the_timeline_so_readiness_holds_and_later_reopens(store):
    contact.record(store, "A900", "not_now", until="2099-01-01", by="Dana")
    held = journey.assess(store, "A900", iso(), "mts")
    assert held.action == "respect_follow_up" and held.until[:10] == "2099-01-01"
    with pytest.raises(ValueError, match="until"):
        contact.record(store, "A900", "not_now")


def test_send_reads_the_ledger_again_so_a_later_never_still_stops_the_card(store):
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    replay = rin(store)  # as of 2026-09-15, before the never below
    assert replay.gate.state == "clear"
    contact.record(store, "A900", "never")
    seen = []
    with pytest.raises(ValueError, match="asked not to be contacted"):
        routing.send(store, replay, "https://hooks.slack.example/x", client=slack(seen))
    assert seen == [] and [e["kind"] for e in contact.history(store, "A900")] == ["identity", "never"]


def test_a_role_gets_at_most_the_weekly_cap_of_cards(store):
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    contact.record(store, "Z1", "pinged", at="2026-09-01T00:00:00+00:00", role_id="mts-research")  # last week
    for person in ("Z2", "Z3", "Z4"):
        assert contact.room(store, "mts-research", AS_OF) > 0
        contact.record(store, person, "pinged", at="2026-09-14T00:00:00+00:00", role_id="mts-research")
    assert contact.room(store, "mts-research", AS_OF) == 0 and contact.room(store, "backend", AS_OF) == 3
    with pytest.raises(ValueError, match="this week's 3 cards"):
        routing.send(store, rin(store), "https://hooks.slack.example/x", now=AS_OF, client=slack())


def test_no_reach_until_two_independent_links_confirm_who_they_are(store):
    first = rin(store).gate
    assert first.state == "check_first" and "tie each profile the engine reads for them (https://rinvale.example/, " \
        "https://github.com/rinvale-example) to a page of theirs on another site" in first.reason
    with pytest.raises(ValueError, match="two different sites"):
        contact.record(store, "A900", "identity", links=["https://rinvale.example/", "https://rinvale.example/about"])
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    assert rin(store).gate.state == "clear"
    contact.record(store, "A900", "wrong_person", at="2026-09-10T00:00:00+00:00")
    assert rin(store).gate.state == "check_first" and "wrong person" in rin(store).gate.reason
    contact.record(store, "A900", "identity", at="2026-09-12T00:00:00+00:00", links=LINKS)
    assert rin(store).gate.state == "clear"


def test_identity_counts_only_when_the_records_tie_every_profile_the_engine_reads():
    read = ["https://x.com/rin_example", "https://rinvale.example/", "https://www.linkedin.com/in/rin-vale"]
    fresh = [{"observed_at": "2026-09-10T00:00:00+00:00"}]

    def gate(*links, profiles=read, later=()):
        return contact.check([entry("identity", "2026-09-01", links=list(links)), *later], AS_OF, call(), fresh,
                             profiles=profiles)

    two = gate("https://rinvale.example/about", "https://twitter.com/Rin_Example")  # a page of their site, and their X
    assert two.state == "check_first" and "(https://www.linkedin.com/in/rin-vale untied)" in two.reason  # not LinkedIn
    linkedin = [entry("identity", "2026-09-05", links=["https://www.linkedin.com/in/rin-vale", "https://rinvale.example/"])]
    assert gate("https://rinvale.example/about", "https://twitter.com/Rin_Example", later=linkedin).state == "clear"
    stranger = gate("https://rinvale.example/", "https://github.com/someone-else")  # two sites, but not what we read
    assert stranger.state == "check_first" and "doesn't tie every profile the engine reads" in stranger.reason
    assert "https://x.com/rin_example, https://www.linkedin.com/in/rin-vale untied" in stranger.reason
    assert gate("https://x.com/rin_example_2", "https://rinvale.example/", later=linkedin).state == "check_first"
    assert gate("https://rinvale.example/", "https://x.com/rin_example", profiles=[]).state == "check_first"
    fixed = [entry("identity", "2026-09-05", links=["https://x.com/rin_example", "https://www.linkedin.com/in/rin-vale"])]
    assert gate("https://rinvale.example/", "https://github.com/someone-else", later=fixed).state == "clear"


def test_one_profile_read_is_confirmed_by_a_record_tying_it_to_their_site_and_a_post_counts_as_its_author():
    fresh = [{"observed_at": "2026-09-10T00:00:00+00:00"}]

    def gate(links, profiles, later=()):
        return contact.check([entry("identity", "2026-09-01", links=links), *later], AS_OF, call(), fresh,
                             profiles=profiles)

    linkedin = ["https://www.linkedin.com/in/rin-vale"]
    assert gate(["https://www.linkedin.com/in/rin-vale/", "https://rinvale.example/"], linkedin).state == "clear"
    post = "https://www.linkedin.com/posts/rin-vale_world-models-activity-7477764155186393088-i9qL"
    assert gate([post, "https://rinvale.example/work"], linkedin).state == "clear"  # their post, on their profile
    other = "https://www.linkedin.com/posts/rin-vale-2_world-models-activity-7477764155186393088-i9qL"
    assert gate([other, "https://rinvale.example/"], linkedin).state == "check_first"  # someone else's post
    # Their X post and their site tie X; GitHub needs its own tie, and a second record gives it.
    read = ["https://x.com/rin_example", "https://github.com/rinvale-example"]
    x_only = gate(["https://x.com/Rin_Example/status/2077092137657905211", "https://rinvale.example/projects/lemma"], read)
    assert x_only.state == "check_first" and "(https://github.com/rinvale-example untied)" in x_only.reason
    github = [entry("identity", "2026-09-02", links=["https://github.com/rinvale-example/lemma",
                                                     "https://rinvale.example/projects/lemma"])]
    assert gate(["https://x.com/Rin_Example/status/2077092137657905211", "https://rinvale.example/projects/lemma"], read,
                later=github).state == "clear"


def test_a_record_ties_a_profile_only_to_a_page_on_another_site():
    one_site = {"person_id": "P", "kind": "identity", "at": "2026-09-01T12:00:00+00:00", "links": [
        "https://x.com/rin_example", "https://twitter.com/rin_example/status/1"]}  # x.com and twitter.com: one site
    assert contact.untied([one_site["links"]], ["https://x.com/rin_example"]) == ["https://x.com/rin_example"]
    assert contact.untied([], ["https://x.com/rin_example"]) == ["https://x.com/rin_example"]
    assert contact.untied([["https://x.com/rin_example", "https://rinvale.example/"]], []) == []


def test_the_card_checks_identity_against_the_profiles_in_their_context(store):
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00",
                   links=["https://rinvale.example/", "https://x.com/someone-else"])
    assert rin(store).gate.state == "check_first" and "https://github.com/rinvale-example" in rin(store).gate.reason
    contact.record(store, "A900", "identity", at="2026-09-02T00:00:00+00:00", links=LINKS)  # their site and their GitHub
    assert rin(store).gate.state == "clear"


def test_a_trigger_older_than_three_weeks_is_checked_first():
    confirmed = [entry("identity", "2026-08-01", links=LINKS)]
    old = [{"observed_at": "2026-08-20T00:00:00+00:00"}]
    stale = contact.check(confirmed, AS_OF, call(), old)
    assert stale.state == "check_first" and "2026-08-20, over three weeks old" in stale.reason
    checked = [*confirmed, entry("checked", "2026-09-14", note="Still at the lab; paper still accepted.")]
    assert contact.check(checked, AS_OF, call(), old).state == "clear"


def test_an_undergraduate_gets_a_note_about_their_work_never_the_pitch(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'u.sqlite'}")
    routing.seed(s, json.loads((FIXTURES / "posts" / "timeline.json").read_text()))
    for e in json.loads((FIXTURES / "posts" / "ledger.json").read_text()):
        contact.record(s, **e)
    contact.record(s, "X902", "undergraduate", at="2026-09-01T00:00:00+00:00", until="2027-06-01")
    noa = routing.route(s, "X902", AS_OF, team=[])
    assert noa.gate.rapport_only and noa.call.track == "rapport"
    assert "hiring" not in noa.draft["body"] and "Rebuilding our component library" in noa.draft["body"]
    # Nothing of their own to write about: no card at all.
    student = [entry("identity", "2026-08-01", links=LINKS), entry("undergraduate", "2026-08-01")]
    assert contact.check(student, AS_OF, call(), [{"observed_at": AS_OF}]).state == "hold"


def test_a_doubt_about_whether_they_are_a_student_asks_first_until_someone_settles_it():
    # Ways it could fail: it holds or clears instead of asking first; it outranks a hold (they said never, a card
    # already went); another check first (an old sign, no identity record) hides it, so that draft pitches the role
    # and its reason never names the doubt, or names it before the reason of its own (who they are comes first); a path
    # with no signals (the re-read before a send) never sees it; a check recorded for an old sign settles it; a later record that they are an undergraduate does not
    # (a note about their work only), nor one that they graduated (the role may be raised); a blank note, or one
    # ending in a colon or a quoted full stop, gives a broken reason.
    signs, ok = [{"observed_at": AS_OF}], entry("identity", "2026-08-01", links=LINKS)
    said = "Their X bio says CS at a school: confirm before pitching a role."
    doubt = entry("unconfirmed", "2026-09-10", note=said)
    asked = contact.check([ok, doubt], AS_OF, call(), signs)
    assert (asked.state, asked.rapport_only) == ("check_first", True) and asked.reason.startswith(said)
    for bare in (contact.check([ok, doubt], AS_OF), contact.check([ok, doubt], AS_OF, call(), role_id="mts")):
        assert (bare.state, bare.rapport_only) == ("check_first", True) and bare.reason.startswith(said)
    assert contact.check([ok, doubt, entry("never", "2026-09-11")], AS_OF, call(), signs).state == "hold"
    assert contact.check([ok, entry("pinged", "2026-09-01"), doubt], AS_OF, call(), signs).state == "hold"
    old = contact.check([ok, doubt], AS_OF, call(), [{"observed_at": "2026-08-01T00:00:00+00:00"}])
    assert (old.state, old.rapport_only) == ("check_first", True) and old.reason.startswith("The newest sign")
    assert said in old.reason
    unknown = contact.check([doubt], AS_OF, call(), signs, profiles=LINKS)
    assert (unknown.state, unknown.rapport_only) == ("check_first", True) and unknown.reason.startswith("Confirm it's")
    assert said in unknown.reason
    wrong = contact.check([ok, doubt, entry("wrong_person", "2026-09-11")], AS_OF, call(), signs)
    assert wrong.reason.startswith("Marked the wrong person") and said in wrong.reason
    assert contact.check([ok, doubt, entry("checked", "2026-09-12")], AS_OF, call(), signs).state == "check_first"
    same = entry("undergraduate", "2026-09-10")  # recorded after the doubt at the same moment: the later one counts
    assert contact.check([ok, doubt, same], AS_OF, call(), signs) == contact.check([ok, same], AS_OF, call(), signs)
    for later in (entry("undergraduate", "2026-09-12"), entry("undergraduate", "2026-09-12", until="2026-06-01")):
        assert contact.check([ok, doubt, later], AS_OF, call(), signs) == contact.check([ok, later], AS_OF, call(), signs)
    assert contact.check([ok, doubt, entry("undergraduate", "2026-09-12", until="2026-06-01")], AS_OF, call(),
                         signs).state == "clear"  # graduated: the role may be raised
    for note in ("", "   "):
        bare = contact.check([ok, entry("unconfirmed", "2026-09-10", note=note)], AS_OF, call(), signs)
        assert bare.reason.startswith("We couldn't confirm whether they're a student"), bare.reason
    colon = contact.check([ok, entry("unconfirmed", "2026-09-10", note="Their bio says CS at a school:")], AS_OF)
    assert colon.reason.startswith("Their bio says CS at a school. Noted"), colon.reason
    quoted = contact.check([ok, entry("unconfirmed", "2026-09-10", note='Bio: "CS student."')], AS_OF)
    assert quoted.reason.startswith('Bio: "CS student." Noted') and "draft" not in quoted.reason, quoted.reason


def test_the_web_apps_old_flags_move_into_the_ledger_once(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'm.sqlite'}")
    s.put("person:live:a", "person", {"mode": "live", "opt_out": True})
    s.put("person:live:b", "person", {"mode": "live", "last_contact_at": "2026-09-01T00:00:00+00:00",
                                      "active_conversation": True})
    contact.migrate(s)
    contact.migrate(s)
    assert [e["kind"] for e in contact.history(s, "person:live:a")] == ["never"]
    assert [(e["kind"], e["at"]) for e in contact.history(s, "person:live:b")] == [("sent", "2026-09-01T00:00:00+00:00")]
    assert "opt_out" not in s.get("person:live:a") and "active_conversation" not in s.get("person:live:b")
    with pytest.raises(ValueError):
        contact.forget(s, "person:live:a")
    assert contact.opted_out(contact.history(s, "person:live:a"))


def test_linking_two_records_joins_their_contact_history(client):  # noqa: F811
    said_never = review(client, create(client, profile="https://profiles.example.test/one"))
    other = review(client, create(client, profile="https://profiles.example.test/two"))
    client.post(f"/api/candidates/{said_never['id']}/action", json={"action": "opt_out"})
    assert state_candidate(client, other["id"])["decision"]["state"] != "suppressed"
    client.post("/api/people/link", json={"candidate_id": said_never["id"], "same_person_as": other["id"]})
    decision = state_candidate(client, other["id"])["decision"]
    assert decision["state"] == "suppressed" and "asked not to be contacted" in decision["reasons"][0]


def test_resetting_the_simulation_clears_its_invented_contact_history(client):  # noqa: F811
    c = review(client, create(client))
    client.post(f"/api/candidates/{c['id']}/action", json={"action": "opt_out"})
    assert contact.history(main.store, c["person_id"])
    assert client.post("/api/simulation/reset", json={}).status_code == 200
    assert contact.history(main.store, c["person_id"]) == []


def route_script(*args, tmp):
    # No Slack app (an empty setting wins over .env), a webhook _post refuses and a throwaway store, so no run here can
    # reach Slack or a real ledger.
    env = {**os.environ, "SLACK_BOT_TOKEN": "", "SLACK_CHANNEL": "", "SLACK_ROUTING_WEBHOOK": "http://refused.invalid",
           "TIMELINES_DB": str(tmp / "r.sqlite"), "GI_PAUSE_FILE": str(tmp / "sending-paused.json")}
    return subprocess.run([sys.executable, "scripts/route.py", *args], cwd=ROOT, env=env, capture_output=True, text=True)


def test_route_says_why_each_reach_now_person_got_no_card(tmp_path):
    out = route_script("--demo", "--fixtures", "tests/fixtures/posts", "--top", "0", tmp=tmp_path).stdout
    assert "Kept quiet: Noa Brandt (product-designer): Not in the top 0 for the role this run." in out
    assert "0 carded, 2 kept quiet" in out
    refused = route_script("--send", "--as-of", "2026-09-15", tmp=tmp_path)
    assert refused.returncode != 0 and "not with --as-of" in refused.stderr


def test_a_people_file_routes_its_watchlist_only_even_when_it_is_empty(tmp_path):
    moment = {"date": "2026-08-01", "kind": "launch", "source_url": "https://example.test/launch"}
    people = tmp_path / "people.json"
    for rows, carded in (([{"subject_id": "X900", "role": "mts-research", "since": "2026-01-01"}], "1 of 1 people"),
                         ([{"subject_id": "X900", "role": "mts-research", "since": "2026-01-01", "moments": [moment]}],
                          "0 of 0 people"),
                         ([{"subject_id": "X900", "role": "mts-research", "since": "2026-01-01", "moments": [moment]},
                           {"subject_id": "backend:X900", "role": "backend", "since": "2026-01-01"}],  # the same person
                          "0 of 0 people")):  # a replay case under any id stays out, as the Monday brief keeps them
        people.write_text(json.dumps(rows))
        out = route_script("--demo", "--fixtures", "tests/fixtures/posts", "--people", str(people), tmp=tmp_path).stdout
        assert carded in out, out
        named = route_script("--demo", "--fixtures", "tests/fixtures/posts", "--people", str(people), "--subject", "X900",
                             tmp=tmp_path).stdout
        assert carded in named, named  # naming someone narrows the watchlist, never adds a replay case to it


def test_a_real_send_needs_the_people_file_that_says_who_is_a_replay_case(tmp_path):
    refused = route_script("--send", tmp=tmp_path)
    assert refused.returncode != 0 and "needs --people" in refused.stderr and "Nothing was posted." in refused.stderr
    assert not refused.stdout


def test_a_refused_post_does_not_print_the_webhook():
    slack = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404, text="no_service")))
    with pytest.raises(RuntimeError) as refused:
        routing._post({"text": "t"}, "https://hooks.slack.example/T/B/secret", slack)
    assert "404 no_service" in str(refused.value) and "secret" not in str(refused.value)


def test_the_webhook_comes_from_the_environment_else_env(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("route_script", ROOT / "scripts" / "route.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "ROOT", tmp_path)
    monkeypatch.delenv("SLACK_ROUTING_WEBHOOK", raising=False)
    assert script.webhook_url() == ""
    (tmp_path / ".env").write_text("SLACK_ROUTING_WEBHOOK=https://hooks.slack.example/T/B/file\n")
    assert script.webhook_url() == "https://hooks.slack.example/T/B/file"
    monkeypatch.setenv("SLACK_ROUTING_WEBHOOK", "https://hooks.slack.example/T/B/env")
    assert script.webhook_url() == "https://hooks.slack.example/T/B/env"


def test_the_demo_posts_the_morning_list_and_its_cards_marked_as_tests_and_never_touches_the_real_ledger(tmp_path,
                                                                                                        monkeypatch):
    spec = importlib.util.spec_from_file_location("route_script", ROOT / "scripts" / "route.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    posted = []
    monkeypatch.setattr(routing, "_post", lambda card, url, *rest: posted.append(card))
    monkeypatch.setattr(script, "webhook_url", lambda: "https://hooks.slack.example/T/B/x")
    monkeypatch.setattr(script.slack, "load", lambda: dict.fromkeys(script.slack.NAMES, ""))  # never a real Slack app
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(sys, "argv", ["route.py", "--demo", "--fixtures", "tests/fixtures/posts", "--send"])
    script.main()
    assert len(posted) == 3  # the morning list, then each of the demo's two carded people (a webhook cannot thread)
    head, note = posted[0]["blocks"][:2]
    assert head["text"]["text"] == "Test · Who to reach this morning" and "everyone on it is made up" in \
        note["elements"][0]["text"]
    for card in posted[1:]:
        head, note = card["blocks"][:2]
        assert head["text"]["text"].startswith("Test · ") and "made-up person" in note["elements"][0]["text"]
        assert card["text"].startswith("Test, made-up person: Early sign: ")
    assert not today.TIMELINES.exists()  # the timing store (a throwaway one here) is never opened by the demo


def test_a_note_written_by_contacts_py_is_seen_by_routing_and_by_the_gate(tmp_path, monkeypatch):
    s = today.ledger()  # the one timing ledger (a throwaway one here: conftest)
    routing.seed(s, json.loads((FIXTURES / "routing" / "timeline.json").read_text()))
    assert routing.route(s, "A900", AS_OF, team=[]).gate.reason.startswith("Confirm it's them")
    spec = importlib.util.spec_from_file_location("contacts_script", ROOT / "scripts" / "contacts.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'web.sqlite'}")  # the web app's store, never the ledger
    days = iter(["2026-09-13T09:00:00+00:00", "2026-09-14T09:00:00+00:00"])  # both before the run's morning
    monkeypatch.setattr(contact, "iso", lambda: next(days))

    def contacts_py(*args):
        monkeypatch.setattr(sys, "argv", ["contacts.py", "record", "A900", *args])
        script.main()
        return routing.route(s, "A900", AS_OF, team=[]).gate, contact.check(contact.history(s, "A900"), AS_OF)

    routed, gate = contacts_py("identity", *(f"--link={u}" for u in LINKS))
    assert routed.state == gate.state == "clear"
    routed, gate = contacts_py("wrong_person", "--note", "Another Rin Vale")
    assert routed == gate and gate.state == "check_first" and gate.reason.startswith("Marked the wrong person")
    assert not (tmp_path / "web.sqlite").exists()


def test_every_writer_and_reader_opens_the_one_timing_ledger(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "TIMELINES_DB"}
    env |= {"SOCIAL_DIR": str(tmp_path), "DATABASE_URL": f"sqlite:///{tmp_path / 'web.sqlite'}"}
    paths = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'scripts'); import social_pull; "
                            "from app import contact, today; "
                            "print(today.TIMELINES, contact.LEDGERS['the timing run'], social_pull.PRIVATE)"],
                           cwd=ROOT, env=env, capture_output=True, text=True)
    assert paths.stdout.split() == [str(tmp_path / "timelines.sqlite")] * 2 + [str(tmp_path)], paths.stderr

    def contacts_py(person):
        done = subprocess.run([sys.executable, "scripts/contacts.py", "record", person, "never"], cwd=ROOT, env=env,
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return done.stdout

    assert f"Recorded in {tmp_path / 'timelines.sqlite'}:" in contacts_py("A901")  # the pull's store, as route.py's
    env["TIMELINES_DB"] = str(tmp_path / "t.sqlite")
    assert f"Recorded in {tmp_path / 't.sqlite'}:" in contacts_py("A902")
    assert [e["person_id"] for e in Store(f"sqlite:///{tmp_path / 'timelines.sqlite'}").all("contact")] == ["A901"]
    assert [e["person_id"] for e in Store(f"sqlite:///{tmp_path / 't.sqlite'}").all("contact")] == ["A902"]
    assert not (tmp_path / "web.sqlite").exists()
    # Only the web app's side picks its store from DATABASE_URL or a bare Store(); contact.py reads it only to find
    # the web app's ledger, and daily.py only to stop when an older job names another store with it
    reads = {str(f.relative_to(ROOT)) for f in [*ROOT.glob("app/*.py"), *ROOT.glob("scripts/*.py")]
             if re.search(r'getenv\("DATABASE_URL"|environ\.get\("DATABASE_URL"|\bStore\(\)', f.read_text())}
    assert reads == {"app/store.py", "app/main.py", "scripts/posts.py", "scripts/build_timelines.py", "app/contact.py",
                     "scripts/daily.py"}


def test_the_web_app_s_ledger_is_its_own_store_and_never_the_timing_one(tmp_path):
    def ledgers(**env):
        base = {k: v for k, v in os.environ.items() if k not in ("TIMELINES_DB", "DATABASE_URL")}
        done = subprocess.run([sys.executable, "-c", "from app import contact; print(*contact.LEDGERS.values())"],
                              cwd=ROOT, env=base | env, capture_output=True, text=True)
        return done.stdout.split()
    timing = str(tmp_path / "t.sqlite")
    assert ledgers(TIMELINES_DB=timing, DATABASE_URL=f"sqlite:///{tmp_path / 'web.sqlite'}") == [
        str(tmp_path / "web.sqlite"), timing]
    pilot = str(ROOT / "data" / "pilot.sqlite")
    assert ledgers(TIMELINES_DB=timing, DATABASE_URL=f"sqlite:///{timing}") == [pilot, timing]  # an older job's
    assert ledgers(TIMELINES_DB="t.sqlite") == [pilot, str(ROOT / "t.sqlite")]  # read against the repo
    assert ledgers(TIMELINES_DB=timing, DATABASE_URL="postgresql://host/db") == [pilot, timing]


def test_the_daily_run_stops_when_an_older_job_names_another_store(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("daily_script", ROOT / "scripts" / "daily.py")
    daily = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daily)
    for url, stops in ((f"sqlite:///{today.TIMELINES}", False), ("sqlite:///data/pilot.sqlite", False),
                       ("postgresql://host/db", False), (f"sqlite:///{tmp_path / 'old.sqlite'}", True)):
        monkeypatch.setenv("DATABASE_URL", url)
        assert bool(daily.old_store()) is stops, url
    assert "set TIMELINES_DB to it" in daily.old_store() and "Nothing ran" in daily.old_store()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'old.sqlite'}"}
    done = subprocess.run([sys.executable, "scripts/daily.py", "--people", str(FIXTURES / "routing" / "timeline.json"),
                           "--dry-run"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert done.returncode != 0 and "Nothing ran" in done.stderr and not (tmp_path / "old.sqlite").exists()
    shown = subprocess.run([sys.executable, "scripts/contacts.py", "show", "A900"], cwd=ROOT, env=env,
                           capture_output=True, text=True)
    assert shown.returncode != 0 and "No timing store" in shown.stderr and not today.TIMELINES.exists()


def test_redraft_without_a_model_key_says_so_once_and_the_run_still_routes(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("route_script", ROOT / "scripts" / "route.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    budget = providers.Budget({"max_calls_per_run": 4})
    paid = types.ModuleType("posts")  # scripts/posts.py's settings, with no key: nothing reads a real one
    paid._paid_settings = lambda max_calls, model: ({"anthropic_key": ""}, budget)
    monkeypatch.setitem(sys.modules, "posts", paid)
    assert script._writer(None, "claude-opus-5", 4, AS_OF) == (None, budget)
    paid._paid_settings = lambda max_calls, model: ({"anthropic_key": "set"}, budget)
    assert callable(script._writer(None, "claude-opus-5", 4, AS_OF)[0])  # built, never called here
    paid._paid_settings = lambda max_calls, model: ({"anthropic_key": ""}, budget)
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(sys, "argv", ["route.py", "--demo", "--redraft"])
    script.main()
    out = capsys.readouterr().out
    assert out.count(script.NO_KEY) == 1 and out.rstrip().endswith("; 0 model calls, $0.0000")


def test_the_demo_inbox_goes_to_slack_marked_as_a_test(monkeypatch):
    spec = importlib.util.spec_from_file_location("route_script", ROOT / "scripts" / "route.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    posted = []
    monkeypatch.setattr(routing, "_post", lambda card, url, *rest: posted.append(card))
    monkeypatch.setattr(script, "webhook_url", lambda: "https://hooks.slack.example/T/B/x")
    monkeypatch.setattr(script.slack, "load", lambda: dict.fromkeys(script.slack.NAMES, ""))  # never a real Slack app
    monkeypatch.chdir(ROOT)
    script.send_inbox("simulation", True)
    card, *_ = posted  # the post; its thread follows
    assert card["text"].startswith("Test, made-up people: Monday brief")
    assert "everyone on it is made up" in card["blocks"][1]["elements"][0]["text"]


def test_identity_records_import_from_a_file_once_and_all_or_nothing(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("contacts_script", ROOT / "scripts" / "contacts.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    s = Store(f"sqlite:///{tmp_path / 'i.sqlite'}")
    rows = [{"person_id": "A900", "kind": "identity", "links": LINKS, "note": "Invented."}]
    assert script.import_entries(s, rows) is None and script.import_entries(s, rows) is None
    assert [e["kind"] for e in contact.history(s, "A900")] == ["identity"]
    bad = [{"person_id": "B1", "kind": "identity", "links": LINKS}, {"person_id": "B2", "kind": "identity", "links": LINKS[:1]}]
    assert "row 2" in script.import_entries(s, bad) and contact.history(s, "B1") == []
    for row in ({"person_id": "B3", "kind": "sent"}, ["not", "an", "object"]):  # outcomes go in one at a time
        assert "Nothing imported: row 1" in script.import_entries(s, [row])
    assert "Warning" not in capsys.readouterr().out  # no context on file, nothing to compare
    fresh = Store(f"sqlite:///{tmp_path / 'j.sqlite'}")
    routing.seed(fresh, json.loads((FIXTURES / "routing" / "timeline.json").read_text()))
    script.import_entries(fresh, [{"person_id": "A900", "kind": "identity",
                                   "links": ["https://rinvale.example/", "https://x.com/someone-else"]}])
    assert "Warning: row 1 (A900): no record ties https://github.com/rinvale-example to a page on another site" in \
        capsys.readouterr().out  # their site is tied; their GitHub is not
    script.import_entries(fresh, rows)  # their site and their GitHub: now every profile read is tied
    assert "Warning" not in capsys.readouterr().out
    contact.record(fresh, "A900", "wrong_person", at=iso())  # the records before it no longer count
    both = [{"person_id": "A900", "kind": "identity", "links": ["https://rinvale.example/", "https://x.com/rin"]},
            {"person_id": "A900", "kind": "identity", "links": ["https://github.com/rinvale-example/lemma",
                                                                "https://rinvale.example/projects/lemma"]}]
    script.import_entries(fresh, both[:1])
    assert "no record ties https://github.com/rinvale-example" in capsys.readouterr().out
    script.import_entries(fresh, both)  # two rows of one file tie both profiles together
    assert "Warning" not in capsys.readouterr().out


def test_never_in_one_role_holds_the_person_in_every_role(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'k.sqlite'}")
    contact.record(s, "mts-research:ann-example", "never", at="2026-09-01T00:00:00+00:00")
    for subject in ("backend:ann-example", "product-designer:ann-example", "ann-example"):
        assert contact.check(contact.history(s, subject), AS_OF, call(), role_id="backend").state == "hold"
    assert contact.history(s, "backend:bo-example") == []
    assert contact.person_key("person:live:abc") == "person:live:abc" and contact.person_key("x:y") == "x:y"


def test_a_card_in_one_role_holds_every_other_role_for_good():
    pinged = [entry("pinged", "2026-09-01", role_id="mts-research", evidence=["e1"], score=0.9)]
    for day in (AS_OF, "2026-12-01T00:00:00+00:00", "2027-09-01T00:00:00+00:00"):
        other = contact.check(pinged, day, call(0.95, ["b1"]), role_id="backend")
        assert other.state == "hold" and "On a card from 2026-09-01 (Member of Technical Staff): one card per person" \
            in other.reason


# How a hold can show a role's id ("(product-designer)") where ops read it, listed before the fix:
#   1. The Monday brief's held line, from a check it gates itself (inbox._gate): tests/test_inbox.py.
#   2. Today's calls, on a reach it routed, once its own rewrite of the reason is gone: tests/test_inbox.py.
#   3. The Events page's hold (events.ledger_hold): tests/test_events.py.
#   4. A card for a role no longer in config/roles.json shows nothing, or the check fails: it keeps the id.
#   5. config/roles.json that won't read opens the gate: it still holds, with the id.
#   6. A card with no role gets empty parentheses (test_one_card_per_person_ever pins it without).
def test_a_card_names_its_role_by_title_or_else_by_id_and_holds_either_way(tmp_path, monkeypatch):
    held = lambda role_id: contact.check([entry("pinged", "2026-09-01", role_id=role_id)], AS_OF, call(0.9, ["b1"]),  # noqa: E731
                                         role_id="backend")
    assert held("product-designer").reason == "On a card from 2026-09-01 (Product Designer): one card per person, ever."
    assert held("dropped-role").reason == "On a card from 2026-09-01 (dropped-role): one card per person, ever."  # 4
    monkeypatch.setattr(contact, "CONFIG", tmp_path)  # 5: no roles.json
    assert held("product-designer").state == "hold" and "(product-designer)" in held("product-designer").reason
    (tmp_path / "roles.json").write_text("{not json")
    assert held("product-designer").state == "hold" and "(product-designer)" in held("product-designer").reason


def test_a_line_on_gtm_s_accounts_list_holds_a_hiring_card_for_the_quiet_period_only():
    listed = [entry("pinged", "2026-09-01", role_id="gtm", team="sales", evidence=["https://example.org/a"])]
    held = contact.check(listed, AS_OF, call(0.9, ["e1"]), role_id="mts-research")
    assert held.state == "hold" and held.reason == "On GTM's accounts list from 2026-09-01: one team at a time, until " \
        "2026-11-30."
    assert contact.check(listed, "2026-12-02T00:00:00+00:00", call(0.9, ["e1"]), role_id="mts-research").state == "clear"
    # A hiring card before or after the listing still holds for good.
    for card in ("2026-08-01", "2026-09-10"):
        both = [*listed, entry("pinged", card, role_id="mts-research", evidence=["e1"], score=0.9)]
        again = contact.check(both, "2027-06-01T00:00:00+00:00", call(0.95, ["e2"]), role_id="backend")
        assert again.state == "hold" and f"On a card from {card} (Member of Technical Staff)" in again.reason


def test_the_week_holds_at_most_four_cards_across_all_roles(store, tmp_path):
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    for person, role_id in (("Z1", "backend"), ("Z2", "backend"), ("Z3", "product-designer"), ("Z4", "controller")):
        assert contact.room_all(store, AS_OF) > 0
        contact.record(store, person, "pinged", at="2026-09-14T00:00:00+00:00", role_id=role_id)
    assert contact.room_all(store, AS_OF) == 0 and contact.room(store, "mts-research", AS_OF) == 3
    contact.record(store, "Z5", "pinged", at="2026-09-14T12:00:00+00:00", role_id="gtm", team="sales")
    assert contact.room_all(store, "2026-09-21T06:00:00+00:00") == 4  # past Z1-Z4's week; the accounts list's never count
    with pytest.raises(ValueError, match="This week's 4 cards across all roles are already out"):
        routing.send(store, rin(store), "https://hooks.slack.example/x", now=AS_OF, client=slack())

    (tmp_path / "timeline.json").write_text((FIXTURES / "routing" / "timeline.json").read_text())
    (tmp_path / "ledger.json").write_text(json.dumps(
        [{"person_id": "A900", "kind": "identity", "links": LINKS, "at": "2026-09-01T00:00:00+00:00"}]
        + [{"person_id": f"Z{n}", "kind": "pinged", "role_id": "backend" if n < 3 else "controller",
            "at": "2026-09-14T00:00:00+00:00"} for n in range(4)]))
    out = route_script("--demo", "--fixtures", str(tmp_path), tmp=tmp_path).stdout
    assert "Kept quiet: Rin Vale (mts-research): Past this week's cap of 4 cards across all roles." in out


def test_while_sending_is_paused_nothing_posts_and_a_bad_pause_file_still_pauses(store, tmp_path):
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    r, seen = rin(store), []
    contact.pause("Justin", "A wrong card went out.", at="2026-09-15T09:00:00+00:00")
    for post in (lambda: routing.send(store, r, "https://hooks.slack.example/x", now=AS_OF, client=slack(seen)),
                 lambda: routing.send_digest({"blocks": []}, "https://hooks.slack.example/x", client=slack(seen))):
        with pytest.raises(ValueError, match=r"Sending is paused \(since 2026-09-15, by Justin\): A wrong card went "
                                             r"out\. Nothing was posted\."):
            post()
    assert seen == [] and "pinged" not in [e["kind"] for e in contact.history(store, "A900")]
    contact.PAUSE.write_text("{not json")
    with pytest.raises(ValueError, match="can't be read, so sending stays paused"):
        routing.send(store, r, "https://hooks.slack.example/x", now=AS_OF, client=slack(seen))
    assert contact.resume()["by"] == "" and contact.paused() is None and contact.resume() is None
    routing.send(store, r, "https://hooks.slack.example/x", now=AS_OF, client=slack(seen))
    assert len(seen) == 1

    env = {**os.environ, "GI_PAUSE_FILE": str(tmp_path / "paused.json"), "TIMELINES_DB": str(tmp_path / "p.sqlite")}
    run = [sys.executable, "scripts/contacts.py"]
    out = subprocess.run([*run, "pause", "--by", "Justin", "--why", "Checking a card"], cwd=ROOT, env=env,
                         capture_output=True, text=True).stdout
    assert "Sending is paused" in out and "by Justin): Checking a card." in out
    (tmp_path / "timeline.json").write_text((FIXTURES / "routing" / "timeline.json").read_text())
    env["SLACK_ROUTING_WEBHOOK"], env["SLACK_BOT_TOKEN"], env["SLACK_CHANNEL"] = "https://hooks.slack.example/x", "", ""
    held = subprocess.run([sys.executable, "scripts/route.py", "--demo", "--fixtures", str(tmp_path), "--send"],
                          cwd=ROOT, env=env, capture_output=True, text=True)
    assert held.returncode != 0 and "Sending is paused" in held.stderr and "resume" in held.stderr
    out = subprocess.run([*run, "resume"], cwd=ROOT, env=env, capture_output=True, text=True).stdout
    assert "Sending resumed" in out and not (tmp_path / "paused.json").exists()


def test_one_person_in_two_roles_gets_one_card_a_run(tmp_path):
    data = json.loads((FIXTURES / "routing" / "timeline.json").read_text())
    both = {"contexts": [], "events": []}
    for subject in ("mts-research:rin", "backend:rin"):  # the same invented person listed twice
        rows = json.loads(json.dumps(data).replace('"A900"', f'"{subject}"'))
        both["contexts"] += rows["contexts"]
        both["events"] += rows["events"]
    (tmp_path / "timeline.json").write_text(json.dumps(both))
    (tmp_path / "ledger.json").write_text(json.dumps([{"person_id": "rin", "kind": "identity", "links": LINKS,
                                                        "at": "2026-09-01T00:00:00+00:00"}]))
    out = route_script("--demo", "--fixtures", str(tmp_path), tmp=tmp_path).stdout
    assert "1 carded, 1 kept quiet" in out and "One card per person: this run cards them for mts-research" in out

    s = Store(f"sqlite:///{tmp_path / 'two.sqlite'}")
    routing.seed(s, both)
    contact.record(s, "rin", "identity", at="2026-09-01T00:00:00+00:00", links=LINKS)
    first = routing.route(s, "mts-research:rin", AS_OF, team=[])
    routing.send(s, first, "https://hooks.slack.example/x", now=AS_OF, client=slack())
    other_role = first.model_copy(update={"subject_id": "backend:rin", "role": {**first.role, "id": "backend"}})
    with pytest.raises(ValueError, match="one card per person"):
        routing.send(s, other_role, "https://hooks.slack.example/x", now=AS_OF, client=slack())


def test_the_web_apps_older_path_holds_an_undergraduates_draft_that_names_the_role_or_hiring():
    student = [entry("undergraduate", "2031-01-10", until="2032-06-01")]
    plain = ready_candidate()
    assert decide(plain, role(), student, now=NOW)["state"] == "ready"  # about their work only: fine
    for body in (f"We're hiring for our {role()['title']} role, and I'd love to chat.",
                 "We're hiring, and your close automation work caught my eye; I'd love to chat."):
        named = {**plain, "draft": {**plain["draft"], "body": body}}
        named["review_hash"] = review_hash(named, role())  # a reviewer passed the new draft
        held = decide(named, role(), student, now=NOW)
        assert held["state"] == "research" and "they're an undergraduate" in held["reasons"][-1]
        assert held["ping"] is None
        assert decide(named, role(), [], now=NOW)["state"] == "ready"  # anyone else's draft names the role
    graduated = [entry("undergraduate", "2029-01-10", until="2030-06-01")]
    assert decide(named, role(), graduated, now=NOW)["state"] == "ready"


def test_route_says_where_it_posts_and_why_not_the_app(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("route_script", ROOT / "scripts" / "route.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    settings = {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "", "SLACK_CHANNEL": ""}
    monkeypatch.setattr(script.slack, "load", lambda: settings)
    monkeypatch.setattr(script, "webhook_url", lambda: "https://hooks.slack.example/T/B/x")
    assert script.target() == "https://hooks.slack.example/T/B/x"
    said = capsys.readouterr().out
    assert "SLACK_CHANNEL is not" in said and "the cards have no buttons" in said and "xoxb-test" not in said
    settings["SLACK_CHANNEL"] = "C0GI"
    assert isinstance(script.target(), script.slack.Poster) and "ledger buttons" in capsys.readouterr().out


def test_the_weekly_caps_count_only_cards_up_to_now(store):
    for day in ("2026-09-14", "2026-09-15", "2026-09-16"):
        contact.record(store, f"mts:p{day}", "pinged", at=f"{day}T13:00:00+00:00", role_id="mts")
    assert contact.room(store, "mts", "2026-09-14T00:00:00+00:00") == contact.WEEKLY_CAP  # a replay's earlier morning
    assert contact.room(store, "mts", "2026-09-15T13:00:00+00:00") == contact.WEEKLY_CAP - 2
    assert contact.room_all(store, "2026-09-14T00:00:00+00:00") == contact.TOTAL_CAP
    assert contact.room_all(store, "2026-09-16T23:00:00+00:00") == contact.TOTAL_CAP - 3


def test_joining_is_the_latest_accepted_offer_still_in_force():
    def hired(at, until=None):
        return {"kind": "hired", "at": f"{at}T09:00:00+00:00", "until": until}
    now = "2026-09-20T12:00:00+00:00"
    assert contact.joining([hired("2026-09-10")], now)["at"].startswith("2026-09-10")
    assert contact.joining([hired("2026-09-10", until="2026-09-20")], now) is None  # ended today
    assert contact.joining([hired("2026-09-10", until="2026-09-21")], now)  # ends tomorrow: still in force
    assert contact.joining([{"kind": "hired", "at": "2026-09-10T09:00:00+00:00"}], now)  # no until at all
    assert contact.joining([hired("2026-09-21")], now) is None  # not yet
    assert contact.joining([hired("2026-08-01"), hired("2026-09-10", until="2026-09-15")], now) is None  # the latest decides
    assert contact.joining([hired("2026-09-10"), hired("2026-09-10", until="2026-09-12")], now) is None  # same time
    assert contact.joining([{"kind": "pinged", "at": "2026-09-10T09:00:00+00:00"}], now) is None
