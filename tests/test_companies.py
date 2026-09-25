"""The company watcher: what just happened at the companies GI hires from, and who GI would want there. Invented
companies, headlines and papers; no network."""

import json
from urllib.parse import unquote

import httpx
import pytest

from app import accounts, companies, journey, today
from app.companies import Company
from app.store import Store

DAY = "2026-09-15"


def rss(*items):
    return "<rss><channel>" + "".join(
        f"<item><title>{title} - Example Wire</title><link>https://example.org/news/{n}</link>"
        f"<pubDate>{published}</pubDate></item>" for n, (title, published) in enumerate(items)) + "</channel></rss>"


def author(name, place, kind="company"):
    return {"author": {"id": f"https://openalex.org/A-{name.split()[0]}", "display_name": name},
            "institutions": [{"id": f"https://openalex.org/I-{place.split()[0]}", "display_name": place, "type": kind}]}


def work(n, title, *authorships, day="2026-09-03"):
    return {"id": f"https://openalex.org/W{n}", "title": title, "publication_date": day,
            "doi": f"https://doi.org/10.1/{n}", "authorships": list(authorships)}


FEEDS = {
    "nimbus labs": rss(("Nimbus Labs lays off 30 researchers", "Thu, 10 Sep 2026 08:00:00 GMT"),
                       ("Acme to acquire Nimbus Labs", "Tue, 01 Sep 2026 08:00:00 GMT"),
                       ("Acme walks away from Nimbus Labs deal", "Sat, 12 Sep 2026 08:00:00 GMT"),
                       ("Nimbus Labs lays off 10", "Wed, 01 Jul 2026 08:00:00 GMT")),  # before the month
    "meta": rss(("Meta to cut 600 jobs in AI unit", "Wed, 09 Sep 2026 08:00:00 GMT"),
                ("Meta lays off 1,000 sales staff", "Wed, 09 Sep 2026 09:00:00 GMT")),  # not its AI work
}
WORKS = [work(1, "Latent action world models from gameplay video", author("Ada Example", "Nimbus Labs (United States)"),
              author("Bo Example", "Nimbus Labs (United States)"), author("Eve Example", "General Intuition"),
              author("Genie", "Nimbus Labs (United States)"), author("Cy Example", "Example University", "education")),
         work(2, "Sim-to-real transfer for legged robots", author("Pat Example", "Quarry Robotics")),
         work(3, "Embodied agents for warehouse picking", author("Pat Example", "Quarry Robotics")),
         work(4, "Conditional tokenization world models", author("Ada Example", "Nimbus Labs (United States)")),
         work(5, "Robot learning from video", author("Dee Example", "Solo Robotics"))]  # one paper: not listed


def fetch(url):
    url = unquote(url)
    if "api.openalex.org" in url:
        return json.dumps({"meta": {"next_cursor": None}, "results": WORKS}).encode()
    if "birch" in url:
        raise httpx.ConnectError("refused")
    return next((body for name, body in FEEDS.items() if f'"{name}"' in url), rss()).encode()


def test_a_collaborator_is_held_a_partner_or_buyer_asks_sales_and_a_rival_or_employer_is_open():
    found = [accounts.Account(id="epic", name="Epic Games", buys="partner"),
             accounts.Account(id="gdm", name="Google DeepMind", buys="watch"),
             accounts.Account(id="acme", name="Acme Robotics", buys="data"),
             accounts.Account(id="kiln", name="Kiln Sim", buys="partner")]
    assert companies.relation("Epic Games, Inc.", found).relation == "collaborator"  # MIRA, before the partner account
    kyutai, gdm, acme = (companies.relation(n, found) for n in ("Kyutai", "Google DeepMind", "Acme Robotics Inc"))
    assert (kyutai.stance, kyutai.why) == ("Held", "GI works with them: MIRA (6 July 2026).")
    assert (gdm.relation, gdm.stance) == ("rival", "Open")
    assert (acme.stance, acme.why) == ("Ask sales first", "A GTM account that buys GI's gameplay data: sales pitches its people.")
    assert companies.relation("Kiln Sim", found).stance == "Ask sales first"
    assert companies.relation("Nimbus Labs", found) == Company("Nimbus Labs")  # open, nothing to say


def test_the_watch_list_is_collaborators_accounts_and_watched_peoples_employers_once(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 't.sqlite'}")
    for sid, employer in (("P1", "Driftwire Inc"), ("P2", "Nimbus Labs"), ("P3", "Hidden Co"), ("P4", "")):
        journey.save_context(store, journey.PersonContext(subject_id=sid, name=f"Person {sid}", employer=employer))
    names = [c.name for c in companies.watched("simulation", store, allowed={"P1", "P2", "P4"})]
    assert names[:2] == ["Kyutai", "Epic Games"] and names[-1] == "Nimbus Labs"
    assert "Driftwire Inc" not in names and names.count("Driftwire") == 1  # the account, once
    assert "Hidden Co" not in names  # P3 is not watched


def test_read_keeps_the_months_news_and_papers_on_gis_work_and_notes_a_failed_feed():
    watched = [Company("Nimbus Labs", "rival"), Company("Meta (FAIR)", "rival"), Company("Birch Robotics")]
    result = companies.read(fetch, watched, DAY, pages=1)
    news = [(m["company"], m["kind"], m["day"]) for m in result["moments"] if m["kind"] != "team_paper"]
    assert news == [("Nimbus Labs", "layoffs_reported", "2026-09-10"),
                    ("Nimbus Labs", "acquisition_called_off", "2026-09-12"),  # and the deal before it is left out
                    ("Meta (FAIR)", "layoffs_reported", "2026-09-09")]  # its AI unit, not its sales staff
    [paper] = [m for m in result["moments"] if m["kind"] == "team_paper"]
    assert (paper["company"], paper["quote"], paper["source_url"]) == \
        ("Nimbus Labs", "Latent action world models from gameplay video", "https://doi.org/10.1/1")
    assert [a["name"] for a in paper["authors"]] == ["Ada Example", "Bo Example"]  # not GI's, a model or a school's
    assert result["errors"] == ["Birch Robotics: news ConnectError"] and result["since"] == "2026-08-16"


def test_a_paper_counts_at_a_company_only_for_authors_whose_own_line_names_it():
    # OpenAlex matches a line that names only a university to a company too. Ways it could fail: the mismatched author
    # is listed there; the paper is listed there with nobody on it (a row on the page from that alone); an author whose
    # own line does name it is dropped with the other.
    def at(name, line):
        return {**author(name, "Nimbus Labs (United States)"), "affiliations": [{"raw_affiliation_string": line}]}
    only_school = work(6, "Latent action world models for racing games", at("Kai Example", "Example University, Toronto"))
    both = work(7, "World models from gameplay video at scale", at("Kai Example", "Example University, Toronto"),
                at("Lu Example", "Nimbus Labs, San Francisco"))

    def openalex(url):
        return json.dumps({"meta": {"next_cursor": None}, "results": [only_school, both]}).encode() \
            if "api.openalex.org" in unquote(url) else rss().encode()
    result = companies.read(openalex, [Company("Nimbus Labs", "rival")], DAY, pages=1)
    papers = [(m["quote"], [a["name"] for a in m["authors"]]) for m in result["moments"] if m["kind"] == "team_paper"]
    assert papers == [("World models from gameplay video at scale", ["Lu Example"])]


def test_the_preview_puts_open_companies_first_with_who_works_there():
    assert "companies" not in today.calls("simulation")  # the brief and the Monday post never read it
    p = today.calls("simulation", companies=True)["companies"]
    assert [(c["company"], c["stance"]) for c in p["companies"]] == [
        ("Mock World Models Lab", "Open"), ("Sample Sim Labs", "Ask sales first"), ("Driftwire", "Ask sales first")]
    mock, _, driftwire = p["companies"]
    assert [m["label"] for m in mock["moments"]] == ["Laying people off", "Paper on GI's work", "Paper on GI's work"]
    assert (mock["moments"][0]["day"], mock["moments"][0]["headlines"]) == ("2026-09-11", 2)  # one story, two headlines
    assert [(a["name"], a["papers"]) for a in mock["authors"]] == [("Ada Example", 2), ("Bo Example", 1), ("Cy Example", 1)]
    assert driftwire["people"] == [{"subject_id": "D902", "name": "Jonah Pike", "role": "Senior / Lead Backend Engineer",
                                    "call": "Stay quiet"}]
    assert [a["name"] for a in driftwire["authors"]] == ["Lee Example"]  # Jonah is listed once, as watched
    assert p["missing"] is None


def test_live_names_only_the_watchlist_and_says_when_there_is_no_read(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path / 't.sqlite'}")
    for sid in ("P1", "P2"):
        journey.save_context(store, journey.PersonContext(subject_id=sid, name=f"Person {sid}", employer="Nimbus Labs"))
    calls = [{"subject_id": sid, "name": f"Person {sid}", "role": {"title": "MTS"}, "headline": "Wait"} for sid in ("P1", "P2")]
    monkeypatch.setitem(companies.RECORDS, "live", tmp_path / "companies")
    assert companies.preview("live", store, calls, {"P1"})["missing"].startswith("No company read yet")
    companies.save(companies.read(fetch, [Company("Nimbus Labs")], DAY, pages=1), tmp_path / "companies")
    [nimbus] = companies.preview("live", store, calls, {"P1"})["companies"]
    assert [w["name"] for w in nimbus["people"]] == ["Person P1"] and nimbus["stance"] == "Open"
    assert [a["name"] for a in nimbus["authors"]] == ["Ada Example", "Bo Example"]
    journey.save_context(store, journey.PersonContext(subject_id="R1", name="Bo Example"))  # a replay case, say
    [nimbus] = companies.preview("live", store, calls, {"P1"})["companies"]
    assert [a["name"] for a in nimbus["authors"]] == ["Ada Example"]  # anyone the engine knows is never an author


def test_a_read_that_wont_load_says_so_instead_of_breaking_todays_calls(tmp_path, monkeypatch):
    (tmp_path / "moments-2026-09-15.json").write_text('{"read_on": ')  # cut off mid-write
    monkeypatch.setitem(companies.RECORDS, "simulation", tmp_path)
    assert today.calls("simulation", companies=True)["companies"]["missing"].startswith("The company read won't load")


def test_a_story_shows_once_and_a_saved_misread_drops_out_by_todays_rules(tmp_path, monkeypatch):
    off = [{"company": "Nimbus Labs", "kind": "acquisition_called_off", "day": day, "quote": quote,
            "source_url": f"https://example.org/news/{n}"}
           for n, (day, quote) in enumerate([("2026-09-08", "Birch Robotics walks away from $6 billion Nimbus Labs deal"),
                                             ("2026-09-07", "Birch Robotics said to drop pursuit of AI startup Nimbus Labs"),
                                             ("2026-09-09", "What made Birch Robotics walk away from the Nimbus Labs deal?")])]
    buyer = {**off[0], "company": "Birch Robotics", "quote": "Birch Robotics said to walk away from $6B Nimbus Labs purchase"}
    cuts = [{"company": "Nimbus Labs", "kind": "layoffs_reported", "day": day, "quote": quote, "source_url": "https://example.org/c"}
            for day, quote in [("2026-08-20", "Nimbus Labs lays off 30 staff"),
                               ("2026-09-14", "Nimbus Labs to cut 20% of workforce in second round")]]
    monkeypatch.setitem(companies.RECORDS, "live", tmp_path)
    companies.save({"read_on": DAY, "since": "2026-08-16", "moments": [*off, buyer, *cuts], "errors": []}, tmp_path)
    store = Store(f"sqlite:///{tmp_path / 't.sqlite'}")
    [nimbus] = companies.preview("live", store, [])["companies"]  # Birch walked away: not its own sale called off
    assert [(m["kind"], m["day"], m["headlines"]) for m in nimbus["moments"]] == [
        ("layoffs_reported", "2026-09-14", 1), ("acquisition_called_off", "2026-09-07", 3),
        ("layoffs_reported", "2026-08-20", 1)]  # a second round weeks later is its own story
    assert nimbus["moments"][1]["quote"] == "Birch Robotics said to drop pursuit of AI startup Nimbus Labs"


@pytest.mark.parametrize("headline, company, counts", [
    ("Birch said to walk away from $6B Acme purchase", "Acme", True),
    ("Birch said to walk away from $6B Acme purchase", "Birch", False),  # the buyer walking away
    ("Birch ends takeover talks with AI startup Acme", "Acme", True),
    ("Birch ends takeover talks with AI startup Acme", "Birch", False),
    ("Birch's takeover bid for Acme collapses", "Birch", False),
    ("Acme deal to buy Birch collapses", "Acme", False),
    ("Acme deal to buy Birch collapses", "Birch", True),
    ("Birch drops $20 billion deal for Acme", "Acme", True),
    ("Acme's acquisition by Birch called off", "Acme", True),
    ("Birch eyes Cedar partnership after $7 billion chip acquisition talks stall", "Birch", False),
])
def test_a_deal_that_fell_through_counts_for_what_it_would_buy_never_the_buyer(headline, company, counts):
    m = {"company": company, "kind": "acquisition_called_off", "day": DAY, "quote": headline, "source_url": ""}
    assert companies._counts(m) is counts


def test_a_hand_edited_read_with_no_headline_says_so(tmp_path, monkeypatch):
    moment = {"company": "Nimbus Labs", "kind": "layoffs_reported", "day": DAY, "quote": None, "source_url": ""}
    companies.save({"read_on": DAY, "since": "2026-08-16", "moments": [moment], "errors": []}, tmp_path)
    monkeypatch.setitem(companies.RECORDS, "live", tmp_path)
    store = Store(f"sqlite:///{tmp_path / 't.sqlite'}")
    assert companies.preview("live", store, [])["missing"].startswith("The company read won't load")


def test_a_big_companys_news_names_its_ai_team_not_the_industry():
    assert companies.AI_WORK.search("Initech to cut 600 jobs in its AI unit")
    assert companies.AI_WORK.search("Initech lays off robotics researchers")
    assert companies.AI_WORK.search("Initech to cut 600 roles in its AI infrastructure unit")
    assert not companies.AI_WORK.search("Globex, Initech layoffs: why big tech is cutting jobs amid AI boom")
    assert not companies.AI_WORK.search("Initech calls its cuts fair")
    assert not companies.AI_WORK.search("Initech cuts 4,000 support jobs as AI replaces workers")
