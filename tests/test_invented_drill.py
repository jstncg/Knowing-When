"""The replay drill over 120 mornings of 16 invented people in all three live roles (tests/fixtures/drill), pinned.

What the engine carded, when, and what held everyone else is pinned here, so a change to detectors, readiness, the
gate or the drafts shows its replay effect in this test without the Mac's real store. When a change moves a pin on
purpose, update the pin and say in the commit what moved and why. See it whole with
    uv run python scripts/replay_drill.py --demo --fixtures tests/fixtures/drill --days 120
The rails (never twice, the caps, holds, opt-outs, identity) are asserted, not pinned: they must hold whatever moves.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("replay_drill", ROOT / "scripts" / "replay_drill.py")
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


@pytest.fixture(scope="module")
def report():
    return drill.main(["--demo", "--fixtures", str(ROOT / "tests/fixtures/drill"), "--days", "120", "--end", "2026-09-15"])


def test_the_cards_the_engine_gives_the_invented_people(report):
    cards = [(c["as_of"][:10], c["name"], c["role"], c["kind"], c["track"]) for c in report["cards"]]
    assert cards == [
        ("2026-06-11", "Aria Venn", "mts-research", "early_sign", "rapport"),  # a technical ask, the morning after
        # Finn Adler's release by hand (v2.0.0): its draft names the repo and tag since 9223de7, so it passes again.
        ("2026-06-29", "Finn Adler", "backend", "just_happened", "rapport"),
        # Gia Moreau's new repo (07-06) gets none: a repo they created is never a reason on its own (Justin,
        # 2026-09-24; golden bk-02, pd-02).
        ("2026-07-14", "Bo Lindqvist", "mts-research", "just_happened", "rapport"),  # a preprint
        ("2026-08-10", "Cleo Marsh", "mts-research", "just_happened", "rapport"),  # their launch, after launch week
        ("2026-08-24", "Kai Moss", "product-designer", "just_happened", "rapport"),  # "v3.1 is live", same
        # Their own "I'm open": the LinkedIn follow-up keeps the quote that ends inside its closing mark.
        ("2026-08-25", "Hal Brenner", "backend", "just_happened", "pitch"),
        ("2026-08-25", "Juno Park", "product-designer", "early_sign", "rapport"),  # work in progress
        ("2026-08-26", "Oren Sato", "mts-research", "early_sign", "rapport"),  # a technical ask
        ("2026-08-31", "Pia Hale", "backend", "early_sign", "rapport"),  # asked on 08-26, held by the week's cap
    ]


def test_what_held_everyone_else_and_which_moments_got_a_card(report):
    s = report["summary"]
    # Nia Obi, once held only for identity, is also held by her draft since dbbcef8: it quotes a sentence of hers cut
    # short ("quote stops mid-sentence").
    assert (s["people"], s["people_carded"], s["people_ever_reach_now"], s["held_only_for_identity"]) == (16, 9, 13, 0)
    assert s["held_person_mornings"] == {
        "No reply since <day>": 132, "Sent on <day>": 54, "On a card from <day>": 27, "The draft fails a check": 21,
        "Confirm it's them before any reach": 20, "They asked not to be contacted": 20,
        "Past this week's cap of 4 cards across all roles.": 4, "The newest sign is from <day>, over three weeks old": 1}
    # One reach is never carded because its free draft names one detail only true of them: Iris Kade's layoff, the
    # drill's only forced move, held until its sign is stale. Live, route.py --redraft has the model retry it; the
    # drill runs free.
    assert s["draft_check_problems"] == {
        "fails the name swap: 1 detail(s) only true of them, needs 2 (or the release their post names)": 1}
    moments = {(m["name"], m["kind"]): m["outcome"] for m in report["moments"]}
    assert moments == {
        ("Aria Venn", "paper"): "still open", ("Bo Lindqvist", "paper"): "carded", ("Cleo Marsh", "launch"): "carded",
        ("Finn Adler", "launch"): "carded", ("Hal Brenner", "open_to_work"): "carded",
        ("Iris Kade", "left_job"): "still open", ("Kai Moss", "launch"): "carded"}
    assert (s["median_lag_hours"], s["max_lag_hours"]) == (22.7, 169.0)  # a launch waits out launch week


def test_no_rail_gives_way_whatever_the_engine_decides(report):
    # The drill's own rails: never twice in the drill, identity, holds, opt-outs, both weekly caps.
    assert report["rails_failures"] == [] and all(c["rails"] == [] for c in report["cards"])
    states = report["people"]
    assert not any(s["state"] == "reach_now, carded" for s in states["D13"])  # on a card before the drill
    assert not any(s["state"].startswith("reach_now") for s in states["D04"] + states["D05"])  # held; a build
    assert all(s["state"].startswith("watch_until") and "short_tenure" in s["state"] for s in states["D04"])
    assert not any(m["name"] == "Esme Tran" for m in report["moments"])  # a build is never a public moment
