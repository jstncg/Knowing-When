import json
from datetime import date, timedelta

import httpx
import pytest

from app import accounts, contact, inbox, journey, today
from app.accounts import Account, Contact, Signal, Tie
from app.sources import jobs, news

AS_OF = today.DEMO_AS_OF


@pytest.fixture
def simulation():
    """The demo store Today's calls reads, fresh before and after, since sending or marking writes to it."""
    today._demo_store.cache_clear()
    yield
    today._demo_store.cache_clear()


def _rows():
    return {r["id"]: r for r in accounts.page("simulation")["accounts"]}


def _account(name):
    (store, as_of), found, records = accounts.workspace("simulation")
    return store, as_of, next(a for a in found if a.id == name), records


def test_a_moment_opens_its_window_and_a_new_lead_gets_two_weeks_first():
    a = Account(id="a", name="Acme", signals=[Signal(kind="new_lead", day="2026-09-10", quote="Joining Acme",
                                                     source_url="https://example.org/a")])
    assert accounts.call(a, date(2026, 9, 15))[:2] == ("watch_until", date(2026, 9, 24))
    action, until, now = accounts.call(a, date(2026, 9, 24))
    assert (action, until, [s.kind for s in now]) == ("reach_now", date(2026, 12, 9), ["new_lead"])
    assert accounts.call(a, date(2026, 12, 10))[0] == "quiet"


def test_undated_evidence_never_opens_a_window_and_competitors_stay_quiet():
    undated = Signal(kind="job_post", quote="Research Engineer, World Models", source_url="https://example.org/j")
    fresh = Signal(kind="team_paper", day="2026-09-10", quote="Our world model", source_url="https://example.org/p")
    assert accounts.call(Account(id="a", name="Acme", signals=[undated]), date(2026, 9, 15))[0] == "quiet"
    rival = Account(id="r", name="Rival", buys="watch", why="Builds its own.", signals=[fresh])
    assert accounts.call(rival, date(2026, 9, 15))[0] == "quiet"
    assert accounts.why(rival, date(2026, 9, 15), "quiet", None, []) == "Builds its own. Watch, don't sell."


def test_a_moment_not_yet_public_is_not_seen():
    later = Signal(kind="funding", day="2026-09-20", quote="Acme raises", source_url="https://example.org/f")
    assert accounts.call(Account(id="a", name="Acme", signals=[later]), date(2026, 9, 15))[0] == "quiet"


def test_the_weekly_list_is_this_weeks_accounts_with_why_who_way_in_and_draft(simulation):
    page = accounts.page("simulation")
    assert [r["action"] for r in page["accounts"]][:5] == ["reach_now"] * 5
    assert page["this_week"] == ["driftwire", "birchwood-lab", "loam-robotics"]
    text = json.dumps(page["weekly"], ensure_ascii=False)
    assert page["weekly"]["text"] == "Accounts to write to this week: Driftwire, Birchwood Lab, Loam Robotics"
    assert "3 of 7 accounts to write to this week" in text
    for part in ("*Why now:*", "*To:*", "*From:*", "*Way in:*", "*Would change this:*", "Nothing is sent",
                 "Loam Robotics* · _also builds its own models_", "Birchwood Lab* · Drones and robotics · "):
        assert part in text
    assert "Mock World" not in text and "Sample Sim" not in text and "Tove" not in text  # Sample Sim waits: the cap


def test_gtm_stays_out_of_the_ops_monday_brief(simulation):
    assert "accounts" not in inbox.week("simulation")
    assert "Driftwire" not in json.dumps(inbox.card(inbox.week("simulation")))


def test_a_person_recruiting_watches_is_never_written_to_even_in_their_conversation(simulation):
    row = _rows()["example-robotics"]  # Tove Lind, the new lead, is watched for MTS and talking with Dana Kest
    assert (row["action"], row["write_to"], row["draft"]) == ("reach_now", None, None)
    assert row["blocked"] == "Nobody to write to. Tove Lind: Another team at GI holds them: sales doesn't write to them."


def _lab(*people):
    return Account(id="b", name="Birch", owner="Nora Example", contacts=list(people),
                   signals=[Signal(kind="job_post", day="2026-09-08", quote="RL engineer", source_url="https://example.org/j")])


def test_a_watched_champion_is_caught_by_name_when_no_subject_id_was_given(simulation):
    (store, as_of), _, records = accounts.workspace("simulation")
    for spelling in ("Lena Okafor", "Léna K. Okafor", "lena-okafor", "LENA OKAFOR"):  # D904, watched for MTS
        row = accounts.assess(store, as_of, _lab(Contact(name=spelling, part="champion")), records)
        assert row["draft"] is None and f"{spelling}: {accounts.WATCHED}" in row["blocked"]


def test_a_watched_researcher_is_caught_by_their_openalex_page_whatever_the_name(simulation):
    (store, as_of), _, records = accounts.workspace("simulation")
    journey.save_context(store, journey.PersonContext(subject_id="Z1", name="Zed Example", role="mts",
                                                      anchors={"openalex": "https://openalex.org/A77"}))
    author = Contact(name="Z. Q. Sample", part="champion", source_url="https://openalex.org/A77")
    assert accounts.assess(store, as_of, _lab(author), records)["draft"] is None
    assert accounts.identify(store, author) == "Z1"
    assert accounts.identify(store, Contact(name="Zed Example")) is None  # a name alone identifies no one
    elsewhere = Contact(name="Z. Q. Sample", part="champion", source_url="https://example.org/people/A77")
    assert accounts.assess(store, as_of, _lab(elsewhere), records)["draft"]  # only an openalex.org page counts
    journey.save_context(store, journey.PersonContext(subject_id="Z7", name="李明", role="mts"))
    row = accounts.assess(store, as_of, _lab(Contact(name="李明", part="champion")), records)
    assert row["draft"] is None and f"李明: {accounts.WATCHED}" in row["blocked"]


def test_names_compare_without_accents_initials_or_apostrophes():
    assert accounts._name("Siobhán O'Brien") == accounts._name("Siobhan OBrien") == "siobhan obrien"
    assert accounts._name("李明") == "李明" and accounts._name("J. K.") == "j. k."  # compared as they are
    assert accounts._openalex("https://openalex.org/a77") == "A77" and accounts._openalex("A5") == "A5"


def test_names_in_another_script_get_their_own_keys_and_accents_do_not_split_one_person(simulation):
    lab = _lab(Contact(name="李明", part="champion"), Contact(name="王芳"))
    li, wang = (accounts.key(lab, c) for c in lab.contacts)
    assert li != wang and li.startswith("account:b:n")
    assert (accounts.key(lab, Contact(name="José García")) == accounts.key(lab, Contact(name="Jose Garcia"))
            == "account:b:jose-garcia")  # a never under one spelling holds the other
    assert accounts.key(lab, Contact(name="A. Chen")) != accounts.key(lab, Contact(name="B. Chen"))  # initials kept
    assert accounts.key(lab, Contact(name="Ming 李")) != accounts.key(lab, Contact(name="Ming 王"))
    (store, as_of), _, records = accounts.workspace("simulation")
    contact.record(store, wang, "replied", at="2026-09-02T10:00:00+00:00", team="sales", by="Luis Example")
    row = accounts.assess(store, as_of, lab, records)
    assert row["write_to"]["name"] == "王芳" and "talking with 王芳" in row["route_in"] and "李明" not in row["route_in"]


def test_a_new_lead_matches_its_person_however_the_name_is_written(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    lead = Signal(kind="new_lead", day="2026-09-01", who="Max Exámple", quote="Joining Acme", source_url="https://example.org/m")
    row = accounts.assess(store, "2026-09-20T12:00:00+00:00", _acme(lead), records)
    assert row["write_to"]["name"] == "Max Example"
    chens = _acme(lead.model_copy(update={"who": "A. Chen"}), contacts=[Contact(name="B. Chen", part="champion")])
    row = accounts.assess(store, "2026-09-20T12:00:00+00:00", chens, records)
    assert row["write_to"] is None and "A. Chen is not a contact on file yet" in row["blocked"]
    assert not accounts._same("Ming 李", "Ming 王") and not accounts._same("Søren A. Berg", "Søren B. Berg")
    assert accounts._same("Søren Berg", "søren  berg") and accounts._same("李明", "李明")


def test_a_sales_conversation_is_the_way_in_and_the_writer(simulation):
    store, as_of, _, _ = _account("driftwire")
    contact.record(store, "account:driftwire:lee-example", "replied", at="2026-09-02T10:00:00+00:00", team="sales",
                   by="Luis Example")
    row = _rows()["driftwire"]
    assert (row["write_to"]["name"], row["writer"]) == ("Lee Example", "Luis Example")
    assert row["route_in"].startswith("Luis Example (sales) has been talking with Lee Example since Sep 2")
    assert "driftwire" in accounts.page("simulation")["this_week"]  # the note goes in that conversation


def _acme(*signals, contacts=None, **fields):
    people = contacts or [Contact(name="Max Example", title="Head of autonomy (new)"),
                          Contact(name="Cleo Example", title="Staff engineer", part="champion")]
    return Account(**{"id": "acme", "name": "Acme", "owner": "Omar Example", "contacts": people, "signals": list(signals),
                      **fields})


CLEO = {"name": "Cleo Example", "at": "Acme"}  # the champion, on the paper
NEW_LEAD = Signal(kind="new_lead", day="2026-09-01", who="Max Example", quote="Joining Acme to lead autonomy",
                  source_url="https://example.org/acme/max")


def test_a_new_lead_is_written_to_that_person_never_to_a_colleague(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    as_of = "2026-09-20T12:00:00+00:00"
    row = accounts.assess(store, as_of, _acme(NEW_LEAD), records)
    assert row["write_to"]["name"] == "Max Example" and row["draft"].startswith("Hey Max, congrats on the new role")
    contact.record(store, "account:acme:max-example", "never", at="2026-09-19T00:00:00+00:00")
    row = accounts.assess(store, as_of, _acme(NEW_LEAD), records)
    assert row["write_to"] is None and row["draft"] is None  # not to Cleo, the champion
    assert row["blocked"].startswith("Nobody to write to. Max Example: They asked not to be contacted")


def test_a_headline_that_names_no_one_does_not_block_an_older_moment(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    paper = Signal(kind="team_paper", day="2026-09-01", quote="Robot policies from video",
                   source_url="https://example.org/acme/paper", authors=[CLEO], gist="training robot policies on video alone")
    headline = Signal(kind="new_lead", day="2026-09-02", quote="Acme names new head of autonomy",
                      source_url="https://example.org/acme/news")
    row = accounts.assess(store, "2026-09-20T12:00:00+00:00", _acme(paper, headline), records)
    assert (row["write_to"]["name"], row["moment"]["quote"]) == ("Cleo Example", "Robot policies from video")
    assert "saw your robot policy paper." in row["draft"] and row["why"].startswith("A paper on GI's topics")
    assert row["until"] == "Oct 31"  # the paper's own deadline, not the headline's


def test_no_note_offers_the_gameplay_data_and_one_that_trains_its_own_says_so(simulation):
    rows = _rows()
    assert rows["loam-robotics"]["label"] == accounts.OWN and rows["birchwood-lab"]["label"] == ""
    assert all(r["draft"] for r in (rows["loam-robotics"], rows["birchwood-lab"]))
    for r in rows.values():  # nothing public says GI sells it, whatever the account would buy
        assert "offer" not in (r["draft"] or "") and "gameplay data" not in (r["draft"] or ""), r["id"]
    for kind in accounts.MOMENTS:
        s = Signal(kind=kind, day="2026-09-10", who="Max Example", quote="Our world model", source_url="https://example.org/s")
        for buys in ("agents", "data", "both", "partner"):
            text = accounts.draft(Account(id="a", name="Acme", buys=buys), s, {"name": "Max Example"}, "Omar Example")
            assert "at General Intuition" in text.replace("At General", "at General") and "Medal" in text, (kind, buys)
            assert "offer" not in text and "gameplay data" not in text, (kind, buys)


def test_the_cli_and_the_card_show_the_label_and_skip_an_empty_segment(simulation, capsys):
    import scripts.accounts as cli
    rows = _rows()
    cli._print_calls([rows["loam-robotics"]])
    assert "Loam Robotics (data, also builds its own models, fit 4)" in capsys.readouterr().out
    bare = dict(rows["loam-robotics"], segment="")
    line = accounts.weekly([bare], 1, "2026-09-15")["blocks"][3]["text"]["text"].split("\n")[0]
    assert line == "*1. Loam Robotics* · _also builds its own models_ · write by Nov 11"


def test_nothing_is_drafted_without_a_gi_person_to_write_it(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    row = accounts.assess(store, "2026-09-20T12:00:00+00:00", _acme(NEW_LEAD, owner=""), records)
    assert row["draft"] is None and row["blocked"] == "No one at GI owns this account yet: pick an owner to write."


def test_nobody_recruiting_watches_is_written_to_even_when_the_champion_is_held(simulation):
    row = _rows()["driftwire"]
    assert row["write_to"]["name"] == "Lee Example"  # the champion, not Jonah Pike, whom recruiting watches
    assert row["route_in"] is None  # and Jonah is no way in either: nobody asks a watched person for an intro
    store, as_of, _, _ = _account("driftwire")
    contact.record(store, "account:driftwire:lee-example", "never", at=as_of)
    row = _rows()["driftwire"]
    assert row["write_to"] is None and row["draft"] is None and f"Jonah Pike: {accounts.WATCHED}" in row["blocked"]


def test_a_contact_who_said_never_is_skipped_and_an_investor_is_the_way_in(simulation):
    row = _rows()["sample-sim"]
    kofi = next(c for c in row["contacts"] if c["name"] == "Kofi Adair")
    assert kofi["gate"]["state"] == "hold" and row["write_to"]["name"] == "Ray Example"
    assert row["route_in"] == ("Example Ventures (invented) backs both GI and Sample Sim Labs: ask them for an "
                               "introduction.")
    assert row["writer"] == "Ines Example"  # the owner, since no one at GI has a tie


def _person(name, talking=None):
    return {"name": name, "title": "", "part": "champion", "key": name, "source_url": "", "watched": None,
            "posted": None, "talking": talking, "gate": None}


def test_a_tie_is_the_way_in_only_to_the_person_it_is_with():
    acme = Account(id="acme", name="Acme", owner="Omar Example",
                   ties=[Tie(by="Nora Example", to="Pat Example", what="Nora worked with Pat.")])
    pat, sam = _person("Pat Example"), _person("Sam Example")
    assert accounts.way_in(acme, pat) == ("Nora worked with Pat. Nora writes.", "Nora Example")
    assert accounts.way_in(acme, sam) == (None, "Omar Example")
    acme.ties = [Tie(by="Noor Hale", what="Noor used to work at Acme.")]  # a tie to the account as a whole
    assert accounts.way_in(acme, sam) == ("Noor used to work at Acme. Noor writes.", "Noor Hale")


HIRING_WORDS = ("Accepted GI's offer", "joining", "In conversation", "Invited to", "Recruiting watches",
                "Member of Technical Staff", "recruiting", "Dana Kest")


def test_sales_never_sees_what_hiring_or_events_have_with_someone(simulation, capsys, monkeypatch):
    from scripts import accounts as cli

    store, as_of, _, _ = _account("driftwire")
    contact.record(store, "account:driftwire:lee-example", "hired", at=as_of, by="Dana Kest")  # before they resign
    contact.record(store, "account:sample-sim:ray-example", "replied", at=as_of, by="Dana Kest")
    contact.record(store, "account:loam-robotics:rue-example", "invited", at=as_of, team="events", by="Dana Kest",
                   until="2026-10-01", note="an evals night")
    real = contact.history  # and a hire the web app's ledger holds, read across (contact.elsewhere)
    hired = {"kind": "hired", "at": as_of, "team": "recruiting", "by": "Dana Kest", "until": None, "note": "",
             "role_id": None, "evidence": [], "score": None, "links": [], "ledger": "the web app"}
    monkeypatch.setattr(contact, "history", lambda store, pid: [*real(store, pid)] + (
        [{**hired, "person_id": pid}] if pid == "account:birchwood-lab:pat-example" else []))
    page = accounts.page("simulation")
    rows = {r["id"]: r for r in page["accounts"]}
    for k, name in (("driftwire", "Lee Example"), ("sample-sim", "Ray Example"), ("loam-robotics", "Rue Example"),
                    ("birchwood-lab", "Pat Example")):
        held = next(c for c in rows[k]["contacts"] if c["name"] == name)
        assert held["gate"] == {"state": "hold", "reason": accounts.HELD} and held["talking"] is None
        assert (rows[k]["write_to"] or {}).get("name") != name
    (store, as_of), found, records = accounts.workspace("simulation")
    cli._print_calls(page["accounts"])
    cli._print_release(accounts.release_day(store, as_of, found, records))
    seen = json.dumps(page, ensure_ascii=False) + capsys.readouterr().out
    assert not [w for w in HIRING_WORDS if w in seen]


def test_a_partner_hold_a_recruiter_noted_reads_with_no_name(simulation):
    (store, as_of), _, records = accounts.workspace("simulation")
    contact.record(store, "account:b:max-sample", "partner_staff", at=as_of, by="Dana Kest")  # recruiting, by default
    row = accounts.assess(store, as_of, _lab(Contact(name="Max Sample", part="champion")), records)
    reason = row["contacts"][0]["gate"]["reason"]
    assert reason.startswith("Works at a GI partner, noted") and "Dana" not in reason and "recruiting" not in reason
    assert "Dana" not in row["blocked"]


def test_a_ledger_key_never_carries_the_role_recruiting_watches_someone_for(simulation, capsys):
    from scripts import accounts as cli

    (store, as_of), _, records = accounts.workspace("simulation")
    journey.save_context(store, journey.PersonContext(subject_id="mts-research:ann-example", name="Ann Example",
                                                      role="mts-research", anchors={"openalex": "https://openalex.org/A88"}))
    author = Contact(name="A. Example", part="champion", source_url="https://openalex.org/A88")
    assert accounts.identify(store, author) == "ann-example"  # what find --update stores: no role
    for c in (author.model_copy(update={"subject_id": "ann-example"}),
              author.model_copy(update={"subject_id": "mts-research:ann-example"})):  # one stored before this change
        row = accounts.assess(store, as_of, _lab(c), records)
        assert row["contacts"][0]["key"] == "ann-example" and row["contacts"][0]["watched"] and row["draft"] is None
        cli._print_calls([row])
        assert "mts-research" not in json.dumps(row) + capsys.readouterr().out


def test_what_sales_itself_did_still_reads_as_it_is(simulation):
    store, as_of, _, _ = _account("sample-sim")
    contact.record(store, "account:sample-sim:ray-example", "replied", at=as_of, team="sales", by="Ines Example")
    ray = next(c for c in _rows()["sample-sim"]["contacts"] if c["name"] == "Ray Example")
    assert ray["gate"]["reason"].startswith("In conversation since") and ray["talking"]["by"] == "Ines Example"
    kofi = next(c for c in _rows()["sample-sim"]["contacts"] if c["name"] == "Kofi Adair")
    assert kofi["gate"]["reason"].startswith("They asked not to be contacted")  # their own wish: every team sees it


def test_a_ledger_that_cant_be_read_is_said_once(simulation, monkeypatch):
    real = contact.history
    broken = {"person_id": "x", "kind": "unreadable", "at": "2026-01-01T00:00:00+00:00", "team": "recruiting",
              "ledger": "the web app", "note": "DatabaseError"}
    monkeypatch.setattr(contact, "history", lambda store, pid: [*real(store, pid), {**broken, "person_id": pid}])
    monkeypatch.setattr(contact, "unreadable", lambda store: contact._broken("the web app", "DatabaseError"))
    page = accounts.page("simulation")
    assert page["ledger"].startswith("The web app's contact ledger can't be read")
    assert json.dumps(page).count("DatabaseError") == 1  # on the page once, not on every contact
    assert page["this_week"] == [] and all(c["gate"]["reason"] == accounts.UNREADABLE
                                           for r in page["accounts"] for c in r["contacts"])
    store = accounts.workspace("simulation")[0][0]
    with pytest.raises(ValueError, match="contact ledger can't be read"):
        accounts.send(store, page["accounts"], "https://hooks.example.org/x", now="2026-09-15T12:00:00+00:00")


def test_drafts_use_only_public_gi_facts_and_break_if_the_name_is_swapped(simulation):
    for row in _rows().values():
        if row["draft"]:
            assert accounts.GI_LINE in row["draft"] or "trained on gameplay video from Medal" in row["draft"]
            assert "$" not in row["draft"]
            assert row["write_to"]["name"].split()[0] in row["draft"]
            gists = {s["source_url"]: s.get("gist", "") for a in json.loads((accounts.RECORDS["simulation"] / "accounts.json").read_text())["accounts"]
                     for s in a["signals"]}  # a paper's note says what it does, never its title
            assert row["name"] in row["draft"] or any(s["quote"] in row["draft"] or gists.get(s["source_url"]) and
                                                      gists[s["source_url"]][1:] in row["draft"]
                                                      for s in row["signals"] if s["open"])


def test_a_note_says_gi_does_similar_work_only_when_it_is_on_gi_own_work():
    to, acme = {"name": "Max Example"}, Account(id="a", name="Acme")
    similar = ", trained on gameplay video from Medal"

    def note(kind, quote, authors=()):
        return accounts.draft(acme, Signal(kind=kind, day="2026-09-02", quote=quote, source_url="https://example.org/p",
                                           authors=[{"name": a} for a in authors]), to, "Oda Example")

    driving = note("team_paper", "Clear Skies Only: Rain-Aware Speed Caps for Delivery Robot VLAs", ["Max Example"])
    assert similar not in driving and "compare notes" not in driving and "team" not in driving
    assert driving.startswith(f"Hey Max, saw your paper. At General Intuition {accounts.GI_LINE}.")  # no line yet: held
    kestrel = note("team_paper", "Kestrel: Writing World Models in Code for Grid Puzzles")
    assert similar in kestrel and "rather than written as code" in kestrel and "compare notes" in kestrel
    manipulation = note("job_post", "Robot Learning Engineer - Manipulation")
    assert similar not in manipulation and f"At General Intuition {accounts.GI_LINE}." in manipulation
    assert similar in note("job_post", "Research Scientist, World Models")
    for quote in ("Action models from gameplay", "Latent actions from video games", "A Minecraft agent",
                  "Video World Modeling at Scale", "Blink: Diffusion Models Are Real-Time Game Engines",
                  "Large Action Models", "Learning World Modelling from Gameplays", "World Models as Game Engines"):
        assert similar in note("team_paper", quote), quote
    for quote in ("Clear Skies Only: Rain-Aware Speed Caps for Delivery Robot Vision-Language-Action Models",
                  "An Open-Source Vision-Language-Action Model", "Vision Language Action Models for Humanoids",
                  "Emergent World Models in Othello-GPT", "Video Game Addiction and Sleep in Adolescents",
                  "Inverse Dynamics Analysis of Knee Joint Loading During Running", "World models for language agents",
                  "Sim-to-Real Robot Grasping with a Game Engine Simulator",
                  "Synthetic Data from a Game Engine for Drone Navigation",
                  "Real-Time Game Engine Rendering for Drone Simulation"):
        text = note("team_paper", quote)
        assert similar not in text and "We're building" not in text, quote
    ask = note("public_ask", "Anyone have a good dataset of drone flight logs?")
    assert "spent a lot of time on this" not in ask and "what we've learned" not in ask
    assert "spent a lot of time on this" in note("public_ask", "Anyone have a big dataset of gameplay video?")
    assert "spent a lot of time on this" not in note("public_ask", "Anyone know how the licensing game plays out for small labs?")


FOG = "Clear Skies Only: Rain-Aware Speed Caps for Delivery Robot VLAs"
KESTREL = "Kestrel: Writing World Models in Code for Grid Puzzles"
RIO = {"name": "Rio Example", "url": "https://openalex.org/A21", "at": "Ashgrove (Norway)",
       "affiliation": "Example University of Technology Ashgrove Exampleburg",
       "listed_at": ["Example University of Technology", "Ashgrove"], "city": "Exampleburg", "own_words": True, "first": True}
FOG_GIST = "capping a delivery robot's speed when it rains"
ASHGROVE = "Ashgrove (Arms, Carts)"
JOSS = {"name": "Joss Example", "url": "https://openalex.org/A7", "at": "Ashgrove (Norway)",
        "affiliation": "Example University of Technology Ashgrove Example City, Norway"}


def _kestrel(authors=()):
    """An account whose top author by paper count is its champion, and whose newest paper is one he is not on."""
    hollis = Contact(name="Hollis Example", title="Author of 2 papers on GI's topics since 2025-09-24",
                       part="champion", source_url="https://openalex.org/A99")
    paper = Signal(kind="team_paper", day="2026-09-10", quote=KESTREL, source_url="https://arxiv.org/abs/2607.00000",
                   authors=list(authors), gist="predicting grid puzzle moves with small programs")
    return Account(id="ashgrove", name=ASHGROVE, owner="Oda Example", buys="data", contacts=[hollis], signals=[paper])


def test_a_paper_is_written_to_one_of_its_authors_there_never_to_the_account_top_author(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    as_of = "2026-09-20T12:00:00+00:00"
    held = accounts.assess(store, as_of, _kestrel(), records)  # stored before its authors were kept: nobody yet
    assert held["write_to"] is None and held["draft"] is None
    assert held["blocked"] == "Which of the paper's authors work there isn't on file: `scripts/accounts.py authors` reads it."
    row = accounts.assess(store, as_of, _kestrel([JOSS]), records)
    assert row["write_to"]["name"] == "Joss Example" and row["write_to"]["source_url"] == "https://openalex.org/A7"
    assert row["write_to"]["title"] == "Author of the paper, listed at Ashgrove"  # stored before the paper's own words
    assert row["draft"].startswith("Hey Joss, saw your Kestrel paper. Predicting grid puzzle moves with small "
                                   "programs really caught my eye. We're building world models")
    assert "Hollis" not in json.dumps(accounts.weekly([row], 1, "2026-09-20"))
    job = Signal(kind="job_post", day="2026-09-12", quote="Research Scientist, World Models", source_url="https://example.org/j")
    jobs_row = accounts.assess(store, as_of, _kestrel([JOSS]).model_copy(update={"signals": [job]}), records)
    assert jobs_row["write_to"]["name"] == "Hollis Example"  # a paper's author hears about their own paper only
    typed = _kestrel().model_copy(update={"signals": [Signal(kind="team_paper", day="2026-09-10", quote=KESTREL,
                                                           who="Joss Example", source_url="https://example.org/t")]})
    assert accounts.assess(store, as_of, typed, records)["blocked"] == "None of the paper's authors there is on file: Joss Example."


def test_a_paper_note_says_your_paper_to_its_author_and_the_card_names_the_company_alone(simulation):
    paper = Signal(kind="team_paper", day="2026-09-10", quote="Driving VLAs under fog", source_url="https://example.org/p",
                   authors=[{"name": "Cleo Example", "at": "Nimbus Labs (United States)"}], gist="keeping driving VLAs safe in fog")
    nimbus = Account(id="n", name="Nimbus Labs (Tern, Heron)")
    assert accounts.draft(nimbus, paper, {"name": "Cleo Example"}, "Oda Example").startswith(
        "Hey Cleo, saw your paper. Keeping driving VLAs safe in fog really caught my eye.")
    assert accounts.draft(nimbus, paper, {"name": "Sid Example"}, "Oda Example").startswith(
        "Hey Sid, saw the paper.")  # never sent: only an author gets a paper's note
    assert accounts.short("Nimbus Labs (Tern, Heron)") == "Nimbus Labs" and accounts.short(ASHGROVE) == "Ashgrove"
    assert accounts.short("Acme") == "Acme" and accounts.short("(Acme)") == "(Acme)"
    for kind, quote in (("funding", "Nimbus Labs raises"), ("job_post", "Research Scientist, World Models"),
                        ("new_lead", "Joins Nimbus Labs")):
        text = accounts.draft(nimbus, Signal(kind=kind, day="2026-09-10", who="Max Example", quote=quote,
                                             source_url="https://example.org/s"), {"name": "Max Example"}, "Oda Example")
        assert "Nimbus Labs" in text and "Tern" not in text, kind
    (store, _), _, records = accounts.workspace("simulation")
    row = accounts.assess(store, "2026-09-20T12:00:00+00:00", _acme(paper, name="Nimbus Labs (Tern, Heron)"), records)
    assert row["write_to"]["name"] == "Cleo Example" and row["draft"].startswith("Hey Cleo, saw your paper.")
    card = json.dumps(accounts.weekly([row], 1, "2026-09-20"))
    assert "*1. Nimbus Labs*" in card and "Tern" not in card and "their team" not in card


def test_find_keeps_each_paper_authors_as_the_paper_lists_them_there():
    acme = Account(id="acme", name="Acme Robotics (Acme)")
    at_acme = {"id": "https://openalex.org/I1", "display_name": "Acme Robotics (United States)", "type": "company"}
    work = {**WORK, "primary_location": {"source": {"display_name": "Example Conference (ECX)"}}, "authorships": [
        {"author_position": "first", "author": {"id": "https://openalex.org/A1", "display_name": "Cleo Example"},
         "institutions": [at_acme],
         "affiliations": [{"raw_affiliation_string": "Acme Robotics, Boston", "institution_ids": [at_acme["id"]]}]},
        {"author": {"id": "https://openalex.org/A3", "display_name": "Dana Example"},
         "institutions": [{"id": "https://openalex.org/I2", "display_name": "Example University", "type": "education"}]}]}
    fetch = _fetch({"api.openalex.org": {"meta": {"next_cursor": None}, "results": [work]}, "news.google.com": RSS})
    row = next(c for c in accounts.find(fetch, "2025-09-24", [acme], topics=("world model",)) if c["account"] == "acme")
    cleo = {"name": "Cleo Example", "url": "https://openalex.org/A1", "at": "Acme Robotics (United States)",
            "affiliation": "Acme Robotics, Boston", "listed_at": ["Acme Robotics"], "city": "Boston", "own_words": True,
            "first": True}
    assert row["papers"][0]["authors"] == [cleo]  # not the university's author on the same paper
    assert row["papers"][0]["venue"] == "Example Conference"
    assert accounts.authors_at(work, acme, [acme]) == [cleo]
    paper = next(s for s in accounts.signals_from(row, "2026-09-01") if s.kind == "team_paper")
    assert [a.model_dump() for a in paper.authors] == [cleo] and paper.venue == "Example Conference"
    assert not paper.checked and paper.first_public is None  # scripts/accounts.py authors looks for earlier versions


def test_authors_fills_in_each_paper_authors_venue_and_first_version_and_keeps_the_file_before(monkeypatch, tmp_path,
                                                                                              capsys):
    from scripts import accounts as cli

    recent, old = date.today().isoformat(), (date.today() - timedelta(days=61)).isoformat()
    kept = {"name": "Joss Example", "url": JOSS["url"], "at": "Ashgrove (Norway)", "listed_at": ["Ashgrove"], "own_words": True}
    papers = [{"kind": "team_paper", "day": recent, "quote": KESTREL, "source_url": "https://doi.org/10.1/kestrel"},
              {"kind": "team_paper", "day": recent, "quote": "Nobody from Ashgrove on it", "source_url": "https://openalex.org/W9"},
              {"kind": "team_paper", "day": recent, "quote": "Gone", "source_url": "https://doi.org/10.1/gone"},
              {"kind": "team_paper", "day": old, "quote": "Past its window", "source_url": "https://doi.org/10.1/old"},
              {"kind": "team_paper", "day": recent, "quote": "Typed in", "who": "the lab", "source_url": "https://doi.org/10.1/kestrel"},
              {"kind": "team_paper", "day": recent, "quote": "On arXiv", "source_url": "https://arxiv.org/abs/2607.00001"},
              {"kind": "team_paper", "day": recent, "quote": "Read before", "source_url": "https://doi.org/10.1/done",
               "authors": [kept], "checked": True},  # nothing left to read: no request
              {"kind": "team_paper", "day": recent, "quote": "Kept before", "source_url": "https://doi.org/10.1/kestrel",
               "authors": [{k: v for k, v in kept.items() if k not in ("listed_at", "own_words")}], "checked": True},
              {"kind": "team_paper", "day": recent, "quote": "Typed with its author", "source_url": "https://example.org/typed",
               "authors": [kept]},  # typed in by hand: nothing to read, nothing to say
              {"kind": "team_paper", "day": recent, "quote": "Moved", "source_url": "https://openalex.org/W4",
               "authors": [kept]}]  # OpenAlex no longer lists them there: the authors kept stay, and it is still checked
    (tmp_path / "accounts.json").write_text(json.dumps({"accounts": [{"id": "ashgrove", "name": ASHGROVE, "signals": papers}]}))
    ashgrove_inst = {"id": "https://openalex.org/I8", "display_name": "Ashgrove (Norway)", "type": "company"}
    uni = {"id": "https://openalex.org/I7", "display_name": "Example University of Technology", "type": "education"}
    kestrel = {"id": "https://openalex.org/W7", "title": KESTREL, "publication_date": recent,
               "abstract_inverted_index": {"We": [0], "learn": [1], "programs.": [2]},
               "primary_location": {"source": {"display_name": "Example Workshop Proceedings"}}, "authorships": [
                   {"author_position": "first", "author": {"id": JOSS["url"], "display_name": "Joss Example"},
                    "institutions": [uni, ashgrove_inst], "affiliations": [
                        {"raw_affiliation_string": JOSS["affiliation"], "institution_ids": [uni["id"], ashgrove_inst["id"]]}]},
                   {"author": {"id": "https://openalex.org/A8", "display_name": "Sade Example"},
                    "institutions": [{"id": "https://openalex.org/I9", "display_name": "Example Library", "type": "facility"}]}]}
    preprint = {"id": "https://openalex.org/W6", "title": KESTREL.upper().replace(":", " -"), "publication_date": "2026-06-11",
                "primary_location": {"source": {"display_name": "arXiv (Cornell University)"}},
                "authorships": [{"author": {"display_name": "Joss Example"}}]}
    namesake = {"id": "https://openalex.org/W5", "title": KESTREL, "publication_date": "2025-01-05",
                "authorships": [{"author": {"display_name": "Nobody Else"}}]}  # the same title, no author in common
    asked = []

    def fetch(url):
        asked.append(url)
        if "search=" in url:
            return json.dumps({"results": [kestrel, namesake, preprint]}).encode()
        if "gone" in url:
            raise httpx.HTTPStatusError("404", request=httpx.Request("GET", url), response=httpx.Response(404))
        if "works/W4" in url:
            return json.dumps({**kestrel, "id": "https://openalex.org/W4", "title": "Moved", "authorships": []}).encode()
        return json.dumps(kestrel if "kestrel" in url else {"id": "https://openalex.org/W9", "authorships": []}).encode()

    monkeypatch.setattr(cli, "LIVE", tmp_path)
    cli.authors(fetch)
    out = capsys.readouterr().out
    after = json.loads((tmp_path / "accounts.json").read_text())["accounts"][0]["signals"]
    joss = {**JOSS, "listed_at": ["Example University of Technology", "Ashgrove"], "city": "Example City", "own_words": True,
            "first": True}
    read = {"venue": "Example Workshop Proceedings", "abstract": "We learn programs."}
    found = {**read, "first_public": "2026-06-11", "first_at": "arXiv", "checked": True}
    assert after[0] == {**papers[0], "authors": [joss], **found} and after[4] == {**papers[4], "authors": [joss], **found}
    assert after[7] == {**papers[7], "authors": [joss], **read}  # checked before: no search
    assert after[1:4] == papers[1:4] and after[5:7] == papers[5:7] and after[8] == papers[8]
    assert after[9] == {**papers[9], **read, "checked": True}
    assert len(asked) == 9 and sum("search=" in u for u in asked) == 3 and not any("done" in u or "typed" in u for u in asked)
    assert any("works/https://doi.org/10.1/kestrel" in u for u in asked) and any("works/W9" in u for u in asked)
    assert f"Read the authors of 3 papers; the file before is {tmp_path}" in out and "Ashgrove: “Kestrel" in out
    assert ("Looked for an earlier version of 3 papers:\n  - Ashgrove: “Kestrel: Writing World Models in Code "
            "for Grid Puzzles”: on arXiv since June 2026") in out
    assert "so its note stays held:\n  - Ashgrove (Arms, Carts): “Nobody from Ashgrove on it”" in out
    assert "Couldn't read:\n  - Ashgrove (Arms, Carts): “Gone” (HTTPStatusError)" in out
    assert "  - Ashgrove (Arms, Carts): “On arXiv” (not a DOI or OpenAlex link)" in out
    assert "Typed with" not in out and "Moved" not in out
    assert accounts.first_version(kestrel, [kestrel, {**kestrel, "publication_date": "2020-02-01"}], [JOSS]) is None  # itself
    backups = list(tmp_path.glob("accounts.before-authors-*.json"))
    assert len(backups) == 1 and json.loads(backups[0].read_text())["accounts"][0]["signals"] == papers


def _paper_account(quote=FOG, **paper):
    s = Signal(kind="team_paper", quote=quote, source_url="https://doi.org/10.1/fog",
               **{"day": "2026-09-02", "authors": [RIO], "checked": True, "gist": FOG_GIST, **paper})
    return Account(id="ashgrove", name=ASHGROVE, segment="Robotics", owner="Oda Example", signals=[s])


def _card(row):
    return accounts.weekly([row], 1, "2026-09-20")["blocks"][3]["text"]["text"]


def test_a_paper_is_dated_by_its_month_and_a_later_version_says_when_it_first_went_up(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    as_of = "2026-09-20T12:00:00+00:00"
    row = accounts.assess(store, as_of, _paper_account(venue="Example Workshop Proceedings", first_public="2026-08-03",
                                                       first_at="arXiv"), records)
    assert "company, in Example Workshop Proceedings, on arXiv since August 2026: “Clear Skies Only" in _card(row)
    assert "(Ashgrove, in Example Workshop Proceedings, on arXiv since August 2026)" in row["why"]
    for text in (_card(row), row["why"], json.dumps(row["signals"])):
        assert "Sep 2" not in text and "Aug 3" not in text  # a record's day is often not the paper's: never a day
    alone = accounts.assess(store, as_of, _paper_account(venue="Example Workshop Proceedings"), records)
    assert "company, in Example Workshop Proceedings, September 2026: “Clear" in _card(alone)
    assert "company, on arXiv, September 2026: “Clear" in _card(accounts.assess(store, as_of, _paper_account(venue="arXiv"), records))
    assert "company, September 2026: “Clear" in _card(accounts.assess(store, as_of, _paper_account(), records))
    same_month = _paper_account(venue="Example Workshop Proceedings", first_public="2026-09-01", first_at="arXiv")
    assert "company, in Example Workshop Proceedings, September 2026:" in _card(accounts.assess(store, as_of, same_month, records))
    job = Signal(kind="job_post", day="2026-09-12", quote="Research Scientist, World Models", source_url="https://example.org/j")
    posted = accounts.assess(store, as_of, _acme(job), records)
    assert "*Why now:* A job post for work GI does, Sep 12: “Research" in _card(posted)  # only a paper's day is left out


def test_a_paper_not_yet_checked_for_an_earlier_version_waits_for_the_check(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    row = accounts.assess(store, "2026-09-20T12:00:00+00:00", _paper_account(checked=False), records)
    assert row["write_to"]["name"] == "Rio Example" and row["draft"] is None
    assert row["blocked"] == ("Whether the paper went up earlier isn't checked yet: `scripts/accounts.py authors` "
                              "reads it.")
    typed = _paper_account(checked=False).model_copy()
    typed.signals[0].source_url = "https://example.org/typed-in"  # typed in by hand: nothing to check it against
    assert accounts.assess(store, "2026-09-20T12:00:00+00:00", typed, records)["draft"]


def test_the_window_counts_from_the_first_public_version(simulation):
    # The one piece to take back if GI would rather count from the proceedings: _public.
    (store, _), _, records = accounts.workspace("simulation")
    as_of = "2026-09-20T12:00:00+00:00"
    old = _paper_account(venue="Example Workshop Proceedings", first_public="2026-06-11", first_at="arXiv")
    row = accounts.assess(store, as_of, old, records)
    assert row["action"] == "quiet" and row["draft"] is None
    assert row["why"] == ("Nothing new: the last moment, a paper on GI's topics by someone at the company (in Example "
                          "Workshop Proceedings, on arXiv since June 2026), is past its window.")
    early = accounts.assess(store, "2026-07-01T12:00:00+00:00", old, records)  # before the proceedings, after arXiv
    assert early["action"] == "reach_now" and early["until"] == "Aug 10"
    fresh = accounts.assess(store, as_of, _paper_account(venue="Example Workshop Proceedings"), records)
    assert fresh["action"] == "reach_now" and fresh["until"] == "Nov 1"


def test_the_card_gives_the_paper_own_affiliation_and_no_group_the_paper_does_not(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    row = accounts.assess(store, "2026-09-20T12:00:00+00:00", _paper_account(), records)
    card = _card(row)
    assert card.split("\n")[0] == "*1. Ashgrove* · write by Nov 1"  # not the account's "Robotics", nor its teams
    assert "Robotics" not in card and "Arms" not in card and "Ashgrove (Norway)" not in card
    assert "*To:* <https://openalex.org/A21|Rio Example>, First author of the paper, listed at Example University of " \
           "Technology and Ashgrove, Exampleburg · *From:* Oda Example" in card
    stored = {k: v for k, v in RIO.items() if k not in ("listed_at", "first", "city", "own_words")}  # kept before
    older = accounts.assess(store, "2026-09-20T12:00:00+00:00", _paper_account(authors=[stored]), records)
    assert older["write_to"]["title"] == "Author of the paper, listed at Ashgrove"
    assert older["draft"] is None and older["blocked"] == ("Where the paper itself lists its authors isn't read yet: "
                                                           "`scripts/accounts.py authors` reads it.")  # OpenAlex's match
    unread = {**RIO, "listed_at": [], "city": "", "affiliation": "Acme Robotics Mountain View"}  # the line reads two ways
    odd = accounts.assess(store, "2026-09-20T12:00:00+00:00", _paper_account(authors=[unread]), records)
    assert odd["write_to"]["title"] == "First author of the paper, which lists them as “Acme Robotics Mountain View”"
    job = Signal(kind="job_post", day="2026-09-12", quote="Research Scientist, World Models", source_url="https://example.org/j")
    posted = accounts.assess(store, "2026-09-20T12:00:00+00:00", _acme(job, segment="Robotics"), records)
    assert _card(posted).startswith("*1. Acme* · Robotics · ")  # the account's own note keeps its segment
    assert accounts._listed(accounts.Author(name="Sol Example", listed_at=["Acme", "Example Institute", "Example University"],
                                            own_words=True), "Acme") == "listed at Acme, Example Institute and Example University"


def _line(raw, *names, first=True):
    """_author on one affiliation line the paper gives, with the institutions OpenAlex matched it to (the first the
    account's company)."""
    insts = [{"id": f"https://openalex.org/I{n}", "display_name": name, "type": "company" if n == 0 else "education"}
             for n, name in enumerate(names)]
    au = {"author_position": "first" if first else "middle",
          "author": {"id": "https://openalex.org/A1", "display_name": "Rio Example"}, "institutions": insts,
          "affiliations": [{"raw_affiliation_string": raw, "institution_ids": [i["id"] for i in insts]}] if raw else []}
    got = accounts._author(au, insts[0])
    return got["listed_at"], got["city"], got["affiliation"]


def test_the_card_names_only_places_the_paper_own_line_gives():
    # OpenAlex matched a line that runs two places together to two places it never names, and missed one it does.
    assert _line("Example University of Technology Ashgrove Exampleburg", "Ashgrove (Norway)",
                 "Example Materials Center", "Example College of Applied "
                 "Sciences") == (["Example University of Technology", "Ashgrove"], "Exampleburg",
                                 "Example University of Technology Ashgrove Exampleburg")
    assert _line("NVIDIA, Santa Clara, CA, USA", "Nvidia (United States)")[:2] == (["NVIDIA"], "Santa Clara")
    assert _line("Google Research, Mountain View, CA 94043", "Google (United States)")[:2] == (["Google Research"],
                                                                                              "Mountain View")
    assert _line("Dept. of Computer Science, Example University, Exampleton, CA 94305, USA; Acme Robotics",
                 "Acme Robotics (United States)", "Example University")[:2] == (["Example University", "Acme Robotics"],
                                                                                 "Exampleton")
    for raw, names, want in (
            ("Computer Science Department, Stanford University, Stanford, CA", ("Stanford University",),
             (["Stanford University"], "Stanford")),
            ("Electrical Engineering, Tsinghua University, Beijing 100084, P.R. China", ("Tsinghua University",),
             (["Tsinghua University"], "Beijing")),
            ("Meta AI (FAIR), New York", ("Meta (United States)",), (["Meta AI"], "New York")),
            ("Samsung Research America, Mountain View, CA", ("Samsung (South Korea)",),
             (["Samsung Research America"], "Mountain View")),
            ("Covariant.ai, Emeryville, California", ("Covariant (United States)",), (["Covariant"], "Emeryville")),
            ("Example University and Acme Robotics, Exampleton", ("Acme Robotics (United States)",),
             (["Example University", "Acme Robotics"], "Exampleton")),
            ("Ashgrove; Chinese Academy of Sciences, Beijing, China", ("Ashgrove (United States)",),
             (["Ashgrove", "Chinese Academy of Sciences"], "Beijing")),  # a subject's word inside an institution's name
            ("NVIDIA; Weizmann Institute of Science, Rehovot, Israel", ("Nvidia (United States)",),
             (["NVIDIA", "Weizmann Institute of Science"], "Rehovot"))):
        assert _line(raw, *names)[:2] == want, raw
    # a part of the line nothing explains: the paper's own words, never OpenAlex's guess
    assert _line("Acme Robotics Mountain View", "Acme Robotics (United States)") == ([], "", "Acme Robotics Mountain View")
    assert _line("", "Acme Robotics (United States)")[:2] == ([], "")  # no line at all: OpenAlex's company, below


def test_an_author_counts_for_the_account_only_when_their_own_line_names_it():
    ashgrove = Account(id="ashgrove", name=ASHGROVE)
    tu = {"id": "https://openalex.org/I1", "display_name": "Example University of Technology", "type": "education"}
    inst = {"id": "https://openalex.org/I2", "display_name": "Ashgrove (Norway)", "type": "company"}

    def work(*lines):
        return {"id": "https://openalex.org/W1", "title": WORK["title"], "publication_date": "2026-09-01",
                "doi": "https://doi.org/10.1/x", "authorships": [
                    {"author_position": "first", "author": {"id": "https://openalex.org/A1", "display_name": "Nia Example"},
                     "institutions": [tu, inst],
                     "affiliations": [{"raw_affiliation_string": line, "institution_ids": [tu["id"], inst["id"]]}
                                      for line in lines]}]}

    # OpenAlex put her at Ashgrove on a line that names only the university: not the account's author
    assert accounts.authors_at(work("Example University of Technology, Exampleburg, Norway"), ashgrove, [ashgrove]) == []
    assert [a["name"] for a in accounts.authors_at(work("Example University of Technology Ashgrove Exampleburg"),
                                                   ashgrove, [ashgrove])] == ["Nia Example"]
    assert [a["name"] for a in accounts.authors_at(work(), ashgrove, [ashgrove])] == ["Nia Example"]  # no line: OpenAlex's
    acme = Account(id="acme", name="Acme")
    inc = {"id": "https://openalex.org/I3", "display_name": "Acme Technologies (United States)", "type": "company"}
    plain = {**work(), "authorships": [{**work()["authorships"][0], "institutions": [inc], "affiliations": [
        {"raw_affiliation_string": "Acme, Exampleton, UK", "institution_ids": [inc["id"]]}]}]}
    assert [a["name"] for a in accounts.authors_at(plain, acme, [acme])] == ["Nia Example"]  # the line drops "Technologies"
    for company, line, named in (("Sky Labs (United States)", "Sky Models Group, Example University, Exampleton", False),
                                 ("Acme Technologies (United States)", "Acme Motors Research, Exampleton, MI", False),
                                 ("Acme AI (United States)", "Large-Acme Learning Lab, Example University", False),
                                 ("Sky Labs (United States)", "Sky Labs, Exampleton, CA", True),
                                 ("Acme Technologies (United States)", "Acme Robotics Lab, Exampleton", True)):
        # a short name inside another name is not the company
        assert accounts._names({"affiliations": [{"raw_affiliation_string": line}]}, {"display_name": company}) is named, line
    fetch = _fetch({"api.openalex.org": {"meta": {"next_cursor": None},
                                         "results": [work("Example University of Technology, Exampleburg, Norway")]},
                    "news.google.com": RSS})
    row = next(c for c in accounts.find(fetch, "2025-09-24", [ashgrove], topics=("world model",)) if c["account"] == "ashgrove")
    assert row["authors"] == [] and row["papers"][0]["authors"] == []


def test_a_paper_note_opens_with_the_paper_short_name_and_where_it_went_up_never_its_title():
    to, acme = {"name": "Rio Example"}, Account(id="a", name="Acme")

    def note(quote, to=to, **paper):
        s = Signal(kind="team_paper", source_url="https://example.org/p", quote=quote, authors=[{"name": "Rio Example"}],
                   **{"day": "2026-09-02", "gist": "doing something new with game agents", **paper})
        return accounts.draft(acme, s, to, "Oda Example", today=date(2026, 9, 20))

    for quote, paper, opener in (
            (KESTREL, {"venue": "arXiv"}, "saw your Kestrel paper on arXiv."),
            (KESTREL, {"venue": "Example Workshop Proceedings", "first_public": "2026-06-11", "first_at": "arXiv"},
             "saw your Kestrel paper on arXiv."),  # where it first went up
            (KESTREL, {"venue": "Example Workshop Proceedings"}, "saw your Kestrel paper."),  # a preprint server only
            (KESTREL, {"venue": "Lecture notes in example science"}, "saw your Kestrel paper."),
            (KESTREL, {"venue": "Example Workshop Proceedings", "day": "2026-10-02"}, "saw your Kestrel paper."),  # not out
            (KESTREL, {}, "saw your Kestrel paper."),
            ("Wren 2: Humanoid Policies from Video", {"venue": "arXiv"}, "saw your Wren 2 paper on arXiv."),
            # no short name: what the paper is about, from its title's subject, when that is a kind of thing GI knows
            ("Training a Captioned Kart Racing Agent on Narrated Lap Logs", {"venue": "arXiv"},
             "saw your captioned kart racing agent paper on arXiv."),
            ("Learning Latent Action Kart Models from Video", {}, "saw your latent action kart model paper."),
            ("Training a Captioned Kart Racing Agent on Narrated Lap Logs",  # a sentence's first word is no name
             {"abstract": "Kart racing agents are usually trained with rewards."}, "saw your captioned kart racing agent paper."),
            ("Pretraining Kart Policies on Web Video", {"gist": "keeping kart arms steady",
                                                        "abstract": "Abstract Kart policies are brittle."}, "saw your kart policy paper."),
            ("Simple Baselines Beat Complex Kart Models", {}, "saw your complex kart model paper."),  # never the verb
            ("Kart Models Help Robots", {}, "saw your paper."),
            ("Scaling Very Deep Latent Action Kart Models", {}, "saw your latent action kart model paper."),  # four words at most
            ("Learning game agents from player clips", {}, "saw your game agent paper."),
            ("Pretraining robot policies on a million hours of video", {}, "saw your robot policy paper."),
            ("A Minecraft agent", {}, "saw your Minecraft agent paper."),
            ("Robot policies from video", {}, "saw your robot policy paper."),  # a sentence's capital, not a name's
            ("Towards Generalist Game Agents", {}, "saw your generalist game agent paper."),
            # else just the paper: never a label ("Position:"), a long lead-in or a subject that isn't a thing
            ("Position: World Models Need Games", {"venue": "arXiv"}, "saw your paper on arXiv."),
            (FOG, {}, "saw your paper."), ("Scaling Laws for Game Agents", {}, "saw your paper."),
            ("On the Robustness of World Models", {}, "saw your paper."), ("Driving VLAs under fog", {}, "saw your paper."),
            ("Position Paper: World Models Need Games", {"venue": "arXiv"}, "saw your paper on arXiv."),
            ("Case Study: Kart Racing Agents from Clips", {}, "saw your kart racing agent paper."),
            ("Direct Kart Optimization: Your Racing Model is Secretly a Planner", {}, "saw your racing model paper."),
            ("One Model To Drive Them All", {}, "saw your paper."), ("Evaluating Agents", {}, "saw your paper."),
            ("Reasoning Models Are Test-Time Learners", {}, "saw your paper."),  # a lone "model" says nothing
            ("Building Open-Ended Embodied Agents", {}, "saw your open-ended embodied agent paper."),
            ("Vision-Language-Action Models for Kart Racing", {}, "saw your vision-language-action model paper."),
            ("Position: Robot policies from video", {}, "saw your robot policy paper."),
            ("Learning Minecraft Agents from Gameplay Video", {"abstract": "We train Minecraft agents from video."},
             "saw your Minecraft agent paper."),  # a name, as its own words write it
            ("Scaling Atari Agents with Video", {"gist": "training Atari agents on video alone"}, "saw your Atari agent paper."),
            ("Kestrel : Writing World Models in Code", {}, "saw your Kestrel paper.")):
        text = note(quote, **paper)
        assert text.startswith(f"Hey Rio, {opener} ") and "really caught my eye." in text, quote
        assert quote not in text and "“" not in text and "  " not in text, quote  # the title is never pasted in
    assert note(KESTREL).startswith("Hey Rio, saw your Kestrel paper. Doing something new with game agents really caught")
    assert note(KESTREL, to={"name": "Sid Example"}, venue="arXiv").startswith("Hey Sid, saw the Kestrel paper on arXiv.")


def test_a_paper_note_says_what_the_paper_does_and_the_gi_fact_closest_to_it():
    to, acme = {"name": "Rio Example"}, Account(id="a", name="Acme")

    def note(quote, kind="team_paper", gist=""):
        s = Signal(kind=kind, day="2026-09-02", who="Rio Example", quote=quote, source_url="https://example.org/p",
                   authors=[{"name": "Rio Example"}], gist=gist)
        return accounts.draft(acme, s, to, "Oda Example")

    assert note(FOG, gist=FOG_GIST) == ("Hey Rio, saw your paper. Capping a delivery robot's speed when it "
                                        f"rains really caught my eye. At General Intuition {accounts.GI_LINE}. "
                                        "Would love to hear more about it if you're up for a chat.\n\nOda")
    gist = "predicting grid puzzle moves with small programs"
    assert note(KESTREL, gist=gist) == ("Hey Rio, saw your Kestrel paper. Predicting grid puzzle moves with small "
                                        "programs really caught my eye. We're building world models at General "
                                        "Intuition too, trained on gameplay video from Medal rather than written as code. "
                                        "Would love to compare notes if you're interested.\n\nOda")
    for quote in (FOG, KESTREL, "Latent Actions from Video Games", "Robot policies from video"):
        text = note(quote)  # no line on what it does: nothing says it caught an eye (and assess holds it)
        assert "caught my eye" not in text and "close to" not in text, quote
        assert ("We're building" in text) == ("compare notes" in text), quote  # the similar line only on GI's own work
    assert "We're building large action models at General Intuition too" in note("Latent Actions from Video Games")
    for quote, line in (("Steering a Game Agent with Notes Its Players Wrote", "We're building large action models at General "
                         "Intuition, trained"), ("A Minecraft agent", "We're building large action models at General "
                         "Intuition, trained"), ("Blink: Diffusion Models Are Real-Time Game Engines", "We're building "
                         "world models at General Intuition, trained"), (KESTREL, "We're building world models at "
                         "General Intuition too"), ("Large Action Models", "We're building large action models at "
                         "General Intuition too")):  # "too" only when the title names world or action models
        assert line in note(quote), quote
    for quote in ("Learning from Human Videos for Dexterous Manipulation", "Video Pretraining for Robot Arms",
                  "Atari 100k Benchmarks Revisited", "Research Engineer, Video Games", "Research Scientist, Minecraft"):
        for kind in ("team_paper", "job_post"):  # names neither kind of model: what GI builds, never "too"
            text = note(quote, kind=kind)
            assert "We're building world models and large action models at General Intuition, trained on gameplay " \
                   "video from Medal." in text and " too" not in text, (quote, kind)
    job = note("Research Scientist, World Models", kind="job_post")
    assert "“Research Scientist, World Models”. We're building world models at General Intuition too, trained on " \
           "gameplay video from Medal. Would love" in job and "close to" not in job
    ask = note("Anyone have a big dataset of gameplay video?", kind="public_ask")
    assert "We're building world models and large action models at General Intuition, trained on gameplay video from " \
           "Medal, so we've spent a lot of time on this. Happy to share" in ask


def test_what_a_paper_does_is_its_own_words_never_the_title_again():
    title = KESTREL
    assert accounts.gist_problem("predicting grid puzzle moves with small programs", title) is None
    assert "title" in accounts.gist_problem("writing world models in code", title)
    assert "title" in accounts.gist_problem("The World Models in Code for Grid Puzzles idea", title)
    assert accounts.gist_problem("programs", title) and accounts.gist_problem(" ".join(["word"] * 30), title)
    assert accounts.gist_problem("", title)


def test_a_paper_with_no_line_on_what_it_does_waits_and_holds_back_nothing_else(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    as_of = "2026-09-20T12:00:00+00:00"
    row = accounts.assess(store, as_of, _paper_account(gist=""), records)
    assert row["write_to"]["name"] == "Rio Example" and row["draft"] is None
    assert row["blocked"] == ("Say what the paper does before anyone writes: `scripts/accounts.py gist` lists it with "
                              "its abstract.")
    copied = accounts.assess(store, as_of, _paper_account(gist="rain-aware speed caps for delivery"), records)
    assert copied["draft"] is None and copied["blocked"].startswith("The line on what the paper does can't go out: It "
                                                                    "repeats the title")
    job = Signal(kind="job_post", day="2026-09-12", quote="Research Scientist, World Models", source_url="https://example.org/j")
    paper = Signal(kind="team_paper", day="2026-09-14", quote=FOG, source_url="https://example.org/fog", authors=[CLEO])
    both = accounts.assess(store, as_of, _acme(job, paper), records)
    assert both["moment"]["kind"] == "job_post" and both["draft"] and not both["blocked"]


def test_the_gist_command_lists_papers_with_their_abstract_and_sets_a_line_it_checks(monkeypatch, tmp_path, capsys):
    from scripts import accounts as cli

    recent = date.today().isoformat()
    paper = {"kind": "team_paper", "day": recent, "quote": KESTREL, "source_url": "https://doi.org/10.1/kestrel",
             "abstract": "We write world models as short programs and use them to predict grid puzzle moves.",
             "authors": [{"name": "Joss Example", "listed_at": ["Ashgrove"], "own_words": True}], "checked": True}
    done = {**paper, "quote": "Done", "source_url": "https://doi.org/10.1/done", "gist": "something already said"}
    typed = {"kind": "team_paper", "day": recent, "quote": "Typed in", "who": "Joss Example", "source_url": "https://example.org/t"}
    copied = {**paper, "quote": "Grid Puzzles Solved Fast", "source_url": "https://doi.org/10.1/copy",
              "gist": "grid puzzles solved fast today"}
    (tmp_path / "accounts.json").write_text(json.dumps({"accounts": [{"id": "ashgrove", "name": ASHGROVE,
                                                                      "signals": [paper, done, typed, copied]}]}))
    monkeypatch.setattr(cli, "LIVE", tmp_path)
    cli.gist()
    out = capsys.readouterr().out
    assert f"Ashgrove: “{KESTREL}” https://doi.org/10.1/kestrel" in out and "short programs" in out and "Done" not in out
    assert "“Typed in” https://example.org/t" in out and "“Grid Puzzles Solved Fast”" in out and "It repeats the title" in out
    with pytest.raises(SystemExit, match="title"):
        cli.gist("https://doi.org/10.1/kestrel", "writing world models in code")
    assert "gist" not in json.loads((tmp_path / "accounts.json").read_text())["accounts"][0]["signals"][0]
    cli.gist("https://doi.org/10.1/kestrel", "predicting grid puzzle moves with short programs.")
    after = json.loads((tmp_path / "accounts.json").read_text())["accounts"][0]["signals"]
    assert after[0]["gist"] == "predicting grid puzzle moves with short programs" and after[1] == done
    assert len(list(tmp_path.glob("accounts.before-gist-*.json"))) == 1


def test_a_line_on_what_a_paper_is_about_opens_its_note_when_a_person_set_one(monkeypatch, tmp_path):
    from scripts import accounts as cli

    title = "Training a Steered Kart Racing Agent on Narrated Lap Logs"
    recent = date.today().isoformat()
    paper = {"kind": "team_paper", "day": recent, "quote": title, "source_url": "https://doi.org/10.1/kart",
             "venue": "arXiv", "gist": "training a kart agent on laps its players narrated", "authors": [{"name": "Rio Example"}]}
    (tmp_path / "accounts.json").write_text(json.dumps({"accounts": [{"id": "ashgrove", "name": ASHGROVE, "signals": [paper]}]}))
    monkeypatch.setattr(cli, "LIVE", tmp_path)
    for bad, why in (("kart", "2 to 6 words"), ("steered kart racing agent", "what the paper is about in other words"),
                     ("about steered kart agents", "about"), ("steered kart agents on arXiv", "where it went up")):
        with pytest.raises(SystemExit, match=why):
            cli.about("https://doi.org/10.1/kart", bad)
    cli.about("https://doi.org/10.1/kart", "steered kart agents.")
    stored = json.loads((tmp_path / "accounts.json").read_text())["accounts"][0]["signals"][0]
    assert stored == {**paper, "about": "steered kart agents"}
    assert len(list(tmp_path.glob("accounts.before-about-*.json"))) == 1
    acme, rio = Account(id="a", name="Acme"), {"name": "Rio Example"}
    note = accounts.draft(acme, Signal.model_validate(stored), rio, "Oda Example")
    plain = accounts.draft(acme, Signal.model_validate(paper), rio, "Oda Example")
    assert plain.startswith("Hey Rio, saw your steered kart racing agent paper on arXiv. Training a kart agent")
    assert note == plain.replace("saw your steered kart racing agent paper", "saw your paper about steered kart "
                                 "agents")  # only the opener moves
    assert accounts.draft(Account(id="a", name="Acme"), Signal.model_validate({**stored, "quote": KESTREL}),
                          {"name": "Sid Example"}, "Oda Example").startswith(
        "Hey Sid, saw the paper about steered kart agents on arXiv.")  # a person's line wins over a short name
    assert accounts.draft(acme, Signal.model_validate({**stored, "about": "steered kart agents,"}), rio,
                          "Oda Example") == note  # a stray comma never lands before "on arXiv"


def test_a_line_on_what_a_paper_is_about_typed_into_the_file_is_checked_before_it_goes_out(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    row = accounts.assess(store, "2026-09-20T12:00:00+00:00", _paper_account(about=FOG), records)  # the title again
    assert row["draft"] is None and row["blocked"] == ("The line on what the paper is about can't go out: Say it in 2 to 6 "
                                                       "words. (`scripts/accounts.py about` sets another).")


def test_authors_drops_an_author_openalex_placed_there_whose_own_line_never_names_the_company(monkeypatch, tmp_path,
                                                                                               capsys, simulation):
    from scripts import accounts as cli

    recent = date.today().isoformat()
    matched = {"name": "Nia Example", "url": "https://openalex.org/A5", "at": "Ashgrove (Norway)", "listed_at": ["Ashgrove"]}
    typed = {"name": "Lou Example"}  # typed in by hand: stays
    paper = {"kind": "team_paper", "day": recent, "quote": KESTREL, "source_url": "https://doi.org/10.1/kestrel",
             "checked": True, "gist": "solving grid puzzles with small programs", "authors": [matched]}
    both = {**paper, "quote": "With one typed in", "source_url": "https://doi.org/10.1/both", "authors": [matched, typed]}
    (tmp_path / "accounts.json").write_text(json.dumps({"accounts": [{"id": "ashgrove", "name": ASHGROVE, "owner": "Oda Example",
                                                                      "signals": [paper, both]}]}))
    uni = {"id": "https://openalex.org/I7", "display_name": "Example University of Technology", "type": "education"}
    inst = {"id": "https://openalex.org/I8", "display_name": "Ashgrove (Norway)", "type": "company"}
    work = {"id": "https://openalex.org/W7", "title": KESTREL, "publication_date": recent, "authorships": [
        {"author_position": "first", "author": {"id": matched["url"], "display_name": "Nia Example"}, "institutions": [uni, inst],
         "affiliations": [{"raw_affiliation_string": "Example University of Technology, Exampleburg, Norway",
                           "institution_ids": [uni["id"], inst["id"]]}]}]}
    monkeypatch.setattr(cli, "LIVE", tmp_path)
    cli.authors(lambda url: json.dumps(work).encode())
    after = json.loads((tmp_path / "accounts.json").read_text())["accounts"][0]
    assert after["signals"][0]["authors"] == [] and after["signals"][1]["authors"] == [typed]
    assert f"so its note stays held:\n  - {ASHGROVE}: “{KESTREL}”" in capsys.readouterr().out
    (store, _), _, records = accounts.workspace("simulation")
    row = accounts.assess(store, f"{recent}T12:00:00+00:00", Account.model_validate({**after, "signals": after["signals"][:1]}),
                          records)
    assert row["draft"] is None and (row["write_to"] or {}).get("name") != "Nia Example"


def test_an_earlier_version_is_a_preprint_by_one_of_the_authors_there_dated_to_the_day():
    work = {"id": "https://openalex.org/W1", "title": "Latent Action Models for Warehouse Robots",
            "publication_date": "2026-09-10", "authorships": [
                {"author": {"id": "https://openalex.org/A1", "display_name": "Max Example"}},
                {"author": {"id": "https://openalex.org/A2", "display_name": "Wen Example"}}]}
    there = [{"name": "Max Example", "url": "https://openalex.org/A1"}]  # the paper's author at the account

    def copy(day, where, *authors, title=work["title"]):
        return {"id": f"https://openalex.org/W{day}", "title": title, "publication_date": day,
                "primary_location": {"source": {"display_name": where}},
                "authorships": [{"author": {"id": i, "display_name": n}} for n, i in authors]}

    arxiv = copy("2026-07-14", "arXiv (Cornell University)", ("Max Example", "https://openalex.org/A1"),
                 title="Latent action models for warehouse robots.")
    year_only = copy("2026-01-01", "arXiv (Cornell University)", ("Max Example", ""))  # a year, not a day
    repository = copy("2026-03-02", "Example University Repository", ("Max Example", "https://openalex.org/A1"))
    theirs_alone = copy("2026-05-05", "arXiv (Cornell University)", ("Wen Example", "https://openalex.org/A2"))
    namesake = copy("2019-03-01", "arXiv (Cornell University)", ("Max Example", "https://openalex.org/A99"))
    found = [work, year_only, repository, theirs_alone, namesake, arxiv]
    assert accounts.first_version(work, found, there) == (date(2026, 7, 14), "arXiv")
    assert accounts.first_version(work, [work, year_only, repository, theirs_alone, namesake], there) is None
    named = [{"name": "Max Example"}]  # no OpenAlex page kept: by name
    assert accounts.first_version(work, [copy("2026-06-01", "arXiv", ("Max Example", "https://openalex.org/A5"))],
                                  named) == (date(2026, 6, 1), "arXiv")


def test_a_later_version_not_yet_out_is_never_named_on_the_card(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    ahead = _paper_account(day="2026-10-05", venue="Example Workshop Proceedings", first_public="2026-09-01", first_at="arXiv")
    row = accounts.assess(store, "2026-09-20T12:00:00+00:00", ahead, records)
    assert "company, on arXiv since September 2026: “Clear Skies" in _card(row) and "Proceedings" not in _card(row)
    assert "Proceedings" not in row["why"] and "Proceedings" not in json.dumps(row["signals"])
    out = accounts.assess(store, "2026-10-06T12:00:00+00:00", ahead, records)
    assert "company, in Example Workshop Proceedings, on arXiv since September 2026:" in _card(out)
    for r in (row, out):  # the note says where it first went up
        assert r["draft"].startswith("Hey Rio, saw your paper on arXiv.") and "Proceedings" not in r["draft"]


def test_a_paper_waiting_for_its_check_does_not_hold_back_an_older_moment(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    job = Signal(kind="job_post", day="2026-09-12", quote="Research Scientist, World Models", source_url="https://example.org/j")
    paper = Signal(kind="team_paper", day="2026-09-14", quote=FOG, source_url="https://doi.org/10.1/fog", authors=[CLEO])
    row = accounts.assess(store, "2026-09-20T12:00:00+00:00", _acme(job, paper), records)
    assert row["moment"]["kind"] == "job_post" and row["draft"] and not row["blocked"]
    alone = accounts.assess(store, "2026-09-20T12:00:00+00:00", _acme(paper), records)
    assert alone["moment"]["kind"] == "team_paper" and alone["blocked"].startswith("Whether the paper went up earlier")


def test_authors_keeps_its_title_searches_inside_openalex_credits(monkeypatch, tmp_path, capsys):
    from scripts import accounts as cli
    from app.sources import openalex

    recent = date.today().isoformat()
    papers = [{"kind": "team_paper", "day": recent, "quote": f"Paper {n}", "source_url": f"https://doi.org/10.1/p{n}",
               "authors": [{"name": "Joss Example", "url": JOSS["url"], "listed_at": ["Ashgrove"]}]} for n in (1, 2)]
    (tmp_path / "accounts.json").write_text(json.dumps({"accounts": [{"id": "ashgrove", "name": ASHGROVE, "signals": papers}]}))
    ashgrove_inst = {"id": "https://openalex.org/I8", "display_name": "Ashgrove (Norway)", "type": "company"}
    asked = []

    def fetch(url):
        asked.append(url)
        if "search=" in url:
            return json.dumps({"results": []}).encode()
        return json.dumps({"id": "https://openalex.org/W1", "title": "Paper", "authorships": [
            {"author": {"id": JOSS["url"], "display_name": "Joss Example"}, "institutions": [ashgrove_inst]}]}).encode()

    monkeypatch.setattr(cli, "LIVE", tmp_path)
    cli.authors(fetch, budget=openalex.Budget(most=openalex.CREDITS_A_PAGE))  # room for one search
    after = json.loads((tmp_path / "accounts.json").read_text())["accounts"][0]["signals"]
    assert [s.get("checked", False) for s in after] == [True, False] and sum("search=" in u for u in asked) == 1
    out = capsys.readouterr().out
    assert "Looked for an earlier version of 1 paper: none went up earlier." in out
    assert "Stopped looking for earlier versions: this run's 10 credits are spent. The rest wait for the next run." in out


def test_a_funding_draft_never_repeats_the_headline():
    raised = Signal(kind="funding", day="2026-09-10", quote="Acme raises $50M at a $1B valuation",
                    source_url="https://example.org/f")
    text = accounts.draft(Account(id="a", name="Acme"), raised, {"name": "Max Example"}, "Omar Example")
    assert text.startswith("Hey Max, saw the news about Acme.") and "$" not in text and "raises" not in text
    assert "congrats" not in text  # the news can be an acquisition
    long = Signal(kind="public_ask", day="2026-09-10", who="Max Example", quote="word " * 400, source_url="https://example.org/p")
    assert len(accounts.draft(Account(id="a", name="Acme"), long, {"name": "Max Example"}, "Omar Example")) < 600


def test_an_account_in_talks_holds_its_people_for_recruiting_and_leaves_the_list(simulation):
    store, as_of, birchwood, _ = _account("birchwood-lab")
    assert contact.check(contact.history(store, "D904"), as_of).state == "clear"
    held = accounts.in_talks(store, birchwood, by="Nora Example", at=as_of)
    assert held == ["account:birchwood-lab:pat-example", "D904"]
    gate = contact.check(contact.history(store, "D904"), as_of)
    assert gate.state == "hold" and "a founder decides" in gate.reason and "Nora Example (sales)" in gate.reason
    row = _rows()["birchwood-lab"]
    assert row["write_to"] is None and row["draft"] is None
    assert (accounts.page("simulation")["weekly"]["text"]
            == "Accounts to write to this week: Driftwire, Loam Robotics, Sample Sim Labs")


def test_an_account_in_talks_holds_the_authors_its_papers_list_too(simulation):
    store, as_of, birchwood, records = _account("birchwood-lab")
    nia = Signal(kind="team_paper", day="2026-09-10", quote="World models for warehouse robots",
                 source_url="https://example.org/birchwood/paper", authors=[{"name": "Nia Example", "url": "https://openalex.org/A55"}],
                 gist="warehouse robots that plan inside a learned simulator")
    before = birchwood.model_copy(update={"signals": [*birchwood.signals, nia]})
    assert accounts.assess(store, as_of, before, records)["write_to"]["name"] == "Nia Example"
    accounts.in_talks(store, birchwood, by="Nora Example", at=as_of)  # marked before the paper was known
    row = accounts.assess(store, as_of, before, records)
    assert row["write_to"] is None and row["draft"] is None and "a founder decides" in row["blocked"]
    assert accounts.this_week(store, [row], as_of) == []
    later = accounts.in_talks(store, before, by="Nora Example", at=as_of)  # marked once the paper is known
    assert "account:birchwood-lab:nia-example" in later
    omi = nia.model_copy(update={"quote": "Latent actions for arms", "source_url": "https://example.org/quay/2",
                                 "authors": [accounts.Author(name="Omi Example")]})
    quay = Account(id="quay", name="Quay Robotics", owner="Nora Example", signals=[nia])  # no one on file but its authors
    assert accounts.in_talks(store, quay, by="Nora Example", at=as_of) == ["account:quay:nia-example"]
    row = accounts.assess(store, as_of, quay.model_copy(update={"signals": [nia, omi]}), records)
    assert row["write_to"] is None and "a founder decides" in row["blocked"] and accounts.this_week(store, [row], as_of) == []


class Poster:
    def __init__(self):
        self.posted = []

    def post(self, url, json):
        self.posted.append(json)
        return httpx.Response(200, text="ok")


def test_send_posts_one_list_records_each_account_and_keeps_to_the_weekly_cap(simulation):
    (store, as_of), _, _ = accounts.workspace("simulation")
    rows = accounts.page("simulation")["accounts"]
    poster = Poster()
    sent = accounts.send(store, rows, "https://hooks.example.org/x", now=as_of, client=poster)
    assert sent == ["driftwire", "birchwood-lab", "loam-robotics"] and len(poster.posted) == 1
    assert "3 of 7 accounts" in json.dumps(poster.posted[0])
    pinged = [e for e in store.all("contact") if e["kind"] == "pinged"]
    assert {(e["team"], e["role_id"]) for e in pinged} == {("sales", "gtm")}
    assert {(e["person_id"], e["by"]) for e in pinged} == {
        ("account:driftwire:lee-example", "Luis Example"), ("account:birchwood-lab:pat-example", "Nora Example"),
        ("account:loam-robotics:rue-example", "Omar Example")}
    with pytest.raises(ValueError, match="cap is used up"):
        accounts.send(store, rows, "https://hooks.example.org/x", now=as_of, client=poster)


def test_the_list_stops_at_the_weekly_cap_and_counts_what_already_went_out(simulation):
    (store, as_of), _, records = accounts.workspace("simulation")
    labs = [Account(id=f"lab{i}", name=f"Lab {i}", owner="Nora Example", contacts=[Contact(name=f"{n} Example", part="champion")],
                    signals=[Signal(kind="job_post", day="2026-09-08", quote="RL engineer", source_url=f"https://example.org/j{i}")])
            for i, n in enumerate(("Ash", "Bo", "Cy", "Di"))]
    rows = [accounts.assess(store, as_of, a, records) for a in labs]
    assert all(r["draft"] for r in rows) and accounts.CAP == 3
    assert [r["id"] for r in accounts.this_week(store, rows, as_of)] == ["lab0", "lab1", "lab2"]  # the 4th waits
    contact.record(store, "account:other:someone", "pinged", at=as_of, role_id="gtm", team="sales", evidence=[])
    assert [r["id"] for r in accounts.this_week(store, rows, as_of)] == ["lab0", "lab1"]


def test_send_rereads_the_ledger_so_a_hold_since_the_page_drops_that_account(simulation):
    store, as_of, driftwire, _ = _account("driftwire")
    rows = accounts.page("simulation")["accounts"]
    accounts.in_talks(store, driftwire, by="Luis Example", at=as_of)
    sent = accounts.send(store, rows, "https://hooks.example.org/x", now=as_of, client=Poster())
    assert sent == ["birchwood-lab", "loam-robotics", "sample-sim"]


def test_a_moment_goes_on_the_list_once_and_an_account_gets_one_note_a_month(simulation):
    (store, as_of), found, records = accounts.workspace("simulation")
    rows = accounts.page("simulation")["accounts"]
    accounts.send(store, rows, "https://hooks.example.org/x", now=as_of, client=Poster())
    later = "2026-09-23T12:00:00+00:00"  # the same rows, a week on: only the one the cap held back goes out
    assert accounts.send(store, rows, "https://hooks.example.org/x", now=later, client=Poster()) == ["sample-sim"]
    with pytest.raises(ValueError):  # and then every moment in them already went out
        accounts.send(store, rows, "https://hooks.example.org/x", now=later, client=Poster())
    driftwire = next(a for a in found if a.id == "driftwire")
    row = accounts.assess(store, later, driftwire, records)
    assert (row["action"], row["draft"]) == ("quiet", None)
    assert row["why"] == "On the list on Sep 15 for a paper on GI's topics by someone at the company: nothing new since."
    driftwire.signals.append(Signal(kind="job_post", day="2026-09-22", quote="Research Engineer, Game Agents",
                                    source_url="https://example.org/driftwire/jobs/agents"))
    row = accounts.assess(store, later, driftwire, records)  # a new moment, but Driftwire was on the list 8 days ago
    assert (row["action"], row["draft"]) == ("reach_now", None)
    assert row["blocked"].startswith("Driftwire was on the list on Sep 15: one note a month per account")
    contact.record(store, "account:driftwire:lee-example", "replied", at="2026-09-20T10:00:00+00:00", team="sales",
                   by="Luis Example")
    assert accounts.assess(store, later, driftwire, records)["draft"] is None  # a conversation doesn't lift it
    month = "2026-10-16T12:00:00+00:00"
    row = accounts.assess(store, month, driftwire, records)
    assert row["write_to"]["name"] == "Lee Example" and "Research Engineer, Game Agents" in row["draft"]
    assert accounts.this_week(store, [row], month) == [row]


def test_a_note_marks_only_its_own_moments_so_a_new_lead_for_someone_else_still_comes(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    paper = Signal(kind="team_paper", day="2026-09-01", quote="Robot policies from video",
                   source_url="https://example.org/acme/paper", authors=[CLEO], gist="training robot policies on video alone")
    lead = Signal(kind="new_lead", day="2026-09-02", quote="Acme names new head of autonomy",
                  source_url="https://example.org/acme/news")
    acme = _acme(paper, lead)
    at = "2026-09-20T12:00:00+00:00"
    assert accounts.send(store, [accounts.assess(store, at, acme, records)], "https://hooks.example.org/x", now=at,
                         client=Poster()) == ["acme"]
    assert [e["evidence"] for e in contact.history(store, "account:acme:cleo-example")] == [[paper.source_url]]
    lead.who = "Max Example"  # the headline's person, found later: news about him, so the month's rule gives way
    row = accounts.assess(store, "2026-09-22T12:00:00+00:00", acme, records)
    assert (row["write_to"]["name"], row["moment"]["kind"]) == ("Max Example", "new_lead") and row["draft"]
    assert accounts.this_week(store, [row], "2026-09-22T12:00:00+00:00") == [row]
    accounts.send(store, [row], "https://hooks.example.org/x", now="2026-09-22T12:00:00+00:00", client=Poster())
    more = Signal(kind="job_post", day="2026-09-23", quote="RL engineer", source_url="https://example.org/acme/job")
    acme.signals.append(more)  # the next day: a job post for Cleo waits out the month
    assert accounts.assess(store, "2026-09-24T12:00:00+00:00", acme, records)["draft"] is None
    release = {r["account"]: r for r in accounts.release_day(store, "2026-09-24T12:00:00+00:00", [acme], records)}
    assert release["Acme"]["to"] is None and "one note a month" in release["Acme"]["blocked"]


def test_in_a_quiet_month_news_about_a_person_still_reaches_them(simulation):
    (store, _), _, records = accounts.workspace("simulation")
    paper = Signal(kind="team_paper", day="2026-09-01", quote="Robot policies from video",
                   source_url="https://example.org/acme/paper", authors=[CLEO], gist="training robot policies on video alone")
    at = "2026-09-20T12:00:00+00:00"
    acme = _acme(paper)
    accounts.send(store, [accounts.assess(store, at, acme, records)], "https://hooks.example.org/x", now=at, client=Poster())
    acme.signals += [NEW_LEAD, Signal(kind="funding", day="2026-09-21", quote="Acme raises its Series C",
                                      source_url="https://example.org/acme/funding")]
    row = accounts.assess(store, "2026-09-22T12:00:00+00:00", acme, records)  # the newer funding news waits
    assert (row["write_to"]["name"], row["moment"]["kind"]) == ("Max Example", "new_lead") and row["draft"]


def test_release_day_names_who_hears_first_and_whose_team_gi_cites(simulation):
    (store, as_of), found, records = accounts.workspace("simulation")
    rows = {r["account"]: r for r in accounts.release_day(store, as_of, found, records, cited=["Lee Example"])}
    assert "Mock World Models Lab" not in rows
    assert rows["Driftwire"]["to"] == "Lee Example" and rows["Driftwire"]["cited"] == ["Lee Example"]
    assert rows["Example Robotics Co"]["to"] == "Sid Example"  # not Tove Lind, whom recruiting watches
    assert rows["Sample Sim Labs"]["to"] == "Ray Example"  # not Kofi Adair, who said never
    assert (rows["Loam Robotics"]["to"], rows["Loam Robotics"]["blocked"]) == ("Rue Example", None)


def _fetch(pages):
    def fetch(url):
        for part, body in pages.items():
            if part in url:
                return body.encode() if isinstance(body, str) else json.dumps(body).encode()
        raise httpx.HTTPStatusError("404", request=httpx.Request("GET", url), response=httpx.Response(404))
    return fetch


WORK = {"id": "https://openalex.org/W1", "title": "Action models from gameplay", "publication_date": "2026-09-01",
        "doi": "https://doi.org/10.1/x", "authorships": [
            {"author": {"id": "https://openalex.org/A1", "display_name": "Cleo Example"},
             "institutions": [{"id": "https://openalex.org/I1", "display_name": "Acme Robotics (United States)",
                               "type": "company", "country_code": "US"},
                              {"id": "https://openalex.org/I2", "display_name": "Example University", "type": "education"}]},
            {"institutions": [{"id": "https://openalex.org/I3", "display_name": "Google (United States)", "type": "company"},
                              {"id": "https://openalex.org/I4", "display_name": "General Intuition", "type": "company"}]}]}
GOOGLE = {"institutions": [{"id": "https://openalex.org/I3", "display_name": "Google (United States)", "type": "company"}]}
PLANET = {"author": {"id": "https://openalex.org/A9", "display_name": "Pat Example"},
          "institutions": [{"id": "https://openalex.org/I9", "display_name": "Planet Example Co", "type": "company"}]}
WORKS = [WORK, {"id": "https://openalex.org/W2", "title": "Sim-to-real transfer for legged robots", "doi": "https://doi.org/10.1/y",
                "publication_date": "2026-08-01", "authorships": [GOOGLE]},
         {"id": "https://openalex.org/W3", "title": "Conditional tokenization world models", "doi": "https://doi.org/10.1/z",
          "publication_date": "2026-08-02", "authorships": WORK["authorships"]},  # "world model" alone: not GI's work
         {"id": "https://openalex.org/W4", "title": "Embodied agents for orbital imaging", "doi": "https://doi.org/10.1/w",
          "publication_date": "2026-08-03", "authorships": [PLANET]}]  # one paper, and no account: noise
RSS = """<rss><channel>
<item><title>Acme Robotics raises $50M Series B - Example Wire</title><link>https://example.org/news/1</link>
<pubDate>Mon, 14 Sep 2026 08:00:00 GMT</pubDate></item>
<item><title>Acme Robotics names new head of autonomy - Example Wire</title><link>https://example.org/news/2</link>
<pubDate>Tue, 15 Sep 2026 08:00:00 GMT</pubDate></item>
<item><title>Acme Robotics opens a new office - Example Wire</title><link>https://example.org/news/3</link>
<pubDate>Tue, 15 Sep 2026 08:00:00 GMT</pubDate></item>
<item><title>Other Co raises money - Example Wire</title><link>https://example.org/news/4</link>
<pubDate>Tue, 15 Sep 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_find_merges_company_authors_into_accounts_and_reads_their_boards_and_news():
    acme = Account(id="acme", name="Acme Robotics (Acme)", boards={"greenhouse": "acme", "lever": "gone", "ashby": "odd"})
    deepmind = Account(id="gdm", name="Google DeepMind", buys="watch")
    fetch = _fetch({
        "api.openalex.org": {"meta": {"next_cursor": None}, "results": WORKS},
        "boards-api.greenhouse.io/v1/boards/acme": {"jobs": [
            {"title": "Research Scientist, World Models", "absolute_url": "https://example.org/j/1",
             "first_published": "2026-09-05T00:00:00Z", "location": {"name": "Boston"}, "content": "&lt;p&gt;Hi&lt;/p&gt;"},
            {"title": "World Model Engineer", "absolute_url": "https://example.org/j/3", "updated_at": "2026-09-06",
             "content": "No first published day."},
            {"title": "Office Manager", "absolute_url": "https://example.org/j/2", "first_published": "2026-09-05",
             "content": "Keeps the office running."}]},
        "news.google.com": RSS,
        "job-board/odd": [1],  # not the shape remembered: noted, and the run goes on
    })
    found = accounts.find(fetch, "2025-09-24", [acme, deepmind], topics=("world model",))
    assert [c["name"] for c in found] == ["Acme Robotics (Acme)", "Google (United States)", "Google DeepMind"]
    acme_row, google = found[0], found[1]
    assert acme_row["account"] == "acme" and acme_row["openalex"] == ["I1"]  # the paper joined the account
    assert [p["title"] for p in acme_row["papers"]] == ["Action models from gameplay"]  # not the tokenization one
    assert acme_row["authors"] == [{"name": "Cleo Example", "url": "https://openalex.org/A1", "papers": 1}]
    assert google["authors"] == [] and len(google["papers"]) == 2
    assert google["account"] is None and google["big"]  # "Google" is not Google DeepMind; big labs come last
    assert not any("General Intuition" in c["name"] for c in found)
    assert [j["title"] for j in acme_row["jobs"]] == ["Research Scientist, World Models", "World Model Engineer"]
    assert acme_row["errors"] == ["lever/gone: HTTPStatusError", "ashby/odd: AttributeError"]
    assert [(h["kind"], h["day"]) for h in acme_row["news"]] == [("funding", "2026-09-14"), ("new_lead", "2026-09-15")]
    assert found[2]["news"] == []  # a watch account's headlines are not read
    signals = accounts.signals_from(acme_row, "2026-09-01")  # newest first; the undated post is left out
    assert [(s.kind, s.day.isoformat()) for s in signals] == [
        ("new_lead", "2026-09-15"), ("funding", "2026-09-14"), ("job_post", "2026-09-05"), ("team_paper", "2026-09-01")]


def test_each_job_board_shape_reads_to_the_same_fields():
    fetch = _fetch({
        "api.lever.co": [{"text": "Robot Learning Engineer", "hostedUrl": "https://example.org/l/1", "createdAt": 1757894400000,
                          "categories": {"location": "Remote"}, "descriptionPlain": "Imitation learning.",
                          "lists": [{"content": "<li>VLA</li>"}]}],
        "api.ashbyhq.com": {"jobs": [{"title": "ML Engineer", "jobUrl": "https://example.org/a/1", "publishedAt": "2026-09-02T10:00:00Z",
                                      "location": "NYC", "descriptionPlain": "Sim-to-real.", "isListed": True},
                                     {"title": "Hidden", "jobUrl": "https://example.org/a/2", "isListed": False}]},
    })
    lever = jobs.postings(fetch, "lever", "x")[0]
    assert (lever["title"], lever["posted"], lever["text"]) == ("Robot Learning Engineer", "2025-09-15", "Imitation learning. VLA")
    assert [(p["title"], p["posted"]) for p in jobs.postings(fetch, "ashby", "x")] == [("ML Engineer", "2026-09-02")]


@pytest.mark.parametrize("headline, company, want", [
    ("Acme Robotics raises $50M Series B", "acme robotics", "funding"),
    ("Acme Robotics, the robot arm maker, reportedly raises $80 million", "acme robotics", "funding"),
    ("Acme Robotics to buy Birch Labs", "acme robotics", "funding"),
    ("Birch Labs acquired by Acme Robotics", "birch labs", "funding"),
    ("Birch Labs to be acquired by Acme Robotics", "birch labs", "funding"),
    ("Acme Robotics names new head of autonomy", "acme robotics", "new_lead"),
    ("Max Example joins Acme Robotics as chief scientist", "acme robotics", "new_lead"),
    # The Mac run's false alarms, in their shapes: the account is not the subject, or it is not a round.
    ("Analog Example to buy Birch to expand into physical intelligence", "physical intelligence", None),
    ("Agency buying robot dogs from Acme Robotics", "acme robotics", None),
    ("Metris Example raises $10M to Scale AI-Powered inspection", "scale ai", None),
    ("Acme Token: How to Buy and Short It on an exchange", "acme", None),
    ("Acme Robotics 2026 Funding Rounds & List of Investors", "acme robotics", None),
    ("Acme Robotics stock price jumps", "acme robotics", None),
    ("Acme Robotics raises full-year revenue forecast", "acme robotics", None),
    ("Acme Games raises console price by $50", "acme games", None),
    ("Acme Robotics lands $198M Air Force contract", "acme robotics", None),
    ("Acme Robotics valued at $31B", "acme robotics", None),
    ("Acme Games to acquire Birch Studios in all-stock deal", "acme games", "funding"),
    ("Acme Games acquires majority stake in Birch Studios", "acme games", "funding"),
    ("Acme raises $100M Series C as revenue triples", "acme", "funding"),
    ("Acme has agreed to acquire Birch", "acme", "funding"),
    ("Acme Announces $50M Series B Financing", "acme", "funding"),
    ("UPDATE 1-Acme raises $30M", "acme", "funding"),
    ("Acme AI raises $500M", "acme", "funding"),  # the account is "Acme"
    ("Max Example to join Acme as head of robotics", "acme", "new_lead"),
    ("Acme buys 2,000 robotaxis", "acme", None),
    ("Acme lands $50M deal with a retailer", "acme", None),
    ("Acme secures $30M order", "acme", None),
    ("Acme hires a bank to lead its IPO", "acme", None),
    ("Acme completes 1 million driverless miles", "acme", None),
    ("Acme gets new CEO as rival Birch raises $50 million", "acme", None),
    ("Acme buys back shares", "acme", None),
    ("Acme to buy robots from Birch", "acme", None),
    ("Acme buys 500 robotaxis", "acme", None),
    ("Acme agrees to buy 10 drones", "acme", None),
    ("Placeholder Autonomy raises $20M Series A", "Placeholder Autonomy Inc", "funding"),  # however it is cased
    ("Acme completes acquisition of Birch", "acme", "funding"),
    ("Acme Robotics announces definitive agreement to acquire Birch Labs", "acme robotics", "funding"),
    ("Placeholder Autonomy raises $20M Series A", "placeholder autonomy inc", "funding"),
    ("Example Robotics Co. raises $30 million", "example robotics co", "funding"),
    ("Acme raises nearly $500 million", "acme", "funding"),
    ("Acme closes its Series B", "acme", "funding"),
    # The Mac's second run: a prize pool and a chip deal are no rounds; talks and being bought count; a deal
    # someone else walked away from doesn't.
    ("Acme Games announces Project Blender 2026 with $100,000 prize pool", "acme games", None),
    ("Acme secures $3.5B Nvidia GPU deal", "acme", None),
    ("Acme launches $100M fund for robotics startups", "acme", None),
    ("Acme in talks to raise at $20B valuation", "acme", "funding"),
    ("Acme Said to Be in Talks to Raise Funds at $20 Billion Valuation", "acme", "funding"),
    ("Birch Chips to buy Acme for about $13 billion", "acme", "funding"),
    ("Birch Chips nears deal to buy AI startup Acme", "acme", "funding"),
    ("Birch walks away from $6 billion Acme deal", "acme", None),
    ("Birch walks away from deal to buy Acme", "acme", None),  # a deal that fell through, however it is said
    ("Birch no longer in talks to buy Acme", "acme", None),
    ("Regulators block Birch bid to buy Acme", "acme", None),
    ("Acme raises $100M led by Founders Fund", "acme", "funding"),  # the noun after the sum decides, not the headline
    ("Acme raises $600M in deal valuing it at $5B", "acme", "funding"),
    ("Acme raises $500M to buy compute", "acme", "funding"),
    ("Award-winning Acme raises $30M Series A", "acme", "funding"),
    ("Acme receives $2 million grant", "acme", None),
    ("Walmart buys Acme robots", "acme", None),  # its products, its shares or a part of it: not the company
    ("Should you buy Acme stock?", "acme", None),
    ("Birch acquires Acme's gaming division", "acme", None),
    ("Birch completes acquisition of Acme", "acme", "funding"),
    ("Acme buying Birch", "acme", "funding"),
    ("Acme in talks to acquire 10,000 GPUs", "acme", None),
    ("Acme in talks with investors to raise at $2B valuation", "acme", "funding"),
    ("Acme raises $20M to build the building blocks of embodied AI", "acme", "funding"),  # "block" alone is no deal
    ("Acme raises $30M as robots no longer need code", "acme", "funding"),
    ("Regulators block Birch's acquisition of Acme", "acme", None),
    ("Acme Robotics opens a new office", "acme robotics", None),
    ("Former Acme Robotics lead joins Birch", "acme robotics", None),
    # The Mac's third run: a deal that fell through, however it is worded, and a plant bought are no moments.
    ("Birch cancels $6B acquisition of Israeli AI start-up Acme following due diligence", "acme", None),
    ("Birch Pulls Out of ~$6B Acme Buyout After Diligence", "acme", None),
    ("Birch decides against $6b Acme acquisition", "acme", None),
    ("Birch drops plan to buy Acme", "acme", None),
    ("Birch's deal to buy Acme falls through", "acme", None),
    ("Birch backs out of deal to acquire Acme", "acme", None),
    ("Birch halts talks to acquire Acme", "acme", None),
    ("Birch shelves plan to buy startup Acme", "acme", None),
    ("Birch's Acme deal is off", "acme", None),
    ("FTC blocks $6B deal to buy Acme", "acme", None),
    ("Acme in talks to buy Nissan's Oppama plant for drones", "acme", None),
    ("Acme Industries in Talks to Acquire Nissan's Oppama Plant for Drone Production", "acme", None),
    ("Acme to buy Nissan's Oppama plant - report", "acme", None),  # however the outlet ends it
    ("Acme to buy Nissan's Oppama plant | Reuters", "acme", None),
    ("Acme to buy Nissan's Oppama plant under $1B deal", "acme", None),
    ("Acme in talks to buy Nissan's shuttered Oppama car plant", "acme", None),
    ("Birch buys Acme warehouse", "acme", None),
    ("Acme acquires plant-inspection startup Birch", "acme", "funding"),
    ("Acme buys Birch to automate factory floors", "acme", "funding"),
    ("Birch's talks to buy Acme broke down", "acme", None),
    ("Birch's bid to buy Acme fails", "acme", None),
    ("Birch loses bid to buy Acme", "acme", None),
    ("Birch denies plans to buy Acme", "acme", None),
    ("Birch not in talks to buy Acme", "acme", None),
    ("Birch will not buy Acme", "acme", None),
    ("EU prohibits Birch's takeover of Acme", "acme", None),
    ("Birch pulls the plug on Acme deal", "acme", None),
    ("Birch's deal to buy Acme lapses", "acme", None),
    ("Birch buys Acme Oppama plant", "acme", None),
    ("Birch acquires Acme Robotics and its Ohio plant", "acme", "funding"),
    ("Birch acquires Acme to automate factories", "acme", "funding"),  # what the deal is for, not what is bought
    ("Acme to acquire Birch for its Ohio plant", "acme", "funding"),
    ("Acme in talks to acquire Birch to scale factories", "acme", "funding"),
    ("Birch completes acquisition of Acme, ending months of talks", "acme", "funding"),
    ("Acme raises $50M after Birch deal falls through", "acme", "funding"),  # a failed deal voids only the deal
    ("Acme raises $200M after rejecting Birch buyout", "acme", "funding"),
    ("Acme names Jane Doe head of research after Birch deal falls through", "acme", "new_lead"),
    ("Acme raises $20M to end cold sales calls", "acme", "funding"),
    ("Acme Industries in talks to acquire drone maker Birch", "acme", "funding"),  # "Industries" is a suffix too
    ("Acme to buy Nissan factory", "acme", None),
    ("Birch buys Acme plant in Ohio", "acme", None),
    ("Acme acquires office software maker Birch", "acme", "funding"),  # a company, whatever it makes
    ("Acme in talks to raise more funds", "acme", "funding"),
    ("Acme raises $100M to end the wait for real-time video models", "acme", "funding"),
])
def test_a_headline_counts_only_when_the_account_is_its_subject_and_it_is_a_round_a_sale_or_a_leader(
        headline, company, want):
    assert news.kind(headline, company) == want


@pytest.mark.parametrize("title, want", [
    ("Action models from gameplay", True),
    ("World models for robot manipulation", True),
    ("Sim-to-real transfer for legged robots", True),
    ("A vision-language-action model for kitchens", True),
    ("Learning from human videos at scale", True),
    ("Conditional tokenization world models", False),  # world models of text
    ("Story world models for creative story generation", False),
    ("Story world models for interactive fiction", False),
    ("Language models as world models for information extraction", False),
    ("Displaying world models in LLMs", False),
    ("Large reaction models for retrosynthesis", False),
    ("Harmonic drive gear wear estimation", False),
    ("Mastering diverse domains through world models", True),
    ("A large-scale foundation world model", True),
    ("Diffusion for world modeling: visual details matter in Atari", True),
    ("Diffusion models are real-time game engines", True),
    ("Learning agile locomotion on legged robots", True),
    ("Tokenized world models for driving", True),  # a strong word beside the world model
    ("Vision-language models as world models for driving", True),
    ("Video tokenizer for world models", True),
    ("Agentic workflows for enterprise customer support", False),  # one common word is not enough
    ("Quality control in manufacturing lines", False),
    ("Game-theoretic pricing for ride hailing", False),
    ("World models for text-based games", False),
])
def test_a_paper_counts_only_when_its_title_is_on_gi_work(title, want):
    assert accounts.on_topic(title) is want


@pytest.mark.parametrize("title, text, want", [
    ("Hardware Engineer, Mechatronics", "Our robots run on foundation models.", False),  # the Mac run's false trigger
    ("Simulation Engineer, Game Engine", "Reinforcement learning at scale.", True),
    ("Humanoid Robotics Engineer", "Robot learning from teleoperation.", True),
    ("Research Scientist, World Models", "", True),
    ("Account Executive", "Sell our world model platform.", False),
    ("Robot Operator (Data Collection)", "Help train our robot learning models.", False),
    ("Vehicle Operator, Autonomy", "Drive our reinforcement learning fleet.", False),
    ("Public Policy Manager", "Shape policy for embodied AI.", False),
    ("Member of Technical Staff", "Build world models.", True),
    ("Software Engineer, Motion Planning", "Imitation learning for manipulation.", True),
    ("Senior ML Engineer, Hardware Acceleration", "Serve our foundation models.", True),
    ("Research Scientist, Neural Fields", "World models from video.", True),
    ("Field Robotics Engineer", "Sim-to-real for legged robots.", True),
    ("Research Scientist, Operator Learning", "Reinforcement learning for control.", True),
    ("Demand Planning Analyst", "Forecast demand for our embodied AI products.", False),
    ("Senior Accountant, Robotics Division", "Our foundation model business.", False),
])
def test_a_job_post_is_gi_work_only_when_its_title_names_that_work(title, text, want):
    assert accounts.gi_work(title, text) is want


def test_a_model_or_a_team_listed_as_an_author_is_never_a_champion():
    candidate = {"authors": [{"name": "Gemini 3.1 (Flash)", "url": "https://openalex.org/A9", "papers": 9},
                             {"name": "Example Robotics Team", "url": "https://openalex.org/A8", "papers": 5},
                             {"name": "Cleo Example", "url": "https://openalex.org/A1", "papers": 2}]}
    assert accounts.champion_from(candidate, Account(id="a", name="Acme"), "2025-09-24").name == "Cleo Example"
    for name in ("李明", "Siobhán O'Brien", "Claude Shannon", "Jean-Claude Latombe", "J. K. Example"):
        assert accounts.a_person(name), name
    for name in ("GPT-5", "Claude Team", "Claude 3 Opus", "OpenAI", "Google DeepMind", "PaLM-E", "Genie", "World Labs"):
        assert not accounts.a_person(name), name


def test_news_keeps_only_headlines_that_name_the_company_and_say_funding_or_a_new_leader():
    rows = news.headlines(_fetch({"news.google.com": RSS}), "Acme Robotics")
    assert [r["quote"] for r in rows] == ["Acme Robotics raises $50M Series B", "Acme Robotics names new head of autonomy"]
    assert news.headlines(_fetch({"news.google.com": RSS}), "Acme Robot") == []  # whole words only
    undated = RSS.replace("<pubDate>Mon, 14 Sep 2026 08:00:00 GMT</pubDate>", "")
    assert len(news.headlines(_fetch({"news.google.com": undated}), "Acme Robotics")) == 1  # no date, no window


def test_a_deal_that_fell_through_voids_every_headline_about_it_in_the_feed_and_in_accounts_json():
    feed = """<rss><channel>
<item><title>Birch in talks to buy AI startup Acme for $6 billion - Example Wire</title><link>https://example.org/n/1</link>
<pubDate>Mon, 07 Sep 2026 08:00:00 GMT</pubDate></item>
<item><title>Birch walks away from $6B Acme deal - Example Wire</title><link>https://example.org/n/2</link>
<pubDate>Tue, 08 Sep 2026 08:00:00 GMT</pubDate></item>
<item><title>Acme raises $100M Series B - Example Wire</title><link>https://example.org/n/3</link>
<pubDate>Wed, 09 Sep 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""
    rows, off = news.read(_fetch({"news.google.com": feed}), "Acme")
    assert [r["quote"] for r in rows] == ["Acme raises $100M Series B"]  # the talks went with the deal; the round stays
    assert off == "Birch walks away from $6B Acme deal"
    assert news.read(_fetch({"news.google.com": feed.replace("walks away from", "signs")}), "Acme")[1] is None
    for other in ("Acme won't train its models on player data", "Army cancels Acme drone contract", "Acme pulls out of CES",
                  "Acme halts sales of its robot dog in the EU", "Acme CEO rules out IPO this year",
                  "Birch walks away from $6B Cedar deal",  # no deal in it, or not Acme's
                  "Acme CEO to exit after Birch acquisition", "Birch drops Acme brand after $200M deal",  # news after one
                  "Judge refuses to block Birch's acquisition of Acme", "3 reasons not to buy Acme stock",
                  "FTC drops challenge to Birch's Acme acquisition"):
        kept, none = news.read(_fetch({"news.google.com": feed.replace("Birch walks away from $6B Acme deal", other)}),
                               "Acme")
        assert none is None and kept[0]["quote"] == "Birch in talks to buy AI startup Acme for $6 billion", other
    stored = "https://news.google.com/rss/articles/"
    row = {"id": "acme", "name": "Acme", "signals": [
        {"kind": "funding", "day": "2026-09-07", "quote": "Birch in talks to buy AI startup Acme for $6 billion",
         "source_url": stored + "1"},
        {"kind": "funding", "day": "2026-09-09", "quote": "Acme raises $100M Series B", "source_url": stored + "3"}]}
    assert accounts.recheck(json.loads(json.dumps(row)))["headlines"] == 0  # no word the deal fell through: it stays
    row["signals"].append({"kind": "funding", "day": "2026-09-08", "quote": "Birch to buy Acme",
                           "source_url": "https://example.org/typed-in"})  # entered by hand: stays
    dropped = accounts.recheck(row, deal_off=off)
    assert dropped["gone"] == ["headline “Birch in talks to buy AI startup Acme for $6 billion”, a deal that fell "
                               "through (“Birch walks away from $6B Acme deal”)"]
    assert [s["quote"] for s in row["signals"]] == ["Acme raises $100M Series B", "Birch to buy Acme"]


def test_find_update_recheck_drops_a_stored_deal_the_feed_now_says_fell_through(monkeypatch, tmp_path, capsys):
    from argparse import Namespace

    from scripts import accounts as cli

    (tmp_path / "accounts.json").write_text(json.dumps({"accounts": [{"id": "acme", "name": "Acme", "signals": [
        {"kind": "funding", "day": date.today().isoformat(), "quote": "Birch to buy Acme",
         "source_url": "https://news.google.com/rss/articles/1"}]}]}))
    found = [{"name": "Acme", "account": "acme", "big": False, "papers": [], "jobs": [], "news": [], "topics": [],
              "errors": [], "authors": [], "deal_off": "Birch walks away from Acme deal"}]
    monkeypatch.setattr(cli, "LIVE", tmp_path)
    monkeypatch.setattr(cli.today, "source", lambda mode: None)
    monkeypatch.setattr(accounts, "find", lambda *a, **k: found)
    cli.find(Namespace(since="2025-09-24", pages=1, update=True, recheck=True))
    assert "Acme: headline “Birch to buy Acme”, a deal that fell through" in capsys.readouterr().out
    assert json.loads((tmp_path / "accounts.json").read_text())["accounts"][0]["signals"] == []


def test_a_lever_posting_with_empty_fields_still_reads():
    fetch = _fetch({"api.lever.co": [{"text": "Robot Learning Engineer", "hostedUrl": "https://example.org/l/1",
                                       "descriptionPlain": None, "lists": None, "additionalPlain": None}]})
    assert jobs.postings(fetch, "lever", "x")[0]["text"] == ""


def test_a_one_word_account_name_matches_only_itself_and_a_company_suffix():
    figure = [Account(id="figure", name="Figure")]
    assert accounts._account_for("Figure AI (United States)", figure) is figure[0]
    assert accounts._account_for("Figure Eight (United States)", figure) is None


def test_a_contact_page_must_be_https():
    with pytest.raises(ValueError):
        Contact(name="Ada Example", source_url="javascript:alert(1)")


def test_ending_talks_ends_every_hold_the_mark_made_and_no_other(simulation):
    store, as_of, birchwood, _ = _account("birchwood-lab")
    accounts.in_talks(store, birchwood, by="Nora Example", at=as_of, also=["X9"])
    contact.record(store, "D901", "partner_staff", at=as_of, team="sales", by="Omar Example", note="Another deal")
    assert contact.check(contact.history(store, "X9"), "2026-09-20T00:00:00+00:00").state == "hold"
    ended = accounts.in_talks(store, birchwood, by="Nora Example", at="2026-09-16T00:00:00+00:00", until="2026-09-16")
    assert "X9" in ended and "D901" not in ended
    assert contact.check(contact.history(store, "X9"), "2026-09-20T00:00:00+00:00").state == "clear"
    assert contact.check(contact.history(store, "D901"), "2026-09-20T00:00:00+00:00").state == "hold"


def test_ending_one_accounts_talks_keeps_another_accounts_hold_on_the_same_person(simulation):
    store, as_of, birchwood, _ = _account("birchwood-lab")  # D904 is one of Birchwood's contacts
    loam = _account("loam-robotics")[2]
    later = "2026-09-20T00:00:00+00:00"
    accounts.in_talks(store, birchwood, by="Nora Example", at=as_of)
    accounts.in_talks(store, loam, by="Rue Example", at="2026-09-16T00:00:00+00:00", also=["D904"])
    accounts.in_talks(store, birchwood, by="Nora Example", at="2026-09-17T00:00:00+00:00", until="2026-09-17")
    assert contact.check(contact.history(store, "D904"), later).state == "hold"  # Loam's talks go on
    assert contact.check(contact.history(store, "account:birchwood-lab:pat-example"), later).state == "clear"
    accounts.in_talks(store, loam, by="Rue Example", at="2026-09-18T00:00:00+00:00", until="2026-09-18")
    assert contact.check(contact.history(store, "D904"), later).state == "clear"


def test_a_hold_someone_ended_by_hand_stays_ended_when_another_account_marks_them(simulation):
    store, as_of, birchwood, _ = _account("birchwood-lab")
    loam = _account("loam-robotics")[2]
    accounts.in_talks(store, birchwood, by="Nora Example", at="2026-09-01T00:00:00+00:00")
    contact.record(store, "D904", "partner_staff", at="2026-09-10T00:00:00+00:00", until="2026-09-12", note="She left")
    assert contact.check(contact.history(store, "D904"), as_of).state == "clear"
    accounts.in_talks(store, loam, by="Rue Example", at=as_of, until="2026-10-01", also=["D904"])
    assert contact.check(contact.history(store, "D904"), "2026-10-05T00:00:00+00:00").state == "clear"


def test_a_renamed_account_still_ends_its_own_hold(simulation):
    store, as_of, birchwood, _ = _account("birchwood-lab")
    accounts.in_talks(store, birchwood, by="Nora Example", at=as_of)
    renamed = birchwood.model_copy(update={"name": "Birchwood Labs"})
    accounts.in_talks(store, renamed, by="Nora Example", at="2026-09-16T00:00:00+00:00", until="2026-09-16")
    assert contact.check(contact.history(store, "D904"), "2026-09-20T00:00:00+00:00").state == "clear"
    odd = birchwood.model_copy(update={"id": "birch wood/lab"})  # any id: ending it ends only its own hold
    accounts.in_talks(store, _account("loam-robotics")[2], by="Rue Example", at=as_of, also=["D904"])
    accounts.in_talks(store, odd, by="Nora Example", at="2026-09-17T00:00:00+00:00", also=["D904"])
    accounts.in_talks(store, odd, by="Nora Example", at="2026-09-18T00:00:00+00:00", until="2026-09-18")
    assert contact.check(contact.history(store, "D904"), "2026-09-20T00:00:00+00:00").state == "hold"


def test_the_order_of_two_marks_does_not_matter_when_one_ends(simulation):
    store, as_of, birchwood, _ = _account("birchwood-lab")
    loam = _account("loam-robotics")[2]
    accounts.in_talks(store, loam, by="Rue Example", at=as_of, also=["D904"])
    accounts.in_talks(store, birchwood, by="Nora Example", at="2026-09-16T00:00:00+00:00")
    end = "2026-09-17T00:00:00+00:00"
    accounts.in_talks(store, birchwood, by="Nora Example", at=end, until="2026-09-17")
    for when in (end, "2026-09-20T00:00:00+00:00"):  # held from the moment of the end, not a second later
        gate = contact.check(contact.history(store, "D904"), when)
        assert gate.state == "hold" and "Rue Example" in gate.reason and as_of[:10] in gate.reason


def test_the_author_with_the_most_papers_becomes_the_champion_only_with_two_or_more():
    candidate = {"authors": [{"name": "Cleo Example", "url": "https://openalex.org/A1", "papers": 2},
                             {"name": "Bo Example", "url": "https://openalex.org/A2", "papers": 1}]}
    champ = accounts.champion_from(candidate, Account(id="a", name="Acme"), "2025-09-24")
    assert (champ.name, champ.part, champ.source_url) == ("Cleo Example", "champion", "https://openalex.org/A1")
    assert champ.title == "Author of 2 papers on GI's topics since 2025-09-24"
    assert accounts.champion_from(candidate, Account(id="a", name="Acme"), "2025-09-24", least=3) is None
    named = Account(id="a", name="Acme", contacts=[Contact(name="Rue Example", part="champion")])
    assert accounts.champion_from(candidate, named, "2025-09-24") is None  # it already has one


def test_find_update_adds_only_recent_evidence_to_its_own_account_a_few_at_a_time(monkeypatch, tmp_path):
    from argparse import Namespace

    from scripts import accounts as cli

    (tmp_path / "accounts.json").write_text(json.dumps({"accounts": [
        {"id": "meta", "name": "Meta", "buys": "watch"}, {"id": "acme", "name": "Acme Robotics (Acme)"}]}))
    recent = date.today().isoformat()
    found = [{"name": "Metaverse Labs (United States)", "account": None, "big": False, "papers": [
                 {"title": "T", "day": recent, "url": "https://example.org/p"}],
              "jobs": [], "news": [], "topics": ["world model"], "errors": [], "authors": []},
             {"name": "Acme Robotics (Acme)", "account": "acme", "big": False, "topics": ["world model"], "errors": [],
              "authors": [{"name": "Cleo Example", "url": "https://openalex.org/A1", "papers": 3}],
              "news": [], "jobs": [{"title": "World Model Engineer", "url": "https://example.org/j", "posted": ""},
                                   {"title": None, "url": None, "posted": recent}],
              "papers": [{"title": f"Paper {n}", "day": recent, "url": f"https://example.org/{n}"} for n in range(7)]
              + [{"title": "Old", "day": "2025-10-01", "url": "https://example.org/old"}]}]
    monkeypatch.setattr(cli, "LIVE", tmp_path)
    monkeypatch.setattr(cli.today, "source", lambda mode: None)  # never the real timelines store
    monkeypatch.setattr(accounts, "find", lambda *a, **k: found)

    def saved():
        return {a["id"]: a for a in json.loads((tmp_path / "accounts.json").read_text())["accounts"]}

    cli.find(Namespace(since="2025-09-24", pages=1, update=True, recheck=False))
    assert "signals" not in saved()["meta"]  # a company that is no account adds nothing
    assert len(saved()["acme"]["signals"]) == cli.UPDATE_MAX
    assert "Old" not in json.dumps(saved()["acme"]) and "World Model Engineer" not in json.dumps(saved()["acme"])
    cli.find(Namespace(since="2025-09-24", pages=1, update=True, recheck=False))  # the next run adds the rest, no duplicates
    urls = [s["source_url"] for s in saved()["acme"]["signals"]]
    assert len(urls) == 7 == len(set(urls))
    assert [c["name"] for c in saved()["acme"]["contacts"]] == ["Cleo Example"]  # added once
    assert "contacts" not in saved()["meta"]


def test_the_price_check_only_reads_apify_listings(monkeypatch, capsys):
    from scripts import accounts as cli

    asked, served = [], {"currentCompanies": {"type": "array", "description": "Company names or URLs"}}

    def handler(request):
        asked.append((request.method, request.url.path))
        if request.url.path.endswith("/store"):
            return httpx.Response(200, json={"data": {"items": [{"username": "harvestapi", "name": "people-search",
                                                                  "title": "People search",
                                                                  "currentPricingInfo": {"pricePerUnitUsd": 0.1}}]}})
        if "/acts/" in request.url.path:
            return httpx.Response(200, json={"data": {"taggedBuilds": {"latest": {"buildId": "B1"}}}})
        return httpx.Response(200, json={"data": {"inputSchema": json.dumps({"properties": served})}})

    monkeypatch.setattr(cli, "_key", lambda name: "")
    real = httpx.Client
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))
    cli.describe()
    out = capsys.readouterr().out
    assert '"pricePerUnitUsd": 0.1' in out and "currentCompanies (array): Company names or URLs" in out
    assert "Asks for a login, cookie or session: no" in out
    assert asked == [("GET", "/v2/store"), ("GET", "/v2/acts/harvestapi~people-search"),
                     ("GET", "/v2/actor-builds/B1")]  # listings only: no run is started
    served.update({"cookies": {"type": "array", "title": "Your LinkedIn cookies (optional)"}, "li_at": {"type": "string"},
                   "sessionId": {"type": "string"}})  # the same guard as the paid X pull, plus LinkedIn's li_at
    cli.describe()
    assert "yes, or mentions one (cookies, li_at, sessionId)" in capsys.readouterr().out


def test_recheck_drops_what_find_wrote_and_the_rules_no_longer_count(simulation, monkeypatch, tmp_path, capsys):
    from argparse import Namespace

    from scripts import accounts as cli

    recent, feed, doi = date.today().isoformat(), "https://news.google.com/rss/articles/", "https://doi.org/10.1/"
    auto = "Author of 3 papers on GI's topics since 2025-09-24"
    before = {"accounts": [
        {"id": "pi", "name": "Physical Intelligence", "signals": [
            {"kind": "funding", "day": recent, "quote": "Analog Example to buy Birch to expand into physical intelligence",
             "source_url": feed + "1"},
            {"kind": "funding", "day": recent, "quote": "Physical Intelligence raises $600M", "source_url": feed + "2"},
            {"kind": "funding", "day": recent, "quote": "Physical Intelligence names Max Example CTO",
             "source_url": feed + "3"},  # read as a new leader now
            {"kind": "funding", "day": recent, "quote": "Physical Intelligence Inc closed its Series B",
             "source_url": "https://example.org/n/4"},  # entered by hand: stays
            {"kind": "new_lead", "day": recent, "who": "Max Example", "quote": "Excited to lead robot learning here.",
             "source_url": "https://example.org/n/5"},  # entered by hand: stays
            {"kind": "team_paper", "day": recent, "quote": "Conditional tokenization world models", "source_url": doi + "1"},
            {"kind": "team_paper", "day": recent, "quote": "A vision-language-action model", "source_url": doi + "2"},
            {"kind": "team_paper", "day": recent, "quote": "Introducing our new model", "source_url": "https://example.org/r"},
            {"kind": "job_post", "day": recent, "quote": "Research Engineer", "source_url": "https://example.org/j/1"}],
         "contacts": [{"name": "Cleo Example", "title": auto, "part": "champion"},
                      {"name": "Rue Example", "title": "Head of research", "part": "champion"}]},
        {"id": "acme", "name": "Acme Robotics", "contacts": [{"name": "José García", "title": auto, "part": "champion"}],
         "ties": [{"by": "Noor Example", "to": "Jose Garcia", "what": "Worked together"}]},
        {"id": "birch", "name": "Birch Labs", "contacts": [{"name": "Sid Example", "title": auto, "part": "champion"}]}]}
    (tmp_path / "accounts.json").write_text(json.dumps(before))
    found = [{"name": "Birch Labs", "account": "birch", "big": False, "papers": [], "jobs": [], "news": [], "topics": [],
              "errors": [], "authors": [{"name": "Ada Example", "url": "https://openalex.org/A5", "papers": 2}]}]
    store = accounts.workspace("simulation")[0][0]
    monkeypatch.setattr(cli, "LIVE", tmp_path)
    monkeypatch.setattr(cli.today, "source", lambda mode: (store, "2026-09-24T00:00:00+00:00"))
    monkeypatch.setattr(accounts, "find", lambda *a, **k: found)
    cli.find(Namespace(since="2025-09-24", pages=1, update=True, recheck=True))
    out = capsys.readouterr().out
    assert "Physical Intelligence: headline “Analog Example to buy Birch" in out and "Birch Labs: champion Sid Example" in out
    after = {a["id"]: a for a in json.loads((tmp_path / "accounts.json").read_text())["accounts"]}
    assert [(s["kind"], s["quote"]) for s in after["pi"]["signals"]] == [
        ("funding", "Physical Intelligence raises $600M"), ("new_lead", "Physical Intelligence names Max Example CTO"),
        ("funding", "Physical Intelligence Inc closed its Series B"), ("new_lead", "Excited to lead robot learning here."),
        ("team_paper", "A vision-language-action model"), ("team_paper", "Introducing our new model"),
        ("job_post", "Research Engineer")]
    assert [c["name"] for c in after["pi"]["contacts"]] == ["Rue Example"]  # added by hand: stays
    assert [c["name"] for c in after["acme"]["contacts"]] == ["José García"]  # a tie names him: stays
    assert [c["name"] for c in after["birch"]["contacts"]] == ["Ada Example"]  # the run's champion, under today's rules
    cli.find(Namespace(since="2025-09-24", pages=1, update=True, recheck=True))  # every run keeps its own copy
    copies = sorted(tmp_path.glob("accounts.before-recheck-*.json"))
    assert len(copies) == 2 and json.loads(copies[0].read_text()) == before


def test_recheck_with_no_ledger_to_ask_keeps_every_paper_champion():
    row = {"id": "birch", "name": "Birch Labs", "contacts": [
        {"name": "Sid Example", "title": "Author of 2 papers on GI's topics since 2025-09-24", "part": "champion"}]}
    assert accounts.recheck(row) == {"headlines": 0, "papers": 0, "posts": 0, "champions": 0, "gone": []}


def test_recheck_keeps_a_paper_champion_the_ledger_has_anything_on(simulation):
    store, as_of, _, _ = _account("birchwood-lab")
    row = {"id": "birch", "name": "Birch Labs", "contacts": [
        {"name": "Sid Example", "title": "Author of 2 papers on GI's topics since 2025-09-24", "part": "champion"},
        {"name": "Ada Example", "title": "Author of 2 papers on GI's topics since 2025-09-24", "part": "champion"}]}
    contact.record(store, "account:birch:sid-example", "pinged", at=as_of, role_id=accounts.GTM, team="sales")
    assert accounts.recheck(row, store) == {"headlines": 0, "papers": 0, "posts": 0, "champions": 1,
                                            "gone": ["champion Ada Example"]}
    assert [c["name"] for c in row["contacts"]] == ["Sid Example"]  # already on the list: the month's rule still sees him
    row = {"id": "gdm", "name": "Google DeepMind", "signals": [
        {"kind": "job_post", "day": "2026-09-01", "quote": "Hardware Engineer, Mechatronics", "source_url": "https://example.org/j"}],
           "contacts": [{"name": "Gemini 3.1 (Flash)", "title": "Author of 4 papers on GI's topics since 2025-09-24",
                         "part": "champion"}]}
    contact.record(store, "account:gdm:gemini-3-1-flash", "pinged", at=as_of, role_id=accounts.GTM, team="sales")
    assert accounts.recheck(row) == {"headlines": 0, "papers": 0, "posts": 1, "champions": 1, "gone": [
        "job post “Hardware Engineer, Mechatronics”", "champion Gemini 3.1 (Flash)"]}  # a model goes, even with no store
    assert row["contacts"] == [] and row["signals"] == []
    row = {"id": "birch", "name": "Birch Labs", "contacts": [{"name": "Bo Example", "subject_id": "backend:bo-example"}]}
    accounts.recheck(row, store)
    assert row["contacts"][0]["subject_id"] == "bo-example"  # an earlier run's role goes from GTM's file


def test_find_stays_inside_openalex_free_daily_searches(monkeypatch, tmp_path):
    from argparse import Namespace

    from scripts import accounts as cli

    monkeypatch.setattr(cli, "LIVE", tmp_path)
    with pytest.raises(SystemExit, match="credits a run may spend"):
        cli.find(Namespace(since="2025-09-24", pages=20, update=False, recheck=False))


class _Metered:
    """An OpenAlex search that answers every page and says, as the Fetcher's meta does, 10 fewer credits after
    each; ``pages`` gives each call's works in turn, and the ``refuse``-th request fails with ``error``."""

    def __init__(self, left=700, refuse=None, pages=None, error=429):
        self.left, self.refuse, self.pages, self.error, self.urls = left, refuse, pages, error, []

    def __call__(self, url):
        self.urls.append(url)
        n = len(self.urls)
        if n == self.refuse and isinstance(self.error, Exception):
            raise self.error
        if n == self.refuse:
            raise httpx.HTTPStatusError(str(self.error), request=httpx.Request("GET", url),
                                        response=httpx.Response(self.error))
        self.left -= 10
        return json.dumps({"meta": {"next_cursor": f"c{n}"},
                           "results": self.pages[n - 1] if self.pages else [{"id": f"https://openalex.org/W{n}"}]}).encode()

    def meta(self, url):
        return {"ratelimit_remaining": self.left}


def test_the_openalex_search_stops_inside_its_credits_and_keeps_what_it_fetched(monkeypatch):
    from app.sources import openalex

    monkeypatch.setattr(openalex, "PER_PAGE", 1)  # every fake page is full, so the search goes on
    budget = openalex.Budget(most=50)
    found = openalex.companies_works(_Metered(), ("world model", "game agent"), "2025-09-24", pages=5, budget=budget)
    # every topic's newest page first, so a run cut short still has each topic's newest works
    assert [phrase for phrase, _ in found] == ["world model", "game agent", "world model", "game agent", "world model"]
    assert budget.said() == ("OpenAlex: 5 search pages, about 10 credits a page, 50 in all; 650 left today. Stopped "
                             "early: this run's 50 credits are spent. What was fetched is kept.")
    budget = openalex.Budget(keep=660)
    assert len(openalex.company_works(_Metered(), "world model", "2025-09-24", pages=9, budget=budget)) == 4
    assert budget.stopped == "660 credits left today, and 660 are kept for the rest of the day"
    budget = openalex.Budget(keep=100)
    assert len(openalex.company_works(_Metered(left=115), "world model", "2025-09-24", pages=9, budget=budget)) == 1
    assert budget.left == 105  # before it has measured a page, it counts 10 a page: no page past the reserve
    budget = openalex.Budget()
    assert len(openalex.company_works(_Metered(), "world model", "2025-09-24", pages=2, budget=budget)) == 2
    assert not budget.stopped and budget.said() == "OpenAlex: 2 search pages, about 10 credits a page, 20 in all; 680 left today."
    for refused, error, why in ((3, 429, 'OpenAlex answered 429 on "world model"'),
                                (2, 503, 'OpenAlex answered 503 on "world model"'),
                                (2, httpx.ReadTimeout("slow"), 'OpenAlex didn\'t answer on "world model" (ReadTimeout)')):
        fetch, budget = _Metered(refuse=refused, error=error), openalex.Budget()
        assert len(openalex.company_works(fetch, "world model", "2025-09-24", pages=5, budget=budget)) == refused - 1
        assert budget.stopped == why and len(fetch.urls) == refused  # none after it
    with pytest.raises(httpx.HTTPStatusError):  # a bad search is a bug, not a limit: it still stops the run
        openalex.company_works(_Metered(refuse=1, error=400), "world model", "2025-09-24", budget=openalex.Budget())
    with pytest.raises(httpx.HTTPStatusError):  # and with no budget to say why, so does a refused page
        openalex.company_works(_Metered(refuse=1), "world model", "2025-09-24")


def test_the_openalex_search_asks_for_no_page_past_a_short_one_and_measures_a_new_day_afresh(monkeypatch):
    from app.sources import openalex

    monkeypatch.setattr(openalex, "PER_PAGE", 2)
    fetch = _Metered(pages=[[{"id": "W1"}, {"id": "W2"}], [{"id": "W3"}]])
    assert len(openalex.company_works(fetch, "world model", "2025-09-24", pages=5)) == 3
    assert len(fetch.urls) == 2  # the short second page was the last: no credits on an empty third
    budget = openalex.Budget()
    for left in (700, 690, 995, 985, 975):  # the count went up: a new day
        budget.note(left)
    assert budget.measured() == 10 and budget.spent() == 50


def test_find_keeps_what_it_fetched_when_openalex_stops_it(monkeypatch):
    from app.sources import openalex

    monkeypatch.setattr(openalex, "PER_PAGE", 1)
    acme = Account(id="acme", name="Acme Robotics (Acme)")
    budget = openalex.Budget()
    # "world model" finds Acme only on its second page, after "sim-to-real" has found it on its first
    fetch = _Metered(refuse=4, pages=[[WORKS[1]], [WORK], [WORK]])
    found = accounts.find(fetch, "2025-09-24", [acme], topics=("world model", "sim-to-real"), budget=budget)
    assert budget.stopped == 'OpenAlex answered 429 on "sim-to-real"'
    acme_row = next(c for c in found if c["account"] == "acme")
    assert [p["title"] for p in acme_row["papers"]] == ["Action models from gameplay"]
    assert acme_row["topics"] == ["world model", "sim-to-real"]  # in the topics' order, not the pages'


def test_a_find_stopped_early_keeps_every_paper_champion_adds_none_and_says_what_it_cost(simulation, monkeypatch,
                                                                                          tmp_path, capsys):
    from argparse import Namespace

    from scripts import accounts as cli

    auto = "Author of 2 papers on GI's topics since 2025-09-24"
    (tmp_path / "accounts.json").write_text(json.dumps({"accounts": [
        {"id": "birch", "name": "Birch Labs", "contacts": [{"name": "Sid Example", "title": auto, "part": "champion"}]},
        {"id": "acme", "name": "Acme Robotics"}]}))
    ada = {"name": "Ada Example", "url": "https://openalex.org/A5", "papers": 3}
    found = [{"name": name, "account": key, "big": False, "papers": [], "jobs": [], "news": [], "topics": [],
              "errors": [], "authors": [ada]} for key, name in (("birch", "Birch Labs"), ("acme", "Acme Robotics"))]

    def stopped(*a, budget=None, **k):
        budget.note(610)
        budget.stopped = 'OpenAlex answered 429 on "world model"'
        return found

    store = accounts.workspace("simulation")[0][0]
    monkeypatch.setattr(cli, "LIVE", tmp_path)
    monkeypatch.setattr(cli.today, "source", lambda mode: (store, "2026-09-24T00:00:00+00:00"))
    monkeypatch.setattr(accounts, "find", stopped)
    cli.find(Namespace(since="2025-09-24", pages=5, update=True, recheck=True))
    out = capsys.readouterr().out
    assert "at most 35 search pages, one a second, about 350 of the 1,000 free credits a day" in out
    assert ('OpenAlex: 1 search page, 610 credits left today. Stopped early: OpenAlex answered 429 on "world model". '
            "What was fetched is kept.") in out
    assert "the search stopped early, so champions from papers stay as they are until a full run" in out
    assert "0 champions from papers (none) to accounts.json; new champions wait for a full run" in out
    after = {a["id"]: a for a in json.loads((tmp_path / "accounts.json").read_text())["accounts"]}
    assert [c["name"] for c in after["birch"]["contacts"]] == ["Sid Example"]  # not dropped, and no second champion
    assert "contacts" not in after["acme"]  # partial paper counts pick no champion
