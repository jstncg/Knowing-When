import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app import timeline
from app.sources import apify, github, social
from app.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "social"
ADA = social.Person.from_dict({"person_id": "ada", "x_handle": "@ada_builds",
                              "linkedin_url": "https://linkedin.com/in/Ada-Example/"})
# A reference-cohort person: only the window before they joined counts.
BO = social.Person.from_dict({"person_id": "bo", "x_handle": "bo_ops", "since": "2024-09-01", "until": "2025-03-01"})


def load(name):
    return json.loads((FIXTURES / name).read_text())


def test_x_items_become_posts_and_replies_for_their_author_only():
    events, rejected = apify.x_events([ADA, BO], load("x_search.json"))
    by_url = {e["source_url"]: e for e in events}
    assert sorted(e["event_type"] for e in events) == ["x_post", "x_post", "x_post", "x_reply"]
    assert rejected == {"repost": 1, "not_asked_author": 1, "outside_window": 1}
    post = by_url["https://x.com/ada_builds/status/1970000000000000001"]
    assert post["subject_id"] == "ada" and post["tier"] == 1 and post["extractor"] == "apify_x_v1"
    assert post["observed_at"] == "2025-09-13T18:06:27+00:00" and post["event_date"] == "2025-09-13"
    reply = by_url["https://x.com/ada_builds/status/1970000000000000002"]
    assert reply["quote"] == ("@gi_researcher agreed, action-conditioned video is the right pretraining target"
                              "\n\nIn reply to @gi_researcher")
    quoted = by_url["https://x.com/ada_builds/status/1970000000000000004"]["quote"]
    assert quoted == "This is the eval I wanted\n\nIn reply to @lab_x: We are releasing an open benchmark for game agents"
    assert by_url["https://x.com/bo_ops/status/1970000000000000006"]["subject_id"] == "bo"


def test_linkedin_posts_skip_reposts_and_empty_posts():
    events, rejected = apify.linkedin_post_events([ADA], load("linkedin_posts.json"))
    assert rejected == {"not_asked_author": 1, "incomplete": 1}
    [post] = events
    assert post["event_type"] == "linkedin_post" and post["subject_id"] == "ada"
    assert post["observed_at"] == "2025-09-13T08:00:00+00:00"
    assert post["quote"].startswith("We open-sourced our agent eval harness today.")


def test_linkedin_comments_carry_the_post_they_answer():
    events, rejected = apify.linkedin_comment_events([ADA], load("linkedin_comments.json"))
    assert rejected == {"not_asked_author": 1}
    [comment] = events
    assert comment["event_type"] == "linkedin_comment"
    assert comment["quote"] == ("Would love to compare notes on how you handle long-horizon credit assignment here."
                                "\n\nIn reply to Gi Researcher: We are hiring for our world-models team.")
    assert "commentUrn=" in comment["source_url"]


def test_events_land_once_on_the_timeline(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 's.sqlite'}")
    raw = {apify.X_SEARCH: load("x_search.json"), apify.LINKEDIN_POSTS: load("linkedin_posts.json"),
           apify.LINKEDIN_COMMENTS: load("linkedin_comments.json")}
    for _ in range(2):
        events, _ = apify.events([ADA, BO], raw)
        for event in events:
            timeline.add_event(store, event)
    ada = timeline.timeline(store, "ada", "2026-01-01T00:00:00+00:00")
    assert [e["event_type"] for e in ada] == ["linkedin_post", "x_post", "linkedin_comment", "x_reply", "x_post"]
    # A replay before a post was public does not see it.
    assert timeline.timeline(store, "ada", "2025-09-13T12:00:00+00:00")[-1]["event_type"] == "linkedin_post"


def test_each_person_gets_their_own_x_search_over_their_window():
    assert apify.x_input(BO, 100) == {"searchTerms": ["from:bo_ops since:2024-09-01 until:2025-03-01"],
                                      "sort": "Latest", "maxItems": 100}
    assert apify.x_input(ADA, 100)["searchTerms"] == ["from:ada_builds"]


def test_runs_price_the_worst_case_and_skip_comments_for_a_past_window():
    past = social.Person.from_dict({"person_id": "c", "linkedin_url": "https://www.linkedin.com/in/c",
                                   "until": "2025-01-01"})
    planned = apify.runs([ADA, BO, past], 200, "any")
    assert [r[0] for r in planned] == [apify.X_SEARCH, apify.X_SEARCH, apify.LINKEDIN_POSTS, apify.LINKEDIN_COMMENTS]
    assert planned[3][1]["profiles"] == [ADA.linkedin_url]
    assert apify.estimate_usd(planned) == round(400 * 0.0004 + 400 * 0.002 + 200 * 0.002, 2)


def test_a_self_reply_continues_a_thread():
    item = {"url": "https://x.com/ada_builds/status/9", "text": "2/ and the latents are tiny",
            "createdAt": "Sat Sep 13 18:10:00 +0000 2025", "isReply": True, "inReplyToUsername": "ada_builds",
            "author": {"userName": "ada_builds"}}
    [event], _ = apify.x_events([ADA], [item])
    assert event["event_type"] == "x_post" and event["quote"] == "2/ and the latents are tiny"


def test_an_opaque_profile_url_falls_back_to_the_public_identifier():
    item = {"commentary": "Nice", "createdAt": "2025-09-14T22:09:33Z", "linkedinUrl": "https://www.linkedin.com/feed/x",
            "actor": {"linkedinUrl": "https://www.linkedin.com/in/ACoAAA1xyz", "publicIdentifier": "ada-example"}}
    [event], _ = apify.linkedin_comment_events([ADA], [item])
    assert event["subject_id"] == "ada"


def test_person_rejects_what_is_not_a_handle_or_profile():
    with pytest.raises(ValueError):
        social.Person.from_dict({"person_id": "x", "x_handle": "not a handle"})
    with pytest.raises(ValueError):
        social.Person.from_dict({"person_id": "x", "linkedin_url": "https://www.linkedin.com/company/gi"})
    with pytest.raises(ValueError):
        social.Person.from_dict({"person_id": "x", "x_handle": "a", "since": "last spring"})


def test_client_runs_waits_and_reads_the_dataset():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/runs"):
            return httpx.Response(201, json={"data": {"id": "r1", "status": "RUNNING"}})
        if request.url.path == "/v2/actor-runs/r1":
            return httpx.Response(200, json={"data": {"id": "r1", "status": "SUCCEEDED", "defaultDatasetId": "d1",
                                                     "usageTotalUsd": 0.021, "chargedEventCounts": {"result": 1}}})
        return httpx.Response(200, json=[{"id": 1}])

    client = apify.Client("t", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), poll_seconds=0)
    assert asyncio.run(client.run(apify.X_SEARCH, {"searchTerms": []}, max_items=10, max_usd=1.5)) == [{"id": 1}]
    assert calls[0][1] == "/v2/acts/apidojo~tweet-scraper/runs"
    assert calls[0][2]["maxTotalChargeUsd"] == "1.5" and calls[0][2]["maxItems"] == "10"
    assert calls[-1][1] == "/v2/datasets/d1/items"
    assert client.charged == [{"actor": apify.X_SEARCH, "run": "r1", "status": "SUCCEEDED", "usd": 0.021,
                               "events": {"result": 1}}]


def test_a_failed_run_raises():
    def handler(request):
        return httpx.Response(201, json={"data": {"id": "r1", "status": "FAILED"}})

    client = apify.Client("t", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(apify.ApifyError):
        asyncio.run(client.run(apify.LINKEDIN_POSTS, {}, max_items=1, max_usd=1))
    assert client.charged == [{"actor": apify.LINKEDIN_POSTS, "run": "r1", "status": "FAILED", "usd": None, "events": {}}]


def test_a_run_lost_while_polling_is_still_recorded_as_last_seen():
    def handler(request):
        if request.url.path.endswith("/runs"):
            return httpx.Response(201, json={"data": {"id": "r1", "status": "RUNNING", "usageTotalUsd": 0.004}})
        return httpx.Response(503)

    client = apify.Client("t", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), poll_seconds=0)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.run(apify.X_SEARCH, {}, max_items=1, max_usd=1))
    assert client.charged == [{"actor": apify.X_SEARCH, "run": "r1", "status": "RUNNING", "usd": 0.004, "events": {}}]


def test_no_token_no_client():
    with pytest.raises(apify.ApifyError):
        apify.Client("")


def test_country_hosts_are_the_same_profile():
    person = social.Person.from_dict({"person_id": "m", "linkedin_url": "https://uk.linkedin.com/in/Some-One-1b2"})
    assert person.linkedin_url == "https://www.linkedin.com/in/some-one-1b2"


def test_pull_splits_the_cap_by_each_runs_worst_case():
    class Fake:
        caps = []

        async def run(self, actor, actor_input, *, max_items, max_usd):
            self.caps.append((actor, max_usd))
            return [{"actor": actor}]

    fake = Fake()
    planned = apify.runs([ADA, BO], 100, "any")
    raw, failed = asyncio.run(apify.pull(fake, planned, apify.estimate_usd(planned)))
    assert failed == []
    assert dict(fake.caps)[apify.LINKEDIN_POSTS] == 0.2 and len(raw[apify.X_SEARCH]) == 2
    assert [usd for actor, usd in fake.caps if actor == apify.X_SEARCH] == [0.05, 0.05]


def test_github_repos_stars_and_activity_become_dated_events():
    person = social.Person.from_dict({"person_id": "ada", "github": "ada-builds", "since": "2025-09-02"})
    found = {e["quote"]: e for e in github.events(person, load("github.json"))}
    # The repo made before since and the fork are left out; the WatchEvent repeats the star.
    assert list(found) == [
        "Starred worldlab-example/diamond",  # the name only: the description is today's
        "Pushed to ada-builds/brambleworld: add latent action model; fix rollout",
        "I got it running on Mario with 4 GPUs\n\nIn reply to worldlab-example/diamond: Training on new games",
        "Released v0.1 of ada-builds/brambleworld: First playable",
    ]
    star = found["Starred worldlab-example/diamond"]
    assert star["event_type"] == "github_star" and star["observed_at"] == "2025-09-05T12:00:00+00:00"
    assert {e["extractor"] for e in found.values()} == {"github_v2"}


def test_a_repo_opened_later_counts_from_its_opening_and_self_stars_are_skipped():
    person = social.Person.from_dict({"person_id": "ada", "github": "ada-builds", "since": "2025-09-02"})
    repo = {"full_name": "ada-builds/wm", "html_url": "https://github.com/ada-builds/wm", "created_at": "2025-10-01T00:00:00Z"}
    raw = {"repos": [repo], "starred": [{"starred_at": "2025-10-02T00:00:00Z", "repo": repo}],
           "events": [{"type": "PublicEvent", "repo": {"name": "ada-builds/wm"}, "created_at": "2026-01-15T09:00:00Z"}]}
    assert [(e["event_type"], e["observed_at"]) for e in github.events(person, raw)] == [
        ("github_repo", "2026-01-15T09:00:00+00:00")]


def test_github_fetch_pages_and_asks_for_starred_at():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.url.params["page"], request.headers["accept"]))
        return httpx.Response(200, json=[] if request.url.params["page"] != "1" else [{"n": 1}] * 100)

    raw = github.fetch("ada-builds", 150, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(raw["repos"]) == 100 and len(raw["starred"]) == 100
    assert ("/users/ada-builds/starred", "1", "application/vnd.github.star+json") in seen
    assert [p for path, p, _ in seen if path.endswith("/events/public")] == ["1", "2"]


def test_comments_are_skipped_unless_asked_for():
    assert [r[0] for r in apify.runs([ADA], 100)] == [apify.X_SEARCH, apify.LINKEDIN_POSTS]


def test_a_failed_run_keeps_the_others():
    class Fake:
        async def run(self, actor, actor_input, *, max_items, max_usd):
            if actor == apify.LINKEDIN_POSTS:
                raise apify.ApifyError("boom")
            return [{"ok": 1}]

    raw, failed = asyncio.run(apify.pull(Fake(), apify.runs([ADA], 100), 5))
    assert raw == {apify.X_SEARCH: [{"ok": 1}]} and failed[0]["actor"] == apify.LINKEDIN_POSTS


def test_search_hits_name_who_posted():
    [hit] = apify.search_hits(load("linkedin_posts.json")[:1])
    assert hit["linkedin_url"] == "https://www.linkedin.com/in/ada-example" and hit["posted_at"].startswith("2025-09-13")


def test_people_a_windowed_search_left_empty_or_thin_are_re_asked_without_the_end_date():
    items = load("x_search.json") + [{"noResults": True}]
    _, rejected = apify.x_events([ADA, BO], items)
    assert rejected["no_results"] == 1
    quiet = social.Person("q", "quiet_one", since="2025-01-01", until="2025-06-01")
    watch = social.Person("w", "watch_one", since="2026-06-01")  # no end date: empty means empty
    thin_bo = replace(BO, until="2025-03-01")  # 2 tweets of his own: thin
    assert apify.thin([ADA, thin_bo, quiet, watch], items) == [thin_bo, quiet]
    assert apify.thin([thin_bo], items, per_person=2) == []  # a cap below 50 that filled is not thin
    [run] = apify.open_runs([quiet], 400, 10)
    assert run[1]["searchTerms"] == ["from:quiet_one since:2025-01-01"] and run[2] == 1600
    assert apify.open_runs([quiet], 400, 0.24)[0][2] == 600  # the budget binds first
    assert apify.open_runs([quiet], 400, 0.01) == []
    late = {"id": "1", "url": "https://x.com/quiet_one/status/1", "text": "after",
            "createdAt": "Mon Jul 07 12:00:00 +0000 2025", "author": {"userName": "quiet_one"}}
    # The parser cuts at until and drops the repeat a re-ask brings back.
    assert apify.x_events([quiet], [late, late])[1] == {"outside_window": 1, "repeat": 1}
    # The re-ask filled its cap without reaching the window end: the cap ran out first.
    assert apify.short_of_window([quiet], [late], cap=1) == [quiet]
    # Fewer than the cap came back, so the account has nothing older: not short.
    assert apify.short_of_window([quiet], [late], cap=2) == []
