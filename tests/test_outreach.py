"""Draft outreach: the free checks (name swap, grounding, clean) and the model writer, over invented people."""

import asyncio
import json
from pathlib import Path

import pytest

from app import outreach, providers, readiness
from app.store import Store

ROLE = {"title": "Member of Technical Staff", "jd_url": "https://jobs.example.test/mts"}
AS_OF = "2026-09-15T00:00:00+00:00"
FACTS = [{"id": "gi", "text": "General Intuition builds large action models and world models."}]
IVY = [{"id": "i1", "date": "2026-09-10", "url": "https://x.com/ivy/1", "context": "",
        "text": "Trying diffusion world models on Minecraft trajectories this week. Early rollouts are surprisingly "
                "stable past 200 frames."}]
MARA = outreach.text_of("Has anyone found a good source of action-labeled gameplay video? Our world model keeps "
                        "overfitting to the three games we have.")
SPECIFIC = {"subject": "Minecraft rollouts past 200 frames",
            "body": "Hey Ivy, saw your post on Minecraft trajectories from September 2026. Early rollouts that are "
                    "\"surprisingly stable past 200 frames\" really caught my eye. We're doing similar work at General "
                    "Intuition, which builds large action models and world models. We're hiring for Member of Technical "
                    "Staff, and I'd love to chat more if you're interested."}
GENERIC = {"subject": "Your world model work",
           "body": "Hey Ivy, I have been following your work on world models. At General Intuition we build world "
                   "models too, and we're hiring. Would you be open to a quick chat?"}


def check(draft):
    return outreach.check(draft, IVY, ROLE, FACTS, [("Mara Quill", MARA)], "Ivy Stroud", AS_OF)


def test_a_specific_draft_passes_the_name_swap_and_a_generic_one_reads_fine_for_anyone():
    good = check(SPECIFIC)
    assert good["passes"], good["problems"]
    assert "minecraft trajectories" in good["name_swap"]["anchors"]
    bad = check(GENERIC)
    assert bad["name_swap"]["reads_fine_for"] == ["Mara Quill"]
    assert bad["problems"] == ["fails the name swap: reads fine for 1 other person"]


def test_one_detail_is_not_enough_even_with_nobody_to_swap_against():
    one = {"subject": "Hello", "body": "Ivy, your Minecraft post was great. General Intuition builds world models, "
                                       "and we're hiring."}
    result = outreach.check(one, IVY, ROLE, FACTS, (), "Ivy Stroud", AS_OF)
    assert result["name_swap"]["anchors"] == ["minecraft"]
    assert result["problems"] == ["fails the name swap: 1 detail(s) only true of them, needs 2 (or the release their "
                                  "post names)"]


def test_quotes_and_numbers_must_come_from_their_items_or_gi_facts():
    draft = {"subject": "Minecraft", "body": "Ivy, you wrote \"rollouts are stable past 500 frames\" on Minecraft "
                                             "trajectories. See https://gi.example/p/9f974f5b for 4096 more."}
    problems = check(draft)["problems"]
    assert 'quote is not in their words: "rollouts are stable past 500 frames"' in problems
    assert "number not in any cited item or fact: 4096" in problems
    assert not any("9f974f5b" in p for p in problems)  # links are not read as numbers


def test_no_signal_talk_nothing_personal_and_the_role_named_whatever_the_track():
    draft = {**SPECIFIC, "body": SPECIFIC["body"] + " I noticed you've been posting a lot, and our system flagged it. "
                                                    "Hope your family is well."}
    problems = check(draft)["problems"]
    assert {"talks about their posting", "says how we found them", "touches something personal"} <= set(problems)
    unnamed = {**SPECIFIC, "body": SPECIFIC["body"].split(" We're hiring")[0]}
    assert check(unnamed)["problems"] == ["does not say GI is hiring for the role"]
    assert check({**SPECIFIC, "body": SPECIFIC["body"] + " No pressure — just curious."})["problems"] == \
        ["reads like a template"]  # an em-dash


def test_nothing_personal_even_in_their_own_quoted_words_and_no_religion_or_politics():
    said = "Trying diffusion world models on Minecraft trajectories this week while my kids nap."
    ivy = [{**IVY[0], "text": said}]
    quoted = {**SPECIFIC, "body": SPECIFIC["body"].replace("\"surprisingly stable past 200 frames\"",
                                                           "\"while my kids nap\"")}
    assert "touches something personal" in outreach.check(quoted, ivy, ROLE, FACTS, (), "Ivy Stroud", AS_OF)["problems"]
    for ours in ("Good luck with the election.", "Hope the church retreat went well.", "Keep up the prayer group."):
        assert "touches something personal" in check({**SPECIFIC, "body": SPECIFIC["body"] + " " + ours})["problems"]
    assert outreach.personal("the Church-Turing thesis") is False  # a name in computing, not a church
    for work in ("A family of world models", "my mental model of RL", "leader election in Raft", "a health check",
                 "the mood board for the redesign", "diagnosing a flaky test", "Church encodings in Lean"):
        assert not outreach.personal(work), work  # their work words, whatever our own list says
    for life in ("Back from parental leave", "was diagnosed with cancer", "my H-1B came through",
                 "prayer group at church", "vote for the democrats", "election night nerves"):
        assert outreach.personal(life), life


def test_their_own_quoted_words_are_theirs_to_say():
    said = {**SPECIFIC, "body": SPECIFIC["body"].replace("\"surprisingly stable past 200 frames\"",
                                                         "\"surprisingly stable past 200 frames, and signals stay clean\"")}
    ivy = [{**IVY[0], "text": IVY[0]["text"].replace("200 frames.", "200 frames, and signals stay clean.")}]
    assert outreach.check(said, ivy, ROLE, FACTS, (), "Ivy Stroud", AS_OF)["passes"]


def test_items_put_the_calls_evidence_first_and_keep_the_fullest_text_per_page():
    view = [
        {"id": "a", "subject_id": "p", "event_type": "x_post", "event_date": "2026-09-01", "source_url": "u1",
         "quote": "Old post about something"},
        {"id": "b", "subject_id": "p", "event_type": "x_reply", "event_date": "2026-09-10", "source_url": "u2",
         "quote": "Try the sampler fix\n\nIn reply to @dana: it NaNs at step 400"},
        {"id": "c", "subject_id": "p", "event_type": "work_in_progress", "event_date": "2026-09-05",
         "source_url": "u3", "quote": "Training a world model"},
        {"id": "d", "subject_id": "p", "event_type": "x_post", "event_date": "2026-09-05", "source_url": "u3",
         "quote": "Training a world model on Doom this week"},
        {"id": "e", "subject_id": "employer", "event_type": "x_post", "event_date": "2026-09-11", "source_url": "u4",
         "quote": "Not theirs"},
        {"id": "f", "subject_id": "p", "event_type": "technical_ask", "event_date": "2026-09-10", "source_url": "u2",
         "quote": "Try the sampler fix"},  # the post reader's tag on b: a post it read
        {"id": "g", "subject_id": "p", "event_type": "x_post", "event_date": "2026-09-02", "source_url": "u5",
         "quote": "Our rollout eval is public"},
        {"id": "h", "subject_id": "p", "event_type": "work_in_progress", "event_date": "2026-09-02", "source_url": "u5",
         "quote": "Our rollout eval"},  # read too, older than b
        {"id": "i", "subject_id": "p", "event_type": "github_repo", "event_date": "2026-08-20", "source_url": "u6",
         "quote": "Created p/evals: rollout evals"},  # code the reader tagged nothing on: a draft may still cite it
    ]
    call = readiness.Readiness(as_of="2026-09-15T00:00:00+00:00", score=0.5, action="reach_now", families={},
                               reasons=["work_in_progress"], holds=[], open_windows=[], earliest_close=None,
                               evidence={"work_in_progress": ["c"]}, explanation="")
    got = outreach.items(view, "p", call)
    # a, a post the reader read nothing off, may say anything: no draft sees it (detectors.unread)
    assert [(i["id"], i["text"]) for i in got] == [("c", "Training a world model on Doom this week"),
                                                    ("b", "Try the sampler fix"), ("g", "Our rollout eval is public"),
                                                    ("i", "Created p/evals: rollout evals")]
    assert got[1]["context"] == "@dana: it NaNs at step 400"


def fake_model(monkeypatch, claims):
    """A stand-in for the model: the generic draft first, the specific one on repair, and ``claims`` when asked to
    check a draft. Returns the list of (what was asked for, payload) sent."""
    sent = []

    async def ask(settings, *, content, output, budget, **kwargs):
        budget.take()  # as the real call does
        sent.append((output.__name__, content))
        if output is outreach.Claims:
            return output(claims=claims), {}
        return output(**(SPECIFIC if "problems" in content else GENERIC), cites=["i1", "made-up"]), {}

    monkeypatch.setattr(outreach.structured, "ask", ask)
    return sent


def write(store, calls=5, **kwargs):
    return asyncio.run(outreach.write("Ivy Stroud", ROLE, "rapport", IVY, FACTS, settings={}, store=store,
                                      budget=providers.Budget({"max_calls_per_run": calls}),
                                      others=[("Mara Quill", MARA)], as_of=AS_OF, **kwargs))


SUPPORTED = [{"claim": "Early rollouts are surprisingly stable past 200 frames", "source": "i1",
              "words": "Early rollouts are surprisingly stable past 200 frames"},
             {"claim": "GI builds world models", "source": "gi", "words": "builds large action models and world models"}]


def test_write_repairs_a_failing_draft_once_checks_its_facts_and_caches_every_call(tmp_path, monkeypatch):
    sent = fake_model(monkeypatch, SUPPORTED)
    store = Store(f"sqlite:///{tmp_path / 'o.sqlite'}")
    first = write(store)
    assert first["checks"]["passes"] and first["body"] == SPECIFIC["body"] + "\n\nJustin" and first["cites"] == ["i1"]
    assert [kind for kind, _ in sent] == ["Draft", "Draft", "Claims"]  # the generic draft never reaches the check
    assert sent[1][1]["problems"] == ["fails the name swap: reads fine for 1 other person"]
    assert {s["id"] for s in sent[2][1]["sources"]} == {"i1", "gi", "role"}
    assert write(store) == first and len(sent) == 3  # a second run reads every call from the cache


def test_the_models_linkedin_message_once_connected_is_its_body_less_the_note_and_checked_with_it(tmp_path,
                                                                                                 monkeypatch):
    note = ("Hey Ivy, saw your post on Minecraft trajectories from September 2026. We're hiring for Member of "
            "Technical Staff at General Intuition, and I'd love to connect.")

    async def ask(settings, *, content, output, budget, **kwargs):
        budget.take()
        if output is outreach.Claims:
            return output(claims=SUPPORTED), {}
        return output(**SPECIFIC, note=note, cites=["i1"]), {}

    monkeypatch.setattr(outreach.structured, "ask", ask)
    got = write(Store(f"sqlite:///{tmp_path / 'o.sqlite'}"), note=True)
    assert got["after"] == outreach.follow_up(got["body"], note, "Ivy Stroud", ROLE)
    assert got["after"].startswith("Thanks for connecting, Ivy! Early rollouts that are \"surprisingly stable")
    assert got["after"].endswith("I'd love to chat more if you're interested.\n\nJustin")
    assert outreach.repeated(note, got["after"]) == "" and got["checks"]["passes"], got["checks"]["problems"]

# How the follow-up's sentence split can fail, listed before the fix (the invented drill's Hal Brenner hit the first):
#   1. A quote of theirs ends a sentence inside its closing mark (."), then our next sentence starts: the two read as
#      one sentence, and when ours says the note again the quote is dropped with it.
#   2. The same with a curly closing mark (.”).
#   3. The same when the quote ends on a question or an exclamation (?" !").
#   4. A quote of several sentences is split inside it and must come back whole, however it closes.
#   5. A quote with no closing stop ("… 200 frames" really caught my eye) ends no sentence: nothing splits there.
#   6. A link's dots and capitals never split it.
# And, found by the separate read after the fix:
#   7. A quoted title inside a sentence (your "Why do rollouts drift?" X post) splits after its closing mark, and the
#      follow-up keeps a fragment ("X post on Minecraft trajectories."). A closing mark ends a sentence only when the
#      quote was brought in as one: its opening mark after a colon, or opening the sentence.
#   8. A quote brought in with a comma, or one of their sentences with its stop inside the mark (your line "Evals are
#      the bottleneck."), read as a title, so the note's repeat takes the next sentence of ours with it. Only a
#      question or an exclamation is a title's end; a comma brings a quote in as a colon does.
HAL_NOTE = ("Hey Hal, saw your work at Example Pay. We're hiring for our Backend Engineer role at General Intuition, "
            "and I'd love to connect.")
HAL = ("Hey Hal, saw your LinkedIn post: {o}I'm looking for my next backend role. Distributed systems, Go, "
       "Postgres{end}{c} We're hiring for our Backend Engineer role (https://jobs.example.test/Backend.Role). Saw your "
       "work at Example Pay. If you're open to it, I'd love to chat.\n\nJustin")
BACKEND = {"title": "Backend Engineer", "jd_url": "https://jobs.example.test/Backend.Role"}


@pytest.mark.parametrize("o, end, c", [('"', ".", '"'), ("“", ".", "”"), ('"', "?", '"'), ('"', "!", '"')])
def test_a_follow_up_keeps_their_quote_whole_when_it_ends_a_sentence_inside_its_closing_mark(o, end, c):
    quote = f"{o}I'm looking for my next backend role. Distributed systems, Go, Postgres{end}{c}"
    after = outreach.follow_up(HAL.format(o=o, end=end, c=c), HAL_NOTE, "Hal Brenner", BACKEND)
    assert after.startswith(f"Thanks for connecting, Hal! Saw your LinkedIn post: {quote} If you're open to it"), after
    assert "We're hiring" not in after and "Example Pay" not in after  # the note says both already
    assert after.count("https://jobs.example.test/Backend.Role") == 1 and "Here's the role if you'd like a look" in after
    assert outreach.repeated(HAL_NOTE, after) == ""


def test_a_follow_up_never_splits_where_no_sentence_ends():
    body = ('Hey Ivy, saw your post: "surprisingly stable past 200 frames" really caught my eye. The notes at '
            "https://x.test/A.B/C.D. Are great. We're hiring for Member of Technical Staff.\n\nJustin")
    assert outreach._sentences(body.split("\n\n")[0]) == [
        'Hey Ivy, saw your post: "surprisingly stable past 200 frames" really caught my eye.',
        "The notes at https://x.test/A.B/C.D.", "Are great.", "We're hiring for Member of Technical Staff."]


@pytest.mark.parametrize("o, c", [('"', '"'), ("“", "”")])
def test_a_follow_up_never_splits_after_a_title_quoted_inside_a_sentence(o, c):
    text = f"Hey Ivy, saw your {o}Why do rollouts drift?{c} X post on Minecraft trajectories. It really caught my eye."
    assert outreach._sentences(text) == [
        f"Hey Ivy, saw your {o}Why do rollouts drift?{c} X post on Minecraft trajectories.", "It really caught my eye."]
    note = f"Hey Ivy, saw your {o}Why do rollouts drift?{c} post from September 2026. Would love to connect."
    after = outreach.follow_up(f"{text} We're hiring for our Member of Technical Staff role.\n\nJustin", note,
                               "Ivy Example", {"title": "Member of Technical Staff", "jd_url": ""})
    assert after.startswith("Thanks for connecting, Ivy! It really caught my eye.") and "X post on" not in after, after
    # Brought in as a quote, after a colon or opening the sentence, it still ends one.
    assert outreach._sentences(f'Ivy asked: {o}Why do rollouts drift?{c} It stuck with me.') == [
        f"Ivy asked: {o}Why do rollouts drift?{c}", "It stuck with me."]
    assert outreach._sentences(f"{o}Ship it.{c} That was the whole review.") == [f"{o}Ship it.{c}",
                                                                                 "That was the whole review."]


@pytest.mark.parametrize("line", ['I loved your line "Evals are the bottleneck for world models."',
                                  'You wrote, "Evals are the bottleneck for world models?"',
                                  'You wrote, “Evals are the bottleneck for world models!”'])
def test_a_follow_up_keeps_our_next_sentence_when_the_note_repeats_their_quoted_sentence(line):
    note = f"Hey Ivy, {line[:1].lower()}{line[1:]} Would love to connect."
    body = (f"Hey Ivy, {line[:1].lower()}{line[1:]} Our team at General Intuition builds evals for agents trained on "
            "game replays. We're hiring for our Member of Technical Staff role.\n\nJustin")
    after = outreach.follow_up(body, note, "Ivy Example", {"title": "Member of Technical Staff", "jd_url": ""})
    assert after.startswith("Thanks for connecting, Ivy! Our team at General Intuition builds evals"), after


def test_the_sender_signs_and_a_tie_is_a_source(tmp_path, monkeypatch):
    sent = fake_model(monkeypatch, SUPPORTED)
    got = write(Store(f"sqlite:///{tmp_path / 'o.sqlite'}"), sender_name="Dana Kest",
                sender_about="Dana Kest is an author of Stable Rollouts.",
                tie="You coauthored 'Stable rollouts' with them (2025-11-10).")
    assert got["body"].endswith("\n\nDana") and got["note"] == ""
    assert sent[0][1]["tie"].startswith("You coauthored") and sent[0][1]["sender_about"].startswith("Dana Kest")
    assert {"tie", "sender"} <= {s["id"] for s in sent[-1][1]["sources"]}
    assert "channel" not in sent[0][1]  # not LinkedIn: no connection note is asked for


def test_a_capped_run_reads_cached_drafts_free_and_asks_for_a_new_one_only_when_its_worst_case_fits(tmp_path,
                                                                                                    monkeypatch):
    sent = fake_model(monkeypatch, SUPPORTED)
    store = Store(f"sqlite:///{tmp_path / 'o.sqlite'}")
    with pytest.raises(outreach.OverCap, match=r"^its worst case \(\$0\.4\d\d\) is more than the \$0\.100 left of this "
                                              r"run's \$0\.10$"):
        write(store, max_usd=0.10)  # a new draft's worst case is more than $0.10
    assert sent == []  # nothing was asked for, so nothing was spent
    first = write(store, max_usd=1.00)  # it fits: the draft, its repair and the fact check all go ahead
    assert first["checks"]["passes"] and len(sent) == 3
    assert write(store, max_usd=0.0) == first and len(sent) == 3  # every call cached: free, whatever the cap
    with pytest.raises(outreach.OverCap, match="its 4 calls are more than the 3 left of this run's 3"):
        write(store, calls=3, max_usd=1.00, tie="You coauthored 'Stable rollouts' with them (2025-11-10).")
    assert len(sent) == 3


def test_a_call_lost_in_transit_counts_at_its_worst_an_error_status_at_nothing_and_only_opus_is_priced(tmp_path,
                                                                                                    monkeypatch):
    budget = providers.Budget({"max_calls_per_run": 5})

    async def ask(settings, *, content, output, budget, **kwargs):  # the draft comes back; its fact check drops
        budget.take()
        if output is outreach.Claims:
            raise providers.ProviderError("Anthropic could not be reached.")
        budget.tokens["input_tokens"] += 4000
        budget.tokens["output_tokens"] += 300
        return output(**SPECIFIC, cites=["i1"]), {}

    monkeypatch.setattr(outreach.structured, "ask", ask)
    with pytest.raises(providers.ProviderError):
        asyncio.run(outreach.write("Ivy Stroud", ROLE, "rapport", IVY, FACTS, settings={},
                                   store=Store(f"sqlite:///{tmp_path / 'f.sqlite'}"), budget=budget,
                                   others=[("Mara Quill", MARA)], as_of=AS_OF, max_usd=1.00))
    content_bytes = budget.tokens["input_tokens"] - 4000  # the dropped call, counted as all it could take in
    assert content_bytes > outreach.FRAMING and budget.tokens["output_tokens"] == 300 + outreach.GROUND_OUT
    rejected = providers.Budget({"max_calls_per_run": 5})

    async def refused(settings, *, content, output, budget, **kwargs):  # a bad key, no credits, a rate limit
        budget.take()
        raise providers.ProviderRejected("Anthropic returned HTTP 529. Check provider availability.")

    monkeypatch.setattr(outreach.structured, "ask", refused)
    with pytest.raises(providers.ProviderRejected):
        asyncio.run(outreach.write("Ivy Stroud", ROLE, "rapport", IVY, FACTS, settings={},
                                   store=Store(f"sqlite:///{tmp_path / 'r.sqlite'}"), budget=rejected,
                                   others=[("Mara Quill", MARA)], as_of=AS_OF, max_usd=1.00))
    assert not any(rejected.tokens.values())  # an error status is not billed
    with pytest.raises(ValueError, match="runs only with that model"):  # the cap's price is Opus 5's
        outreach.drafter({}, budget, None, model="claude-other", max_usd=1.00)
    assert outreach.drafter({}, budget, None, model="claude-other")  # uncapped: only the calls cap, as before


def test_a_drafts_worst_case_grows_with_what_it_is_given_and_a_left_out_fact_is_never_given(tmp_path, monkeypatch):
    small = outreach.worst_usd(outreach.SYSTEM, {"items": [{"text": "Training a world model on Doom"}]})
    assert 0.3 < small < 0.5  # both prompts and the framing, four calls at their most tokens out
    assert outreach.worst_usd(outreach.SYSTEM, {"items": [{"text": "word " * 8_000}]}) > 1.0  # never tried at $1
    sent = fake_model(monkeypatch, SUPPORTED)
    draft = outreach.drafter({}, providers.Budget({"max_calls_per_run": 5}), Store(f"sqlite:///{tmp_path / 'd.sqlite'}"),
                             as_of=AS_OF, max_usd=1.00, leave_out=("gi",))
    draft("Ivy Stroud", ROLE, "rapport", IVY, FACTS, [("Mara Quill", MARA)])
    assert sent and all(f["id"] != "gi" for _, payload in sent if "facts" in payload for f in payload["facts"])


def test_a_statement_no_source_makes_holds_the_draft(tmp_path, monkeypatch):
    made_up = SUPPORTED + [{"claim": "Ivy ported Tetris to Rust", "source": "i1", "words": "ported Tetris to Rust"},
                           {"claim": "GI built DIAMOND", "source": "", "words": ""}]
    fake_model(monkeypatch, made_up)
    got = write(Store(f"sqlite:///{tmp_path / 'o.sqlite'}"))
    assert not got["checks"]["passes"]
    assert got["checks"]["problems"] == ["no source says: Ivy ported Tetris to Rust", "no source says: GI built DIAMOND"]


def test_out_of_calls_before_the_fact_check_holds_the_draft(tmp_path, monkeypatch):
    fake_model(monkeypatch, SUPPORTED)
    got = write(Store(f"sqlite:///{tmp_path / 'o.sqlite'}"), calls=2)  # the draft and its repair, nothing left
    assert got["checks"]["problems"] == ["not fact-checked: the run's model calls ran out"]


def test_dates_are_absolute_gi_keeps_to_its_public_facts_and_where_they_work_is_left_out():
    draft = {**SPECIFIC, "body": SPECIFIC["body"] + " Last February you wrote about it; in July you added more, and in "
                                                    "March 2026 more again. Frame drift is a live question for us, and "
                                                    "coming from design at Pine Lab you would know. May we talk?"}
    problems = check(draft)["problems"]
    assert 'a date that is not absolute: "Last February"' in problems
    assert 'a date that is not absolute: "July"' in problems and not any("March" in p for p in problems)
    assert not any("May" in p for p in problems)  # "May we talk?" is a question, not a month
    assert {"says what GI works on beyond its public facts", "talks about their job situation"} <= set(problems)
    for loss in ("Sorry your team was disbanded.", "Heard Examplesoft is winding down.", "Sorry about the job loss.",
                 "Heard Examplesoft shut down.", "Sorry Examplesoft closed its doors.", "Sorry about Examplesoft's shutdown.",
                 "Sorry about the cuts at Examplesoft.",
                 'You wrote "our team was disbanded on Sept 10, so I am open-sourcing the netcode".'):  # their words too
        assert outreach.LOSS_WHY in check({**SPECIFIC, "body": SPECIFIC["body"] + " " + loss})["problems"], loss
    for work in ("Removing redundant frames helped.", "The restructuring of the renderer is neat.",
                 "You let go of fixed timesteps.", "You shut down the server after the run."):
        assert outreach.LOSS_WHY not in check({**SPECIFIC, "body": SPECIFIC["body"] + " " + work})["problems"], work
    quoted = {**SPECIFIC, "body": SPECIFIC["body"] + ' In May 2026 you said "the next step this summer is 400 frames".'}
    assert not any("absolute" in p for p in check(quoted)["problems"])  # their own words keep their own dates


def test_a_month_alone_is_fine_for_this_years_item_and_names_are_not_dates():
    draft = {**SPECIFIC, "body": SPECIFIC["body"].replace("from September 2026", "in September")}
    assert check(draft)["passes"]  # Ivy's item is from September this year
    older = {**SPECIFIC, "body": SPECIFIC["body"].replace("from September 2026", "in June")}
    assert 'a date that is not absolute: "June"' in check(older)["problems"]
    june = outreach.check({**SPECIFIC, "body": SPECIFIC["body"].replace("Hey Ivy", "Hey June")}, IVY, ROLE, FACTS,
                          [("Mara Quill", MARA)], "June Park", AS_OF)
    assert june["passes"], june["problems"]
    titled = {"subject": "Reward Signals for World Models", "body": SPECIFIC["body"] + " I also read 'Reward Signals for "
                                                                                      "World Models'."}
    theirs = IVY + [{"id": "i2", "date": "2025", "url": "u", "context": "", "text": "Reward Signals for World Models"}]
    assert outreach.check(titled, theirs, ROLE, FACTS, (), "Ivy Stroud", AS_OF)["passes"]  # their title, not our words


def test_a_note_on_their_work_keeps_its_prompt_and_a_note_with_no_role_gains_its_own(tmp_path, monkeypatch):
    systems = []

    async def ask(settings, *, system, content, output, budget, **kwargs):
        budget.take()
        systems.append((output.__name__, system))
        if output is outreach.Claims:
            return output(claims=SUPPORTED), {}
        return output(**SPECIFIC, cites=["i1"]), {}

    monkeypatch.setattr(outreach.structured, "ask", ask)
    write(Store(f"sqlite:///{tmp_path / 'a.sqlite'}"))
    assert [s for kind, s in systems if kind == "Draft"] == [outreach.SYSTEM]
    systems.clear()
    write(Store(f"sqlite:///{tmp_path / 'b.sqlite'}"), role_free=True)
    drafts = [s for kind, s in systems if kind == "Draft"]
    assert drafts and all(s == outreach.SYSTEM + outreach.NO_ROLE for s in drafts)
    for message, shape in (("acquisition", outreach.DEAL), ("work", outreach.DEAL_QUIET)):  # a layoff post: no congrats
        systems.clear()
        write(Store(f"sqlite:///{tmp_path / f'{message}.sqlite'}"), role_free="acquisition", message=message)
        drafts = [s for kind, s in systems if kind == "Draft"]
        assert drafts and all(s == outreach.SYSTEM + shape for s in drafts)


def test_the_writer_is_told_what_the_message_is_about_and_a_note_on_their_work_keeps_its_prompt(tmp_path, monkeypatch):
    drafts = []  # the system prompt of each call that writes a draft

    async def ask(settings, *, system, content, output, budget, **kwargs):
        budget.take()
        if output is outreach.Claims:
            return output(claims=SUPPORTED), {}
        drafts.append(system)
        return output(**SPECIFIC, cites=["i1"]), {}

    monkeypatch.setattr(outreach.structured, "ask", ask)
    store = Store(f"sqlite:///{tmp_path / 'o.sqlite'}")
    write(store, message="launch")
    write(store, message="work")  # their recent work: the prompt, and so its cache, as before
    write(store, role_free="acquisition", message="acquisition")
    assert list(dict.fromkeys(drafts)) == [outreach.SYSTEM + outreach.MOMENT["launch"], outreach.SYSTEM,
                                           outreach.SYSTEM + outreach.DEAL]  # a repair asks again the same way
    assert "Not sure what you're looking for next" in outreach.DEAL


def test_the_writer_is_given_no_date_of_its_own_so_a_draft_stays_cached(tmp_path, monkeypatch):
    payloads = []  # what each call that writes a draft is given

    async def ask(settings, *, system, content, output, budget, **kwargs):
        budget.take()
        if output is outreach.Claims:
            return output(claims=SUPPORTED), {}
        payloads.append(content)
        return output(**SPECIFIC, cites=["i1"]), {}

    monkeypatch.setattr(outreach.structured, "ask", ask)
    store = Store(f"sqlite:///{tmp_path / 'y.sqlite'}")
    for as_of in ("2026-09-17T13:00:00+00:00", "2026-09-18T13:00:00+00:00"):  # the next morning: the cached draft
        asyncio.run(outreach.write("Ivy Stroud", ROLE, "rapport", IVY, FACTS, settings={}, store=store,
                                   budget=providers.Budget({"max_calls_per_run": 5}), others=[("Mara Quill", MARA)],
                                   as_of=as_of, message="release"))
    assert len(payloads) == 1 and "this_year" not in payloads[0] and "today" not in payloads[0]
    assert "never the day something was posted" in outreach.SYSTEM
    assert "never call it one" in outreach.MOMENT["release"]


def test_every_known_bad_draft_fails_its_checks():
    """Each bot-like draft found live or in a fact-check (invented people, the same words) fails for what was wrong."""
    corpus = json.loads((Path(__file__).parent / "fixtures" / "drafts" / "bad-drafts.json").read_text())
    for bad in corpus:
        role = {"id": bad["role"], "title": {"mts-research": "Member of Technical Staff"}.get(bad["role"], "Product Designer"),
                "jd_url": "https://jobs.example.test/"}
        problems = outreach.check(bad["draft"], bad["items"], role, outreach.facts(bad["role"]), [], "Rin Vale",
                                  "2026-09-15T00:00:00+00:00", bad["draft"]["body"].rsplit("\n", 1)[-1],
                                  moments=bad["happened"])["problems"]
        for why in bad["fails"]:
            assert any(why in p for p in problems), (bad["found"], why, problems)
