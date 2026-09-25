"""The X follows pull: the free schema read, then the paid pull on a fake Apify, and how its records reach a route."""

import argparse
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import contact, routing
from app.sources import apify, x_follows
from app.store import Store

ACTOR = {"title": "Following Scraper", "taggedBuilds": {"latest": {"buildId": "B1"}},
         "pricingInfos": [{"pricingModel": "PRICE_PER_DATASET_ITEM", "pricePerUnitUsd": 0.0001, "unitName": "result"}]}
BUILD = {"actorDefinition": {
    "input": {"required": ["user_names"], "properties": {
        "user_names": {"type": "array", "title": "Handles", "description": "X handles, without the @"},
        "getFollowing": {"type": "boolean", "default": False, "description": "  Fetch the accounts they follow  "}}},
    "storages": {"dataset": {"views": {"overview": {"transformation": {"fields": ["screen_name", "source"]}}}}}}}


def test_the_schema_step_reads_an_actors_pricing_and_fields_and_runs_nothing():
    asked = []

    def apify_api(request):
        asked.append((request.method, request.url.path))
        return httpx.Response(200, json={"data": ACTOR if request.url.path.startswith("/v2/acts/") else BUILD})
    client = apify.Client("token", http=httpx.AsyncClient(transport=httpx.MockTransport(apify_api)))
    actor, build = asyncio.run(client.actor("someone~following"))
    assert asked == [("GET", "/v2/acts/someone~following"), ("GET", "/v2/actor-builds/B1")]  # reads only
    s = x_follows.summary("someone~following", actor, build)
    assert s["pricing"] == ["PRICE_PER_DATASET_ITEM: $0.0001 per result"] and s["output_fields"] == ["screen_name", "source"]
    assert s["input"]["getFollowing"]["default"] is False and s["required"] == ["user_names"]
    shown = "\n".join(x_follows.lines(s))
    assert "input user_names (required): array | Handles | X handles, without the @" in shown
    assert "input getFollowing: boolean, default false | Fetch the accounts they follow" in shown


def test_an_older_build_gives_its_schema_as_a_string_and_a_bad_one_reads_as_none():
    older = {"inputSchema": json.dumps(BUILD["actorDefinition"]["input"])}
    assert set(x_follows.summary("a~b", ACTOR, older)["input"]) == {"user_names", "getFollowing"}
    assert x_follows.summary("a~b", {}, {"inputSchema": "not json"})["input"] == {}
    assert "  input: no schema found in the latest build" in x_follows.lines(x_follows.summary("a~b", {}, {}))


def test_only_the_price_in_force_is_shown():
    actor = {"pricingInfos": [
        {"pricingModel": "FLAT_PRICE_PER_MONTH", "pricePerUnitUsd": 20, "startedAt": "2025-01-01T00:00:00Z"},
        {"pricingModel": "PRICE_PER_DATASET_ITEM", "pricePerUnitUsd": 0.0001, "unitName": "result",
         "startedAt": "2026-03-01T00:00:00Z"},
        {"pricingModel": "PRICE_PER_DATASET_ITEM", "pricePerUnitUsd": 0.0003, "unitName": "result",
         "startedAt": "2026-12-01T00:00:00Z"}]}  # a scheduled rise
    assert x_follows.summary("a~b", actor, {}, "2026-09-24T00:00:00Z")["pricing"] == \
        ["PRICE_PER_DATASET_ITEM: $0.0001 per result"]
    assert x_follows.summary("a~b", actor, {}, "2025-06-01T00:00:00Z")["pricing"] == ["FLAT_PRICE_PER_MONTH: $20 per month"]


# ------------------------------------------------------------------ the paid pull, on invented people

ROOT = Path(__file__).resolve().parents[1]
TEAM = {"members": [{"name": "Ana Teammate", "x_handle": "@AnaGI"}, {"name": "Ben Teammate", "x_handle": "bengi"},
                    {"name": "Cal Teammate"}]}
PEOPLE = [{"person_id": "mts-research:rin-vale", "x_handle": "rinvale"},
          {"person_id": "mts-research:sol-park", "x_handle": "@solpark"},
          {"person_id": "backend-engineer:kai-lee", "x_handle": "kailee", "since": "2025-01-01", "until": "2025-06-01"},
          {"person_id": "product-designer:dee-no-x", "linkedin_url": "https://www.linkedin.com/in/dee-no-x"}]
# The actor's schema as the Mac read it on 2026-09-24: every count and switch required, followers' included.
PULL_BUILD = {"actorDefinition": {"input": {
    "required": ["maxFollowers", "maxFollowings", "getFollowers", "getFollowing"], "properties": {
        name: {"type": "string"} for name in ("user_names", "user_ids", "maxFollowings", "getFollowing", "getFollowers")
    } | {"maxFollowers": {"type": "integer", "minimum": 200, "default": 200}}}}}


def files(tmp_path, people=PEOPLE):
    (tmp_path / "people.json").write_text(json.dumps(people))
    (tmp_path / "team.json").write_text(json.dumps(TEAM))
    return tmp_path / "people.json", tmp_path / "team.json"


def script(tmp_path, monkeypatch, client):
    spec = importlib.util.spec_from_file_location("social_pull_script", ROOT / "scripts" / "social_pull.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PRIVATE", tmp_path)
    monkeypatch.setattr(module, "key", lambda name: "token")
    monkeypatch.setattr(module.apify, "Client", lambda token: client)
    return module


PRICED = {"title": "Following", "pricingInfos": [  # per event, the dataset item the only one charged, as on the Mac
    {"pricingModel": "PAY_PER_EVENT", "startedAt": "2026-01-01T00:00:00Z", "pricingPerEvent": {"actorChargeEvents": {
        "apify-default-dataset-item": {"eventPriceUsd": 0.00015, "eventTitle": "user"},
        "apify-actor-start": {"eventPriceUsd": 0}}}}]}


class FakeApify:
    """Apify as the pull sees it: the actor's record and schema, then one run per person."""

    def __init__(self, follows, build=PULL_BUILD, actor=PRICED, undeleted=()):
        self.follows, self.build, self.record, self.asked, self.undeleted = follows, build, actor, [], list(undeleted)
        self.inputs = []

    async def actor(self, actor_id):
        self.asked.append(("schema", actor_id))
        return self.record, self.build

    async def run(self, actor_id, actor_input, *, max_items, max_usd, keep=True):
        self.asked.append(("run", actor_input["user_names"], max_items, max_usd, keep))
        self.inputs.append(actor_input)
        found = self.follows[actor_input["user_names"][0]]
        if isinstance(found, BaseException):
            raise found
        return found


def pull(module, people, team, paid=True, max_usd=10.0, rows_are_follows=False):
    return asyncio.run(module.follows_pull(argparse.Namespace(people=str(people), team=str(team), paid=paid,
                                                              max_usd=max_usd, db=None,
                                                              rows_are_follows=rows_are_follows)))


def follow(handle):
    return {"screen_name": handle, "type": "following"}


def test_the_dry_run_prices_people_times_accounts_times_price_and_spends_nothing(tmp_path):
    people, team = files(tmp_path)
    env = {**os.environ, "SOCIAL_DIR": str(tmp_path), "APIFY_TOKEN": ""}
    done = subprocess.run([sys.executable, "scripts/social_pull.py", "follows", "--people", str(people), "--team",
                           str(team)], cwd=ROOT, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    # Two watchlist people with a handle; Kai's moment is past and Dee has no X. $10, the default, becomes $3.
    assert "2 watchlist people with an X handle x up to 1,000 accounts they follow and 200 follower rows the actor " \
           "may add (dropped) x $0.00015, each run capped at $0.19 = worst case $0.38 (cap $3.0); looking for 2 team " \
           "handles." in done.stdout
    assert '"getFollowers": false' in done.stdout and "Dry run: add --paid to spend." in done.stdout
    assert not (tmp_path / "x-follows.json").exists()


def test_the_paid_pull_checks_the_schema_first_and_keeps_only_team_handles(tmp_path, monkeypatch, capsys):
    people, team = files(tmp_path)
    (tmp_path / "x-follows.json").write_text(json.dumps({"people": {"mts-research:old": {"follows": ["bengi"]}}}))
    client = FakeApify({"rinvale": [follow("AnaGI"), {"user": {"userName": "someone_else"}, "type": "following"},
                                    {"screen_name": "bengi", "type": "follower"}],  # a follower row is not a follow
                        "solpark": [follow(f"stranger{i}") for i in range(x_follows.CAP)]})
    pull(script(tmp_path, monkeypatch, client), people, team)
    assert client.asked[0] == ("schema", x_follows.ACTOR)  # free, before anything paid
    # Each run deletes its dataset on Apify once read: the whole list is kept nowhere.
    # Each run has room for 1,000 follows and the 200 follower rows the actor may add, so no cap cuts a read short.
    assert client.asked[1:] == [("run", ["rinvale"], 1200, 0.19, False), ("run", ["solpark"], 1200, 0.19, False)]
    assert client.inputs[0] == {"user_names": ["rinvale"], "maxFollowings": 1000, "getFollowing": True,
                                "getFollowers": False, "maxFollowers": 200}  # required, so the least the schema allows
    saved = (tmp_path / "x-follows.json").read_text()
    kept = json.loads(saved)["people"]
    assert kept["mts-research:rin-vale"]["follows"] == ["anagi"] and kept["mts-research:rin-vale"]["complete"]
    # Sol follows 1,000 or more and none of the first 1,000 is on the team: unknown, not "follows nobody".
    assert kept["mts-research:sol-park"]["follows"] is None and kept["mts-research:sol-park"]["read"] == 1000
    assert kept["mts-research:old"] == {"follows": ["bengi"]}  # an earlier pull's record stays
    assert "someone_else" not in saved and "stranger" not in saved  # nothing else of anyone's list is kept
    out = capsys.readouterr().out
    assert "mts-research:rin-vale: follows @anagi (2 marked as follows, 1 follower rows dropped, 0 not saying which " \
           "list, 0 other rows; fields: screen_name, type, user)" in out
    assert "unknown: read 1,000, about the cap" in out


def test_a_read_that_names_no_account_says_nothing_and_a_follower_row_is_not_a_follow():
    team = {"anagi"}
    for items in ([], [{"error": "User not found"}], [{"screen_name": "anagi", "type": "followers"}]):
        row = x_follows.result("rin", items, team, 1000, "2026-09-24")
        assert row["follows"] is None and row["read"] == 0
    assert x_follows.result("rin", [follow("someone")], team, 1000, "2026-09-24")["follows"] == []


def test_the_pull_reads_each_account_once_skips_never_and_keeps_what_it_paid_for(tmp_path, monkeypatch, capsys):
    people = [{"person_id": "mts-research:rin-vale", "x_handle": "RinVale"},
              {"person_id": "backend-engineer:rin-vale", "x_handle": "rinvale"},  # the same account, another role
              {"person_id": "mts-research:sol-park", "x_handle": "solpark"},
              {"person_id": "mts-research:ty-quiet", "x_handle": "tyquiet"}]
    people_file, team = files(tmp_path, people)
    ledger = Store(f"sqlite:///{tmp_path / 'timelines.sqlite'}")
    contact.record(ledger, "mts-research:ty-quiet", "never", at="2026-09-01T00:00:00+00:00")
    client = FakeApify({"RinVale": [follow("bengi")], "solpark": KeyboardInterrupt()}, undeleted=["d1"])
    module = script(tmp_path, monkeypatch, client)
    with pytest.raises(KeyboardInterrupt):
        pull(module, people_file, team, max_usd=2.0)
    # Two accounts, not four: Rin once for both roles, Ty not at all.
    assert [a[1] for a in client.asked[1:]] == [["RinVale"], ["solpark"]] and client.asked[1][3] == 0.19
    kept = x_follows.load(tmp_path / "x-follows.json")  # Rin was paid for before the pull stopped: kept
    assert kept["mts-research:rin-vale"]["follows"] == kept["backend-engineer:rin-vale"]["follows"] == ["bengi"]
    assert "mts-research:sol-park" not in kept and "mts-research:ty-quiet" not in kept
    assert "worst case $0.38" in capsys.readouterr().out


def test_the_paid_pull_stops_when_the_price_rose(tmp_path, monkeypatch):
    people, team = files(tmp_path)
    risen = {"pricingInfos": [*PRICED["pricingInfos"], {"pricingModel": "PRICE_PER_DATASET_ITEM",
                                                         "pricePerUnitUsd": 0.0005, "unitName": "item",
                                                         "startedAt": "2026-09-01T00:00:00Z"}]}
    client = FakeApify({}, actor=risen)
    with pytest.raises(SystemExit, match=r"Not run: the price is now PRICE_PER_DATASET_ITEM: \$0.0005 per item"):
        pull(script(tmp_path, monkeypatch, client), people, team)
    assert client.asked == [("schema", x_follows.ACTOR)]
    at = "2026-09-24T00:00:00Z"
    assert x_follows.price_problems(PRICED, at) == []
    per_item = {"pricingInfos": [{"pricingModel": "PRICE_PER_DATASET_ITEM", "pricePerUnitUsd": 0.00015}]}
    assert x_follows.price_problems(per_item, at) == []
    assert x_follows.price_problems({"pricingInfos": [{"pricingModel": "PAY_PER_EVENT"}]}, at)
    start_fee = {"pricingInfos": [{"pricingModel": "PAY_PER_EVENT", "pricingPerEvent": {"actorChargeEvents": {
        "apify-default-dataset-item": {"eventPriceUsd": 0.00015}, "apify-actor-start": {"eventPriceUsd": 0.02}}}}]}
    assert x_follows.price_problems(start_fee, at)  # anything else charged: the worst case would be wrong
    assert x_follows.price_problems({"pricingInfos": [{"pricingModel": "FLAT_PRICE_PER_MONTH", "pricePerUnitUsd": 0}]}, at)


def test_the_paid_pull_refuses_above_the_cap_and_when_the_actor_changed(tmp_path, monkeypatch):
    people, team = files(tmp_path)
    client = FakeApify({})
    module = script(tmp_path, monkeypatch, client)
    with pytest.raises(SystemExit, match=r"worst case \$0.38 is above the cap \$0.2"):
        pull(module, people, team, max_usd=0.2)
    assert client.asked == []
    changed = FakeApify({}, build={"actorDefinition": {"input": {"required": ["handles"], "properties": {
        "handles": {"type": "array"}, "getFollowing": {"type": "boolean"}}}}})
    module = script(tmp_path, monkeypatch, changed)
    with pytest.raises(SystemExit, match="Not run: the actor has no input getFollowers; .*needs handles"):
        pull(module, people, team)
    assert changed.asked == [("schema", x_follows.ACTOR)] and not (tmp_path / "x-follows.json").exists()


def test_a_record_reaches_the_x_route_only_for_the_handle_it_read():
    records = {"mts-research:rin-vale": {"x_handle": "RinVale", "follows": ["anagi"]}}
    assert x_follows.of(records, "mts-research:rin-vale")["follows"] == ["anagi"]
    assert x_follows.of(records, "backend-engineer:rin-vale")["follows"] == ["anagi"]  # the same person, another role
    assert x_follows.of(records, "mts-research:sol-park") is None
    context = SimpleNamespace(anchors={"x": "https://x.com/rinvale"})
    x = next(c for c in routing.on_file([], context, records["mts-research:rin-vale"]) if c.kind == "x")
    assert x.follows == ["anagi"]
    sent_by_ana = routing.channel({"name": "Ana Teammate", "x_handle": "@AnaGI"}, [x], [], "2026-09-24T00:00:00+00:00")
    assert sent_by_ana.kind == "x_dm" and "They follow Ana on X" in sent_by_ana.reason
    by_ben = routing.channel({"name": "Ben Teammate", "x_handle": "bengi"}, [x], [], "2026-09-24T00:00:00+00:00")
    assert by_ben.kind == "x_request" and "since they don't follow Ben there" in by_ben.reason
    other = routing.on_file([], SimpleNamespace(anchors={"x": "https://x.com/rin_new"}), records["mts-research:rin-vale"])
    assert other[0].follows is None  # a pull of an older handle says nothing about this one
    by_hand = routing.ContactRoute(kind="x", value="rinvale", source_url="https://x.com/rinvale", follows=[])
    assert routing.on_file([by_hand], None, records["mts-research:rin-vale"])[0].follows == []  # the file's word wins
    assert x_follows.load(Path("/nonexistent/x-follows.json")) == {}


def test_a_run_that_keeps_nothing_deletes_its_dataset_after_reading_it():
    for delete_status, left in ((204, []), (500, ["D1"])):
        asked = []

        def apify_api(request):
            asked.append((request.method, request.url.path))
            if request.method == "POST":
                return httpx.Response(201, json={"data": {"id": "R1", "status": "SUCCEEDED", "defaultDatasetId": "D1"}})
            if request.method == "DELETE":
                return httpx.Response(delete_status)
            return httpx.Response(200, json=[{"screen_name": "anagi"}])
        client = apify.Client("token", http=httpx.AsyncClient(transport=httpx.MockTransport(apify_api)))
        items = asyncio.run(client.run("a~b", {}, max_items=10, max_usd=0.1, keep=False))
        assert items == [{"screen_name": "anagi"}] and client.undeleted == left  # a failed delete keeps the items
        assert asked[-1] == ("DELETE", "/v2/datasets/D1")


def test_an_actor_that_takes_a_login_is_never_run_even_when_the_field_is_optional():
    fields = ("user_names", "maxFollowings", "maxFollowers", "getFollowing", "getFollowers")
    s = {"input": {name: {"title": "", "editor": ""} for name in fields}, "required": ["user_names"]}
    assert x_follows.schema_problems(s) == []
    s["input"]["cookies"] = {"title": "Your X cookies (optional)", "editor": "json"}
    s["input"]["sessionId"] = {"title": "", "editor": "textfield"}
    assert x_follows.schema_problems(s) == ["the actor takes a login input (cookies): no cookies or logins",
                                            "the actor takes a login input (sessionId): no cookies or logins"]


def test_the_followers_count_the_actor_requires_is_its_least_never_zero_and_never_many():
    s = x_follows.summary("a~b", {}, {"actorDefinition": {"input": {"properties": {
        name: {"type": "integer"} for name in ("user_names", "maxFollowings", "getFollowing", "getFollowers")} | {
        "maxFollowers": {"type": "integer", "minimum": 0}}}}})
    assert x_follows.fewest_followers(s) == 1 and x_follows.fewest_followers() == 1 and x_follows.schema_problems(s) == []
    s["input"]["maxFollowers"]["minimum"] = 5
    assert x_follows.fewest_followers(s) == 5 and x_follows.schema_problems(s) == []
    s["input"]["maxFollowers"]["minimum"] = 200
    assert x_follows.fewest_followers(s) == 200 and x_follows.schema_problems(s) == []  # the actor's floor: budgeted
    s["input"]["maxFollowers"]["minimum"] = 201
    assert x_follows.schema_problems(s) == ["the actor reads at least 201 followers"]


def test_each_run_has_room_for_every_item_and_only_a_following_row_counts():
    assert x_follows.run_usd() == 0.19 and x_follows.worst_usd(10) == 1.9  # 1,200 rows at $0.00015, and a cent
    assert x_follows.worst_usd(15) <= x_follows.MAX_USD < x_follows.worst_usd(16)  # the $3 cap: up to 15 accounts
    rows = [{"screen_name": "anagi", "type": "follower"}, {"screen_name": "bengi", "type": "following"},
            {"screen_name": "cal", "relation": "friends"}, {"screen_name": "dee", "type": "user"}, {"screen_name": "eve"},
            {"screen_name": "fay", "relation": "followers"}]
    everyone = {"anagi", "bengi", "cal", "dee", "eve", "fay"}
    # The rows say which list they are from, so only those marked as follows count: not "user", not unmarked.
    assert x_follows.result("rin", rows, everyone, 1000, "2026-09-24")["follows"] == ["bengi", "cal"]
    # 999 follows: near enough the cap that there may be more; 200 follower rows on top of 500 follows don't count
    assert x_follows.result("rin", [follow(f"s{i}") for i in range(999)], {"anagi"}, 1000, "d")["follows"] is None
    mixed = [{"screen_name": f"s{i}", "type": "following"} for i in range(500)] + \
        [{"screen_name": "anagi", "type": "follower"} for _ in range(200)]
    row = x_follows.result("rin", mixed, {"anagi"}, 1000, "d", limit=1200)
    assert (row["follows"], row["read"], row["complete"]) == ([], 500, True)
    assert x_follows.shape(mixed + [{"error": "x"}]) == ("500 marked as follows, 200 follower rows dropped, 0 not "
                                                         "saying which list, 1 other rows; fields: screen_name, type")
    assert x_follows.result("rin", mixed * 2, {"anagi"}, 1000, "d", limit=1400)["complete"] is False  # hit the limit


def test_login_words_match_whole_word_parts_only():
    for name in ("cookies", "authToken", "x_csrf_token", "ct0", "apiKey", "sessionId", "Your X login", "XSRFToken"):
        assert x_follows._logs_in(name), name
    for name in ("includeAuthorInfo", "Author details", "user_names", "maxFollowings", "getFollowers"):
        assert not x_follows._logs_in(name), name


def test_rows_that_dont_say_which_list_never_count_and_stop_the_pull_after_one_account(tmp_path, monkeypatch, capsys):
    # A teammate who follows Rin, in rows that could be either list: never read as one Rin follows.
    unmarked = [{"screen_name": "AnaGI", "type": "user"}, {"screen_name": "someone"}]
    row = x_follows.result("rin", unmarked, {"anagi"}, 1000, "d", limit=1200)
    assert (row["follows"], row["read"], row["marked"]) == (None, 0, False)
    trusted = x_follows.result("rin", unmarked, {"anagi"}, 1000, "d", limit=1200, trust_unmarked=True)
    assert trusted["follows"] == ["anagi"] and not trusted["marked"]
    people, team = files(tmp_path)
    client = FakeApify({"rinvale": unmarked, "solpark": [follow("bengi")]})
    pull(script(tmp_path, monkeypatch, client), people, team)
    assert [a[1] for a in client.asked[1:]] == [["rinvale"]]  # one run, $0.19 at most, then it stops
    out = capsys.readouterr().out
    assert "0 marked as follows, 0 follower rows dropped, 2 not saying which list" in out and "AnaGI" not in out
    assert "If @rinvale's Following count on X matches the rows not saying which list, they are follows: run again " \
           "with --rows-are-follows." in out
    assert x_follows.load(tmp_path / "x-follows.json")["mts-research:rin-vale"]["follows"] is None
    client = FakeApify({"rinvale": unmarked, "solpark": [follow("bengi")]})
    pull(script(tmp_path, monkeypatch, client), people, team, rows_are_follows=True)
    kept = x_follows.load(tmp_path / "x-follows.json")
    assert kept["mts-research:rin-vale"]["follows"] == ["anagi"] and kept["mts-research:sol-park"]["follows"] == ["bengi"]
    # A protected account (only an error row) names no account, so it never stops the pull.
    client = FakeApify({"rinvale": [{"error": "Not authorized"}], "solpark": [follow("bengi")]})
    pull(script(tmp_path, monkeypatch, client), people, team)
    assert [a[1] for a in client.asked[1:]] == [["rinvale"], ["solpark"]]


def test_an_actor_that_marks_only_its_follower_rows_stops_the_pull_and_the_check_recovers_it(tmp_path, monkeypatch):
    rows = [{"screen_name": "AnaGI", "type": "follower"}] + [{"screen_name": f"s{i}"} for i in range(3)] + \
        [{"screen_name": "bengi"}]
    row = x_follows.result("rin", rows, {"anagi", "bengi"}, 1000, "d", limit=1200)
    assert (row["follows"], row["read"], row["marked"]) == (None, 0, True)
    trusted = x_follows.result("rin", rows, {"anagi", "bengi"}, 1000, "d", limit=1200, trust_unmarked=True)
    assert (trusted["follows"], trusted["read"]) == (["bengi"], 4)  # the follower row stays out
    people, team = files(tmp_path)
    client = FakeApify({"rinvale": rows, "solpark": [follow("bengi")]})
    pull(script(tmp_path, monkeypatch, client), people, team)
    assert [a[1] for a in client.asked[1:]] == [["rinvale"]]
