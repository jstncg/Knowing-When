"""The posted pay range per role, from GI's own job posts (an invented posting-API response)."""

import json
import subprocess
import sys
from pathlib import Path

from app import pay

ROOT = Path(__file__).resolve().parents[1]
ROLES = [{"id": "backend", "jd_url": "https://jobs.ashbyhq.com/example/job-backend"},
         {"id": "product-designer", "jd_url": "https://jobs.ashbyhq.com/example/job-design/"},
         {"id": "mts-research", "jd_url": "https://jobs.ashbyhq.com/example/job-mts"},
         {"id": "ops", "jd_url": "https://jobs.ashbyhq.com/example/job-ops"}]
PAYLOAD = {"jobs": [
    {"id": "job-backend", "title": "Backend", "compensation": {"summaryComponents": [
        {"compensationType": "EquityPercentage", "minValue": 0.1, "maxValue": 0.2},
        {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD", "minValue": 180000, "maxValue": 240000}]}},
    {"id": "job-design", "title": "Design", "compensation": {"summaryComponents": [
        {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD", "minValue": 150000, "maxValue": None}]}},
    {"id": "job-mts", "title": "MTS", "compensation": {"summaryComponents": [
        {"compensationType": "Salary", "interval": "1 YEAR", "minValue": 150000, "maxValue": 200000},  # no currency
        {"compensationType": "Salary", "currencyCode": "USD", "minValue": 150000, "maxValue": 200000},  # no interval
        {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD", "minValue": "150k", "maxValue": 200000}]}},
    {"id": "other-id", "jobUrl": "https://jobs.ashbyhq.com/example/job-ops", "compensation": {"summaryComponents": [
        {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD", "minValue": 90000, "maxValue": 120000}]}},
]}
# Shaped like scripts/pay_bands.py's output (GI's real ranges live only on the Mac): invented.
REAL_LOOKING = {role: {"min": low, "max": high, "currency": "USD", "interval": "year", "read_on": "2026-09-24",
                       "source_url": f"https://jobs.ashbyhq.com/generalintuition-medal/{role}"}
                for role, low, high in (("mts-research", 200000, 350000), ("backend", 180000, 260000),
                                        ("product-designer", 150000, 220000))}


def test_only_a_stated_salary_range_counts_matched_by_the_post():
    found = pay.from_ashby(PAYLOAD, ROLES, "2026-09-24")
    assert list(found) == ["backend", "ops"]  # the designer post states one end; the MTS post leaves a part out
    assert found["backend"] == {"min": 180000, "max": 240000, "currency": "USD", "interval": "year",
                                "source_url": ROLES[0]["jd_url"], "read_on": "2026-09-24"}
    assert pay.line("backend", found) == "Posted range USD 180,000 to 240,000 per year (GI's job post, read 2026-09-24)"
    assert pay.line("mts-research", found) == pay.NONE == "No posted range on file"
    assert found["ops"]["min"] == 90000  # matched by the post's link when its id differs
    assert pay.line("x", {"x": {**found["ops"], "interval": "hour"}}).startswith("Posted range USD 90,000 to 120,000 per hour")


def test_the_repo_has_no_invented_range():
    real = pay.bands(ROOT / "config" / "pay-bands.json")  # GI's own file, where there is one (the Mac)
    invented = [role for role, b in real.items() if not b["source_url"].startswith("https://jobs.ashbyhq.com/")]
    assert invented == []  # role ids only: a failure never prints GI's ranges


def test_a_test_reads_its_own_ranges_never_gi_s():
    assert pay.BANDS.parent != ROOT / "config" and pay.bands() == {}  # conftest points BANDS at a scratch file
    pay.BANDS.write_text(json.dumps({"roles": REAL_LOOKING}))
    assert pay.bands() == REAL_LOOKING  # read from wherever BANDS points when called


def test_the_script_writes_nothing_when_no_post_states_a_range(tmp_path):
    empty = tmp_path / "board.json"
    empty.write_text(json.dumps({"jobs": [{"id": "x"}]}))
    run = subprocess.run([sys.executable, "scripts/pay_bands.py", "--file", str(empty)], cwd=ROOT,
                         capture_output=True, text=True)
    assert run.returncode == 1 and "nothing written" in run.stderr


def test_a_gi_post_link_is_read_for_pay_as_it_was_pasted():
    """The JD drop's preview says a GI post's range is read on the next refresh only when that read will find it."""
    post = "https://jobs.ashbyhq.com/generalintuition-medal/9f974f5b"
    board = {"jobs": [{"id": "9f974f5b", "compensation": {"summaryComponents": [
        {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD", "minValue": 1, "maxValue": 2}]}}]}
    for link in (post, post + "/", post + "/application", post + "?utm_source=x", post + "/application?src=li",
                 post + "#apply", "http://www.jobs.ashbyhq.com/GeneralIntuition-Medal/9f974f5b"):
        assert pay.gi_post(link) and pay.from_ashby(board, [{"id": "r", "jd_url": link}], "d"), link
    for link in ("", "https://jobs.ashbyhq.com/generalintuition-medal", "https://jobs.ashbyhq.com/generalintuition-medal/",
                 "https://jobs.ashbyhq.com/featherlessai/9f974f5b", "https://example.com/generalintuition-medal/9f974f5b"):
        assert not pay.gi_post(link), link


def test_gi_s_posted_ranges_never_reach_the_simulation(monkeypatch):
    """With GI's real ranges on file (config/pay-bands.json, on the Mac), the invented week is as it is without them:
    its brief and Monday post, cards, hiring plan and one-pager. Live shows them."""
    from app import hiring, month, today, weekly

    def invented():
        today._demo_store.cache_clear()
        hiring.reset()
        w, routes = weekly.week("simulation", today.DEMO_AS_OF)
        return json.dumps({"post": weekly.post(w), "brief": w, "cards": {k: r.card for k, r in routes.items()},
                           "hiring": hiring.page("simulation"), "month": month.text(month.month("simulation")),
                           "asked": today.person("simulation", "A900")[0]},  # one person's panel
                          sort_keys=True, default=str)

    monkeypatch.setattr(pay, "bands", lambda: {})
    before = invented()
    monkeypatch.setattr(pay, "bands", lambda: REAL_LOOKING)
    assert invented() == before and "Posted range" not in before
    assert pay.line("backend", pay.for_mode("live")).startswith("Posted range USD 180,000 to 260,000 per year")
    today._demo_store.cache_clear()
    hiring.reset()
