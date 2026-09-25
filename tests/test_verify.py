import asyncio

from app import verify
from app.engine import digest

QUOTE = "Dana Reyes joined Northwind as VP Finance on March 3, 2026."
CLAIM = "Dana Reyes is currently looking for a new role."
PERSON = {"id": "dana-reyes", "name": "Dana Reyes"}
LATER = ("Northwind announced on July 8, 2026 that Dana Reyes has been promoted to CFO. "
         "She joined Northwind in March.")
EARLIER = "Dana Reyes joined Northwind as VP Finance on March 3, 2026."


def run(coro):
    return asyncio.run(coro)


def test_entails_is_cached_per_quote_and_claim(model, store, settings):
    model.reply({"verdict": "contradicts", "reason": "The quote states a new job started."})
    first = run(verify.entails(QUOTE, CLAIM, settings=settings, store=store))
    second = run(verify.entails(QUOTE, CLAIM, settings=settings, store=store))
    assert first == second == {"verdict": "contradicts",
                               "reason": "The quote states a new job started.", "provider": "claude"}
    assert len(model.calls) == 1
    assert model.calls[0]["payload"]["max_tokens"] <= 300
    # Pinned: results cached before the Jev path was cut stay readable.
    assert store.get("entailment:" + digest([QUOTE, CLAIM, "entails_v1", "claude"]))["verdict"] == "contradicts"


def test_supersession_keeps_only_grounded_findings_after_the_trigger(model, store, settings):
    seen = []

    async def search(query, after):
        seen.append((query, after))
        return [
            {"url": "https://example.com/promo", "text": LATER, "published_at": "2026-07-08T00:00:00Z"},
            {"url": "https://example.com/old", "text": EARLIER, "published_at": "2026-03-01T00:00:00Z"},
            {"url": "https://example.com/promo", "text": LATER},  # duplicate url
        ]

    model.reply({"findings": [
        {"kind": "promotion", "event_date": "2026-07-08",
         "quote": "Northwind announced on July 8, 2026 that Dana Reyes has been promoted to CFO."},
        # Date not in the quote and page is after the trigger: kept undated.
        {"kind": "new_job", "event_date": "2026-03-03", "quote": "She joined Northwind in March."},
        {"kind": "retention_deal", "event_date": None, "quote": "not on the page"},
    ]})
    result = run(verify.supersession_check(
        PERSON, "2026-03-03", "Northwind", search, settings=settings, store=store,
    ))
    assert len(seen) == 2 and all(after == "2026-03-03" for _, after in seen)
    assert all('"Dana Reyes"' in query and "Northwind" in query for query, _ in seen)
    assert result["sources_checked"] == 1  # The pre-trigger page was never sent to the model.
    assert [(f["kind"], f["event_date"], f["source_url"]) for f in result["findings"]] == [
        ("promotion", "2026-07-08", "https://example.com/promo"),
        ("new_job", None, "https://example.com/promo"),
    ]
    assert len(model.calls) == 1
    # Same page, same trigger: no second call.
    again = run(verify.supersession_check(
        PERSON, "2026-03-03", "Northwind", search, settings=settings, store=store,
    ))
    assert again["findings"] == result["findings"] and len(model.calls) == 1


def test_supersession_drops_undated_findings_on_undated_pages(model, store, settings):
    def search(query, after):
        return [{"url": "https://example.com/bio", "text": EARLIER}]

    model.reply({"findings": [{"kind": "new_job", "event_date": None, "quote": EARLIER}]})
    result = run(verify.supersession_check(
        PERSON, "2026-03-03", "", search, settings=settings, store=store,
    ))
    assert result["findings"] == [] and result["sources_checked"] == 1


def contact_now_proposal():
    return {"events": [{"timing_action": "contact_now", "date": "2026-03-03", "why_now": CLAIM,
                        "timing_assessment": {"person_impact": {"quote": QUOTE}}}]}


def test_kill_pass_downgrades_on_contradiction_and_records_why(model, store, settings):
    model.reply({"verdict": "contradicts", "reason": "A new job started."})

    def search(query, after):
        return []

    proposal = run(verify.kill_pass(contact_now_proposal(), {"name": "Dana Reyes"}, settings=settings,
                                    store=store, search_fn=search, budget=verify.providers.Budget(settings)))
    event = proposal["events"][0]
    assert event["timing_action"] == "verify_first"
    assert event["kill_pass"]["entailment"]["verdict"] == "contradicts"


def test_kill_pass_downgrades_on_later_supersession_and_survives_search_failure(model, store, settings):
    # Supersession runs first (one small call per source), then entailment.
    model.reply({"findings": [{"kind": "promotion", "quote": "Dana Reyes has been promoted to CFO",
                               "event_date": None}]})
    model.reply({"verdict": "supports", "reason": "Consistent."})

    def search(query, after):
        return [{"url": "https://news.example.test/dana", "text": LATER, "published_at": "2026-07-08"}]

    event = run(verify.kill_pass(contact_now_proposal(), {"name": "Dana Reyes"}, settings=settings, store=store,
                                 search_fn=search, budget=verify.providers.Budget(settings)))["events"][0]
    assert event["timing_action"] == "verify_first" and event["kill_pass"]["superseded_by"]

    def broken(query, after):
        raise verify.providers.ProviderError("Exa is not configured")

    quiet = run(verify.kill_pass({"events": [{"timing_action": "watch"}]}, {"name": "x"}, settings=settings,
                                 store=store, search_fn=broken, budget=verify.providers.Budget(settings)))
    assert quiet["events"][0]["timing_action"] == "watch"  # no contact_now: nothing runs


def test_restated_trigger_and_feed_page_dates_do_not_supersede(model, store, settings):
    model.reply({"findings": [{"kind": "new_job", "quote": QUOTE, "event_date": None}]})
    model.reply({"verdict": "supports", "reason": "Consistent."})

    def search(query, after):  # a profile page: its date is latest activity, not the statement's
        return [{"url": "https://www.linkedin.com/in/dana", "text": QUOTE + " More.", "published_at": "2026-08-22"}]

    proposal = {**contact_now_proposal(), "evidence": [{"excerpt": QUOTE}]}
    event = run(verify.kill_pass(proposal, {"name": "Dana Reyes"}, settings=settings, store=store,
                                 search_fn=search, budget=verify.providers.Budget(settings)))["events"][0]
    assert event["timing_action"] == "contact_now" and event["kill_pass"]["superseded_by"] == []


def hook(**overrides):
    """A watch hook engine.decide could promote: grounded purpose, value and reasons."""
    return {"timing_action": "watch", "kind": "professional_update", "date": "2026-03-03", "evidence_ids": ["src"],
            "contact_purpose": "rapport", "purpose_reason": {
                "statement": "Her new finance role invites a comparison of close practices.",
                "evidence_id": "src", "quote": QUOTE},
            "recipient_value": "A concrete comparison of close automation approaches.", "why_now": CLAIM,
            "why_wait": "Waiting would show whether the new role settles in.",
            "falsifier": "A later announcement that she stays on would disprove it.",
            "conversation_score": 2, "confidence": "medium", **overrides}


def reach_now():
    from app.readiness import score

    reach = score([{"detector_id": "own_departure", "family": "career", "strength": 0.8, "window_open": "2026-06-01",
                    "window_close": "2026-12-01", "evidence_event_ids": ["e"], "holds": []}], "2026-09-01")
    assert reach.action == "reach_now"
    return reach


def test_kill_pass_checks_the_watch_events_readiness_would_promote(model, store, settings):
    from app.readiness import score

    model.reply({"findings": [{"kind": "promotion", "quote": "Dana Reyes has been promoted to CFO",
                               "event_date": None}]})

    def search(query, after):
        return [{"url": "https://news.example.test/dana", "text": LATER, "published_at": "2026-07-08"}]

    def proposal():  # decide would reject the second (low confidence) and third (no date)
        return {"evidence": [{"id": "src", "excerpt": QUOTE}],
                "events": [hook(), hook(confidence="low", date="2026-01-05"), hook(date=None)]}

    reach = reach_now()
    model.reply({"verdict": "supports", "reason": "Consistent."})
    checked, *rejected = run(verify.kill_pass(proposal(), {"name": "Dana Reyes"}, settings=settings, store=store,
                                              search_fn=search, budget=verify.providers.Budget(settings),
                                              readiness=reach))["events"]
    assert checked["timing_action"] == "verify_first" and checked["kill_pass"]["superseded_by"]
    assert rejected == proposal()["events"][1:]  # never checked: they can never carry a ping
    assert [c["payload"]["system"] == verify.ENTAILS_SYSTEM for c in model.calls] == [False, True]
    # Readiness not reaching: nothing would be promoted, so nothing is searched.
    quiet = run(verify.kill_pass(proposal(), {"name": "Dana Reyes"}, settings=settings, store=store,
                                 search_fn=search, budget=verify.providers.Budget(settings),
                                 readiness=score([], "2026-09-01")))
    assert quiet == proposal() and len(model.calls) == 2


def test_the_kill_pass_spends_calls_on_the_hook_decide_tries_first_and_a_capped_check_is_not_done(model, store, settings):
    model.reply({"verdict": "supports", "reason": "Consistent."})

    def proposal():  # decide tries the stronger hook first, whatever the proposal's order
        return {"evidence": [{"id": "src", "excerpt": QUOTE}], "events": [
            hook(), hook(conversation_score=3, why_now="Dana Reyes has just started a new finance role.")]}

    cap = "Per-run provider call limit reached; increase it in Settings to expand research."
    one_call = verify.providers.Budget({"max_calls_per_run": 1})
    weaker, stronger = run(verify.kill_pass(proposal(), {"name": "Dana Reyes"}, settings=settings, store=store,
                                            search_fn=lambda query, after: [], budget=one_call,
                                            readiness=reach_now()))["events"]
    assert (stronger["kill_pass"]["checked"], stronger["kill_pass"]["errors"]) == (True, [])
    assert [weaker["kill_pass"][k] for k in ("checked", "entailment", "errors")] == [False, None, [cap]]
    # The cap stopping the search for later news leaves every hook unchecked, with nothing more spent.
    capped = verify.providers.Budget({"max_calls_per_run": 1})

    def search(query, after):
        capped.take()
        return []

    events = run(verify.kill_pass(proposal(), {"name": "Dana Reyes"}, settings=settings, store=store,
                                  search_fn=search, budget=capped, readiness=reach_now()))["events"]
    assert [[e["kill_pass"][k] for k in ("checked", "entailment", "errors")] for e in events] == [[False, None, [cap]]] * 2
    assert len(model.calls) == 1


def test_a_provider_error_in_the_kill_pass_still_counts_as_checked(model, store, settings):
    """Fail-open, as agreed: only the run's own call cap leaves a check undone."""
    model.reply({"verdict": "supports", "reason": "Consistent."}, stop_reason="max_tokens")
    kill = run(verify.kill_pass(contact_now_proposal(), {"name": "Dana Reyes"}, settings=settings, store=store,
                                search_fn=lambda query, after: [],
                                budget=verify.providers.Budget(settings)))["events"][0]["kill_pass"]
    assert [kill[k] for k in ("checked", "entailment", "errors")] == [
        True, None, ["Entailment step reached its output limit; nothing was accepted."]]
    # A later source that could not be read: the hook's own quote is still checked.
    model.reply({"findings": []}, stop_reason="max_tokens").reply({"verdict": "supports", "reason": "Consistent."})

    def search(query, after):
        return [{"url": "https://news.example.test/dana", "text": LATER, "published_at": "2026-07-08"}]

    kill = run(verify.kill_pass(contact_now_proposal(), {"name": "Dana Reyes"}, settings=settings, store=store,
                                search_fn=search, budget=verify.providers.Budget(settings)))["events"][0]["kill_pass"]
    assert [kill["checked"], kill["entailment"]["verdict"], kill["errors"]] == [
        True, "supports", ["Findings step reached its output limit; nothing was accepted."]]


def test_reading_a_full_research_run_never_starves_the_kill_pass(model, store, settings, monkeypatch):
    """Eight pages to extract, then two searches and six later sources: the hook's own quote is still checked."""
    from app import main

    monkeypatch.setattr(main, "store", store)
    pages = [{"id": f"p{i}", "url": f"https://pages.example.test/{i}", "text": f"Page {i} about Dana Reyes.",
              "observed_at": "2026-09-01T00:00:00+00:00"} for i in range(8)]
    later = [{"url": f"https://news.example.test/{i}", "text": f"Item {i}.", "published_at": "2026-08-01"} for i in range(6)]

    async def search(query, role, s, budget):
        budget.take()
        return later

    monkeypatch.setattr(main.providers, "_search", search)
    for reply in [{"events": []}] * 8 + [{"findings": []}] * 6 + [{"verdict": "contradicts", "reason": "A new job."}]:
        model.reply(reply)
    proposal = {**contact_now_proposal(), "_input_documents": pages}
    candidate = {"name": "Dana Reyes", "person_id": "person:dana", "role_id": "controller",
                 "profile_url": "https://pages.example.test/0"}
    checked, timeline, calls = run(main.check_research(proposal, candidate, {}, settings | {"exa_key": "x"}))
    assert timeline["pages"] == 8 and timeline["errors"] == []
    assert calls == {"extraction": 8, "kill_pass": 9}
    event = checked["events"][0]
    assert event["kill_pass"]["errors"] == [] and event["kill_pass"]["sources_checked"] == 6
    assert (event["timing_action"], event["kill_pass"]["entailment"]["verdict"]) == ("verify_first", "contradicts")
