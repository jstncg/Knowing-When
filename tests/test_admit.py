"""The watchlist's admit gate: invented candidates and a fake GitHub, no network."""

import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

from app import admit, scorecard
from app.sources import github
from app.timeline import add_event

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-09-24T12:00:00+00:00"


def candidate(**kw):
    rows, bad = admit.load([{"name": "Rin Vale", "role": "backend", **kw}])
    assert not bad, bad
    return rows[0]


def repos(n, day="2026-08-01"):
    return {"repos": [{"full_name": f"rinv/tool{i}", "html_url": f"https://github.com/rinv/tool{i}",
                       "created_at": f"{day}T12:00:00Z"} for i in range(n)], "starred": [], "events": []}


def about(**kw):
    return {"name": "Rin Vale", "bio": "", "blog": "", "company": "", "twitter_username": "", "type": "User",
            "social": [], **kw}


def test_rows_from_any_research_file_load_and_bad_ones_say_why():
    rows, bad = admit.load([
        {"name": "Rin Vale", "x_handle": "@rin_v", "affiliation": "Acme", "reason": "ships a netcode library"},
        {"name": "Bo", "role": "mts", "x_handle": "not a handle!"},
        {"name": "Cy", "github": "cy-dev", "linkedin_url": "linkedin.com/in/cy-dev/"},
        {"name": "Dee", "linkedin_url": "https://www.linkedin.com/company/acme"},
        {"name": "Eve", "role": "recruiter", "x_handle": "eve"},
        {"name": "Fay", "role": "product_designer", "x_handle": "fay"},
        {"name": "Gus", "role": "mts", "x_handle": "gus"}], role="backend")
    assert [(c.name, c.role, c.x_handle, c.note) for c in rows[:1]] == [("Rin Vale", "backend", "rin_v",
                                                                         "Acme; ships a netcode library")]
    assert rows[1].profiles() == ["https://www.linkedin.com/in/cy-dev", "https://github.com/cy-dev"]
    assert [name for name, _ in bad] == ["Bo", "Dee", "Eve"] and "not an X handle" in bad[0][1]
    assert [c.role for c in rows[2:]] == ["product-designer", "mts-research"]  # as the research lists write them


def test_an_active_github_whose_profile_names_their_x_is_admitted_and_the_x_is_added():
    c = candidate(github="rinv")
    result = admit.assess(c, raw=repos(12), about=about(twitter_username="rin_v", blog="rin.example"), as_of=AS_OF)
    assert (result.verdict, result.github_a_year, result.add_x) == ("admit", 12, "rin_v")
    assert [r["links"] for r in result.records] == [["https://github.com/rinv", "https://x.com/rin_v"],
                                                   ["https://github.com/rinv", "https://rin.example"]]


def test_a_handle_that_spells_their_name_is_never_guessed_and_a_different_x_ties_nothing():
    quiet_profile = admit.assess(candidate(github="rinv", x_handle="rin_vale"), raw=repos(12), about=about(), as_of=AS_OF)
    assert quiet_profile.verdict == "tie_first" and "https://x.com/rin_vale" in quiet_profile.why
    assert quiet_profile.add_x == "" and quiet_profile.records == []
    other = admit.assess(candidate(github="rinv", x_handle="rin_vale"), raw=repos(12),
                         about=about(twitter_username="someone_else"), as_of=AS_OF)
    assert other.verdict == "tie_first" and "@someone_else" in other.why and other.records == []
    linked = admit.assess(candidate(github="rinv", x_handle="rin_vale"), raw=repos(12),
                          about=about(social=["https://twitter.com/another"]), as_of=AS_OF)
    assert linked.verdict == "tie_first" and "@another" in linked.why
    two = admit.assess(candidate(github="rinv"), raw=repos(12),
                       about=about(twitter_username="rin_v", social=["https://x.com/rin_work", "https://x.com/home"]),
                       as_of=AS_OF)
    assert two.verdict == "tie_first" and "@rin_v, @rin_work" in two.why and two.add_x == ""


def test_someone_elses_github_or_an_organization_is_never_tied():
    bob = admit.assess(candidate(github="rinvale"), raw=repos(12), about=about(name="Bob Smith", twitter_username="bob"),
                       as_of=AS_OF)
    assert bob.verdict == "tie_first" and "'Bob Smith'" in bob.why and bob.records == [] and bob.add_x == ""
    org = admit.assess(candidate(github="acme"), about=about(name="Acme", type="Organization"), as_of=AS_OF)
    assert org.verdict == "skipped" and "organization" in org.why


@pytest.mark.parametrize("github_name, name, theirs", [
    ("Rin Vale", "Rin Vale", True), ("Rin Vale.", "rin  vale", True), ("Rin V.", "Rin Vale", True),
    ("José García", "Jose Garcia", True), ("王伟", "王伟", True),
    ("Alex Johnson", "Alex Chen", False), ("Chen Li", "Wei Chen", False)])
def test_a_github_is_theirs_only_when_every_word_of_the_shorter_name_is_in_the_other(github_name, name, theirs):
    c = candidate(name=name, github="rinv")
    result = admit.assess(c, raw=repos(12), about=about(name=github_name, twitter_username="rin_v", blog="rin.example"),
                          as_of=AS_OF)
    assert (result.verdict == "admit") is theirs and (result.add_x == "rin_v") is theirs
    # Someone else's GitHub adds no X to look up or pull, and its repos, quiet or busy, count for nothing.
    assert (admit.enriched(c, about(name=github_name, twitter_username="rin_v")).x_handle == "rin_v") is theirs
    if not theirs:
        quiet = admit.assess(c, raw=repos(2), about=about(name=github_name), as_of=AS_OF)
        assert quiet.verdict == "tie_first" and quiet.why.startswith("check the GitHub is theirs")
        assert quiet.github_a_year is None  # never shown as the candidate's


@pytest.mark.parametrize("blog, tied", [("none", False), ("N/A", False), ("coming soon", False), ("rin.example", True),
                                        ("https://alex.computer", True), ("https://x.com/rin_v", False),
                                        ("rin@example.com", False), ("mailto:rin@example.com", False), ("e.g.", False),
                                        ("127.0.0.1", False), ("https://rin@rin.example", False),
                                        ("gist.github.com/rinv", False), ("uk.linkedin.com/in/rin", False),
                                        ("rinv.github.io", True)])
def test_only_a_real_site_in_the_blog_field_ties(blog, tied):
    result = admit.assess(candidate(github="rinv"), raw=repos(12), about=about(blog=blog), as_of=AS_OF)
    assert (result.verdict == "admit") is tied


def test_quiet_unmeasured_and_nothing_to_watch():
    assert admit.assess(candidate(github="rinv"), raw=repos(11), about=about(), as_of=AS_OF).verdict == "too_quiet"
    old = admit.assess(candidate(github="rinv"), raw=repos(20, day="2025-06-01"), about=about(), as_of=AS_OF)
    assert (old.verdict, old.github_a_year) == ("too_quiet", 0)  # more than a year ago
    never_pulled = admit.assess(candidate(x_handle="rin_v"), missing=["https://x.com/rin_v"], as_of=AS_OF)
    assert never_pulled.verdict == "unmeasured" and "https://x.com/rin_v" in never_pulled.why
    assert admit.assess(candidate(x_handle="rin_v"), posts=3, as_of=AS_OF).verdict == "too_quiet"
    assert admit.assess(candidate(), as_of=AS_OF).verdict == "skipped"


def test_posts_on_file_count_through_any_context_that_reads_their_profile(store):
    c = candidate(x_handle="rin_v", linkedin_url="https://www.linkedin.com/in/rin-v")
    assert admit.on_file(store, c, AS_OF) == (None, ["https://linkedin.com/in/rin-v", "https://x.com/rin_v"])
    store.put("person-context:x1", "person_context", {"subject_id": "backend:rin", "name": "Rin Vale",
                                                       "anchors": {"x": "https://twitter.com/Rin_V"}})
    for n, day in enumerate(["2026-09-01", "2026-03-01", "2025-06-01"]):
        add_event(store, {"subject_type": "person", "subject_id": "backend:rin", "event_type": "x_post",
                          "event_date": day, "date_precision": "day", "observed_at": f"{day}T12:00:00+00:00",
                          "source_url": f"https://x.com/rin_v/status/{n}", "source_version_hash": str(n),
                          "quote": "shipped it", "tier": 1, "extractor": "test"})
    # The one more than a year ago is not counted; the LinkedIn nobody reads is still unmeasured.
    assert admit.on_file(store, c, AS_OF) == (2, ["https://linkedin.com/in/rin-v"])


def test_someone_on_the_people_file_or_gis_team_is_known_before():
    c = candidate(x_handle="rin_v", github="rinv", linkedin_url="https://www.linkedin.com/in/rin-v")
    assert admit.known([{"person_id": "X1", "x_handle": "RIN_V"}], [], c) == "already on the people file as X1"
    assert admit.known([{"person_id": "X2", "name": "rin vale"}], [], c) == "already on the people file as X2"
    assert admit.known([], [{"name": "Oda", "github": "rinv"}], c) == "on GI's team"
    assert admit.known([], [{"name": "Oda", "linkedin_url": "https://uk.linkedin.com/in/rin-v/"}], c) == "on GI's team"
    assert admit.known([{"person_id": "X3", "x_handle": "other"}], [], c) == ""
    assert admit.same_person(c, candidate(name="", github="RinV"))
    assert admit.known([{"person_id": "X4", "name": "Rin  Vale."}], [], candidate(name="Rin Vale", github="rv2"))


def test_a_proposed_id_never_shares_a_ledger_key_with_the_people_file_or_another_admit():
    admits = [admit.assess(candidate(name=name, github=login, role=role), raw=repos(12), about=about(name=name, blog=site),
                           as_of=AS_OF)
              for name, login, role, site in (("Rin Vale", "rv2", "backend", "a.example"),
                                             ("Rin Vale", "rv3", "backend", "b.example"),
                                             ("Bo Lee", "bo", "mts", "c.example"))]
    rows, identity = admit.proposals(admits, "2026-09-24", taken=["backend:rin-vale", "backend:bo-lee"])
    assert [r["person_id"] for r in rows] == ["backend:rin-vale-2", "backend:rin-vale-3", "mts-research:bo-lee-2"]
    assert {i["person_id"] for i in identity} == {r["person_id"] for r in rows}


def test_admits_become_rows_the_people_file_reads_identity_records_and_a_sheet(tmp_path):
    admitted = admit.assess(candidate(github="rinv", role="mts"), raw=repos(12), about=about(twitter_username="rin_v"),
                            as_of=AS_OF)
    unnamed = admit.assess(candidate(name="王伟", github="wangwei"), raw=repos(12), about=about(name="王伟", blog="wang.example"),
                           as_of=AS_OF)
    quiet = admit.assess(candidate(name="Bo Quiet", github="bo"), raw=repos(2), about=about(name="Bo Quiet"), as_of=AS_OF)
    rows, identity = admit.proposals([admitted, unnamed, quiet], "2026-09-24")
    assert [r["person_id"] for r in rows] == ["mts-research:rin-vale", "backend:wangwei"]  # never an empty id
    assert rows[0] == {"person_id": "mts-research:rin-vale", "name": "Rin Vale", "role": "mts-research",
                       "x_handle": "rin_v", "linkedin_url": "", "github": "rinv", "since": "2026-09-24"}
    people = tmp_path / "people.json"
    people.write_text(json.dumps(rows))
    assert [p.subject_id for p in scorecard.load_moments(people)] == ["mts-research:rin-vale", "backend:wangwei"]
    assert identity[0] == {"person_id": "mts-research:rin-vale", "kind": "identity",
                           "links": ["https://github.com/rinv", "https://x.com/rin_v"],
                           "note": "Their GitHub profile names X @rin_v."}
    text = admit.sheet([admitted, quiet], [("Oda", "on GI's team")], [("Cy", "GitHub answered 404")], "$0.", "2026-09-24")
    assert "### Rin Vale (mts-research)" in text and "- Yes / No:" in text and "- Adds X @rin_v" in text
    assert "- Bo Quiet (backend): fewer than 12" in text and "## Already known (1)\n\n- Oda: on GI's team" in text
    assert "## Not read (1)\n\n- Cy: GitHub answered 404" in text


def test_github_search_keeps_people_and_their_repos_never_organizations():
    def handler(request):
        assert "pushed:>=2026-03-01" in request.url.params["q"] and "fork:false" in request.url.params["q"]
        return httpx.Response(200, json={"items": [
            {"owner": {"login": "rinv", "type": "User"}, "html_url": "https://github.com/rinv/netcode"},
            {"owner": {"login": "acme", "type": "Organization"}, "html_url": "https://github.com/acme/engine"},
            {"owner": {"login": "rinv", "type": "User"}, "html_url": "https://github.com/rinv/rollback"}]})
    found = github.search_owners("rollback netcode", "2026-03-01", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert found == [{"github": "rinv", "evidence_urls": ["https://github.com/rinv/netcode", "https://github.com/rinv/rollback"],
                      "source": "GitHub search: rollback netcode"}]


def cli():
    spec = importlib.util.spec_from_file_location("people_cli", ROOT / "scripts" / "people.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_github(users, limit_after=None, calls=None):
    calls = [] if calls is None else calls

    def handler(request):
        calls.append(request.url.path)
        if limit_after is not None and len(calls) > limit_after:
            return httpx.Response(403, json={"message": "API rate limit exceeded"})
        login = request.url.path.split("/")[2].lower()  # GitHub logins ignore case
        if request.url.path.lower() == f"/users/{login}":
            return httpx.Response(200, json=users[login])
        if request.url.path.endswith("/repos") and request.url.params.get("page") == "1":
            return httpx.Response(200, json=[{**r, "full_name": f"{login}/{r['full_name'].split('/')[1]}"}
                                             for r in repos(12)["repos"]])
        return httpx.Response(200, json=[])
    return httpx.Client(transport=httpx.MockTransport(handler))


def run_admit(tmp_path, store, monkeypatch, rows, users, limit_after=None, calls=None):
    people_cli = cli()
    people = tmp_path / "people.json"
    people.write_text(json.dumps([{"person_id": "backend:known", "name": "Known Person", "role": "backend",
                                   "x_handle": "known", "since": "2026-01-01"}]))
    team = tmp_path / "team.json"
    team.write_text(json.dumps({"members": [{"name": "Omar Lind", "x_handle": "omarlind"}]}))
    found = tmp_path / "found.json"
    found.write_text(json.dumps(rows))
    monkeypatch.setattr(people_cli, "Store", lambda url: store)
    args = Namespace(files=[str(found)], role="backend", people=str(people), team=str(team), db=None, out=None)
    before = people.read_text()
    try:
        people_cli.score(args, fake_github(users, limit_after, calls), "")
    finally:
        assert people.read_text() == before and store.all("contact") == []  # never the people file or the ledger
    day = people_cli.utcnow().date().isoformat()
    return (tmp_path / f"admit-{day}.md").read_text(), json.loads((tmp_path / f"admit-{day}-people.json").read_text())


def test_the_admit_command_checks_who_is_known_with_what_their_github_says(tmp_path, store, monkeypatch):
    users = {"rinv": {"name": "Rin Vale", "twitter_username": "rin_v", "type": "User"},
             "kp-dev": {"name": "Known Person", "twitter_username": "known", "type": "User"},
             "olind": {"name": "O. L.", "twitter_username": "omarlind", "type": "User"},
             "bob": {"name": "Bob Smith", "twitter_username": "known", "type": "User"}}
    calls = []
    sheet, rows = run_admit(tmp_path, store, monkeypatch, [{"github": "rinv"}, {"github": "kp-dev"}, {"github": "olind"},
                                                           {"name": "Rin V.", "github": "RINV"},
                                                           {"name": "Cy Dune", "github": "bob"},
                                                           {"name": "Omar Lind", "github": "omar-l"},
                                                           {"name": "Known Person", "x_handle": "known", "github": "kp-2"}],
                            users, calls=calls)
    assert "### Rin Vale (backend)" in sheet and "- Rin V.: also proposed as Rin Vale" in sheet
    assert "- Known Person: already on the people file as backend:known" in sheet and "- O. L.: on GI's team" in sheet
    # Known by what the list itself says, a GitHub is never read.
    assert "- Omar Lind: on GI's team" in sheet and sheet.count("already on the people file as backend:known") == 2
    assert not any(login in path for path in calls for login in ("omar-l", "kp-2"))
    # A GitHub under another name lends the candidate neither its X nor who it is known as.
    assert "- Cy Dune (backend): check the GitHub is theirs: it is named 'Bob Smith'" in sheet
    assert [r["x_handle"] for r in rows] == ["rin_v"]


def test_the_admit_command_stops_reading_github_at_its_limit_and_lists_the_rest_apart(tmp_path, store, monkeypatch):
    users = {login: {"name": login.title(), "type": "User"} for login in ("ann", "ben", "cat")}
    with pytest.raises(SystemExit, match="hourly limit"):
        run_admit(tmp_path, store, monkeypatch, [{"github": "ann"}, {"github": "ben"}, {"github": "cat"}], users,
                  limit_after=5)
    sheet = next(tmp_path.glob("admit-*.md")).read_text()
    assert "## Not read (2)" in sheet and "- cat: GitHub's hourly limit" in sheet and "Already known" not in sheet
    # A rerun the same day reads only the two it could not, never a star, and writes the whole sheet.
    calls = []
    sheet, rows = run_admit(tmp_path, store, monkeypatch, [{"github": "ann"}, {"github": "ben"}, {"github": "cat"}], users,
                            calls=calls)
    assert {path.split("/")[2] for path in calls} == {"ben", "cat"} and not any("starred" in path for path in calls)
    assert "Not read" not in sheet and all(f"untied: https://github.com/{login}" in sheet for login in ("ann", "ben", "cat"))


def test_a_cache_cut_off_mid_write_is_read_again_not_a_crash(tmp_path, store, monkeypatch):
    (tmp_path / f"admit-{cli().utcnow().date().isoformat()}-github.json").write_text('{"ann": {"about"')
    calls = []
    sheet, _ = run_admit(tmp_path, store, monkeypatch, [{"github": "ann"}], {"ann": {"name": "Ann", "type": "User"}},
                         calls=calls)
    assert any(path.split("/")[2] == "ann" for path in calls) and "untied: https://github.com/ann" in sheet
