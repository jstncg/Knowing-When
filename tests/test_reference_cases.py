"""The two reference cases, anonymized, from page text to the decision.

"Verify case": the person's own post says student researcher until mid-October
and seeking full-time roles, while a third-party profile lists an incoming
research scientist role. The right call is verify first, with the announced
role as the falsifier. "Reach case": the person says they are available for
full-time roles from October 2026; the right call is reach now, not waiting
until October.
"""

import asyncio
from copy import deepcopy

from app import extraction, journey, providers, readiness
from app.engine import decide, review_hash
from app.models import parse_time
from app.sequence import internal_proposal
from tests.test_engine_adversarial import ready_candidate, role

NOW = parse_time("2026-09-22T12:00:00Z")
SEEN = "2026-09-21T09:00:00+00:00"
OWN_POST = ("Update: I am a student researcher at Example Lab until mid-October, "
            "and I am seeking full-time research roles starting after that.")
PROFILE = "Sam Lee. Incoming Research Scientist at Frontier Lab. Previously PhD, Example University."
AVAILABLE = "I will be available for full-time research positions from October 2026."


def record(store, settings, model, person_id, pages):
    """Extract each (url, text, reply) page onto the person's timeline via the production path."""
    for _, _, events in pages:
        model.reply({"events": events})
    docs = [{"url": url, "text": text, "observed_at": SEEN, "content_kind": "full_text"} for url, text, _ in pages]
    candidate = {"person_id": person_id, "name": "Sam Lee", "profile_url": pages[0][0]}
    report = asyncio.run(extraction.record_documents(docs, candidate, settings=settings, store=store,
                                                     budget=providers.Budget(settings)))
    assert report["errors"] == [] and report["events"] >= len(pages)


def own_post_events():
    return [
        {"event_type": "self_stated_availability", "event_date": None, "about_subject": True,
         "quote": "I am seeking full-time research roles starting after that."},
        # "mid-October" states no year, so grounding keeps it undated.
        {"event_type": "placement_end_expected", "event_date": "2026-10", "about_subject": True,
         "quote": "I am a student researcher at Example Lab until mid-October"},
    ]


def dossier(person_id):
    """A reviewed contact-now dossier the model proposed, re-dated to NOW."""
    c = deepcopy(ready_candidate())
    c.update(id="candidate:" + person_id, person_id=person_id, revision=1, reviewed_at=NOW.isoformat())
    for e in c["evidence"]:
        e.update(observed_at="2026-09-21T12:00:00Z", event_date="2026-09-21")
    c["events"][0]["date"] = "2026-09-21"
    c["review_hash"] = review_hash(c, role())
    return c


WATCH = {"status": "active", "brief_version": 1, "fit_basis": "Fictional reference case"}


def test_verify_case_comes_out_verify_first(store, settings, model):
    record(store, settings, model, "person:verify", [
        ("https://posts.example.test/sam", OWN_POST, own_post_events()),
        ("https://papers.example.test/sam", PROFILE, [
            {"event_type": "role_announced", "event_date": None, "about_subject": True,
             "quote": "Incoming Research Scientist at Frontier Lab."}]),
    ])
    scored = journey.assess(store, "person:verify", NOW.isoformat())
    assert scored.action == "verify_first" and "announced_next_role" in scored.holds
    assert scored.falsifiers[0].startswith("Confirm whether the announced role was accepted")
    assert "Incoming Research Scientist at Frontier Lab." in scored.falsifiers[0]

    c = dossier("person:verify")
    assert decide(c, role(), now=NOW)["state"] == "ready"  # the model alone said contact now
    result = decide(c, role(), now=NOW, readiness=scored)
    assert result["state"] == "needs_review" and result["ping"] is None
    assert result["reasons"][0].startswith("Verify first:") and "announced role was accepted" in result["reasons"][0]
    assert internal_proposal(c, role(), WATCH, now=NOW, readiness=scored) is None


def test_verify_case_without_the_announced_role_reaches_now(store, settings, model):
    record(store, settings, model, "person:verify", [("https://posts.example.test/sam", OWN_POST, own_post_events())])
    assert journey.assess(store, "person:verify", NOW.isoformat()).action == "reach_now"


def test_reach_case_stays_reach_now(store, settings, model):
    record(store, settings, model, "person:reach", [
        ("https://home.example.test/kai", AVAILABLE, [
            {"event_type": "self_stated_availability", "event_date": "2026-10", "about_subject": True,
             "quote": AVAILABLE}]),
    ])
    scored = journey.assess(store, "person:reach", NOW.isoformat())
    assert (scored.action, scored.until, scored.holds) == ("reach_now", None, [])
    assert scored.window["detector_id"] == "self_stated_availability"
    assert scored.window["opens"][:10] == "2026-09-21" and scored.window["closes"][:10] == "2026-11-30"

    c = dossier("person:reach")
    result = decide(c, role(), now=NOW, readiness=scored)
    assert result["state"] == "ready"
    ping = result["ping"]
    assert ping["readiness"]["action"] == "reach_now"
    assert ping["trigger"]["window"] == scored.window
    assert ping["confidence"]["timing_evidence"] == scored.confidence == "high"
    assert ping["confidence"]["what_would_prove_wrong"] == [
        "They restate a later date", "They start a new role elsewhere before the window closes"]
    proposal = internal_proposal(c, role(), WATCH, now=NOW, readiness=scored)
    assert proposal["ping"]["readiness"]["action"] == "reach_now"


def test_readiness_overrides_a_model_watch_and_a_quiet_timeline_holds_a_model_contact_now(store, settings, model):
    record(store, settings, model, "person:reach", [
        ("https://home.example.test/kai", AVAILABLE, [
            {"event_type": "self_stated_availability", "event_date": "2026-10", "about_subject": True,
             "quote": AVAILABLE}]),
    ])
    scored = journey.assess(store, "person:reach", NOW.isoformat())
    c = dossier("person:reach")
    # A model watch carries no why-now assessment of its own: readiness supplies the timing.
    c["events"][0].update(timing_action="watch", timing_assessment=None)
    c["review_hash"] = review_hash(c, role())
    unchecked = decide(c, role(), now=NOW, readiness=scored)
    assert unchecked["state"] == "research" and unchecked["reasons"][0].startswith("Research again: the kill pass")
    weak = deepcopy(c)  # a hook that cannot earn a ping says why, whatever the kill pass
    weak["events"][0]["confidence"] = "low"
    assert decide(weak, role(), now=NOW, readiness=scored)["reasons"] == [
        "The evidence does not yet support a strong conversation opportunity."]
    c["events"][0]["kill_pass"] = {"entailment": None, "superseded_by": [], "sources_checked": 1, "errors": []}
    c["review_hash"] = review_hash(c, role())
    assert decide(c, role(), now=NOW, readiness=scored)["state"] == "ready"
    assert internal_proposal(c, role(), WATCH, now=NOW, readiness=scored)["event_id"] == c["events"][0]["id"]
    quiet = readiness.score([], NOW.isoformat())
    held = decide(dossier("person:reach"), role(), now=NOW, readiness=quiet)
    assert held["state"] == "watch" and held["reasons"][0].startswith("Keep watching: No window open")


def test_live_readiness_reads_the_employer_as_a_replay_does(store):
    from app import main, sequence
    from app.journey import PersonContext, save_context
    from app.timeline import add_event
    from tests.test_detectors import ev

    save_context(store, PersonContext(subject_id="person:employer-only", name="Pat Doe", related=["org:acme"]))
    add_event(store, ev("org:acme", "acquisition_closed", "2026-08-01", quote="Acme completed its acquisition by Globex"))
    add_event(store, ev("org:acme", "warn_notice", "2026-09-01", quote="WARN notice filed: 140 employees affected"))
    scored = journey.assess(store, "person:employer-only", NOW.isoformat())
    assert scored.action == "reach_now" and scored.explanation.endswith(journey.BORROWED)
    assert journey.assess(store, "person:nobody", NOW.isoformat()) is None  # nothing but GI in view
    assert main.assess is sequence.assess is journey.assess  # live decisions score what a replay measures


def test_a_layoff_post_reaches_now_until_a_next_job_is_announced(store, settings, model):
    laid_off = "I was laid off from Example Lab on September 15, 2026, and I am figuring out what is next."
    record(store, settings, model, "person:laid-off", [("https://posts.example.test/pat", laid_off, [
        {"event_type": "layoff", "event_date": "2026-09-15", "about_subject": True,
         "quote": "I was laid off from Example Lab on September 15, 2026"}])])
    scored = journey.assess(store, "person:laid-off", NOW.isoformat())
    assert scored.action == "reach_now" and scored.open_windows == ["own_departure"]
    joining = "In January 2027 I will join Frontier Lab as a research scientist."
    record(store, settings, model, "person:laid-off", [("https://posts.example.test/pat-next", joining, [
        {"event_type": "role_announced", "event_date": "2027-01", "about_subject": True, "quote": joining}])])
    held = journey.assess(store, "person:laid-off", NOW.isoformat())
    assert held.action == "watch_until" and "accepted_next_role" in held.holds


def test_a_request_to_wait_that_research_found_holds_on_every_surface(store):
    from app import routing

    quote = "Please contact me after October 20, 2026 to discuss the protocol."
    doc = {"id": "src-1", "url": "https://posts.example.test/sam", "text": "Update from Sam. " + quote,
           "observed_at": SEEN, "content_kind": "highlights"}  # a highlight: the extractor never reads it
    proposal = {"_input_documents": [doc], "events": [
        {"timing_action": "follow_up", "follow_up_on": "2026-10-20", "follow_up_quote": quote, "evidence_ids": ["src-1"]},
        {"timing_action": "watch", "follow_up_on": None, "follow_up_quote": "", "evidence_ids": ["src-1"]}]}
    candidate = {"person_id": "person:wait", "profile_url": "https://home.example.test/sam"}
    assert extraction.record_follow_ups(proposal, candidate, store) == {"follow_ups": 1, "errors": []}
    extraction.record_follow_ups(proposal, candidate, store)  # the same fact, written once
    [event] = store.all("timeline_event")
    assert (event["event_type"], event["event_date"], event["tier"]) == ("contact_constraint", "2026-10-20", 2)
    scored = journey.assess(store, "person:wait", NOW.isoformat())
    assert (scored.action, scored.until[:10]) == ("respect_follow_up", "2026-10-20")
    result = decide(dossier("person:wait"), role(), now=NOW, readiness=scored)
    assert result["state"] == "watch" and "follow up from 2026-10-20" in result["reasons"][0]
    assert routing.route(store, "person:wait", NOW.isoformat(), team=[]) is None


def test_a_note_never_carries_a_hiring_hook(store, settings, model):
    """The web app's ping follows the call's track: on a note, a hook whose purpose is hiring does not ping."""
    record(store, settings, model, "person:reach", [
        ("https://home.example.test/kai", AVAILABLE, [
            {"event_type": "self_stated_availability", "event_date": "2026-10", "about_subject": True,
             "quote": AVAILABLE}]),
    ])
    note = journey.assess(store, "person:reach", NOW.isoformat()).model_copy(update={"track": "rapport"})
    c = dossier("person:reach")
    assert decide(c, role(), now=NOW, readiness=note)["state"] == "ready"  # a rapport hook still pings
    c["events"][0]["contact_purpose"] = "hiring"
    c["review_hash"] = review_hash(c, role())
    assert decide(c, role(), now=NOW, readiness=note.model_copy(update={"track": "pitch"}))["state"] == "ready"
    held = decide(c, role(), now=NOW, readiness=note)
    assert held["state"] == "research" and held["ping"] is None
    assert held["reasons"][0].startswith("A note about their work only")
