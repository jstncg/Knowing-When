"""GI's posted pay range for each role, read from its own job posts. Never a guess at anyone's pay.

config/pay-bands.json holds what scripts/pay_bands.py read from GI's public job board (Ashby's
posting API, with compensation): per role id, the posted salary range, the post and the day it
was read. A role with no posted range says so. Nothing here estimates a person's current pay.
"""

import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

BANDS = Path(__file__).resolve().parents[1] / "config" / "pay-bands.json"
BOARD = "https://api.ashbyhq.com/posting-api/job-board/generalintuition-medal?includeCompensation=true"
NONE = "No posted range on file"
POSTS = ("jobs.ashbyhq.com", "generalintuition-medal")  # where GI's own job posts live, the only ones read for pay


def post_id(link):
    """A job post's id from its link as pasted: the last part of its path, less an /application page, a query or a
    fragment; None without one."""
    parts = [p for p in urlsplit(link or "").path.split("/") if p]
    if parts and parts[-1].lower() == "application":
        parts.pop()
    return parts[-1] if parts else None


def gi_post(url):
    """Whether a link is one of GI's own job posts on its board, the only posts a pay range is read from."""
    u = urlsplit(url or "")
    parts = [p for p in u.path.split("/") if p]
    return (u.hostname or "").removeprefix("www.") == POSTS[0] and len(parts) >= 2 and parts[0].lower() == POSTS[1]


def bands(path=None):
    """{role id: posted range} from a pay-bands file: GI's own (BANDS, read when called) unless ``path`` names one."""
    path = path or BANDS
    return _read(path, path.stat().st_mtime) if path.exists() else {}


def for_mode(mode):
    """The posted ranges a workspace shows: live, GI's own (``bands``); the simulation none, so its invented week
    never mixes in GI's real pay (each role says NONE, and its plan prices the invented estimates)."""
    return {} if mode == "simulation" else bands()


@lru_cache(maxsize=4)
def _read(path, mtime):  # read once per change to the file, not once per card
    return json.loads(path.read_text()).get("roles", {})


def line(role_id, found=None):
    """The role's posted range in words, or that there is none on file."""
    band = (bands() if found is None else found).get(role_id)
    if not band:
        return NONE
    return (f"Posted range {band['currency']} {band['min']:,.0f} to {band['max']:,.0f} per {band['interval']} "
            f"(GI's job post, read {band['read_on']})")


def from_ashby(payload, roles, read_on):
    """{role id: band} from Ashby's posting API response, matching each role's jd_url to a job by its id or link.

    Only a salary component that states both ends, its currency and its interval counts: nothing is filled
    in. A role whose post shows none gets no entry.
    """
    jobs = {}
    for job in payload.get("jobs", []):  # by the job's id, and by its post's link in case the two ever differ
        for key in (job.get("id"), post_id(job.get("jobUrl"))):
            if key:
                jobs[key] = job
    out = {}
    for role in roles:
        job = jobs.get(post_id(role.get("jd_url"))) or {}
        salary = next((c for c in (job.get("compensation") or {}).get("summaryComponents") or [] if _stated(c)), None)
        if salary:
            out[role["id"]] = {"min": salary["minValue"], "max": salary["maxValue"], "currency": salary["currencyCode"],
                               "interval": salary["interval"].split()[-1].lower(),
                               "source_url": role["jd_url"], "read_on": read_on}
    return out


def _stated(c):
    ends = [c.get("minValue"), c.get("maxValue")]
    return (c.get("compensationType") == "Salary" and all(isinstance(v, (int, float)) and v > 0 for v in ends)
            and ends[0] <= ends[1] and isinstance(c.get("currencyCode"), str) and c["currencyCode"]
            and isinstance(c.get("interval"), str) and c["interval"].strip())
