"""Prompt caching: each Claude call's system prompt is a cached block, what changes from call to call comes after it,
and what the cache writes and reads is priced in every spend cap."""

import asyncio
import importlib.util
import json

import pytest

from app import ask, extraction, outreach, providers, role_compiler, structured, today

# Anthropic's minimum cacheable prompt, in tokens, for the models these calls use: a shorter one is not cached.
MINIMUM = {"claude-sonnet-5": 1024, "claude-opus-5": 512}


def blocks(payload):
    """The request's text blocks in the order the model reads them: the system prompt, then the user turn."""
    user = payload["messages"][0]["content"]
    return [*payload["system"], *([{"type": "text", "text": user}] if isinstance(user, str) else user)]


def prefix(payload):
    """The blocks up to the last cache marker: what a later call must repeat byte for byte to read the cache."""
    sent = blocks(payload)
    last = max(i for i, b in enumerate(sent) if "cache_control" in b)
    return json.dumps(sent[:last + 1])


def test_a_structured_call_caches_its_system_prompt_and_nothing_that_varies(model, settings):
    for n in (1, 2):
        model.reply({"lines": [{"text": "Not in the data.", "cites": []}]})
        asyncio.run(structured.ask(settings, system="Answer from the facts.", content={"question": f"q{n}"},
                                   output=ask.Answer))
    first, second = (c["payload"] for c in model.calls)
    assert first["system"] == [{"type": "text", "text": "Answer from the facts.", "cache_control": {"type": "ephemeral"}}]
    assert prefix(first) == prefix(second)
    assert first["messages"][0]["content"] == json.dumps({"question": "q1"})  # the user turn as before, not cached


def test_the_ask_panels_facts_are_cached_ahead_of_the_question(model, monkeypatch):
    monkeypatch.setattr(ask, "_spent", {"usd": 0.0})
    monkeypatch.setattr(ask, "_answers", {})
    for question in ("What did they ask for?", "What have they built?"):
        model.reply({"lines": [{"text": "Not in the data.", "cites": []}]})
        asyncio.run(ask.ask("simulation", "X900", question, {"anthropic_key": "k"}))
    first, second = (c["payload"] for c in model.calls)
    system, facts, question = blocks(second)
    assert "cache_control" in system and "cache_control" in facts and "cache_control" not in question
    assert json.loads(facts["text"]) == ask.given(ask.facts("simulation", "X900"))
    assert json.loads(question["text"]) == {"question": "What have they built?"}
    assert prefix(first) == prefix(second)  # the second question reads the first one's cache


def test_the_post_readers_calls_share_one_cached_system_prompt(model, store, settings):
    for n, name in enumerate(("Ada", "Bo"), 1):
        model.reply({"events": []})
        post = {"source_url": f"https://x.com/{name}/status/{n}", "event_date": f"2026-09-1{n}",
                "observed_at": f"2026-09-1{n}T12:00:00+00:00", "quote": f"Post {n} about world models."}
        asyncio.run(extraction.extract_posts([post], {"type": "person", "id": name, "name": name}, settings=settings,
                                             store=store))
    first, second = (c["payload"] for c in model.calls)
    assert first["system"] == [providers.cached(extraction.POSTS_SYSTEM)]
    assert prefix(first) == prefix(second)  # a person's posts only ever follow the cached prompt
    assert "Ada" not in prefix(first) and "Ada" in first["messages"][0]["content"]


def test_each_prompt_repeated_in_a_run_clears_its_models_minimum():
    """At about 4 characters a token (a token covers fewer, so this undercounts), each cached prompt that repeats
    within a run is long enough to cache on its model: the post reader's and the page reader's across a run's people
    and pages, research's across a run's candidates."""
    for system, model in ((extraction.POSTS_SYSTEM, structured.SMALL_MODEL), (extraction.SYSTEM, structured.SMALL_MODEL),
                          (providers.RESEARCH_SYSTEM, providers.DEFAULT_MODEL),
                          (role_compiler.STRATEGY_SYSTEM, providers.DEFAULT_MODEL)):
        assert len(system) / 4 > MINIMUM[model], system[:60]


def test_a_drafts_calls_are_not_cached_so_the_redraft_cap_prices_them_as_before(model, settings):
    model.reply({"lines": [{"text": "Not in the data.", "cites": []}]})
    asyncio.run(structured.ask(settings, system=outreach.SYSTEM, content={"x": 1}, output=ask.Answer, cache=False))
    assert model.calls[0]["payload"]["system"] == outreach.SYSTEM  # the request as it was before caching
    draft = {"person": "Ivy", "items": [{"id": "i1", "text": "A post."}]}
    assert outreach.worst_usd(outreach.SYSTEM, draft) == pytest.approx(outreach.usd(
        {"input_tokens": outreach.DRAFT_CALLS * outreach.tokens_in(outreach.SYSTEM, draft),
         "output_tokens": 2 * (outreach.WRITE_OUT + outreach.GROUND_OUT)}))


def sent_tokens(payload):
    """An upper bound on a request's input tokens: one per UTF-8 byte of everything sent."""
    return len(json.dumps(payload, ensure_ascii=False).encode())


def test_the_ask_panels_worst_case_covers_what_it_sends_as_a_cache_write(model, monkeypatch):
    monkeypatch.setattr(ask, "_spent", {"usd": 0.0})
    monkeypatch.setattr(ask, "_answers", {})
    model.reply({"lines": [{"text": "Not in the data.", "cites": []}]})
    asyncio.run(ask.ask("simulation", "X900", "What did they ask for?", {"anthropic_key": "k"}))
    sent = model.calls[0]["payload"]
    f = ask.facts("simulation", "X900")
    worst = ask.worst_usd({**ask.given(f), "question": "What did they ask for?"})
    assert worst >= outreach.usd({"cache_creation_input_tokens": sent_tokens(sent), "output_tokens": sent["max_tokens"]})


def test_the_post_readers_worst_case_prices_every_input_token_as_a_cache_write():
    spec = importlib.util.spec_from_file_location("posts_script", today.ROOT / "scripts" / "posts.py")
    posts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(posts)
    todo = [{"posts": [{"text": "A week of posts."}]}] * 3
    worst = posts.estimate_usd(todo, worst=True)
    everything = sum(len(extraction.POSTS_SYSTEM) // 2 + len(json.dumps(c)) // 2 + posts.OVERHEAD_TOKENS for c in todo)
    assert worst == pytest.approx(providers.usd({"cache_creation_input_tokens": everything,
                                                 "output_tokens": 3 * extraction.POSTS_MAX_TOKENS}, posts.USD_IN, posts.USD_OUT))
    assert posts.estimate_usd(todo) < worst  # typically the first call writes the prompt and the rest read it
