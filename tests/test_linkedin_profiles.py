import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

from app.sources import linkedin_profiles as lp
from app.sources.social import Person
from app.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "social"
ADA = Person.from_dict({"person_id": "ada", "linkedin_url": "https://linkedin.com/in/Ada-Example/"})
BO = Person.from_dict({"person_id": "bo", "linkedin_url": "https://www.linkedin.com/in/bo-ops"})
CY = Person.from_dict({"person_id": "cy", "linkedin_url": "https://www.linkedin.com/in/cy-private"})
DEE = Person.from_dict({"person_id": "dee", "linkedin_url": "https://www.linkedin.com/in/dee-missing"})


def test_profiles_become_current_jobs_with_start_months():
    rows = lp.roles([ADA, BO, CY, DEE], json.loads((FIXTURES / "linkedin_profiles.json").read_text()))
    assert set(rows) == {"ada", "bo", "cy", "dee"}  # the profile nobody asked for is left out
    ada = rows["ada"]
    assert ada["status"] == "current" and ada["linkedin_url"] == "https://www.linkedin.com/in/ada-example"
    assert ada["current"] == [{"title": "Advisor", "company": "Side Lab", "started": "2025", "joined": "2025"},
                              {"title": "Staff Engineer", "company": "Brambleworld", "started": "2024-03", "joined": "2024-03"}]
    assert ada["last_ended"] == {"title": "Engineer", "company": "Old Co", "started": "2020-06", "ended": "2024-02"}
    # A country host and a grouped company block; every job has ended.
    bo = rows["bo"]
    assert bo["status"] == "no_current_role" and bo["current"] == []
    assert bo["last_ended"] == {"title": "Lead Designer", "company": "Grouped Inc", "started": "2022-01", "ended": "2025-08"}
    assert rows["cy"]["status"] == "no_experience" and rows["dee"] == {"linkedin_url": DEE.linkedin_url,
                                                                       "status": "not_returned"}


def test_dates_read_every_shape_the_profile_may_use():
    assert [lp._month(v) for v in ({"month": 3, "year": 2024}, {"month": "September", "year": 2023},
                                   "Sep 2023", "2023-9", "2023", {"text": "Present"}, None)] == \
        ["2024-03", "2023-09", "2023-09", "2023-09", "2023", "", ""]
    assert lp._ended({"endDate": {"text": "Present"}}) == "" and lp._ended({}) == ""
    assert lp._ended({"endDate": {"text": "a while ago"}}) == "unknown"


def test_links_come_from_x_bios_and_github_and_stay_blank_otherwise(tmp_path):
    raw = tmp_path / "pull.json"
    raw.write_text(json.dumps({"apidojo~tweet-scraper": [
        {"author": {"userName": "Ada_Builds", "description": "world models. linkedin.com/in/ada-example",
                    "entities": {"url": {"urls": [{"expanded_url": "https://ada.example"}]}}}},
        {"author": {"userName": "eve_x", "description": "https://www.linkedin.com/in/eve-one"}}]}))
    bios = lp.x_bios([raw, tmp_path / "missing.json"])
    assert bios["ada_builds"] == {"https://www.linkedin.com/in/ada-example"}

    def handler(request):
        if request.url.path == "/users/bo-gh":
            return httpx.Response(200, json={"bio": "designer", "blog": "https://uk.linkedin.com/in/bo-ops"})
        if request.url.path == "/users/ada-gh":
            return httpx.Response(200, json={"bio": "builds worlds", "blog": "  ada.example  "})
        if request.url.path == "/users/eve-gh/social_accounts":
            return httpx.Response(200, json=[{"provider": "linkedin", "url": "https://www.linkedin.com/in/eve-two"}])
        return httpx.Response(200, json=[] if request.url.path.endswith("social_accounts") else {"bio": None, "blog": ""})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    people = [Person.from_dict(r) for r in (
        {"person_id": "ada", "x_handle": "ada_builds"},
        {"person_id": "bo", "github": "bo-gh"},
        {"person_id": "eve", "x_handle": "eve_x", "github": "eve-gh"},
        {"person_id": "fay", "x_handle": "fay_quiet"},
        {"person_id": "known", "linkedin_url": "https://www.linkedin.com/in/known"})]
    found = {p.person_id: lp.github_links(p.github, http)[0] for p in people if p.github}
    # The blog address comes back too, trimmed, for the personal_site hint.
    assert lp.github_links("bo-gh", http) == ({"https://www.linkedin.com/in/bo-ops"}, "https://uk.linkedin.com/in/bo-ops")
    assert lp.github_links("ada-gh", http) == (set(), "ada.example")
    rows = {r["person_id"]: r for r in lp.links(people, bios, found)}
    assert "known" not in rows
    assert rows["ada"] == {"person_id": "ada", "linkedin_url": "https://www.linkedin.com/in/ada-example", "source": "x_bio"}
    assert rows["bo"]["linkedin_url"] == "https://www.linkedin.com/in/bo-ops" and rows["bo"]["source"] == "github"
    # Two places disagree: left blank for a person to settle.
    assert rows["eve"]["linkedin_url"] == "" and rows["eve"]["disagree"] == [
        "https://www.linkedin.com/in/eve-one", "https://www.linkedin.com/in/eve-two"]
    assert rows["fay"] == {"person_id": "fay", "linkedin_url": "", "source": "none found"}


def run_script(*args, tmp_path):
    env = {**os.environ, "SOCIAL_DIR": str(tmp_path), "APIFY_TOKEN": ""}
    return subprocess.run([sys.executable, "scripts/social_pull.py", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True)


def test_profiles_dry_run_prices_and_refuses_past_moment_people(tmp_path):
    people = tmp_path / "people.json"
    people.write_text(json.dumps([{"person_id": "ada", "linkedin_url": "https://www.linkedin.com/in/ada-example"},
                                  {"person_id": "fay", "x_handle": "fay_quiet"}]))
    done = run_script("profiles", "--people", str(people), tmp_path=tmp_path)
    assert done.returncode == 0 and "1 profiles; worst case $0.004 (cap $10.0)" in done.stdout
    assert '"profileScraperMode"' in done.stdout and "Dry run" in done.stdout
    assert not (tmp_path / "linkedin-profiles.json").exists()
    # A replay case (an until date) is skipped, so the people file can be read as it is.
    people.write_text(json.dumps([{"person_id": "ada", "linkedin_url": "https://www.linkedin.com/in/ada-example"},
                                  {"person_id": "bo", "linkedin_url": "https://www.linkedin.com/in/bo-ops",
                                   "since": "2025-01-01", "until": "2025-06-01"}]))
    skipped = run_script("profiles", "--people", str(people), tmp_path=tmp_path)
    assert skipped.returncode == 0 and "Skipped 1 replay cases" in skipped.stdout
    assert "1 profiles; worst case $0.004" in skipped.stdout and "bo-ops" not in skipped.stdout


def test_profiles_re_read_a_saved_pull_free(tmp_path):
    people = tmp_path / "people.json"
    people.write_text(json.dumps([{"person_id": "ada", "linkedin_url": "https://www.linkedin.com/in/ada-example"}]))
    raw = tmp_path / "2026-09-24T010203.json"
    raw.write_text(json.dumps({lp.PROFILE: json.loads((FIXTURES / "linkedin_profiles.json").read_text())}))
    done = run_script("profiles", "--people", str(people), "--raw", str(raw), tmp_path=tmp_path)
    assert done.returncode == 0, done.stderr
    written = json.loads((tmp_path / "linkedin-profiles.json").read_text())
    assert written["pulled_on"] == "2026-09-24" and written["people"]["ada"]["status"] == "current"
    # The latest job's start goes on the timeline, seen the day the profile was read, for the first-year hold.
    assert "1 job starts on the timeline" in done.stdout
    events = Store(f"sqlite:///{tmp_path / 'timelines.sqlite'}").all("timeline_event")
    assert [(e["subject_id"], e["event_type"], e["event_date"], e["observed_at"][:10]) for e in events] == [
        ("ada", "profile_job_started", "2024-03", "2026-09-24")]

    people.write_text(json.dumps([{"person_id": "ada", "linkedin_url": "https://www.linkedin.com/in/ada-old"}]))
    raw.write_text(json.dumps({lp.PROFILE: [{"linkedinUrl": "https://www.linkedin.com/in/ada-example",
                                             "publicIdentifier": "ada-example", "experience": [],
                                             "originalQuery": {"query": "https://www.linkedin.com/in/ada-old"}}]}))
    moved = run_script("profiles", "--people", str(people), "--raw", str(raw), tmp_path=tmp_path)
    assert "MOVED ada: now https://www.linkedin.com/in/ada-example" in moved.stdout


def test_only_the_latest_real_job_becomes_a_start():
    rows = {"ada": {"linkedin_url": "u", "current": [{"title": "Advisor", "company": "Side Lab", "started": "2025-01"},
                                                     {"title": "Designer", "company": "Tiny", "started": "2026-02"}]},
            "bo": {"linkedin_url": "v", "current": [{"title": "Engineer", "company": "Big", "started": "2025"}]},
            "cy": {"linkedin_url": "w", "status": "no_current_role", "current": [],
                   "last_ended": {"title": "Designer", "company": "Gone", "started": "2025-01", "ended": "2026-08"}},
            "dee": {"linkedin_url": "x", "headline": "Staff Designer at Old Co", "current": [
                {"title": "Fellow", "company": "New Program", "started": "2026-09"},
                {"title": "Staff Designer", "company": "Old Co", "started": "2023-04"}]}}
    events = lp.job_events(rows, "2026-09-24")
    assert [(e["subject_id"], e["event_type"], e["event_date"], e["date_precision"]) for e in events] == [
        ("ada", "profile_job_started", "2026-02", "month"),  # never the advisory seat
        ("bo", "profile_job_started", "2025", "year"),  # a year alone still holds (detectors.profile_start)
        ("cy", "profile_job_ended", "2026-08", "month"),  # no job now: lifts an earlier read's hold
        ("dee", "profile_job_started", "2023-04", "month")]  # the job the headline names, as the card reads it
    assert events[0]["quote"] == "Designer at Tiny since 2026-02"


def test_odd_shapes_neither_crash_nor_misread():
    assert lp._month({"year": "abc", "month": 3}) == "" and lp._month({"year": 2024, "month": "13"}) == "2024"
    # An all-empty end date is how scrapers often mark a current job.
    assert lp._ended({"endDate": {"month": None, "year": None, "text": ""}}) == ""
    assert lp._ended({"endDate": "Feb 2024"}) == "2024-02"
    items = [{"linkedinUrl": "https://www.linkedin.com/in/ada-example",
              "experience": [None, "x", {"position": "Lead", "companyName": {"name": "Brambleworld"},
                                         "startDate": None, "endDate": {"year": None, "text": ""}}]},
             {"linkedinUrl": "https://www.linkedin.com/in/ada-example", "experience": "private"},
             {"linkedinUrl": "https://www.linkedin.com/in/j%C3%B6rg-k"}, None]
    joerg = Person.from_dict({"person_id": "jorg", "linkedin_url": "https://www.linkedin.com/in/jörg-k"})
    rows = lp.roles([ADA, joerg], items)
    # The later empty item for Ada does not replace the full one; the encoded slug matches.
    assert rows["ada"]["status"] == "current" and rows["ada"]["current"] == [
        {"title": "Lead", "company": "Brambleworld", "started": "", "joined": ""}]
    assert rows["jorg"]["status"] == "no_experience"


def test_bio_links_take_only_a_standalone_profile_slug():
    assert lp._urls_in({"d": "hire me: linkedin.com/in/ada-example. or https://uk.linkedin.com/in/Bo-X, thanks",
                        "e": ["see\nhttps://www.linkedin.com/in/cy/details/experience/"],
                        "f": "mylinkedin.com/in/zed https://evil.example/?r=linkedin.com/in/zed",
                        "g": "https://m.linkedin.com/in/j%C3%B6rg-k linkedin.com/in/a%2Fb"}) == {
        "https://www.linkedin.com/in/ada-example", "https://www.linkedin.com/in/bo-x",
        "https://www.linkedin.com/in/cy", "https://www.linkedin.com/in/jörg-k"}


def test_links_refuse_past_moment_people(tmp_path):
    people = tmp_path / "people.json"
    people.write_text(json.dumps([{"person_id": "bo", "x_handle": "bo_ops", "since": "2025-01-01", "until": "2025-06-01"}]))
    refused = run_script("links", "--people", str(people), tmp_path=tmp_path)
    assert refused.returncode != 0 and "watchlist only" in refused.stderr


def test_pull_takes_the_found_links_and_one_start_date(tmp_path):
    people, found = tmp_path / "people.json", tmp_path / "links.json"
    people.write_text(json.dumps([{"person_id": "ada", "linkedin_url": "https://www.linkedin.com/in/ada-example",
                                   "since": "2020-01-01"}, {"person_id": "bo", "x_handle": "bo_ops"}]))
    # bo is in the people file without a URL: the links command found it, so it fills in.
    found.write_text(json.dumps([{"person_id": "bo", "linkedin_url": "https://www.linkedin.com/in/bo-ops", "source": "github"},
                                 {"person_id": "fay", "linkedin_url": "", "source": "none found"},
                                 {"person_id": "ada", "linkedin_url": "https://www.linkedin.com/in/not-ada", "source": "x_bio"}]))
    done = run_script("pull", "--people", str(people), "--links", str(found), "--since", "2026-07-25",
                      "--per-person", "50", tmp_path=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "3 people: 1 on X, 2 on LinkedIn" in done.stdout
    assert '"https://www.linkedin.com/in/bo-ops"' in done.stdout and "not-ada" not in done.stdout
    assert '"postedLimitDate": "2026-07-25' in done.stdout
    bad = run_script("pull", "--people", str(people), "--since", "60 days ago", tmp_path=tmp_path)
    assert bad.returncode != 0 and "--since must be a date" in bad.stderr
    profiles = run_script("profiles", "--people", str(people), "--links", str(found), tmp_path=tmp_path)
    assert "2 profiles" in profiles.stdout and "bo-ops" in profiles.stdout


def test_a_redirected_profile_matches_the_address_asked_for_and_reports_where_it_moved():
    tim = Person.from_dict({"person_id": "tim", "linkedin_url": "https://www.linkedin.com/in/tim-old-slug"})
    items = [{"linkedinUrl": "https://www.linkedin.com/in/t-new", "publicIdentifier": "t-new",
              "originalQuery": {"query": "https://www.linkedin.com/in/tim-old-slug"},
              "experience": [{"position": "Researcher", "companyName": "Lab", "startDate": {"month": 1, "year": 2025}}]},
             {"linkedinUrl": "https://www.linkedin.com/in/ACoAAA_invented0000000000000000000000", "originalQuery": {"query": ADA.linkedin_url},
              "experience": []}]
    rows = lp.roles([tim, ADA], items)
    assert rows["tim"]["status"] == "current" and rows["tim"]["moved_to"] == "https://www.linkedin.com/in/t-new"
    assert rows["tim"]["linkedin_url"] == "https://www.linkedin.com/in/tim-old-slug"
    # An opaque member-id address is not a move; a profile at the address asked for has no moved_to.
    assert "moved_to" not in rows["ada"]
    assert "moved_to" not in lp.roles([ADA], json.loads((FIXTURES / "linkedin_profiles.json").read_text()))["ada"]
    # A vanity slug that merely starts like a member id is still a move.
    assert lp.roles([tim], [{**items[0], "publicIdentifier": "acoa-invented-vanity",
                                     "linkedinUrl": "https://www.linkedin.com/in/acoa-invented-vanity"}])[
        "tim"]["moved_to"] == "https://www.linkedin.com/in/acoa-invented-vanity"
    # A short slug in the member-id case is a slug too: only the full-length id is opaque.
    assert lp.roles([tim], [{**items[0], "publicIdentifier": "ACoAshort", "linkedinUrl": None}])[
        "tim"]["moved_to"] == "https://www.linkedin.com/in/acoashort"
    # An encoded capital letter is the same slug, not a move.
    elise = Person.from_dict({"person_id": "elise", "linkedin_url": "https://www.linkedin.com/in/%C3%89lise-k"})
    assert "moved_to" not in lp.roles([elise], [{"publicIdentifier": "élise-k", "experience": [],
                                                  "originalQuery": {"query": elise.linkedin_url}}])["elise"]
    # A sub-page or a non-profile address with no public slug is not a move.
    for odd in ("https://www.linkedin.com/in/tim-old-slug/details/experience/", "https://www.linkedin.com/company/lab"):
        assert "moved_to" not in lp.roles([tim], [{**items[0], "publicIdentifier": None, "linkedinUrl": odd}])["tim"]
    # The item goes to whoever asked for it, even when it now lives at someone else's asked address.
    bo = Person.from_dict({"person_id": "bo", "linkedin_url": "https://www.linkedin.com/in/t-new"})
    rows = lp.roles([tim, bo], items[:1])
    assert rows["tim"]["status"] == "current" and rows["bo"]["status"] == "not_returned"


def test_a_personal_site_is_named_for_a_person_to_check_never_fetched_or_used(tmp_path):
    people = [Person.from_dict({"person_id": "d", "github": "ada-gh"}),
              Person.from_dict({"person_id": "e", "x_handle": "eve_x", "github": "eve-gh"})]
    bios = {"eve_x": {"https://www.linkedin.com/in/eve-one"}}
    rows = {r["person_id"]: r for r in lp.links(people, bios, {}, {"d": "https://ada.example", "e": "eve.example"})}
    assert rows["d"] == {"person_id": "d", "linkedin_url": "", "source": "none found",
                         "personal_site": "https://ada.example"}
    assert "personal_site" not in rows["e"]  # a link was found, so there is nothing to check
    # The profiles step never reads the site as a URL.
    listed, found = tmp_path / "people.json", tmp_path / "links.json"
    listed.write_text(json.dumps([{"person_id": "ada", "linkedin_url": "https://www.linkedin.com/in/ada-example"}]))
    found.write_text(json.dumps(list(rows.values())))
    done = run_script("profiles", "--people", str(listed), "--links", str(found), tmp_path=tmp_path)
    assert "2 profiles" in done.stdout and "ada.example" not in done.stdout
