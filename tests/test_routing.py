"""Routing: warm path, channel pick, the draft and the Slack card, over invented people."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import outreach, routing
from app.detectors import PAGE_EXTRACTOR
from app.readiness import Readiness
from app.store import Store
from app.timeline import add_event

FIXTURES = Path(__file__).parent / "fixtures" / "routing"
AS_OF = "2026-09-15T00:00:00+00:00"
TEAM = json.loads((FIXTURES / "team.json").read_text())["members"]
CONTACTS = json.loads((FIXTURES / "contacts.json").read_text())["A900"]
DANA, OMAR, INES = TEAM


@pytest.fixture
def store(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'r.sqlite'}")
    routing.seed(s, json.loads((FIXTURES / "timeline.json").read_text()))
    return s


def query(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def texts(card):
    return "\n".join(b["text"]["text"] if b.get("text") else e["text"] for b in card["blocks"]
                     for e in (b.get("elements") or [b]) if b.get("text") or b["type"] == "context")


def test_a_coauthor_on_the_team_writes_the_draft_themselves(store):
    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    assert r.call.action == "reach_now"
    assert [(p.kind, p.teammate.name) for p in r.paths] == [("coauthor", "Dana Kest"), ("overlap", "Omar Lind")]
    assert r.paths[1].detail == "Omar Lind and they were both at Acme Research in 2022-2023"
    # A real tie beats the table: Dana writes, not Omar (who writes for the role), and says what they did together.
    assert r.sender["name"] == "Dana Kest" and r.sender["why"].endswith("Dana has worked with them directly")
    assert r.channel.kind == "email" and query(r.channel.url)["body"] == r.draft["body"]
    body = r.draft["body"]
    # Their accepted paper came after the work together: "since", then the paper, never an older one.
    # A public moment: the note opens with congratulations that name the paper.
    assert body.startswith("Hey Rin, congrats on getting 'Scaling laws II' accepted at NeurIPS 2026! It really caught "
                           "my eye. Good to see what you've done since we wrote 'Action-conditioned world models' "
                           "together.")
    assert r.message == "accepted" and r.kind == "just_happened"
    assert body.endswith("\n\nDana") and r.outreach["cites"] == [r.call.evidence["new_work"][0]]  # the paper
    assert r.outreach["checks"]["passes"] and query(r.channel.url)["authuser"] == "dana@gi.example"  # Dana's own mail
    assert r.draft["note"] == "" and "Nothing is sent until a person presses send." in json.dumps(r.card)
    # A warm intro: a Slack message asking Dana, in plain words, carrying the draft, since Dana never sees the card.
    assert r.ask["to"] == "Dana Kest" and r.ask["slack_user_id"] == "U0DANA"
    assert r.ask["body"] == (
        "Hey Dana, Rin had a paper accepted on Aug 20 and GI's own work cited their work on Sep 10. You and Rin wrote "
        "'Action-conditioned world models' together (Nov 2025), so a note from you would land far better than a cold "
        "one. We're hiring for our Member of Technical Staff role: would you be up for reaching out to Rin? Here's a "
        "draft you can use or rewrite (an email to rin@rinvale.example, subject “Scaling laws II”):\n\n" + body)
    shown = texts(r.card)
    assert "*Route in*\nWarm path: ask Dana Kest for an intro. Dana wrote 'Action-conditioned world models' with Rin " \
           "(Nov 2025).\nOmar may know Rin too (both at Acme Research, 2022-2023).\n" \
           "Dana can email Rin at the address on rinvale.example, or use LinkedIn if they're connected." in shown
    # The draft is shown once, with its subject; the Slack ask to Dana shows its opening and points back to it.
    assert "*Draft: congratulations on the acceptance*\nEmail from Dana to rin@rinvale.example\n*Subject:* Scaling laws II" \
        in shown and shown.count(body) == 1
    assert "*Message <@U0DANA> on Slack*" in shown and "Then paste the draft above." in shown
    assert "Open draft" not in json.dumps(r.card)  # the draft opens in Dana's account, from Dana's message
    assert "*What would prove it wrong:* Rin gets promoted or starts a new role elsewhere before Oct 19." in shown
    assert ("*Confidence*\nHigh by the engine's own rules, not a measured rate. Not yet tested on past hires for this "
            "role.") in shown
    assert "Draft checked" not in shown and "free template" not in shown  # no machinery on the card
    # Plain words only: none of the engine's own labels on the card, in the ask or in the draft.
    for text in (shown, body, r.ask["body"]):
        assert "timing engine" not in text and not any(reason in text for reason in r.call.reasons)
        assert "2026-" not in text.replace("NeurIPS 2026", "")  # people's dates, not ISO ones


def test_a_release_a_build_made_is_never_offered_to_the_draft_as_their_work(store):
    url = "https://github.com/rinvale/scraper/releases/tag/rules-20260811-0932"
    for kind, quote in (("github_activity", "Released rules-20260811-0932 of rinvale/scraper: Rules update"),
                        ("project_release", "Rules update for the scraper")):
        add_event(store, {"subject_type": "person", "subject_id": "A900", "event_type": kind,
                          "event_date": "2026-09-13", "date_precision": "day",
                          "observed_at": "2026-09-13T12:00:00+00:00", "quote": quote, "tier": 1, "extractor": "test",
                          "source_url": url, "source_version_hash": kind})
    offered = []

    def writer(name, role, track, cited, *rest, **kw):
        offered.extend(cited)
        return None  # the checked template writes it

    assert routing.route(store, "A900", AS_OF, TEAM, CONTACTS, writer=writer).call.action == "reach_now"
    assert offered and url not in {i["url"] for i in offered}


def test_the_tie_says_since_only_when_what_it_names_came_later():
    role = {"id": "mts-research", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    tie = routing.Path(kind="coauthor", level="confirmed", teammate=routing.Teammate(**DANA), detail="",
                       work="Action-conditioned world models", latest="2025-11-10")
    older = [{"event_type": "coauthor_link", "event_date": "2025-02-01", "quote": "Sam Peer (A1) on 'Scaling laws'"}]
    body = routing.template("Rin Vale", role, None, older, sender_name="Dana Kest", tie=tie)["body"]
    assert body.startswith("Hey Rin, we wrote 'Action-conditioned world models' together in November 2025. Saw "
                           "'Scaling laws' from February 2025")


def test_how_sure_says_the_level_is_the_rule_and_a_rate_only_when_measured():
    def sure(level, **c):
        return routing._sure(type("R", (), {"confidence": c, "call": type("C", (), {"confidence": level})})())
    assert sure("medium", measured=False) == ("Medium by the engine's own rules, not a measured rate. Not yet tested "
                                              "on past hires for this role.")
    shown = sure("high", measured=True, beats_ordinary=True, before_moment=[3, 5], ordinary=[2, 20])
    assert shown.startswith("High by the engine's own rules, not a measured rate. On past people") \
        and shown.endswith("came before 3 of 5 of their public moments and in only 2 of 20 quiet stretches.")
    # Not yet shown to predict: the counts as seen, never a comparison 3 of 5 against 3 of 9 would contradict, and
    # the bar it actually missed, never one it met. How it can go wrong, listed before the fix (a live card read "2 of
    # 3 ... 0 of 6: not enough to count it as a sign (the bar is twice as often ...)", far more than twice):
    #   1. It names the lift when only p failed (2 of 3 against 0 of 6, p=0.08): too few cases, not too rare.
    #   2. It names p when only the lift failed (12 of 15 against 20 of 45, p=0.017).
    #   3. Both failed (3 of 5 against 3 of 9): both are named.
    #   4. No quiet stretches to compare, or never seen before a moment: neither bar can be met; say why.
    def missed(before, ordinary, lift, p):
        return sure("high", measured=True, beats_ordinary=False, before_moment=before, ordinary=ordinary, lift=lift, p=p)
    seen = "On past people for this role this kind of sign was seen before a public moment "
    assert missed([2, 3], [0, 6], None, 0.083).endswith(
        seen + "2 of 3 times and in a quiet stretch 0 of 6 times: too few cases to be sure yet (p=0.08, and the bar is "
        "p under 0.05).")
    assert missed([12, 15], [20, 45], 1.8, 0.017).endswith(
        seen + "12 of 15 times and in a quiet stretch 20 of 45 times: not twice as often as in a quiet stretch, the bar "
        "for counting it as a sign.")
    assert missed([3, 5], [3, 9], 1.8, 0.41).endswith(
        seen + "3 of 5 times and in a quiet stretch 3 of 9 times: not enough to count it as a sign (the bar is twice as "
        "often as in a quiet stretch, at p under 0.05).")
    assert missed([1, 4], [0, 0], None, None).endswith(
        seen + "1 of 4 times: no quiet stretches to compare it against yet, so it doesn't count as a sign.")
    assert missed([0, 4], [2, 8], 0.0, 1.0).endswith(
        seen + "0 of 4 times and in a quiet stretch 2 of 8 times: never before a moment, so it doesn't count as a sign.")
    assert "more common" not in missed([1, 4], [5, 20], 0.8, 0.6)


def test_a_run_out_of_model_calls_falls_back_to_the_checked_template(store):
    def writer(*args, **kwargs):
        raise routing.providers.CallLimitReached("30 calls")
    r = routing.route(store, "A900", AS_OF, [OMAR], CONTACTS, writer=writer)
    assert r.outreach["model"] == "template" and r.outreach["fallback"]
    assert "Don't send this draft yet" not in texts(r.card)  # the template is checked the same way, and passes


def test_without_a_coauthor_the_public_email_gets_a_direct_draft(store):
    r = routing.route(store, "A900", AS_OF, [OMAR], CONTACTS)
    assert r.sender["name"] == "Omar Lind" and r.sender["why"] == "closest to their work: both work on scaling laws"
    assert r.channel.kind == "email" and "https://rinvale.example/about" in r.channel.reason
    assert query(r.channel.url)["to"] == "rin@rinvale.example"
    body = r.draft["body"]
    assert body.startswith("Hey Rin, congrats on getting 'Scaling laws II' accepted at NeurIPS 2026! It really caught "
                           "my eye.")
    assert r.ask is None and "*Message" not in texts(r.card)  # no one worked with them: no one to ask
    assert "our Member of Technical Staff role (https://jobs.ashbyhq.com/" in body
    assert body.endswith("if you're interested.\n\nOmar")
    assert "General Intuition teaches agents spatial reasoning" in body  # the role's own GI fact, not a stock line
    # The engine's inference stays on the card: the message never mentions departures or detectors.
    assert "coauthor_departure" not in body and "left" not in body


def test_a_work_dated_by_its_year_alone_or_undated_still_opens_the_draft():
    role = {"id": "mts-research", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    for day, opener in (("2025", "saw 'World models' from 2025,"), ("2025-03", "saw 'World models' from March 2025,"),
                        (None, "saw 'World models',")):
        events = [{"event_type": "coauthor_link", "event_date": day, "quote": "Dana Kest (A1) on 'World models'"}]
        draft = routing.template("Rin Vale", role, None, events)
        assert draft["body"].startswith("Hey Rin, " + opener) and draft["body"].endswith("\n\nJustin")
        assert draft["note"].startswith("Hey Rin, " + opener.rstrip(",")) and len(draft["note"]) <= 200


def test_the_template_never_quotes_a_post_about_a_layoff():
    role = {"id": "mts-research", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    post = {"quote": "Our team was disbanded today, so I'm open-sourcing the netcode", "event_date": "2026-09-10",
            "source_url": "https://x.com/rin/status/1"}
    events = [{"event_type": "coauthor_link", "event_date": "2025", "quote": "Dana Kest (A1) on 'World models'"}]
    body = routing.template("Rin Vale", role, None, events, post)["body"]
    assert "disbanded" not in body and body.startswith("Hey Rin, saw 'World models' from 2025")


def test_a_quoted_post_is_never_dated_in_the_draft_nor_by_a_date_it_names():
    role = {"id": "mts-research", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    events = [{"event_type": "coauthor_link", "event_date": "2025", "quote": "Dana Kest (A1) on 'World models'"}]
    said = {"id": "p1", "subject_id": "A900", "event_type": "self_stated_availability", "event_date": "2026-11",
            "observed_at": "2026-09-01T10:00:00+00:00", "source_url": "https://x.com/rin/status/1",
            "quote": "Wrapping up my postdoc: available for full-time roles from November"}
    body = routing.template("Rin Vale", role, None, events, said, message="open_to_work")["body"]
    assert body.startswith('Hey Rin, saw your X post: "Wrapping up my postdoc')  # its day depends on a time zone
    assert "from November 2026" not in body  # November is when they're free, not when they said it
    sign = {**said, "event_type": "work_in_progress", "event_date": "2026-12",
            "quote": "Training a world model on game clips, demo in December"}
    assert "saw your X post:" in routing.template("Rin Vale", role, None, events, sign)["body"]
    launch = {**said, "event_type": "launch_announced", "event_date": "2026-10-15",
              "quote": "Launching clipcache 1.0 on October 15"}
    body = routing.template("Rin Vale", role, None, events, news=launch)["body"]
    assert "Saw your X post:" in body and "October 2026" not in body and "Sep 1" not in body


def test_the_card_and_the_paid_writer_date_a_saying_by_when_it_went_public():
    from app import outreach

    call = Readiness(as_of=AS_OF, score=0.5, action="reach_now", families={}, reasons=["self_stated_availability"],
                     holds=[], open_windows=[], earliest_close=None, explanation="",
                     evidence={"self_stated_availability": ["p1"]}, track="pitch")
    since = {"id": "p1", "subject_id": "A900", "event_type": "self_stated_availability", "event_date": "2026-08",
             "observed_at": "2026-09-09T10:00:00+00:00", "source_url": "https://x.com/rin/status/1",
             "quote": "Open to new research roles since August", "extractor": "test"}
    [shown] = routing.signals(call, {"p1": since})
    assert shown["date"] == "2026-09-09"  # the day they said it, not the August they named
    sign = {**since, "id": "p2", "event_type": "work_in_progress", "event_date": "2026-12",
            "extractor": "claude_extract_v1:posts:v3:abc:def:small", "quote": "Demo of my world model lands in December"}
    assert outreach.said_on(sign) == "2026-09-09"  # read from their post: dated by the post
    paper = {**since, "id": "p3", "event_type": "paper_v1", "event_date": "2026-09-02", "source_url": "https://arxiv.org/abs/1"}
    assert outreach.said_on(paper) == "2026-09-02"  # a record seen later keeps its own date
    view = [since, sign]
    dated = {i["url"]: i["date"] for i in outreach.items(view, "A900", call)}
    assert dated == {"https://x.com/rin/status/1": "2026-09"}  # what the paid writer is told: the month, never the day
    late = {**since, "id": "p4", "event_type": "linkedin_post", "event_date": "2026-08-03",
            "observed_at": "2026-09-12T08:00:00+00:00", "quote": "Shipped the new level editor"}
    assert outreach.said_on(late) == "2026-08-03"  # a LinkedIn post that reached us later: the day it was posted
    assert outreach.said_on({**late, "event_type": "work_in_progress", "extractor": sign["extractor"]}) == "2026-08-03"
    started = {**since, "id": "p5", "event_type": "job_started", "event_date": "2024-09", "extractor": sign["extractor"],
               "quote": "Two years at Rift today"}
    bought = {**started, "id": "p6", "event_type": "acquisition", "event_date": "2025-09",
              "quote": "Rift was acquired a year ago"}
    call = call.model_copy(update={"reasons": ["tenure_milestone", "retention_cliff", "acquired"],
                                   "evidence": {"tenure_milestone": ["p5"], "retention_cliff": ["p6"], "acquired": ["p6"]}})
    assert [s["date"] for s in routing.signals(call, {"p5": started, "p6": bought})] == \
        ["2024-09", "2025-09", "2026-09-09"]  # when the role started and the deal closed; the post about the deal


def test_a_post_about_something_personal_is_never_quoted_on_the_card_or_in_the_draft():
    role = {"id": "mts-research", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    post = {"id": "p1", "subject_id": "A900", "event_type": "x_post", "event_date": "2026-09-10",
            "observed_at": "2026-09-10T00:00:00+00:00", "source_url": "https://x.com/rin/status/1",
            "quote": "Back from parental leave and prototyping a world model for the kids' games"}
    events = [{"event_type": "coauthor_link", "event_date": "2025", "quote": "Dana Kest (A1) on 'World models'"}]
    body = routing.template("Rin Vale", role, None, events, post)["body"]
    assert "parental" not in body and body.startswith("Hey Rin, saw 'World models' from 2025")
    call = Readiness(as_of=AS_OF, score=0.5, action="reach_now", families={}, reasons=["reason"], holds=[],
                     open_windows=[], earliest_close=None, explanation="", evidence={"reason": ["p1"]}, track="rapport")
    [shown] = routing.signals(call, {"p1": post})
    assert shown["quote"] == routing.WITHHELD and shown["source_url"] == post["source_url"]  # dated and linked, not quoted
    paper = {**post, "id": "p2", "event_type": "paper_v1", "quote": "Depression and play: a world model study"}
    assert routing.signals(call.model_copy(update={"evidence": {"reason": ["p2"]}}), {"p2": paper})[0]["quote"] == \
        paper["quote"]  # a paper's title is its title
    drafted = routing.template("Rin Vale", role, None, events, news=paper)
    assert drafted["body"].startswith('Hey Rin, congrats on the new paper! Saw your X post: "Depression and play')
    titled = [{"id": "p2", "date": "2026-09-10", "url": paper["source_url"], "text": paper["quote"], "context": "",
               "title": True}]
    assert "touches something personal" not in outreach.check({**drafted, "subject": ""}, titled, role, [])["problems"]
    sign = {**post, "id": "p3", "event_type": "work_in_progress", "quote": "election-night world models for my church group"}
    [read] = routing.signals(call.model_copy(update={"evidence": {"reason": ["p3"]}}), {"p3": sign})
    assert read["quote"] == routing.WITHHELD  # a sign read from their post is their words too
    assert "· reason · (not quoted: it touches something personal) <" in routing._signal_lines([read])  # no quote marks


def test_channel_is_a_dm_when_they_follow_the_sender_then_email_then_where_they_post_most():
    message = {"subject": "s", "body": "hello", "note": "hi"}
    omar = {"name": "Omar Lind", "x_handle": "OmarLind"}
    x = {"kind": "x", "value": "@rin", "source_url": "https://x.com/rin", "x_user_id": "42"}
    email = {"kind": "email", "value": "rin@rin.example", "source_url": "https://rin.example"}
    linkedin = {"kind": "linkedin", "value": "https://www.linkedin.com/in/rin", "source_url": "https://rin.example"}
    posts = [{"event_type": kind, "observed_at": "2026-09-01T00:00:00+00:00"} for kind in ("linkedin_post",) * 2 + ("x_post",)]

    def pick(*contacts, events=(), who=omar):
        routes = [routing.ContactRoute.model_validate(c) for c in contacts]
        return routing.fill(routing.channel(who, routes, list(events), AS_OF), who, routes, message)

    dm, draft = pick({**x, "follows": ["@omarlind"]}, email)  # they follow Omar: a DM lands in their inbox
    assert dm.kind == "x_dm" and query(dm.url) == {"recipient_id": "42", "text": "hello"} and draft["subject"] == "s"
    mail, draft = pick({**x, "follows": []}, email, linkedin)
    assert mail.kind == "email" and draft["subject"] == "s" and draft["note"] == "" and "Omar checks LinkedIn" in mail.check_first
    on_linkedin, draft = pick(x, linkedin, events=posts)  # no email: where they post most
    assert on_linkedin.kind == "linkedin" and "2 posts in 90 days" in on_linkedin.reason and draft["note"] == "hi"
    assert pick(x, linkedin, events=posts[1:])[0].kind == "x_request"  # one post each: X on a tie
    request, _ = pick(x, linkedin, events=posts[2:])
    assert request.kind == "x_request" and "unless they follow Omar, which isn't pulled yet" in request.reason
    assert "since they don't follow Omar there" in pick({**x, "follows": ["someone"]})[0].reason
    no_x = pick({**x, "follows": []}, who={"name": "Kai Moss", "x_handle": ""})[0].reason
    assert "Kai has no X account in the team file" in no_x and "follow" not in no_x  # can't know without a handle
    # A sender with no X account writes where they can: LinkedIn, even when the person posts more on X.
    noor = {"name": "Noor Hale", "x_handle": ""}
    on_x = [{"event_type": "x_post", "observed_at": "2026-09-01T00:00:00+00:00"}] * 3 + posts[:1]
    chosen = pick(x, linkedin, events=on_x, who=noor)[0]
    assert chosen.kind == "linkedin" and "they post most on X (3 posts in 90 days), but Noor has no X account in the " \
        "team file, so LinkedIn (1 post in 90 days)" in chosen.reason
    quiet = pick(x, linkedin, who=noor)[0]
    assert quiet.kind == "linkedin" and "Noor has no X account in the team file, so their LinkedIn" in quiet.reason
    old = [{**e, "observed_at": "2026-01-01T00:00:00+00:00"} for e in posts]  # more than 90 days before: not counted
    assert pick(x, linkedin, events=old)[0].kind == "x_request"
    site = pick({"kind": "site", "value": "rin.example", "source_url": "https://rin.example"})[0]
    assert site.kind == "site" and site.url == "https://rin.example"  # a bare host is made a link
    assert pick({**linkedin, "value": "rin"}, events=posts)[0].url == "https://www.linkedin.com/in/rin"
    assert pick({**linkedin, "value": "Rin Vale"}, events=posts)[0].url == "https://www.linkedin.com/in/Rin%20Vale"
    assert pick({**linkedin, "value": "HTTPS://www.linkedin.com/in/rin/"}, events=posts)[0].url == \
        "https://www.linkedin.com/in/rin/"
    assert pick()[0].kind == "none" and pick()[0].url == ""


def test_a_linkedin_card_shows_the_note_and_the_message(store):
    routes = CONTACTS[1:]  # no email: LinkedIn when that is where they post
    routing.seed(store, {"events": [{"subject_id": "A900", "event_type": "linkedin_post", "event_date": "2026-09-01",
                                     "quote": "Shipping the action-conditioned sampler", "source_url": "https://li.example/1"}]})
    r = routing.route(store, "A900", AS_OF, [OMAR], routes)
    assert r.channel.kind == "linkedin" and r.draft["note"] and len(r.draft["note"]) <= 200
    shown = texts(r.card)
    assert f"Connection note ({len(r.draft['note'])} of 200 characters)" in shown and "Message, once connected" in shown
    # Once connected: a follow-up in the same thread as the note, never the note again, and still about their work.
    assert r.draft["after"].startswith("Thanks for connecting, Rin! ") and "```Thanks for connecting, Rin! " in shown
    assert routing.outreach.repeated(r.draft["note"], r.draft["after"]) == ""
    assert "Scaling laws II" in r.draft["after"]  # what the body said of them, the note too short for it
    assert not any("connection note again" in p for p in r.outreach["checks"]["problems"])
    # A note too short for their words asks only to connect: the follow-up then carries what the body said of them.
    body = ('Hey Mara, saw your X post: "Taught a tiny model to guess the next frame of my kart replays." It really caught my eye. '
            "General Intuition builds world models. We're hiring for our Member of Technical Staff role "
            "(https://jobs.example.test/mts), and I'd love to chat more if you're interested.\n\nOda")
    note = "Hey Mara, we're hiring for our Member of Technical Staff role at General Intuition, and I'd love to connect."
    role = {"id": "mts-research", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    assert outreach.follow_up(body, note, "Mara Quill", role) == (
        'Thanks for connecting, Mara! Saw your X post: "Taught a tiny model to guess the next frame of my kart replays." It really '
        "caught my eye. General Intuition builds world models. Here's the role if you'd like a look: "
        "https://jobs.example.test/mts. I'd love to chat more if you're interested.\n\nOda")
    # A quote of theirs goes or stays whole, however many sentences it holds; a role with no link gets no link line.
    two = body.replace("of my kart replays.", "of my kart replays. It learns the enemy AI.")
    said = note.replace("we're hiring", 'saw your X post: "Taught a tiny model to guess the next frame of my kart replays. It learns '
                                      'the enemy AI." We\'re hiring')
    after = outreach.follow_up(two, said, "Mara Quill", {**role, "jd_url": ""})
    assert "enemy AI" not in after and '"' not in after and "role if you'd like a look" not in after, after
    # The follow-up is checked as the note is: what it says of them, and never the role when the draft may not name it.
    problems = outreach.check({"subject": "Kart replays", "body": body, "note": note, "after": after + " Hope your kids are well."},
                              [], role, [], (), "Mara Quill", AS_OF, "Oda")["problems"]
    assert "LinkedIn follow-up: touches something personal" in problems
    undergrad = {"subject": "Kart replays", "body": body.split(" We're hiring")[0], "note": note.split(", we're")[0] + ".",
                 "after": after + " Here is our Member of Technical Staff role."}
    assert outreach.ROLE_FREE_WHY in outreach.check(undergrad, [], role, [], (), "Mara Quill", AS_OF, "Oda",
                                                   "undergraduate")["problems"]
    titled = [{"id": "p1", "date": "2026-09", "url": "https://openreview.net/forum?id=x1", "context": "", "title": True,
               "text": "World Models from Children's Play"}]
    congrats = {"subject": "Your paper", "body": body, "note": note, "after": "Thanks for connecting, Mara! Congrats on "
                "'World Models from Children's Play'! General Intuition builds world models.\n\nOda"}
    assert not any("personal" in p for p in outreach.check(congrats, titled, role, [], (), "Mara Quill", AS_OF,
                                                           "Oda")["problems"])  # a title is their work
    assert "Open channel" in json.dumps(r.card)
    empty = routing.card(r.model_copy(update={"draft": {**r.draft, "note": ""}}))
    assert "No connection note: connect without one" in texts(empty)


def test_who_writes_follows_the_table_by_role_and_seniority():
    team = [routing.Teammate(**m) for m in TEAM] + [routing.Teammate(name="Kai Moss", title="COO", signs_for=["controller"])]
    work = routing.outreach.text_of("Scaling laws for small world models")
    assert routing.sender([], [], team, "controller", work)["name"] == "Kai Moss"
    both = team + [routing.Teammate(name="Lea Park", signs_for=["mts-research"], works_on=["world model", "Doom"])]
    doom = routing.outreach.text_of("Scaling laws for a Doom world model")  # both match twice; Doom only Lea lists
    assert routing.sender([], [], both + [routing.Teammate(name="Max Ode", signs_for=["mts-research"],
                                                           works_on=["world model", "scaling laws"])],
                          "mts-research", doom)["name"] == "Lea Park"
    assert routing.sender([], [], team, "mts-research", work)["why"] == "closest to their work: both work on scaling laws"
    other = routing.outreach.text_of("Diffusion for driving")
    assert routing.sender([], [], team, "mts-research", other)["why"] == \
        "the team file names them for this role, and no writer's own work matches theirs"
    senior = routing.seniority([event("job_started", "Rin Vale joined Pine Lab as Staff Research Scientist")])
    assert senior.startswith("their record says") and routing.seniority([event("job_started", "Joined as a PhD intern")]) == ""
    for junior in ("Joined OpenAI as Member of Technical Staff", "PhD student in Professor Kim's lab",
                   "Lead author on the Doom world model paper"):
        assert routing.seniority([event("job_started", junior)]) == "", junior
    ceo = routing.sender([], [], team, "mts-research", work, senior)
    assert ceo["name"] == "Ines Ward" and ceo["slack_user_id"] == "U0INES" and ceo["why"].startswith("they read as senior")
    warm = [{"date": "2026-08-01", "what": "They replied to Ines Ward on X", "url": "u", "teammate": "Ines Ward"}]
    assert routing.sender([], warm, team, "mts-research", work, senior)["why"] == \
        "They replied to Ines Ward on X (2026-08-01), and Ines writes for this role"
    assert routing.sender([], warm, team, "mts-research", work)["name"] == "Omar Lind"  # Ines writes to senior people only
    ceo_only = [routing.Teammate(name="Ines Ward", signs_for=["mts-research:senior"])]
    assert routing.sender([], [], ceo_only, "mts-research", work)["why"] == \
        "the team file names a writer for this role only for senior people, so the default sender"
    blank = [routing.Teammate(name="Lea Park", signs_for=["mts-research"], works_on=["", "--"])]
    assert routing.sender([], [], blank, "mts-research", work)["why"].startswith("the team file names them")
    assert routing.sender([], [], team, "backend", work)["name"] == "Justin"  # no one writes for it: the default


def test_the_linkedin_note_fits_and_is_checked():
    role = {"id": "mts-research", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    items = [{"id": "i1", "date": "2026-09-01", "url": "u", "text": "Action-conditioned world models", "context": ""}]
    facts = routing.outreach.facts("mts-research")
    draft = {"subject": "s", "body": "Hey Rin, saw 'Action-conditioned world models'. We're hiring.", "note": "x" * 201}
    problems = routing.outreach.check(draft, items, role, facts, as_of=AS_OF)["problems"]
    assert "LinkedIn note: longer than 200 characters (201)" in problems
    assert "LinkedIn note: does not say GI is hiring for the role" in problems


def test_links_seen_after_as_of_and_a_quiet_call_give_nothing(store):
    before = routing.route(store, "A900", "2025-12-01T00:00:00+00:00", TEAM, CONTACTS)
    assert before is None  # no window open yet
    early = routing.paths(routing.timeline.timeline(store, "A900", "2025-06-01T00:00:00+00:00"), [routing.Teammate(**DANA)])
    assert early == []  # Dana's paper was not public yet


def test_card_escapes_text_and_keeps_slack_limits(store):
    r = routing.route(store, "A900", AS_OF, [OMAR], CONTACTS)  # cold: the card shows the draft itself
    card = routing.card(r.model_copy(update={"draft": {**r.draft, "body": "x < y & z"}}))
    assert "```x &lt; y &amp; z```" in texts(card)  # Slack mrkdwn would otherwise read <...> as a link
    warm = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    asked = routing.card(warm.model_copy(update={"ask": {**warm.ask, "body": "a <b> & c"}}))
    assert "```a &lt;b&gt; &amp; c```" in texts(asked)
    assert all(len(b["text"]["text"]) <= 3000 for b in card["blocks"] if b.get("text"))
    assert all(len(e["url"]) <= 3000 for b in card["blocks"] if b["type"] == "actions" for e in b["elements"])
    assert routing._compose_email("a@b.c", "s", "x" * 4000).endswith("body=")


def test_a_card_is_posted_only_to_the_given_https_webhook():
    seen = []
    client = httpx.Client(transport=httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(200, text="ok")))
    assert routing._post({"text": "t", "blocks": []}, "https://hooks.slack.example/T/B/x", client) == "ok"
    assert json.loads(seen[0].content) == {"text": "t", "blocks": []}
    with pytest.raises(ValueError):
        routing._post({}, "http://hooks.slack.example/x", client)


def test_a_webhook_that_cannot_be_reached_says_so_without_its_url():
    def offline(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
    with pytest.raises(RuntimeError) as refused:
        routing._post({}, "https://hooks.slack.example/T/B/secret", httpx.Client(transport=httpx.MockTransport(offline)))
    assert str(refused.value) == "Slack could not be reached: ConnectError" and refused.value.__cause__ is None


def test_the_call_is_scored_for_the_persons_own_role(store, monkeypatch):
    """A person with no role is scored on every detector; the MTS default only picks the card's role."""
    seen = []
    monkeypatch.setattr(routing.journey, "assess", lambda s, subject, as_of, role: seen.append(role))
    routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    routing.journey.save_context(store, routing.journey.load_context(store, "A900").model_copy(update={"role": ""}))
    routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    assert seen == ["mts", ""]


def event(kind, quote, day="2026-09-01", url="https://example.test/1"):
    return {"event_type": kind, "quote": quote, "event_date": day, "source_url": url}


MATE = routing.Teammate(name="Dana Kest", x_handle="@danakest", github="dkest")


def test_route_in_is_work_together_and_one_way_attention_is_warmth():
    events = [
        event("x_reply", "Nice result!\n\nIn reply to @DanaKest", "2026-08-01"),
        event("x_post", "Building on @danakest's sampler here", "2026-08-20"),
        event("linkedin_comment", "Congrats!\n\nIn reply to Dana Kest: We shipped the model", "2026-07-01"),
        event("github_activity", "Opened issue on dkest/sampler: NaN at step 400", "2026-06-01"),
        event("github_activity", "Pushed to dkest/sampler: fix the loader", "2026-05-01"),
        event("github_activity", "Released v1.0 of dkest/sampler: first", "2026-04-01"),
    ]
    found = routing.paths(events, [MATE])
    assert [(p.kind, p.level) for p in found] == [("commit", "confirmed")]
    assert found[0].detail == "They pushed to Dana Kest's repo dkest/sampler (2026-05-01, 2 pushes or releases in all)"
    assert found[0].tie == "They pushed to your repo dkest/sampler (2026-05-01, 2 pushes or releases in all)."
    assert [(w["date"], w["what"]) for w in routing.warmth(events, [MATE])] == [
        ("2026-08-20", "They mentioned Dana Kest on X"), ("2026-08-01", "They replied to Dana Kest on X"),
        ("2026-07-01", "They commented on Dana Kest's LinkedIn post"),
        ("2026-06-01", "They opened an issue on Dana Kest's repo dkest/sampler")]


def test_work_together_long_ago_is_a_lead_to_check():
    old = [event("github_activity", "Pushed to dkest/sampler: fix", "2023-06-01")]
    assert routing.paths(old, [MATE], "2026-09-15T00:00:00+00:00")[0].level == "likely"
    assert routing.paths(old, [MATE], "2026-05-15T00:00:00+00:00")[0].level == "confirmed"


def test_stars_and_forks_are_warmth_never_a_path():
    events = [event("github_star", "Starred dkest/sampler: a fast sampler", "2026-04-02", "https://github.com/dkest/sampler"),
              event("github_activity", "Forked dkest/sampler", "2026-04-03"), event("github_star", "Starred someone/else")]
    assert routing.paths(events, [MATE]) == []
    assert routing.warmth(events, [MATE]) == [
        {"date": "2026-04-03", "what": "They forked Dana Kest's repo dkest/sampler", "url": "https://example.test/1",
         "teammate": "Dana Kest"},
        {"date": "2026-04-02", "what": "They starred Dana Kest's repo dkest/sampler",
         "url": "https://github.com/dkest/sampler", "teammate": "Dana Kest"}]


def test_searched_says_what_none_found_covers():
    checked, missing = routing.searched([event("x_post", "hello")], [MATE])
    assert checked == []
    assert "shared papers (the team roster lacks it)" in missing
    assert "pushes to teammates' repos (nothing pulled for them)" in missing
    assert "answered exchanges with a teammate (their replies are not pulled)" in missing
    assert routing.searched([event("github_star", "Starred a/b")], [MATE])[0] == []  # stars show no pushes
    assert routing.searched([event("github_activity", "Pushed to a/b")], [MATE])[0] == ["pushes to teammates' repos"]


def test_nothing_checked_says_so_and_the_draft_is_still_checked(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'p.sqlite'}")
    posts = Path(__file__).parent / "fixtures" / "posts"
    routing.seed(s, json.loads((posts / "timeline.json").read_text()))
    r = routing.route(s, "X900", AS_OF, [MATE.model_dump()])
    shown = texts(r.card)
    assert r.route_in["level"] == "not checked"
    assert "Warm path: not checked yet for Mara. Ask the team first." in shown
    assert "by the engine's own rules" in shown and "Not yet tested on past hires for this role." in shown
    assert r.falsifiers[0]["kind"] == "news"
    assert r.outreach["checks"]["passes"] and "Don't send this draft yet" not in shown
    # Their X profile only, and the default sender has no X account: no way in, said plainly, never an X request.
    assert r.channel.kind == "none" and "No way in for Justin: X is their only route on file" in r.channel.reason
    assert "*Subject:*" in shown and "Message from Justin (no channel yet: see Route in)" in shown
    assert "From Justin: no one in the team file writes for this role, so the default sender." in shown
    from app import journey

    ctx = journey.load_context(s, "X900")
    journey.save_context(s, ctx.model_copy(update={"anchors": {}}))  # no profile on file either
    bare = routing.route(s, "X900", AS_OF, [MATE.model_dump()])
    assert bare.channel.kind == "none" and "No public way to reach Mara found yet" in texts(bare.card)


def test_none_found_only_after_a_route_source_was_checked(store):
    r = routing.route(store, "A900", AS_OF, [{**DANA, "openalex_ids": ["A0"]}], CONTACTS)
    assert r.route_in["level"] == "none found" and "shared papers" in r.route_in["checked"]
    employers_only = routing.route(store, "A900", AS_OF, [OMAR], CONTACTS)  # an overlap can only ever be a lead
    assert employers_only.route_in == {**employers_only.route_in, "level": "not checked", "checked": ["shared employers"]}


def test_titles_keep_their_apostrophes_and_comments_are_not_pushes():
    quote = "Dana Kest (A555) on 'Don't Stop Pretraining' | at Pine Lab | last"
    found = routing.paths([event("coauthor_link", quote)], [routing.Teammate(name="Dana Kest", openalex_ids=["A555"])])
    assert found[0].work == "Don't Stop Pretraining"
    comment = event("github_activity", "Pushed to dkest/x, looks good\n\nIn reply to dkest/x: the loader")
    assert routing.paths([comment], [MATE]) == [] and routing.warmth([comment], [MATE])[0]["what"] == \
        "They commented on Dana Kest's repo dkest/x"
    release = routing.paths([event("github_activity", "Released  of dkest/sampler")], [MATE])
    assert release[0].detail.startswith("They made a release of Dana Kest's repo dkest/sampler")


def test_why_now_leads_with_their_work_in_plain_sentences_with_the_sources_after(store):
    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    shown = texts(r.card)
    # Their own news first (the paper, then GI citing it), then the coauthor's move; why this month, never a guess
    # that they might leave.
    assert "*Trigger*\n• Rin had a paper accepted on Aug 20: “Scaling laws II: Accept (NeurIPS 2026)” " \
           "<https://openreview.net/forum?id=example|openreview.net>\n• GI's own work cited Rin's work on Sep 10: " in shown
    assert shown.index("Scaling laws II: Accept") < shown.index("Sam Peer")  # each linked, their own news first
    assert "*Why now*\nTogether they make this a natural time to write, and all three came in the last month. Three " \
           "months ago none of this was public, and by Sep 19, a month after the paper, it's old news: reach them " \
           "before then." in shown
    assert "Sam Peer" not in r.ask["body"] and "coauthor" not in r.ask["body"].split(".")[0]  # only their own news
    assert "*Member of Technical Staff* · Just happened, day 26 of about 30 · reach by Sep 19\n" in shown


def test_the_posted_pay_range_is_one_small_line_under_the_role_and_never_in_the_draft(store, monkeypatch):
    band = {"currency": "USD", "min": 150000, "max": 250000, "interval": "year", "read_on": "2026-09-20"}
    monkeypatch.setattr(routing.pay, "bands", lambda: {"mts-research": band})
    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    assert r.card["blocks"][3] == {"type": "context", "elements": [{"type": "mrkdwn", "text":
                                   "Posted range USD 150,000 to 250,000 per year (GI's job post, read Sep 20)"}]}
    assert "150,000" not in r.draft["body"] and "150,000" not in r.ask["body"]


class Stub:
    """Just what the card's sentences read from a Route."""
    def __init__(self, reasons, signals, falsifiers=(), as_of=AS_OF, name="Kim Lee", subject_id="K1"):
        self.name, self.subject_id, self.signals, self.falsifiers = name, subject_id, list(signals), list(falsifiers)
        self.call = type("Call", (), {"reasons": reasons, "as_of": as_of})()


def signal(reason, day, subject_id="K1", kind=None, url="https://example.test/1", quote="q"):
    return {"reason": reason, "date": day, "subject_id": subject_id, "type": kind or reason, "source_url": url,
            "quote": quote}


def test_each_date_says_what_it_is():
    assert routing.what_happened(Stub(["placement_end"], [signal("placement_end", "2026-06-01")])) == \
        "Kim's fixed-term role is ending (announced on Jun 1)."  # not "ending on Jun 1": that is when it was said
    assert routing.what_happened(Stub(["tenure_milestone"], [signal("tenure_milestone", "2024")])) == \
        "Kim reached a work anniversary in the role they started in 2024."
    assert routing.what_happened(Stub(["new_work"], [signal("new_work", "2025-11", kind="paper_v1")])) == \
        "Kim posted a new preprint in Nov 2025."
    two = Stub(["exec_departure", "technical_ask"], [signal("exec_departure", "2026-09-01", "org"),
                                                      signal("technical_ask", "2026-09-10")])
    assert routing.what_happened(two) == ("Kim asked in public for help GI could give on Sep 10, and an executive left "
                                     "Kim's employer, as of Sep 1.")
    assert routing._natural(two) == "Together they make this a natural time to write, and both came in the last month."
    assert routing._natural(Stub(["technical_ask"], [signal("technical_ask", "2026-09-10")])).startswith("It's an open door")
    assert routing.what_happened(Stub(["mystery_sign"], [signal("mystery_sign", "2026-09-01")])) == \
        "Kim: mystery sign, as of Sep 1."  # an unknown reason still reads as words, never an id


def test_what_would_change_it_is_one_plain_sentence():
    claim = lambda text, by="2026-10-19": {"kind": "person", "by": by, "claim": f"{text} (check on {by})."}  # noqa: E731
    promoted, moved = "A promotion or new title before the window closes", "They start a new role elsewhere before the window closes"
    assert routing._changes(Stub([], [], [claim(promoted), claim(moved)])) == \
        "Kim gets promoted or starts a new role elsewhere before Oct 19."
    passing = claim("It was a passing post: nothing more on the work within three weeks")
    news = {"kind": "news", "by": "2026-11-10", "claim": "No public news by 2026-11-10: a false alarm."}
    assert routing._changes(Stub([], [], [news, passing])) == \
        "Kim has no public news by Nov 10, or nothing more comes of the work within three weeks."
    linked = claim("The company files and the restatement is clean (https://sec.example/x)")
    assert routing._changes(Stub([], [], [linked])) == "The company files and the restatement is clean."
    # A claim with a colon is read whole, never cut at it.
    assert routing._changes(Stub([], [], [claim("It goes public before we write: then everyone can see it")])) == \
        "The work goes public before anyone writes."
    # The engine's own fact to check is not a change: it is its own line, without the quote and link.
    late = claim("Find out why the filing was late and whether it lands on them: 'Form 12b-25: the registrant "
                 "cannot file its Form 10-Q.' (https://sec.example/late)")
    assert routing._changes(Stub([], [], [late, linked])) == "The company files and the restatement is clean."
    assert routing._checking(Stub([], [], [late, linked])) == \
        "Find out why the filing was late and whether it lands on them."
    assert routing._checking(Stub([], [], [linked])) == ""


def test_every_falsifier_the_engine_names_has_plain_words():
    from app import moments
    named = {text for rows in moments.FALSIFIERS.values() for text, _ in rows} | {moments.MOVED[0]}
    assert named <= set(routing.CHANGE)


def test_why_now_reads_at_any_dates_precision():
    month = Stub(["paper_accepted", "coauthor_departure"], [signal("paper_accepted", "2026-09-10"),
                                                             signal("coauthor_departure", "2026-08", "org")])
    assert routing._natural(month) == \
        "Together they make this a natural time to write, and both came in the last two months."
    old = [signal("paper_accepted", "2026-05-10"), signal("gi_citation", "2025")]
    assert routing._natural(Stub(["paper_accepted", "gi_citation"], old)) == \
        "Together they make this a natural time to write, and both have come since 2025."
    assert routing._natural(Stub(["paper_talk"], [signal("paper_talk", "2026-05-10")])) == \
        "That's a natural reason to write, and it came on May 10."


def test_their_own_news_leads_and_they_is_never_someone_else():
    # The head of their lab leaving is on their own record (they posted it), but their paper still comes first.
    lab = Stub(["pi_departure", "paper_accepted"], [signal("pi_departure", "2026-09-01"),
                                                     signal("paper_accepted", "2026-08-20")])
    assert routing.what_happened(lab) == \
        "Kim had a paper accepted on Aug 20, and the head of Kim's lab left, as of Sep 1."
    assert routing.what_happened(lab, only_theirs=True) == "Kim had a paper accepted on Aug 20."
    # After a clause about another person, and inside a teammate's note, they're named again.
    after = Stub(["pi_departure", "tenure_milestone"], [signal("pi_departure", "2026-09-01"),
                                                         signal("tenure_milestone", "2024")])
    assert routing.what_happened(after) == ("The head of Kim's lab left, as of Sep 1, and Kim reached a work anniversary "
                                       "in the role they started in 2024.")
    # A clause that brings in another person names them in it: "their" could be the executive's.
    two = Stub(["warn_notice", "exec_departure"], [signal("warn_notice", "2026-08-20", "org"),
                                                    signal("exec_departure", "2026-09-01", "org")])
    assert routing.what_happened(two) == ("Kim's employer filed a layoff notice on Aug 20, and an executive left Kim's "
                                     "employer, as of Sep 1.")
    # A headline says only what it reports: layoffs reported, a deal to buy them reported, never a filing or a close.
    news = Stub(["warn_notice", "acquisition_closed"], [signal("warn_notice", "2026-09-10", "org", kind="layoffs_reported"),
                                                         signal("acquisition_closed", "2026-09-01", "org", kind="acquisition")])
    assert routing.what_happened(news) == ("Layoffs at Kim's employer were reported on Sep 10 and a deal to buy their "
                                      "employer was reported on Sep 1.")
    # Their new work comes before GI citing it, whatever order the engine gave.
    cited = Stub(["gi_citation", "paper_accepted"], [signal("gi_citation", "2026-09-01"),
                                                     signal("paper_accepted", "2026-08-20")])
    assert routing.what_happened(cited).startswith("Kim had a paper accepted on Aug 20 and GI's own work cited their work")
    note = Stub(["paper_accepted", "private_note"], [signal("paper_accepted", "2026-08-20"),
                                                     signal("private_note", "2026-09-01")])
    assert routing.what_happened(note) == \
        "Kim had a paper accepted on Aug 20 and a GI teammate noted on Sep 1 that Kim may be open to talking."


def test_a_moment_told_late_gets_no_day_of_about_30_and_is_never_also_listed():
    from app.happened import Happened
    late = Happened(kind="acquired", date="2026-07-01", day=76, source_url="https://x.com/k/1", quote="q", event_id="e1")
    older = Happened(kind="left_job", date="2026-07-02", day=75, source_url="https://x.com/k/2", quote="q", event_id="e2")
    call = type("Call", (), {"evidence": {"acquired": ["e1"]}, "as_of": AS_OF})()
    r = type("Route", (), {"kind": "just_happened", "happened": [late, older], "call": call})()
    assert routing._moment(r) is None and routing._also(r) == ""


def test_the_reach_by_day_follows_the_moment_a_just_happened_card_is_about(store):
    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)  # day 26 of the paper's month; the window closes Oct 19
    soon = r.model_copy(update={"call": r.call.model_copy(update={"earliest_close": "2026-09-17T00:00:00+00:00"})})
    # Just happened: the draft is about the paper, so its month sets the day, whatever other window closes first.
    assert "· Just happened, day 26 of about 30 · reach by Sep 19\n" in texts(routing.card(soon))
    assert "reach by Sep 19: " not in texts(routing.card(soon)) and routing.reach_by(soon) == "2026-09-19"
    older = soon.model_copy(update={"kind": None})  # a reach on older signals: the first window to close
    assert "*Member of Technical Staff* · reach by Sep 17\n" in texts(routing.card(older))


def test_the_demo_test_card_has_no_buttons_links_or_mentions(store):
    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS, profile_url="https://rinvale.example")
    test = json.dumps(routing.as_test(r.card, r.name), ensure_ascii=False)
    assert '"actions"' not in test and "<http" not in test and "<@" not in test and "https://rinvale.example" not in test
    assert '"Test \u00b7 Rin Vale"' in test and "made-up person" in test


def test_a_papers_title_and_venue_come_from_its_record():
    paper = lambda kind, quote: routing._paper({"event_type": kind, "quote": quote})  # noqa: E731
    assert paper("paper_accepted", "Scaling laws II: Accept (NeurIPS 2026)") == ("Scaling laws II", "NeurIPS 2026")
    assert paper("paper_accepted", "Latent actions: a study: Accept: poster (ICLR 2023 poster)") == \
        ("Latent actions: a study", "ICLR 2023")
    assert paper("paper_accepted", "World models: Accept (Oral) (ICLR.cc/2026/Conference)") == ("World models", "")
    assert paper("paper_accepted", "Latent plans: Accept: notable top 5% (ICLR 2023 notable top 5%)") == \
        ("Latent plans", "ICLR 2023")
    assert paper("paper_v1", "Bramble worlds (2601.01234v1): submitted 2026-01-02") == ("Bramble worlds", "arXiv")
    assert paper("paper_v1", "Bramble worlds: published 2026-01-02 (OpenAlex)") == ("Bramble worlds", "")
    assert paper("paper_v1", "https://openalex.org/W1: published 2026-01-02 (OpenAlex)") == ("", "")


def test_the_draft_never_congratulates_the_senders_own_paper_twice():
    role = {"id": "mts-research", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    tie = routing.Path(kind="coauthor", level="confirmed", teammate=routing.Teammate(**DANA), detail="",
                       work="Scaling laws II", latest="2026-08-01")
    news = {"event_type": "paper_accepted", "event_date": "2026-08-20", "quote": "Scaling laws II: Accept (NeurIPS 2026)"}
    body = routing.template("Rin Vale", role, None, [], sender_name="Dana Kest", tie=tie, news=news)["body"]
    assert body.count("Scaling laws II") == 1
    shouted = {**news, "quote": "SCALING LAWS II: Accept (NeurIPS 2026)"}  # a record's own capitals are the same paper
    body = routing.template("Rin Vale", role, None, [], sender_name="Dana Kest", tie=tie, news=shouted)["body"]
    assert "SCALING" not in body


def test_an_undergraduate_is_never_pitched_by_the_writer_the_template_or_the_teammate(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'u.sqlite'}")
    posts = Path(__file__).parent / "fixtures" / "posts"
    routing.seed(s, json.loads((posts / "timeline.json").read_text()))
    routing.seed(s, {"events": [{"subject_id": "X900", "event_type": "coauthor_link", "event_date": "2025-11-10",
                                 "quote": "Dana Kest (A555) on 'Action-labeled gameplay' | at Pine Lab | last",
                                 "source_url": "https://openalex.example/W1"}]})
    for entry in json.loads((posts / "ledger.json").read_text()):
        routing.contact.record(s, **entry)
    routing.contact.record(s, "X900", "undergraduate", at="2026-09-01T00:00:00+00:00", until="2027-06-01")
    seen = {}

    def writer(name, role, track, items, facts, others, **kwargs):
        seen.update(kwargs, facts=facts)
        raise routing.providers.CallLimitReached("the template, checked the same way")
    contacts = json.loads((posts / "contacts.json").read_text()).get("X900", [])
    r = routing.route(s, "X900", AS_OF, TEAM, contacts, writer=writer)
    assert r.ask and r.sender["name"] == "Dana Kest"  # Dana wrote with Mara: Dana is asked
    assert r.gate.rapport_only and seen["role_free"] and seen["facts"]
    assert not any("Member of Technical Staff" in f["text"] for f in seen["facts"])
    assert "hiring" not in r.draft["body"] and "Member of Technical Staff" not in r.draft["body"]
    assert r.outreach["checks"]["passes"]
    assert "They're an undergraduate, so we're not raising the role" in r.ask["body"]
    assert "hiring" not in r.ask["body"].split("Here's a draft")[0]
    assert "spatial reasoning" in r.draft["body"]  # the role's own GI fact, since it doesn't name the role


def test_someone_who_may_still_be_a_student_gets_no_pitch_no_undergraduate_label_and_no_send(tmp_path):
    # Ways it could fail: the card, the teammate's ask or the draft raises the role or calls them an undergraduate;
    # routing.send posts the card while it asks first.
    s = Store(f"sqlite:///{tmp_path / 'm.sqlite'}")
    posts = Path(__file__).parent / "fixtures" / "posts"
    routing.seed(s, json.loads((posts / "timeline.json").read_text()))
    routing.seed(s, {"events": [{"subject_id": "X900", "event_type": "coauthor_link", "event_date": "2025-11-10",
                                 "quote": "Dana Kest (A555) on 'Action-labeled gameplay' | at Pine Lab | last",
                                 "source_url": "https://openalex.example/W1"}]})
    for entry in json.loads((posts / "ledger.json").read_text()):
        routing.contact.record(s, **entry)
    routing.contact.record(s, "X900", "unconfirmed", at="2026-09-01T00:00:00+00:00",
                           note="Their X bio says CS at a school, so they may still be a student.")
    contacts = json.loads((posts / "contacts.json").read_text()).get("X900", [])
    r = routing.route(s, "X900", AS_OF, TEAM, contacts)
    assert r.gate.state == "check_first" and r.gate.rapport_only and r.ask
    shown = texts(routing.card(r)) + r.ask["body"].split("Here's a draft")[0]
    assert "undergraduate" not in shown and "hiring" not in r.draft["body"] + r.ask["body"].split("Here's a draft")[0]
    assert "Member of Technical Staff" not in r.draft["body"] and "their work only until it's confirmed" in shown
    with pytest.raises(ValueError, match="not cleared to contact"):
        routing.send(s, r, "https://hooks.slack.example/x", now=AS_OF)
    # Recorded after the card was built, it still stops the send: the ledger is read again before anything posts.
    s2 = Store(f"sqlite:///{tmp_path / 'm2.sqlite'}")
    routing.seed(s2, json.loads((posts / "timeline.json").read_text()))
    for entry in json.loads((posts / "ledger.json").read_text()):
        routing.contact.record(s2, **entry)
    built = routing.route(s2, "X900", AS_OF, TEAM, contacts)
    assert built.gate.state == "clear"
    routing.contact.record(s2, "X900", "unconfirmed", at="2026-09-02T00:00:00+00:00")
    assert built not in routing.sendable(s2, [built], AS_OF)
    with pytest.raises(ValueError, match="not cleared to contact"):
        routing.send(s2, built, "https://hooks.slack.example/x", now=AS_OF)
    from app import events  # an invite pitches nothing: the Events page still invites them (its host brief: no pitch)
    assert events.ledger_hold(s2, "X900", AS_OF) is None


def test_an_undergraduates_draft_never_names_the_role_and_the_check_holds_one_that_does():
    role = {"id": "mts-research", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    items = [{"id": "i1", "date": "2026-09-01", "url": "u", "text": "'World models' by Rin", "context": ""}]
    events = [{"event_type": "coauthor_link", "event_date": "2026-09-01", "quote": "Dana Kest (A1) on 'World models'"}]
    draft = routing.template("Rin Vale", role, None, events, role_free=True)
    assert "hiring" not in draft["body"] + draft["note"] and "Member of Technical Staff" not in draft["body"]
    assert draft["body"].endswith("I'd love to chat more about it if you're interested.\n\nJustin")
    facts = routing.outreach.facts("mts-research")
    held = routing.outreach.check(routing.template("Rin Vale", role, None, events), items, role, facts, as_of=AS_OF,
                                  role_free=True)["problems"]
    assert routing.outreach.ROLE_FREE_WHY in held
    assert "does not say GI is hiring for the role" not in \
        routing.outreach.check(draft, items, role, facts, as_of=AS_OF, role_free=True)["problems"]


def test_a_note_in_deal_week_asks_about_their_work_and_the_check_holds_one_naming_the_role():
    role = {"id": "mts-research", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    items = [{"id": "i1", "date": "2026-09-01", "url": "u", "text": "'World models' by Rin", "context": ""}]
    events = [{"event_type": "coauthor_link", "event_date": "2026-09-01", "quote": "Dana Kest (A1) on 'World models'"}]
    draft = routing.template("Rin Vale", role, None, events, role_free="acquisition")
    # Congratulations, what GI builds, and an open door: never the role.
    assert draft["body"].startswith("Hey Rin, congrats on the acquisition! Saw 'World models' from September 2026")
    assert "General Intuition builds large action models and world models" in draft["body"]
    assert draft["body"].endswith("Not sure what you're looking for next, but if this interests you, I'd love to "
                                  "chat.\n\nJustin") and draft["subject"] == "Congrats on the acquisition"
    assert "hiring" not in draft["body"] + draft["note"] and "Member of Technical Staff" not in draft["body"]
    facts = routing.outreach.facts("mts-research")
    held = routing.outreach.check(routing.template("Rin Vale", role, None, events), items, role, facts, as_of=AS_OF,
                                  role_free="acquisition")["problems"]
    assert routing.outreach.DEAL_WHY in held and routing.outreach.ROLE_FREE_WHY not in held


def test_without_a_contacts_file_their_x_and_linkedin_come_from_the_people_files_profiles(store):
    from app import journey

    bare = routing.route(store, "A900", AS_OF, team=[])
    assert bare.channel.kind == "none"
    ctx = journey.load_context(store, "A900")
    journey.save_context(store, ctx.model_copy(update={"anchors": {**ctx.anchors, "x": "https://x.com/rinvale",
                                                                   "linkedin": "https://www.linkedin.com/in/rinvale"}}))
    r = routing.route(store, "A900", AS_OF, team=[])  # the default sender has no X account: LinkedIn
    assert (r.channel.kind, r.channel.target) == ("linkedin", "https://www.linkedin.com/in/rinvale")
    ctx = journey.load_context(store, "A900")
    routes = routing.on_file([routing.ContactRoute(kind="x", value="rin_on_file", source_url="https://rinvale.example/a")],
                             ctx)
    assert [(c.kind, c.value, c.source_url) for c in routes] == [  # the contacts file's own route wins
        ("x", "rin_on_file", "https://rinvale.example/a"),
        ("linkedin", "https://www.linkedin.com/in/rinvale", "https://www.linkedin.com/in/rinvale")]


def test_a_writer_is_closest_to_their_work_only_through_their_own_words_never_a_repo_they_starred():
    from app import outreach

    def ev(kind, quote):
        return {"subject_id": "P", "event_type": kind, "quote": quote}
    starred = [ev("github_star", "Starred someone/world-model-lab"), ev("x_post", "Shipped RiverGrad, a tiny autograd")]
    assert "world" not in outreach.own_work(starred, "P") and "rivergrad" in outreach.own_work(starred, "P").lower()
    team = [routing.Teammate(name="Tomas Marr", signs_for=["mts-research"], works_on=["world model"]),
            routing.Teammate(name="Omar Lind", signs_for=["mts-research"], works_on=["scaling laws"])]
    picked = routing.sender([], [], team, "mts-research", outreach.own_work(starred, "P"))
    assert picked["why"] == "the team file names them for this role, and no writer's own work matches theirs"
    built = [*starred, ev("github_repo", "Created bramble-world-model: a world model in 200 lines"),
             ev("x_reply", "Agreed on the data point" + routing.REPLY_CONTEXT + "world models need more data")]
    assert "world model" not in outreach.own_work(built[1:2] + built[3:], "P")  # what they answered isn't theirs
    assert routing.sender([], [], team, "mts-research", outreach.own_work(built, "P"))["why"] == \
        "closest to their work: both work on world model"
    # Said back as their own words say it: a plural in what they answered never sets it.
    assert routing.sender([], [], team, "mts-research", outreach.own_work(built, "P"),
                          said=outreach.own_texts(built, "P"))["why"] == "closest to their work: both work on world model"
    # Said back as words, never with the characters between them in their text: a repo's "world_models", a full stop.
    for text in ("Created rin/world_models: action-conditioned sims", "We model the world. Models that predict it"):
        assert outreach.as_written("world model", text) == "world models", text
    assert outreach.as_written("JEPA", "Two JEPAs, one (tiny) world") == "JEPAs"
    # A letter outside ASCII is part of its word, and a hyphen or an apostrophe inside a word stays.
    assert outreach.as_written("pokémon agents", "Training Pokémon agents") == "pokémon agents"
    assert outreach.as_written("world-model", "Two world-models") == "world-models"
    for text in ("Scaling world - models", "a world 'models' paper"):  # a hyphen or an apostrophe between words
        assert outreach.as_written("world model", text) == "world models", text


def test_the_way_in_says_what_both_work_on_as_their_own_words_say_it(store):
    # Ways it could fail: the team file's singular is said back when they write the plural ("world model" for their
    # "world models"); the writer picked changes with the wording; a phrase they don't write is added.
    team = [routing.Teammate(**{**OMAR, "works_on": ["world model", "scaling laws", "diffusion"]})]
    r = routing.route(store, "A900", AS_OF, team, CONTACTS)
    assert r.sender["name"] == "Omar Lind"
    assert r.sender["why"] == "closest to their work: both work on world models, scaling laws", r.sender["why"]


@pytest.mark.parametrize("extractor, url", [
    (PAGE_EXTRACTOR, "https://leepark.example/news"),  # their own page
    (PAGE_EXTRACTOR + ":posts:2026-09-23.5:abc123def456:a1b2c3:small", "https://x.com/leepark/status/1")])  # their post
def test_a_layoff_is_a_reason_to_reach_but_the_draft_and_why_now_never_name_it(extractor, url):
    s = Store("sqlite://")
    routing.seed(s, {"contexts": [{"subject_id": "L100", "name": "Lee Park", "role": "mts"}],
                     "events": [{"subject_id": "L100", "event_type": "layoff", "event_date": "2026-09-10",
                                 "extractor": extractor, "source_url": url,
                                 "quote": "I was laid off from Example Lab on September 10, 2026, with the research team."}]})
    r = routing.route(s, "L100", AS_OF, [], [])
    assert r.call.action == "reach_now" and "own_departure" in r.call.evidence  # a forced move: they are on the market
    # The draft goes to them and the why-now line is what ops reads first; the card's sources keep their own words.
    said = (r.draft["body"] + "\n" + r.call.explanation).lower()
    assert not any(word in said for word in ("laid off", "layoff", "let go", "lost their job"))


def test_only_a_teammate_on_the_list_knows_them_and_a_wrong_person_cancels_what_came_before():
    team = [routing.Teammate(name="Dana Kest")]

    def said(kind, by, at, note=""):
        return {"kind": kind, "by": by, "at": f"2026-09-{at}T00:00:00+00:00", "note": note}
    history = [said("knows", "Dana Kest", "01", "Worked together"), said("knows", "jdoe", "02"),
               said("wrong_person", "Dana Kest", "03"), said("knows", "old handle", "04"),
               said("knows", "Dana Kest", "05", "Met them. Met at NeurIPS"), said("knows", "Dana Kest", "20")]
    known, unlisted = routing.knows(history, team, "2026-09-10T00:00:00+00:00")
    assert [e["at"][:10] for e in known] == ["2026-09-05"] and [e["by"] for e in unlisted] == ["old handle"]
    assert routing.knows(history[:3], team, "2026-09-10T00:00:00+00:00") == ([], [])  # the wrong person cancels both
    assert "(met them, said" in routing._said(known[0], "Rin", "2026-09-10T00:00:00+00:00")
    assert "(NeurIPS dinner, said" in routing._said({**known[0], "note": "NeurIPS dinner"}, "Rin",
                                                     "2026-09-10T00:00:00+00:00")


def test_the_card_opens_with_their_name_the_profiles_their_identity_ties_and_their_public_email(store):
    from app import contact

    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00",
                   links=["https://rinvale.example/about", "https://github.com/rinvale-example/"])
    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    assert r.card["blocks"][0]["text"]["text"] == "Rin Vale"
    assert r.card["blocks"][1]["text"]["text"] == (
        "<https://rinvale.example/about|rinvale.example/about> · <https://github.com/rinvale-example|GitHub>\n"
        "Email: rin@rinvale.example (listed on <https://rinvale.example/about|rinvale.example>)")
    shown = texts(r.card)
    assert [shown.index(part) for part in ("*Trigger*", "*Why now*", "*Confidence*", "*Draft:", "*Route in*")] == \
        sorted(shown.index(part) for part in ("*Trigger*", "*Why now*", "*Confidence*", "*Draft:", "*Route in*"))
    # Marked the wrong person later: no profile is listed, and none as of a day before the record either.
    assert routing.tied(contact.history(store, "A900"), "2026-08-31T00:00:00+00:00") == []
    contact.record(store, "A900", "wrong_person", at="2026-09-10T00:00:00+00:00")
    assert routing.tied(contact.history(store, "A900"), AS_OF) == []
    none = routing.route(store, "A900", AS_OF, TEAM, [])
    assert "Email: no public email found." in texts(none.card)


# How a confirmed person's profiles line can go wrong, listed before the fix (a real card said "No profiles tied to
# them yet" for someone whose record tied only a post and a repo of theirs):
#   1. The record ties only their posts and repos: the card shows none, though each comes from an account the engine
#      reads for them. It must show those accounts (X, GitHub, LinkedIn).
#   2. A post or repo on someone else's account (another person's post, an organisation's repo) must never make that
#      account theirs.
#   3. A post or repo is never shown as a profile itself, and with no profiles read nothing is inferred from one.
#   4. One account named twice (its page and a post under it, another case, twitter.com for x.com) shows once.
#   5. Without a record, or after a later "wrong person", none, whatever the engine reads.
def test_a_confirmed_person_s_card_shows_the_accounts_their_tied_posts_and_repos_come_from():
    from app import contact

    links = ["https://x.com/TessV_Example/status/123", "https://github.com/tessv-example/tidebrowser",
             "https://x.com/someone_else/status/9", "https://github.com/some-org/tool",
             "https://www.linkedin.com/posts/tessv-example_a-launch-activity-1", "https://twitter.com/tessv_example"]
    read = ["https://x.com/tessv_example", "https://github.com/tessv-example",
            "https://www.linkedin.com/in/tessv-example", "https://tessv.example/"]
    history = [contact.Entry(person_id="P", kind="identity", at="2026-09-01T00:00:00+00:00", links=links).model_dump()]
    assert routing.tied(history, AS_OF, read) == [
        {"label": "X", "url": "https://x.com/tessv_example"}, {"label": "GitHub", "url": "https://github.com/tessv-example"},
        {"label": "LinkedIn", "url": "https://www.linkedin.com/in/tessv-example"}]
    assert routing.tied(history, AS_OF) == [{"label": "X", "url": "https://x.com/tessv_example"}]  # its own page only
    assert routing.tied(history, "2026-08-31T00:00:00+00:00", read) == []
    wrong = [*history, contact.Entry(person_id="P", kind="wrong_person", at="2026-09-10T00:00:00+00:00").model_dump()]
    assert routing.tied(wrong, AS_OF, read) == []


def test_the_card_of_someone_confirmed_by_a_repo_of_theirs_links_their_account(store):
    from app import contact

    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00",
                   links=["https://rinvale.example/about", "https://github.com/rinvale-example/tidewire",
                          "https://github.com/tide-org/tidewire"])
    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    assert r.card["blocks"][1]["text"]["text"].startswith(
        "<https://rinvale.example/about|rinvale.example/about> · <https://github.com/rinvale-example|GitHub>\n")
    assert "tide-org" not in texts(r.card)


def test_a_profile_is_an_account_page_or_the_page_as_given_never_a_repository_post_or_search():
    assert routing.profile("https://www.cs.cmu.edu/~rin/") == {"label": "cs.cmu.edu/~rin",
                                                             "url": "https://www.cs.cmu.edu/~rin/"}
    assert routing.profile("https://x.com/rin_v/?s=20") == {"label": "X", "url": "https://x.com/rin_v"}
    assert routing.profile("https://m.twitter.com/rin_v") == {"label": "X", "url": "https://x.com/rin_v"}
    assert routing.profile("HTTPS://GitHub.com/rin") == {"label": "GitHub", "url": "https://github.com/rin"}
    assert routing.profile("https://scholar.google.com/citations?user=AbC123&hl=en")["label"] == "Google Scholar"
    for url in ("https://github.com/openai/gpt-2", "https://github.com/features", "https://x.com/openai/status/1",
                "https://x.com/hashtag/AI", "https://huggingface.co/blog", "https://huggingface.co/openai/whisper",
                "https://scholar.google.com/scholar?q=rin", "https://openreview.net/forum?id=abc",
                "https://orcid.org/search", "https://www.linkedin.com/company/acme"):
        assert routing.profile(url) is None, url


def test_x_is_never_the_channel_for_a_sender_without_an_x_account():
    noor = {"name": "Noor Hale"}
    x = routing.ContactRoute(kind="x", value="@rin_example", source_url="https://x.com/rin_example")
    posts = [{"event_type": "x_post", "observed_at": "2026-09-01T00:00:00+00:00"}] * 3
    none = routing.channel(noor, [x], posts, AS_OF)
    assert none.kind == "none" and none.reason.startswith("No way in for Noor: X is their only route on file")
    site = routing.ContactRoute(kind="site", value="https://rinvale.example", source_url="https://rinvale.example")
    assert routing.channel(noor, [x, site], posts, AS_OF).kind == "site"
    # Another teammate who writes for the role and has an X account writes instead.
    team = [routing.Teammate(name="Noor Hale", signs_for=["product-designer"]),
            routing.Teammate(name="Sam Ode", signs_for=["product-designer"], x_handle="samode"),
            routing.Teammate(name="Ines Ward", signs_for=["mts-research"], x_handle="inesward")]
    other = routing.on_x(noor, team, "product-designer")
    assert other["name"] == "Sam Ode" and "Noor has no X account in the team file" in other["why"]
    assert routing.on_x(noor, team[:1] + team[2:], "product-designer") is None  # nobody for this role: no way in


def test_each_circumstance_gets_a_plainly_different_message():
    role = {"id": "product-designer", "title": "Product Designer", "jd_url": "https://jobs.example.test/pd"}
    work = [{"event_type": "coauthor_link", "event_date": "2026-08", "quote": "Dana Kest (A1) on 'Clip editor'"}]
    launch = {"event_type": "launch_announced", "event_date": "2026-09-10", "quote": "Clip editor 2 is live",
              "source_url": "https://x.com/rin/status/9"}
    post = {"event_type": "x_post", "event_date": "2026-09-12", "quote": "Prototyping a timeline scrubber for clips",
            "source_url": "https://x.com/rin/status/10"}
    drafts = {m: routing.template("Rin Vale", role, None, work, sender_name="Noor Hale", message=m,
                                  news=launch if m == "launch" else None, opener=post if m == "early_sign" else None,
                                  role_free=m if m == "acquisition" else False)["body"]
              for m in ("launch", "early_sign", "open_to_work", "acquisition", "work")}
    assert drafts["launch"].startswith("Hey Rin, congrats on the launch! Saw your X post: \"Clip editor 2 is live.\"")
    assert drafts["early_sign"].startswith("Hey Rin, saw your X post: \"Prototyping")
    assert drafts["open_to_work"].startswith("Hey Rin, we're hiring for our Product Designer role (") and \
        drafts["open_to_work"].endswith("If you're open to it, I'd love to chat.\n\nNoor")
    assert drafts["acquisition"].startswith("Hey Rin, congrats on the acquisition!") and "Product Designer" not in \
        drafts["acquisition"] and "General Intuition builds large action models" in drafts["acquisition"]
    assert len({d.split(".")[0] for d in drafts.values()}) == len(drafts)  # five different openings
    # One short GI line, never the job description's paragraph.
    for body in drafts.values():
        assert "joins a small, talented product team" not in body
    assert "General Intuition builds large action models" in drafts["work"]  # the design lines wait on the posting
    assert {routing.MESSAGE[m] for m in drafts} >= {"congratulations on the launch",
                                                    "congratulations on the acquisition, no role named"}


def test_a_github_release_is_congratulated_by_its_repo_and_tag_which_passes_the_name_swap():
    from app import happened, outreach

    role = {"id": "backend", "title": "Backend Engineer", "jd_url": "https://jobs.example.test/be"}
    said = "Released v2.0.0 of rin/tidewire: Exactly-once delivery for Postgres-backed job queues"
    gh = {"id": "g1", "subject_id": "A900", "event_type": "github_activity", "event_date": "2026-09-10",
          "observed_at": "2026-09-10T12:00:00+00:00", "source_url": "https://github.com/rin/tidewire/releases/tag/v2.0.0",
          "source_version_hash": "h1", "quote": said}
    read = {**gh, "id": "r1", "event_type": "project_release", "quote": "Exactly-once delivery for Postgres-backed job queues"}
    assert happened.shipped(said) == "tidewire v2.0.0" and happened.shipped("Released nightly of rin/tidewire") == ""
    assert happened.shipped("Released v2.4 of rin/tide-journal: Tide Journal v2.4") == "Tide Journal v2.4"  # its own name first
    assert happened.shipped("Released nightly-2026 of rin/tidewire") == ""  # a build's
    drafted = routing.template("Rin Vale", role, None, [gh, read], None, "Oda", news=read, message="release")
    assert drafted["subject"] == "Congrats on tidewire v2.0.0" and "congrats on shipping tidewire v2.0.0!" in drafted["body"]
    assert 'on GitHub: "Exactly-once delivery for Postgres-backed job queues."' in drafted["body"]
    items = [{"id": "g1", "date": "2026-09-10", "url": gh["source_url"], "text": said, "context": "", "title": False}]
    checks = outreach.check(drafted, items, role, outreach.facts("backend"), [], "Rin Vale", AS_OF, "Oda", False,
                            {"release"})
    assert checks["passes"], checks["problems"]
    # A release with no words of its own: its repo and tag are the one detail the name swap needs.
    bare = {**gh, "quote": "Released v2.0.0 of rin/tidewire"}
    drafted = routing.template("Rin Vale", role, None, [bare, {**read, "quote": ""}], None, "Oda", news={**read, "quote": ""},
                               message="release")
    assert "congrats on shipping tidewire v2.0.0!" in drafted["body"], drafted["body"]
    checks = outreach.check(drafted, [{**items[0], "text": bare["quote"]}], role, outreach.facts("backend"), [],
                            "Rin Vale", AS_OF, "Oda", False, {"release"})
    assert checks["passes"] and "tidewire v2.0.0" in checks["name_swap"]["anchors"], checks
    # Someone else who released their own tidewire v2.0.0: the draft reads fine for them, so it fails.
    bo = outreach.written_by([{"subject_id": "B1", "quote": "Released v2.0.0 of bo/tidewire"}], "B1")
    assert outreach.check(drafted, [{**items[0], "text": bare["quote"]}], role, outreach.facts("backend"),
                          [("Bo Lind", bo)], "Rin Vale", AS_OF, "Oda", False, {"release"})["name_swap"]["reads_fine_for"] \
        == ["Bo Lind"]
    assert happened.shipped("Released abc1234 of rin/tidewire") == ""  # a commit, not a version


def test_a_release_is_congratulated_by_name_and_version_dated_plainly_and_passes_on_that_one_detail():
    from app import outreach

    role = {"id": "product-designer", "title": "Product Designer", "jd_url": "https://jobs.example.test/pd"}
    news = {"id": "r1", "subject_id": "A900", "event_type": "project_release", "event_date": "2026-09-10",
            "observed_at": "2026-09-10T15:00:00+00:00", "source_url": "https://x.com/rin/status/11",
            "quote": "Pocket Diary v2.3 is out now", "extractor": "claude_extract_v1:posts:v3:abc:def:small"}
    assert routing.circumstance(False, news=news) == "release"
    assert routing.circumstance(False, news={**news, "event_type": "launch_announced"}) == "release"  # it names a version
    assert routing.circumstance(False, news={**news, "event_type": "launch_announced", "quote": "Launching today"}) == \
        "launch"
    drafted = routing.template("Rin Vale", role, None, [news], None, "Noor Hale", news=news)
    assert drafted["subject"] == "Congrats on Pocket Diary v2.3"  # not their post word for word
    # The post says nothing more, so it isn't quoted, and nothing claims it caught an eye without saying what.
    assert drafted["body"].startswith("Hey Rin, congrats on shipping Pocket Diary v2.3! Saw your X post. General ")
    assert "launch" not in drafted["body"] and "September 2026" not in drafted["body"]
    assert drafted["note"].startswith("Hey Rin, congrats on shipping Pocket Diary v2.3! Saw your X post. We're hiring")
    items = [{"id": "r1", "date": "2026-09-10", "url": news["source_url"], "text": news["quote"], "context": "",
              "title": False}]
    # On LinkedIn, the message once they accept the note thanks them and never says the note again: the body less
    # what the note said, with the role's link and the ask to chat given again.
    drafted["after"] = outreach.follow_up(drafted["body"], drafted["note"], "Rin Vale", role)
    assert drafted["after"].startswith("Thanks for connecting, Rin! General Intuition ") and drafted["after"].endswith(
        "Here's the role if you'd like a look: https://jobs.example.test/pd. I'd love to chat more if you're "
        "interested.\n\nNoor")
    assert outreach.repeated(drafted["note"], drafted["after"]) == ""
    checks = outreach.check(drafted, items, role, outreach.facts("product-designer"),
                            [("Ada Lune", "Shipped a clip editor in 2026")], "Rin Vale", AS_OF, "Noor Hale",
                            moments=["release"])
    assert checks["passes"], checks["problems"]  # what their own post ships, with its version, is enough alone
    for other in ("work", "paper"):  # the same congratulations on anything but a release
        assert any("not what happened" in p for p in outreach.check(drafted, items, role, outreach.facts(
            "product-designer"), [], "Rin Vale", AS_OF, "Noor Hale", moments=[other])["problems"]), other
    assert checks["name_swap"]["anchors"] == ["pocket diary v2.3"]
    # A post that says more is quoted, closed with a full stop.
    more = {**news, "quote": "Pocket Diary v2.3 is out now with offline sync"}
    assert 'Saw your X post: "Pocket Diary v2.3 is out now with offline sync." ' in \
        routing.template("Rin Vale", role, None, [more], news=more)["body"]


def test_a_draft_never_quotes_the_github_feed_only_the_words_they_wrote():
    from app import outreach

    role = {"id": "product-designer", "title": "Product Designer", "jd_url": "https://jobs.example.test/pd"}
    tag = "https://github.com/rin/pocket-diary/releases/tag/v2.3"
    news = {"id": "r1", "subject_id": "A900", "event_type": "project_release", "event_date": "2026-09-10",
            "observed_at": "2026-09-10T15:00:00+00:00", "source_url": tag,
            "quote": "Released v2.3 of rin/pocket-diary: Pocket Diary v2.3"}
    drafted = routing.template("Rin Vale", role, None, [news], None, "Noor Hale", news=news, message="release")
    assert drafted["subject"] == "Congrats on Pocket Diary v2.3"
    repo = "https://github.com/rin/pocket-diary"
    created = {"id": "g1", "subject_id": "A900", "event_type": "github_repo", "event_date": "2026-09-10",
               "observed_at": "2026-09-10T15:00:00+00:00", "source_url": repo, "source_version_hash": "h",
               "quote": "Created rin/pocket-diary: A diary app that keeps game clips by day (Swift)"}
    # The post reader's copy of the item keeps the language: the draft quotes the item's own words instead.
    built = {**created, "id": "w1", "event_type": "work_in_progress", "quote": "rin/pocket-diary: A diary app that keeps "
             "game clips by day (Swift)"}
    wip = routing.template("Rin Vale", role, None, [created, built], built, "Noor Hale", message="early_sign")
    assert 'saw your pocket-diary repo on GitHub: "A diary app that keeps game clips by day." ' in wip["body"]
    for draft in (drafted, wip):
        text = "\n".join(draft.values())
        assert not any(s in text for s in ("Released v2.3", "Created rin", "rin/pocket-diary", "(Swift)")), text
        assert outreach.quoting(text) == []
    items = [{"id": "g1", "date": "2026-09-10", "url": repo, "text": created["quote"], "context": "", "title": False}]
    framed = {**wip, "body": wip["body"].replace('"A diary app', '"Created rin/pocket-diary: A diary app')}
    problems = outreach.check(framed, items, role, outreach.facts("product-designer"), [], "Rin Vale", AS_OF,
                              "Noor Hale")["problems"]
    assert any("not in their words" in p for p in problems), problems


def test_a_push_with_no_messages_still_quotes_its_repo_line_and_passes_its_checks():
    # tests/test_posts.py lists how a post nobody read can leak (6: a free draft that passed its checks fails them).
    # The push's words are its repo's description, from the created repo the reader tagged nothing on: code stays in
    # what a draft may cite, the model writer's list included. (A push alone no longer makes a call since 3bc3a1e, so
    # this reads the draft and its items directly.)
    from app import outreach

    role = {"id": "mts-research", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    repo = "https://github.com/rin/signalyard"

    def at(n, kind, quote, day, h):
        return {"id": n, "subject_id": "A900", "event_type": kind, "event_date": day, "source_url": repo,
                "observed_at": f"{day}T1{h}:00:00+00:00", "source_version_hash": f"h{h}", "quote": quote}
    created = at("g1", "github_repo", "Created rin/signalyard: Tools for sorting old match replays by map and mode (Python)",
                 "2026-09-01", 1)
    pushed = at("g2", "github_activity", "Pushed to rin/signalyard", "2026-09-10", 2)
    wip = {**pushed, "id": "w2", "event_type": "work_in_progress"}
    call = Readiness(as_of=AS_OF, score=0.5, action="reach_now", families={}, reasons=["work_in_progress"], holds=[],
                     open_windows=[], earliest_close=None, explanation="", evidence={"work_in_progress": ["w2"]},
                     track="rapport")
    view = [created, pushed, wip]
    cited = outreach.items(view, "A900", call)  # the model writer's: never a post the reader read nothing off
    assert [i["text"] for i in cited] == [created["quote"]]  # the push's page keeps the repo's fuller line
    draft = routing.template("Rin Vale", role, None, view, wip, "Oda", message="early_sign")
    assert 'saw your signalyard commits on GitHub: "Tools for sorting old match replays by map and mode." ' in draft["body"]
    checks = outreach.check(draft, cited, role, outreach.facts("mts-research"), [], "Rin Vale", AS_OF, "Oda")
    assert checks["passes"], checks["problems"]


def test_a_free_draft_is_checked_against_every_post_as_before_and_only_the_writer_loses_unread_ones(tmp_path):
    # tests/test_posts.py lists how a post nobody read can leak (8: the free draft's checks read every post). A casual
    # post the reader tagged nothing on shares the template's own words, which the name swap counts as theirs: taking
    # it from the free draft's check would hold a card that went before. Only the model writer loses it.
    def post(n, day, quote):
        return {"subject_id": "A901", "event_type": "x_post", "event_date": day, "observed_at": f"{day}T12:00:00+00:00",
                "source_url": f"https://x.com/ivy_example/status/{n}", "source_version_hash": f"h{n}", "quote": quote,
                "extractor": "x_v1"}
    ask = post(1, "2026-09-10", "Anyone have action-labeled gameplay video?")
    casual = post(2, "2026-09-11", "That trailer really caught my eye, would love to chat more if you're interested in it")
    s = Store(f"sqlite:///{tmp_path / 'anchor.sqlite'}")
    routing.seed(s, {"contexts": [{"subject_id": "A901", "name": "Ivy Stroud", "role": "mts-research"}],
                     "events": [ask, {**ask, "event_type": "technical_ask", "extractor": "claude_extract_v1:posts:x"},
                                casual]})
    free = routing.route(s, "A901", AS_OF, [], [])
    assert free.call.action == "reach_now" and free.outreach["checks"]["passes"], free.outreach["checks"]["problems"]
    given = []
    routing.route(s, "A901", AS_OF, [], [], writer=lambda *args, **kwargs: given.extend(args[3]))
    assert given and not any("trailer" in i["text"] for i in given)


def test_a_github_note_names_the_repo_and_needs_its_words_too_for_the_name_swap():
    from app import outreach

    role = {"id": "mts", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    repo = "https://github.com/rin/signalyard"

    def at(kind, quote, url=repo, n=1):
        return {"id": f"g{n}", "subject_id": "A900", "event_type": kind, "event_date": "2026-09-10",
                "observed_at": f"2026-09-10T1{n}:00:00+00:00", "source_url": url, "source_version_hash": f"h{n}",
                "quote": quote}

    def wip(item):  # the post reader's copy of a GitHub item: work in progress
        return {**item, "id": f"w{item['id']}", "event_type": "work_in_progress"}

    def checked(events, opener):
        draft = routing.template("Rin Vale", role, None, events, opener, "Oda", message="early_sign")
        items = [{"id": e["id"], "date": "2026-09-10", "url": e["source_url"], "text": e["quote"], "context": "",
                  "title": False} for e in events if e["event_type"] != "work_in_progress"]
        return draft["body"], outreach.check(draft, items, role, outreach.facts("mts-research"), [], "Rin Vale",
                                             AS_OF, "Oda")
    created = at("github_repo", "Created rin/signalyard: Tools for sorting old match replays by map and mode (Python)")
    body, checks = checked([created, wip(created)], wip(created))
    assert 'saw your signalyard repo on GitHub: "Tools for sorting old match replays by map and mode." ' in body
    assert checks["passes"] and "signalyard" in checks["name_swap"]["anchors"], checks
    # A push with no messages, on a repo of theirs: the repo by name, and its description as their words.
    pushed = at("github_activity", "Pushed to rin/signalyard", n=2)
    body, checks = checked([created, pushed, wip(pushed)], wip(pushed))
    assert 'saw your signalyard commits on GitHub: "Tools for sorting old match replays by map and mode." ' in body
    assert checks["passes"], checks["problems"]
    # The repo alone is one detail: the note still fails, never "your work on GitHub" with nothing in it.
    body, checks = checked([pushed, wip(pushed)], wip(pushed))
    assert "saw your signalyard commits on GitHub, and it really caught my eye." in body
    assert checks["problems"] == [outreach.VAGUE_WHY, "fails the name swap: 1 detail(s) only true of them, needs 2 (or "
                                  "the release their post names)"]  # the repo, not what in it caught the eye
    # Someone else's repo is never "your ... repo"; a release with no version is the repo's release.
    pr = at("github_activity", "Opened pull request on ada/gridsim: Add a contact solver",
            url="https://github.com/ada/gridsim/pull/7", n=3)
    assert 'saw your gridsim pull request on GitHub: "Add a contact solver." ' in checked([pr, wip(pr)], wip(pr))[0]
    # Only a one-word name of a repo of theirs, on GitHub, is a name: never a star, a fork, a generic or README repo,
    # a site, or a word of a longer name (eye_tracker is two words, and "eye" is in "caught my eye").
    def names(text, url=repo, context=""):
        return outreach.repos([{"text": text, "url": url, "context": context}])
    assert names("Created rin/signalyard: A tool") == {"signalyard"} and names("Pushed to rin/brambleworld-lab") == \
        {"brambleworld-lab"}
    assert names("Fixed it", "https://github.com/ada/gridsim/issues/1#c", "ada/gridsim: A bug") == {"gridsim"}
    for text in ("Starred ada/gridsim", "Forked ada/gridsim", "Created rin/website", "Created rin/rin: About me",
                 "Created rin/rin.github.io", "Created rin/.github", "Created rin/my-website", "Pushed to rin/eye_tracker"):
        assert names(text) == set(), text
    assert names("Created foo/bar: a post that reads like one", "https://x.com/rin/status/9") == set()
    assert names("Weekly notes", "https://www.linkedin.com/posts/rin-2", "AI/ML Weekly: issue 9") == set()
    tag = at("github_activity", "Released spring of rin/signalyard", url=f"{repo}/releases/tag/spring", n=4)
    news = {**tag, "id": "r1", "event_type": "project_release", "quote": ""}
    drafted = routing.template("Rin Vale", role, None, [tag, news], None, "Oda", news=news, message="release")
    assert "Saw the signalyard release on GitHub" in drafted["body"], drafted["body"]


def test_a_post_in_styled_letters_is_read_and_quoted_in_plain_ones(store):
    from app import journey

    styled = "𝐈 𝐭𝐮𝐫𝐧𝐞𝐝 my old level maps into a 𝘱𝘭𝘢𝘺𝘢𝘣𝘭𝘦 board game prototype."
    routing.seed(store, {"events": [{"id": "s1", "subject_id": "A900", "event_type": "project_release",
                                     "event_date": "2026-09-10", "observed_at": "2026-09-10T15:00:00+00:00",
                                     "source_url": "https://www.linkedin.com/posts/rin-1", "quote": styled}]})
    [read] = [e for e in journey.view(store, "A900", AS_OF) if e["source_url"] == "https://www.linkedin.com/posts/rin-1"]
    assert read["quote"] == "I turned my old level maps into a playable board game prototype."
    role = {"id": "product-designer", "title": "Product Designer", "jd_url": "https://jobs.example.test/pd"}
    drafted = routing.template("Rin Vale", role, None, [read], None, "Noor Hale", news=read, message="release")
    assert '"I turned my old level maps into a playable board game prototype."' in drafted["body"]
    from app.detectors import their_words
    assert their_words({"event_type": "x_post", "quote": "𝑇ℎ𝑒 𝐧𝐞𝐰 renderer"}) == "The new renderer"  # the raw timeline too


def test_a_draft_quotes_a_short_clean_phrase_never_a_list_a_cut_or_a_link():
    from app import outreach

    role = {"id": "mts", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    post = ("Wrote a toy physics engine from scratch - rigid bodies - a contact solver - broadphase - sleeping islands "
            "- joints, motors, ragdolls and a replay tool https://github.com/rin/toyphys")

    def quoted(quote):
        sign = {"id": "s1", "subject_id": "A900", "event_type": "work_in_progress", "event_date": "2026-09-10",
                "observed_at": "2026-09-10T15:00:00+00:00", "source_url": "https://x.com/rin/status/12", "quote": quote}
        return routing.template("Rin Vale", role, None, [sign], sign, "Oda", message="early_sign")["body"]

    for quote in (post, post[:150] + "…"):  # the whole post, and one a feed already cut
        body = quoted(quote)
        assert 'saw your X post: "Wrote a toy physics engine from scratch." It really caught my eye, especially "rigid ' \
            'bodies" and "a contact solver".' in body  # the parts their list names: a second detail, in their words
        assert not any(s in body for s in ("…", " - ", "https://github.com/rin")), body
    cut = quoted("Been hacking on a tiny renderer for game clips that runs in the browser and…")
    assert '"' not in cut and outreach.VAGUE.search(cut)  # nothing to name: the checks hold it for a model draft
    assert '"' not in quoted("• A tiny renderer for game clips\n• Runs in the browser")  # a list's first item
    assert "especially" not in quoted("Spent the weekend on a tiny renderer for game clips - took way longer than "
                                      "planned - but it works")  # two dashes in a sentence are not a list
    assert 'post: "Working on world models, e.g. for racing sims." ' in quoted("Working on world models, e.g. for racing "
                                                                                "sims.\nhttps://x.test/clip")
    # A sentence too long to quote that runs on past an emoji into a new one ends at the emoji: never a clause of the
    # next one cut off after it. An emoji before a name, or one the post opens with, ends nothing.
    assert 'post: "Six months of evenings went into this clip renderer and the public beta lands on Monday for ' \
        'all of you \U0001F979" ' in quoted("Six months of evenings went into this clip renderer and the public "
                                                  "beta lands on Monday for all of you \U0001F979 "
                                                  "And yes, the beta opens to the whole waitlist this Friday with export to "
                                                  "GIF")
    assert 'post: "Huge thanks to \U0001F64F Tidewire and the whole team for the renderer we trained on game clips all ' \
        'summer long." ' in quoted("Huge thanks to \U0001F64F Tidewire and the whole team for the renderer we trained on "
                                  "game clips all summer long, and to everyone who tested it in the browser with us over "
                                  "many long weekends")
    assert 'post: "\U0001F680 Excited to share Tidewire v2, our renderer for game clips." ' in quoted(
        "\U0001F680 Excited to share Tidewire v2, our renderer for game clips.")
    # A second sentence when both fit; a sentence too long to quote, up to where a clause ends.
    assert 'post: "New prototype. A physics camera for clip replays." ' in quoted("New prototype. A physics camera for "
                                                                                  "clip replays.")
    assert 'post: "Been hacking on a tiny renderer for game clips." ' in quoted(
        "Been hacking on a tiny renderer for game clips, it runs in the browser and…")
    items = [{"id": "s1", "date": "2026-09", "url": "https://x.com/rin/status/12", "text": post, "context": "",
              "title": False}]
    swap = outreach.check({"subject": "s", "body": quoted(post)}, items, role, outreach.facts("mts-research"), [],
                          "Rin Vale", AS_OF, "Oda")["name_swap"]
    assert swap["passes"] and len(swap["anchors"]) >= 2, swap
    assert outreach.quoting('Saw your post: "a tiny renderer that…" Also "one - two - three" and "see https://x.test".') \
        == ['quote is cut off: "a tiny renderer that…"', "quote is a list", "quote has a link in it"]
    assert outreach.quoting('"- a tiny renderer for clips"') == ["quote is a list"]
    assert outreach.quoting(f'"{" ".join(["word"] * 31)}"') == ["quote is longer than 30 words"]


def test_a_post_that_names_what_they_built_and_lists_its_parts_is_named_not_pasted():
    # Ways it could fail: the post's opening line is pasted in quotes ("Build log 3: ..."); the wrong name is picked
    # (what it grew out of, a tool it uses or is a clone of, an acronym, a hashtag or a handle); the parts are said in
    # our words, so a check reads them as ours ("Reward signals": how we found them) or a verb-first item breaks the
    # sentence; a one-word part leaves the draft one detail short of the name swap, so the card waits; and a post with
    # no such name, no list, a release or a GitHub item reads differently than before.
    role = {"id": "mts", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}

    def drafted(quote, url="https://x.com/rin/status/12"):
        sign = {"id": "s1", "subject_id": "A900", "event_type": "work_in_progress", "event_date": "2026-09-10",
                "observed_at": "2026-09-10T15:00:00+00:00", "source_url": url, "quote": quote}
        return sign, routing.template("Rin Vale", role, None, [sign], sign, "Oda", message="early_sign")

    def checked(quote):
        sign, draft = drafted(quote)
        items = [{"id": "s1", "date": "2026-09", "url": sign["source_url"], "text": quote, "context": "", "title": False}]
        return draft, outreach.check(draft, items, role, outreach.facts("mts"), [], "Rin Vale", AS_OF, "Oda")

    post = "Build log 3: turned TinyRay into VoxelRay. - GPU voxel traversal - Soft shadow sampling - A CLI - 40 tests"
    draft, check = checked(post)
    assert 'saw your X post about VoxelRay, and it really caught my eye, especially "GPU voxel traversal" and "Soft ' \
        'shadow sampling".' in draft["body"], draft["body"]
    assert "Build log 3" not in draft["body"] and "TinyRay" not in draft["body"]
    assert len(draft["note"]) <= outreach.NOTE_MAX
    assert check["name_swap"]["passes"] and not check["problems"], check
    for quote in ("Built VoxelRay, a GPU voxel tracer. - GPU voxel traversal - Soft shadow sampling - A CLI",
                  "Day 3 of #BuildInPublic: shipped VoxelRay. - GPU voxel traversal - Soft shadows - A CLI",
                  "Built VoxelRay, merged into LangChain. - GPU voxel traversal - Soft shadow sampling - A CLI",
                  "I built VoxelRay. - GPU voxel traversal - Soft shadow sampling - A CLI",
                  "Extended TinyRay into VoxelRay. - GPU voxel traversal - Soft shadow sampling - A CLI"):
        assert "about VoxelRay," in drafted(quote)[1]["body"], quote
    # Their words stay in quotes, so no check reads them as ours, and a verb-first item stays a quote of theirs.
    for parts in ("Reward signals for agents - Benchmarks this week", "Added voxel traversal - Wrote the docs"):
        draft, check = checked(f"Built VoxelRay, a voxel tracer for game clips. - {parts} - A CLI")
        assert "about VoxelRay" in draft["body"] and not check["problems"] and check["name_swap"]["passes"], check
    # Nothing to name, so the draft stays as it was: a tool or an acronym with no word saying they made it; a venue, a
    # company or a tool after a looser word ("made it into", "meet", "compiled into"); a name that
    # describes the thing rather than names it ("ChatGPT plugin"); a second name, which may be someone else's; a part
    # in quotes of its own; a one-word part; or no list.
    parts = " - GPU voxel traversal - Soft shadow sampling - A CLI"
    for quote in ("A NumPy clone in pure Go. - Formula parser - Lazy recalculation - A CLI",
                  "My tiny LLM from scratch. - Byte pair tokenizer - Rotary attention layers - Top-k sampling",
                  "Our paper made it into NeurIPS!" + parts, "Excited to meet OpenAI researchers at the summit." + parts,
                  "Compiled VoxelRay into WebAssembly." + parts,
                  "Built ChatGPT plugin for game clips." + parts, "Released PyPI package for game clips." + parts,
                  "Built macOS menubar tracker for clips." + parts, "Built VoxelRay with NumPy today." + parts,
                  "Rin built VoxelRay and Dana built GridWorld." + parts, "Dana built GridWorld, and I built VoxelRay." + parts,
                  "Our paper made NeurIPS!" + parts, "Extended our renderer into WebAssembly." + parts,
                  "Extended support into WebAssembly." + parts, "Turned feedback into LangChain." + parts,
                  "Grew traffic into GitHub." + parts,
                  'Built VoxelRay, a voxel tracer for game clips. - "GPU" voxel traversal - Soft shadow sampling - A CLI',
                  "Built VoxelRay, a voxel tracer for game clips. - kernels - shaders - A CLI",
                  "Wrote a toy physics engine from scratch - rigid bodies - a contact solver - broadphase",
                  "Built VoxelRay over the weekend, a voxel tracer for game clips."):
        assert " about " not in drafted(quote)[1]["body"] and 'post: "' in drafted(quote)[1]["body"], quote


def test_a_named_draft_that_reads_fine_for_someone_else_falls_back_to_the_post_line(tmp_path):
    # Ways it could fail: the named draft's details are all also true of another person in the run (they posted about
    # the same project), so the name swap holds a card the pasted line used to let through; or the fallback hides a
    # named draft that passes.
    posts = Path(__file__).parent / "fixtures" / "posts"
    said = "Build log 3: turned TinyRay into VoxelRay. - GPU voxel traversal - Soft shadow sampling - A CLI - 40 tests"
    asked = "Has anyone found a good source of action-labeled gameplay video?"  # Mara's post behind her call
    timeline = json.loads((posts / "timeline.json").read_text())
    timeline["events"] = [{**e, "quote": e["quote"].replace(asked, said)} if e["subject_id"] == "X900" else e
                          for e in timeline["events"]]

    def mara(others):
        s = Store(f"sqlite:///{tmp_path / f'v{len(others)}.sqlite'}")
        routing.seed(s, timeline)
        for entry in json.loads((posts / "ledger.json").read_text()):
            routing.contact.record(s, **entry)
        contacts = json.loads((posts / "contacts.json").read_text()).get("X900", [])
        return routing.route(s, "X900", AS_OF, TEAM, contacts, cohort=others)
    alone = mara({})
    assert "post about VoxelRay," in alone.draft["body"] and alone.outreach["checks"]["passes"], alone.draft["body"]
    same = mara({"B100": ("Omar Lind", outreach.text_of("VoxelRay update: GPU voxel traversal and soft shadow sampling"))})
    assert 'post: "Build log 3: turned TinyRay into VoxelRay."' in same.draft["body"], same.draft["body"]
    assert same.outreach["checks"]["passes"], same.outreach["checks"]
    # Both fail: the card is held reading as it did before, with the post's line.
    both = mara({"B100": ("Omar Lind", outreach.text_of(said))})
    assert not both.outreach["checks"]["passes"] and 'post: "Build log 3' in both.draft["body"], both.draft["body"]


def test_a_congratulation_is_checked_against_what_happened_not_what_the_note_is_about():
    from app import outreach

    role = {"id": "mts-research", "title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
    news = {"id": "p1", "subject_id": "A900", "event_type": "paper_accepted", "event_date": "2026-09-10",
            "observed_at": "2026-09-10T15:00:00+00:00", "source_url": "https://openreview.net/forum?id=x1",
            "quote": "Sparse Action Tokens for Game Agents: Accept (poster) (NeurIPS 2026)"}
    items = [{"id": "p1", "date": "2026-09", "url": news["source_url"], "text": news["quote"], "context": "",
              "title": True}]
    for role_free in ("undergraduate", "acquisition"):  # a note about their work, whatever it is about
        drafted = routing.template("Rin Vale", role, None, [news], None, "Oda", role_free=role_free, news=news)
        assert "was accepted" in drafted["body"]
        moments = {"accepted"} | ({"acquisition"} if role_free == "acquisition" else set())
        problems = outreach.check(drafted, items, role, outreach.facts("mts-research"), [], "Rin Vale", AS_OF, "Oda",
                                  role_free, moments)["problems"]
        assert not any("not what happened" in p for p in problems), (role_free, problems)
    # A part named in their words may stop where their sentence goes on; a sentence of theirs may not.
    said = [{"id": "x1", "date": "2026-09", "url": "https://x.com/rin/status/1", "context": "", "title": False,
             "text": "Shipped a replay tool with a contact solver that never sleeps and a tiny trainer"}]
    fragment = 'Hey Rin, "a contact solver that never sleeps" really caught my eye.'
    cut = 'Hey Rin, saw your X post: "Shipped a replay tool with a contact solver." It really caught my eye.'
    assert not outreach.grounded(fragment, said, "")
    assert outreach.grounded(cut, said, "") == ['quote stops mid-sentence: "…pped a replay tool with a contact solver"']
    assert not outreach.VAGUE.search("Your Lantern release really caught my eye, and so did the GitHub repo tour.")
    assert outreach.VAGUE.search("Saw your new GitHub repo, and it really caught my eye.")


def test_a_bare_year_or_date_is_never_a_detail_only_true_of_them():
    from app import outreach

    own = outreach.text_of("Pocket Diary shipped in 2026 on Sep 10", "clips")
    found = outreach.anchors("Saw your post from 2026 on Sep 10 about clips.", own, outreach.text_of(""))
    assert found == []  # 2026, Sep and 10 are true of anyone
    assert not outreach.name_swap("Saw Pocket Diary in 2026.", own, outreach.text_of(""), [])["passes"]  # one detail
    # Their own numbers still count; a day or a year never does.
    mine = outreach.text_of("Our clip tool hit 737 stars and 12000 downloads")
    assert [a for a, _ in outreach.anchors("Saw 12000 downloads!", mine, outreach.text_of(""))] == ["12000 download"]


def test_one_detail_is_enough_only_when_it_names_what_their_own_post_ships():
    from app import happened, outreach

    for post, named in [("Tide Journal v2.4 is live for everyone", "Tide Journal v2.4"),
                        ("Shipped Pocket Diary v2.3 today", "Pocket Diary v2.3"),
                        ("Introducing Tide Journal Studio Pro Max v12.10.3", "Tide Journal Studio Pro Max v12.10.3"),
                        ("My game Tiny Harbor is out on Steam, built in Godot 4.3", ""),  # someone else's engine
                        ("Updated Tide Journal for Unity 6.1", ""), ("Tide Journal is out! Rated 4.8 on the App Store", ""),
                        ("Chapter 3.2 of my design notes is up", ""), ("Version 2.4 of Tide Journal is live", ""),
                        ("Big News: Tiny Harbor 2.0 is live", "Tiny Harbor 2.0"), ("Finally moved our tooling to Python 3.12", "")]:
        assert happened.shipped(post) == named, post
    role = {"id": "backend", "title": "Backend Engineer", "jd_url": "https://jobs.example.test/be"}
    item = lambda text: [{"id": "p1", "date": "2026-09-10", "url": "https://x.com/a/status/1", "text": text,  # noqa: E731
                          "context": "", "title": False}]
    weak = {"subject": "Hello", "body": "Hey Ada, saw you moved to Python 3.12. We're hiring for our Backend Engineer "
                                        "role, and I'd love to chat."}
    checked = outreach.check(weak, item("Finally moved our tooling to Python 3.12"), role, [], (), "Ada Lune", AS_OF)
    assert checked["name_swap"]["anchors"] == ["python 3.12"] and not checked["passes"]  # a tool, not their release
    own = {"subject": "Hello", "body": "Hey Ada, congrats on shipping Tiny Harbor 2.0! We're hiring for our Backend "
                                       "Engineer role, and I'd love to chat."}
    assert outreach.check(own, item("Big News: Tiny Harbor 2.0 is live"), role, [], (), "Ada Lune", AS_OF)["passes"]


def test_a_draft_never_names_the_day_a_post_went_up_and_a_short_month_needs_its_year():
    from app import outreach

    items = [{"id": "p1", "date": "2025-09-17", "text": "Pocket Diary v2.3 is out now"}]
    day = f'{outreach.DAY_WHY}: "Sep 17"'  # its day depends on a time zone we don't know
    assert outreach.clean("We're hiring. Saw your post on Sep 17, 2025.", items, AS_OF) == [day]
    assert outreach.clean("We're hiring. Saw your post from 17 September.", items, AS_OF)[0].startswith(outreach.DAY_WHY)
    assert outreach.clean("We're hiring. Saw your post from Sep.", items, AS_OF) == \
        ['a date that is not absolute: "Sep"']  # last year's month needs its year
    assert outreach.clean("We're hiring. Saw your post from Sep 2025.", items, AS_OF) == []
    assert outreach.clean("We're hiring. Saw your post from Sep.", [{**items[0], "date": "2026-09-17"}], AS_OF) == []


def test_a_day_they_named_in_their_own_post_is_theirs_to_say():
    from app import outreach

    # Phones and LinkedIn curl quote marks, so part of their quote can read as ours: the day is still theirs.
    items = [{"id": "p1", "date": "2026-09-17", "text": "Our “Brambleworld” demo is live, come play before Sep 30"}]
    draft = 'We\'re hiring. Saw your X post: "Our “Brambleworld” demo is live, come play before Sep 30."'
    assert outreach.clean(outreach.ours(draft, items), items, AS_OF) == []
    assert outreach.clean(outreach.ours(draft + " Before Sep 29?", items), items, AS_OF) == \
        [f'{outreach.DAY_WHY}: "Sep 29"']  # a day they didn't name is ours, and fails


def test_a_quote_is_closed_with_a_full_stop_only_after_a_word():
    assert routing._in_quotes("Tide Journal v2.4 is live for everyone") == '"Tide Journal v2.4 is live for everyone."'
    assert routing._in_quotes("It's out now 🎉") == '"It\'s out now 🎉"'
    assert routing._in_quotes("Get it at https://tool.example") == '"Get it at https://tool.example"'
    assert routing._in_quotes("Is it out?") == '"Is it out?"'


def test_a_public_moment_beside_an_early_sign_is_just_happened_and_the_draft_opens_with_it(store):
    from app import happened, readiness

    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    moment = routing.behind_moment(r.happened, r.call)
    assert r.kind == "just_happened" and moment.kind == "paper"
    # The same call with an early sign as well stays Just happened: once it is public, it is no longer early.
    early = r.call.model_copy(update={"evidence": {"work_in_progress": ["x1"], **r.call.evidence}})
    assert readiness.opener(early) == "x1" and happened.kind(early, r.happened) == "just_happened"
    assert happened.kind(early, []) == "early_sign"


class Poster:
    """The Slack app's poster, as routing sees it: the list's ts back, cards under it."""
    def __init__(self):
        self.posts = []

    def post(self, card, route=None, test=False, thread=None):
        self.posts.append((card, route, thread))
        return {"channel": "C1", "ts": f"{len(self.posts)}.000"}


def test_the_morning_list_names_who_to_reach_and_each_card_goes_in_its_thread(store):
    from app import contact

    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00",
                   links=["https://rinvale.example/", "https://github.com/rinvale-example"])
    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    to = Poster()
    sent = routing.send_morning(store, [r], to, now=AS_OF)
    (listed, _, none), (card, who, thread) = to.posts
    assert none is None and who is r and thread == "1.000" and card == r.card  # the card, under the list
    assert listed["blocks"][0]["text"]["text"] == "Who to reach this morning"
    assert "• *Rin Vale*, Member of Technical Staff: Just happened · Rin had a paper accepted on Aug 20 · reach by " \
           "Sep 19" in listed["blocks"][1]["text"]["text"]
    assert [e["kind"] for e in contact.history(store, "A900")][-1] == "pinged" and len(sent) == 1
    # The next morning nothing is new: the ledger holds the card, so nothing posts at all.
    later = Poster()
    assert routing.send_morning(store, [r], later, now="2026-09-16T00:00:00+00:00") == [] and later.posts == []
    assert routing.morning([], AS_OF) is None


def test_the_morning_list_leaves_out_anyone_the_ledger_or_the_weekly_cap_holds(store):
    from app import contact

    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)  # no identity record: check first, never listed
    assert r.gate.state == "check_first"
    to = Poster()
    assert routing.send_morning(store, [r], to, now=AS_OF) == [] and to.posts == []
    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00",
                   links=["https://rinvale.example/", "https://github.com/rinvale-example"])
    clear = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    for n in range(contact.WEEKLY_CAP):  # the role's cards this week are used up
        contact.record(store, f"Z{n}", "pinged", at="2026-09-14T00:00:00+00:00", role_id="mts-research")
    assert routing.send_morning(store, [clear], to, now=AS_OF) == [] and to.posts == []


def test_the_morning_list_keeps_the_rails_the_cap_across_roles_a_failing_draft_and_the_pause(store):
    from app import contact

    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00",
                   links=["https://rinvale.example/", "https://github.com/rinvale-example"])
    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    to = Poster()
    failing = r.model_copy(update={"outreach": {**r.outreach, "checks": {"passes": False, "problems": ["too long"]}}})
    assert routing.sendable(store, [failing], AS_OF) == []  # the card would say "Don't send this draft yet"
    assert routing.send_morning(store, [failing], to, now=AS_OF) == [] and to.posts == []
    contact.pause("Justin", "Checking a card.", at="2026-09-15T00:00:00+00:00")
    with pytest.raises(ValueError, match="Sending is paused"):
        routing.send_morning(store, [r], to, now=AS_OF)
    assert to.posts == [] and "pinged" not in [e["kind"] for e in contact.history(store, "A900")]
    contact.resume()
    for n in range(contact.TOTAL_CAP):  # other roles' cards this week use up the cap across roles
        contact.record(store, f"Z{n}", "pinged", at="2026-09-14T00:00:00+00:00", role_id=f"role-{n}")
    assert routing.sendable(store, [r], AS_OF) == []
    assert routing.send_morning(store, [r], to, now=AS_OF) == [] and to.posts == []


def test_the_morning_list_names_a_person_once_and_a_refused_card_does_not_stop_the_rest(store):
    from app import contact

    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00",
                   links=["https://rinvale.example/", "https://github.com/rinvale-example"])
    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    backend = r.model_copy(update={"subject_id": "backend:A900", "role": {**r.role, "id": "backend"}})
    assert routing.sendable(store, [r, backend], AS_OF) == [r]  # one card per person, whatever the role
    bo = r.model_copy(update={"subject_id": "B900", "name": "Bo Test"})

    class Refusing(Poster):
        def post(self, card, route=None, test=False, thread=None):
            if route is r:
                raise RuntimeError("Slack refused the card: 429 ratelimited")
            return super().post(card, route, test, thread)
    to, held = Refusing(), []
    sent = routing.send_morning(store, [r, bo], to, now=AS_OF, held=held)
    assert [s for s, _ in sent] == [bo] and held == [(r, "Slack refused the card: 429 ratelimited")]
    assert "Rin Vale" in to.posts[0][0]["text"] and to.posts[1][1] is bo  # the list, then the card that went
    assert "pinged" not in [e["kind"] for e in contact.history(store, "A900")]


def test_the_model_redrafts_only_a_failing_free_draft_and_a_model_error_holds_only_that_card(store):
    from app import contact, outreach, providers

    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00",
                   links=["https://rinvale.example/", "https://github.com/rinvale-example"])
    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    failing = r.model_copy(update={"outreach": {**r.outreach, "checks": {"passes": False, "problems": ["too long"]}}})
    bo = failing.model_copy(update={"subject_id": "B900", "name": "Bo Test"})
    cy = failing.model_copy(update={"subject_id": "C901", "name": "Cy Test"})
    di = failing.model_copy(update={"subject_id": "D901", "name": "Di Test"})
    held = r.model_copy(update={"subject_id": "C900", "gate": contact.Clearance(state="hold", reason="never"),
                                "outreach": failing.outreach})
    tried = []

    def again(route):  # Rin's model draft passes; Bo's gives nothing back; Cy's is over the cap; Di's call fails
        tried.append(route.subject_id)
        if route.subject_id == "C901":
            raise outreach.OverCap("this run's cap ($1.00, 30 calls) is spent")
        if route.subject_id == "D901":
            raise providers.ProviderError("Anthropic returned HTTP 529. Try again later.")
        return r if route.subject_id == "A900" else None
    found, not_tried, failed = routing.redraft([failing, bo, held, r, cy, di], again)
    assert sorted(tried) == ["A900", "B900", "C901", "D901"]  # never a held route, never one that already passes
    assert [f.subject_id for f in found] == ["A900", "B900", "C900", "A900", "C901", "D901"] and found[0] is r
    assert not_tried == "1 model draft not tried: this run's cap ($1.00, 30 calls) is spent"
    assert failed == ["Di Test's model draft failed, so the card stays held: Anthropic returned HTTP 529. Try again "
                      "later."]  # and the run went on past it
    kept, quiet = routing.pick(store, [found[0], found[1], found[5]], AS_OF)
    assert [k.subject_id for k in kept] == ["A900"]
    assert [q.gate.reason for q in quiet] == ["The draft fails a check (too long): fix it before a card goes out."] * 2


def test_a_test_morning_marks_the_list_and_every_card(store):
    from app import contact

    contact.record(store, "A900", "identity", at="2026-09-01T00:00:00+00:00",
                   links=["https://rinvale.example/", "https://github.com/rinvale-example"])
    r = routing.route(store, "A900", AS_OF, TEAM, CONTACTS)
    to = Poster()
    routing.send_morning(store, [r], to, now=AS_OF, test=True)
    (listed, _, _), (card, who, thread) = to.posts
    assert listed["blocks"][0]["text"]["text"] == "Test · Who to reach this morning"
    assert card["blocks"][0]["text"]["text"] == "Test · Rin Vale" and "<http" not in json.dumps(card)
    assert who.card == card and thread == "1.000"


# How a card's quote of their words can go wrong, listed before the fix (a real card cut a numbered
# list mid-sentence at 90 characters):
#   1. Cut mid-sentence, or mid-word, at the length limit. It must end where a sentence, a line or a list's item ends.
#   2. A first sentence a little over the limit loses its end: shown whole up to 160 characters.
#   3. A sentence too long even for that is still cut: it is left out (the line keeps its link), never cut.
#   4. A quote the feed or the reader already cut ("…" or "..." at its end) is shown up to the cut: only whole
#      sentences before it may be.
#   5. A stop inside a sentence ("e.g. for", "v2. then", "3.5 hours") is read as a sentence's end.
#   6. A number or a dash inside a sentence ("version 2. Then", "fast - really fast") is read as a list and cut there.
#   7. A quote ends on their own ellipsis mid-post ("canvas…"), which reads as our cut: never end on one.
#   8. A short quote, a paper's title or the withheld note changes.
# And, found by the separate read after the fix:
#   9. An abbreviation or an initial before a capital or a number ("Dr. Smith", "et al. 2024", "U.S. Army", "e.g.
#      Foo", "vs. GPT-4") is read as a sentence's end, and the quote stops there with no mark of a cut.
#  10. The post they answered ("In reply to ...") is quoted as their words, whatever it says.
#  11. An aside between two dashes, or "1) ... and 2) ..." inside a sentence, is read as a list and cut there.
#  12. A quote that fits whole is dropped for ending on a colon ("Big news:").
#  13. A list's own item number ("1. Data ...", "2. Evals ...") is read as a sentence's end, and the quote ends on "2.".
#  14. A sentence's own numbers ("Step 1. Then step 2.") are read as a list and the quote is cut before "2.": a list
#      starts with a "1." that opens the text, a line, or follows a stop, a colon or an ellipsis, and counts on.
#  15. A list whose "1." opens a line takes a later "2." inside a sentence as its next item, and cuts the sentence.
#  16. A list the rule doesn't find (one starting at "2.", or after an emoji) ends the quote on its bare number: a
#      number that opens a line never ends a sentence.
#  17. A list indented with a non-breaking or an em space is no longer found, and the quote ends on its "3.".
CANVAS = ("clip board round two…\n1. load a match\n2. circle the player you want to follow\n3. say 'keep the camera "
          "on them through the fight' and watch it recut")


@pytest.mark.parametrize("quote, shown", [
    (CANVAS, "“clip board round two… 1. load a match 2. circle the player you want to follow”"),  # 1, 7
    (CANVAS.replace("\n", " "), "“clip board round two… 1. load a match 2. circle the player you want to follow”"),
    ("Rebuilding our component library in Figma variables so a clip editor prototype can ship on desktop and mobile "
     "from one set of tokens. Early, but auto layout is holding up.",
     "“Rebuilding our component library in Figma variables so a clip editor prototype can ship on desktop and mobile "
     "from one set of tokens.”"),  # 2
    ("Trained it for 3.5 hours on e.g. four GPUs. It holds. Next up is a v2. then a writeup of what broke along the "
     "way and why the rollouts drift after ten seconds.",
     "“Trained it for 3.5 hours on e.g. four GPUs. It holds.”"),  # 5
    ("We shipped version 2. Then the renderer got fast - really fast, and the clips stopped dropping frames on every "
     "laptop we tried it on.", "“We shipped version 2.”"),  # 6: a sentence end, never a list
    ("Been hacking on a tiny renderer for game clips. It runs in the browser and streams straight from the replay "
     "buffer into a…", "“Been hacking on a tiny renderer for game clips.”"),  # 4
    ("Scaling laws II: Accept (NeurIPS 2026)", "“Scaling laws II: Accept (NeurIPS 2026)”"),  # 8
    (routing.WITHHELD, routing.WITHHELD),
    ("Had a great chat with Dr. Smith about world models for replays and what data would help most for the next run.",
     "“Had a great chat with Dr. Smith about world models for replays and what data would help most for the next "
     "run.”"),  # 9: one sentence, whole
    ("Building on Smith et al. 2024 we trained on U.S. Army sim logs, e.g. Foo and Bar, and it beat vs. GPT-4 on "
     "every replay.", "“Building on Smith et al. 2024 we trained on U.S. Army sim logs, e.g. Foo and Bar, and it beat "
                      "vs. GPT-4 on every replay.”"),  # 9
    ("Great point, agreed.\n\nIn reply to @someone_example: their post", "“Great point, agreed.”"),  # 10
    ("Our world model – trained only on game replays – now holds together past ten seconds. Clips soon.",
     "“Our world model – trained only on game replays – now holds together past ten seconds.”"),  # 11
    ("Ranked 1) first on replays and 2) second on the new sim benchmark we built for it over the summer. Paper soon.",
     "“Ranked 1) first on replays and 2) second on the new sim benchmark we built for it over the summer.”"),  # 11
    ("Big news:", "“Big news:”"),  # 12
    ("Two things I learned:\n1. Data beats tricks for world models on replays.\n2. Evals matter more than anything "
     "else we built this year, by far.",
     "“Two things I learned: 1. Data beats tricks for world models on replays.”"),  # 13
    ("1. Load a match 2. Circle the player you want to follow 3. Say 'keep the camera on them' and watch it recut",
     "“1. Load a match 2. Circle the player you want to follow”"),  # 13
    ("Step 1. Then step 2. Then a long wait for the sim to finish its run on every replay we have, which took days.",
     "“Step 1. Then step 2.”"),  # 14
    ("Lessons:\n1. Data beats tricks for world models on replays\nWe then shipped version 2. Then the clips got long "
     "enough to matter for every eval.",
     "“Lessons: 1. Data beats tricks for world models on replays We then shipped version 2.”"),  # 15
    ("Picking up where I left off:\n2. Circle the player you want to follow\n3. Say 'keep the camera on them' and "
     "watch it recut the whole clip", "“Picking up where I left off: 2. Circle the player you want to follow”"),  # 16
    ("Lessons:\n\u00a01. Data beats tricks for world models\n\u00a02. Evals matter more than you think\n\u00a03. Ship "
     "the thing and then keep shipping it", "“Lessons: 1. Data beats tricks for world models 2. Evals matter more than "
                                            "you think”"),  # 17
    ("Lessons:\n\u20031. Data beats tricks for world models\n\u20032. Evals matter more than you think\n\u20033. Ship "
     "the thing and then keep shipping it", "“Lessons: 1. Data beats tricks for world models 2. Evals matter more than "
                                            "you think”"),  # 17
])
def test_a_card_quotes_their_words_up_to_where_a_sentence_ends_never_mid_sentence(quote, shown):
    assert routing._quoted(quote, 90) == shown


@pytest.mark.parametrize("quote", [
    "Been hacking on a tiny renderer for game clips that runs in the browser and streams straight from the replay "
    "buffer into a shared canvas where anyone in the call can scrub, annotate and cut a highlight together live",  # 3
    "clip board round two…",  # 7: their own ellipsis, or a cut
    "Been hacking on a tiny renderer for game clips that runs in the browser and streams…",  # 4
    "In reply to @someone_example: their post, never the subject's words",  # 10
])
def test_a_card_leaves_out_a_quote_it_could_only_cut_and_keeps_the_link(quote):
    assert routing._quoted(quote, 90) == ""
