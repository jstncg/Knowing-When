"""Drop in a job description, get a role the engine runs.

preview reads the JD with the role compiler, two model calls (propose_strategy for the brief, compile_role for the
spec, each with at most one repair), and writes nothing. accept writes what the preview showed where every path
reads a role: its spec in config/role-specs/<id>.json and its entry in config/roles.json. From the next run, a person
listed with that role in the people file is pulled, read for its topics, judged by its spec and carded with its
title, like anyone else. remove takes a dropped role out again. sample previews an invented sample JD from answers
written by hand in the model's format: no key, no call, and it never becomes a role.
"""

import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import get_args

from . import pay, providers
from . import role_compiler as rc
from .models import canonical_url, public_url
from .outreach import MODEL, sender, usd
from .detectors import FINANCE_TITLE
from .readiness import CONTEXT_ONLY

ROLES = rc.SPECS.parent / "roles.json"
SAMPLE = Path(__file__).resolve().parents[1] / "tests/fixtures/roles/data-engineer"  # invented JD, hand-written answers
DROPPED = "dropped"  # a role's implementation_stage when it came in this way: the only roles remove takes out
PRICE = ("Two model calls, up to four if an answer needs fixing: at most about $1 for a JD of normal length. The "
         "preview says what this one cost.")
EVERY_ROLE = ("Early signs in their own posts (work in progress, a technical ask, something just submitted), a shift "
              "in what they post about, public moments (a paper, a launch), what they say (open to work, when to "
              "talk), a GI teammate's note, and having to move (laid off, a job or placement ending, their company "
              "closing) count for every role. Time in a seat, an acquisition's retention cliff, a path like people GI "
              "hired and the pattern that came before their past moves are never a reason; a role that watches them "
              "only lists them on a call.")
HOLDS = ("Every role holds for a first year in a new job, a recent promotion, new equity, their own launch week, "
         "\"not looking\", and a next role already settled.")
FINANCE = "finance-operations"
FINANCE_ONLY = {"calendar_quiet_close"}  # holds only a person with a finance title (detectors.calendar_quiet_close)


def before(role):
    """What a role still needs before its calls go out, in plain words. Its pay range can only come from its job post
    on GI and Medal's job board (pay.gi_post)."""
    link = role.get("jd_url") or ""
    paid = ("No pay range yet: it comes from the role's job post on GI and Medal's job board, and this role has no "
            "link to one." if not link else "Its pay range is read from its job post on GI and Medal's job board the "
            "next time pay ranges are refreshed." if pay.gi_post(link) else "No pay range: it comes only from GI's own "
            "job posts, and this link isn't on GI and Medal's job board.")
    return [
        "Nobody is watched for it yet: add people to the watchlist with this role, and the next morning's run reads "
        "them.",
        f"Drafts are signed {sender()} until someone on the team is set as the writer for this role.",
        paid,
        "Its calls haven't been checked yet: that takes made-up cases for this role with the right call written "
        "down, and a look back at how its calls would have done, planned before the results are seen.",
    ]


# What happens once a role is added, in plain words: nobody is watched for it yet, how people come to be, and when
# their public posts are read.
ADDED = [
    "Nobody is watched for this role yet, so it makes no calls.",
    "People join the watchlist by hand, listed under this role; a free check first says whether each one posts "
    "enough in public to read, and that the profiles read are really theirs.",
    "Reads run each morning: the daily run pulls each watched person's new public posts and code, and news about "
    "their employer, and the engine makes their call on Today's calls.",
]


def slug(title):
    """The role's id from its title: "Data Engineer" -> "data-engineer"."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].strip("-")


def roles():
    return json.loads(ROLES.read_text())["roles"]


def clash(role):
    """Why this role can't be added (the same id, title or JD link as a role on file), or ""."""
    for r in roles():
        if r["id"] == role["id"] or r["title"].casefold() == role["title"].casefold() or (
                role["jd_url"] and canonical_url(r.get("jd_url") or "") == canonical_url(role["jd_url"])):
            return f"{r['title']} is already a role."
    if role["id"] in rc.ROLE_IDS or rc.spec_path(role["id"]).exists():
        return "A role by this name is already on file."
    return ""


def watch(spec, role):
    """What the engine watches for a role, in words: what counts for it alone, what counts for every role, the holds,
    its topics and what it can't see. A sign that can't fire for the role's people never shows as counting, whatever
    the spec selected: one in readiness.CONTEXT_ONLY (a work anniversary, a retention cliff, a path like past hires',
    the pattern before their past moves) decides nothing and is no gap either, one no source reads is a gap, and the
    quarter-close hold holds only people with a finance title (the role's lane or title says finance)."""
    manifest = {d["id"]: d for d in rc.manifest()["detectors"]}
    unread = {i: rc.DETECTOR_FEEDS[i].gap() for i in manifest if rc.DETECTOR_FEEDS[i].gap()}
    finance = role.get("lane") == FINANCE or bool(FINANCE_TITLE.search(role.get("title") or ""))
    idle = CONTEXT_ONLY | set(unread) | (set() if finance else FINANCE_ONLY)
    selected = [d["id"] for d in spec["detectors"]]
    by_line = {line: i for i, line in unread.items()}
    lines = [g for g in dict.fromkeys([*spec["gaps"], *(unread[i] for i in selected if i in unread)])
             if by_line.get(g) not in CONTEXT_ONLY]  # a source would not make it count
    gaps = [f"{manifest[by_line[g]]['means']} No source reads it yet." if g in by_line
            else _plain(g.removeprefix("gap: needs adapter for ")) for g in lines]
    if not finance and FINANCE_ONLY & set(selected):
        gaps.append("A hold for this role's busy season: the quarter-close hold is only for people with a finance title.")
    return {
        "signs": [{"means": manifest[d["id"]]["means"], "why": d["reason"]} for d in spec["detectors"]
                  if d["id"] in manifest and not manifest[d["id"]]["always"] and d["id"] not in idle],
        "every_role": EVERY_ROLE, "holds": HOLDS,
        "topics": spec.get("topics") or [],
        "gaps": gaps,
    }


def _plain(gap):
    """A model's gap line without the manifest's ids: a trailing "(some_id)" goes, and snake_case words read as words
    (a handle such as @some_name stays). It starts with a capital, as a list item does, unless its first word is a
    name in mixed case (arXiv, iOS), a domain or a link."""
    gap = re.sub(r"\s*\([a-z0-9]+(?:_[a-z0-9]+)+\)$", "", gap)
    gap = re.sub(r"(?<![@/#.\w])[a-z0-9]+(?:_[a-z0-9]+)+\b", lambda m: m[0].replace("_", " "), gap)
    return gap[:1].upper() + gap[1:] if re.match(r"[a-z]+(?![\w.:/])", gap) else gap  # not a domain or link


def spec_of(role_id):
    path = rc.spec_path(role_id)
    return json.loads(path.read_text()) if path and path.exists() else None


def _preview(proposal, spec, sample=False, cost=None):
    role = {
        "id": slug(proposal["title"]), "title": proposal["title"], "jd_url": proposal["jd_url"], "locations": [],
        "lane": proposal["lane"], "criteria": proposal["criteria"], "required_criteria": proposal["required_criteria"],
        "manager_preferences": [], "observed_on": date.today().isoformat(), "brief_version": 1,
        "brief_status": "proposed", "implementation_stage": DROPPED, "jd_sha256": proposal["jd_sha256"],
    }
    spec = spec.model_dump()
    return {"role": role, "spec": spec, "watch": watch(spec, role), "before": before(role), "clash": clash(role),
            "sample": sample, "cost": cost}


async def preview(jd_text, settings, jd_url=""):
    """The JD as a role and its spec, and what the engine would watch for it; nothing is written. Two model calls."""
    proposal = await rc.propose_strategy(jd_text, jd_url, settings)
    budget = providers.Budget(settings)
    spec = await rc.compile_role(jd_text, slug(proposal["title"]), settings, proposal["signals"], budget)
    tokens = {k: proposal["_usage"]["tokens"].get(k, 0) + budget.tokens.get(k, 0)
              for k in ("input_tokens", "output_tokens")}
    priced = (settings.get("model") or providers.DEFAULT_MODEL) == MODEL
    return _preview(proposal, spec, cost={"usd": round(usd(tokens), 2) if priced else None, **tokens})


def sample():
    """The invented sample JD's preview from its hand-written answers, through the same checks a model's answer
    passes."""
    jd = (SAMPLE / "jd.txt").read_text()
    proposal = rc.parse_strategy(json.loads((SAMPLE / "strategy.json").read_text()), jd)
    spec = rc.compile_output(json.loads((SAMPLE / "compiled.json").read_text()), slug(proposal["title"]),
                             [s["id"] for s in proposal["signals"]])
    return {**_preview(proposal, spec, sample=True), "jd_text": jd}


def _texts(value):
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def accept(previewed, jd_url="", locations=()):
    """Write a previewed role where the engine reads roles: its spec, then its config/roles.json entry, built from the
    preview's fields and nothing else. Refused before anything is written: the sample, a malformed role or spec, and
    a clash with a role on file. ``jd_url`` (optional) and ``locations`` (its offices) are what the JD says that the
    preview doesn't take in."""
    if not isinstance(previewed, dict) or not isinstance(previewed.get("role"), dict):
        raise ValueError("Add a role from its preview.")
    if previewed.get("sample"):
        raise ValueError("The sample JD is invented: it previews, it never becomes a role.")
    given, spec = previewed["role"], rc.RoleConfig.model_validate(previewed["spec"])
    title = given.get("title").strip() if isinstance(given.get("title"), str) else ""
    criteria, required = given.get("criteria"), given.get("required_criteria")
    if not title or slug(title) != given.get("id") or spec.role_id != given["id"]:
        raise ValueError("The role's id must come from its title, and its spec must be for that id.")
    if not (_texts(criteria) and isinstance(required, list) and set(required) <= set(criteria)):
        raise ValueError("Give the role nonempty criteria, and required criteria only from among them.")
    if not (isinstance(locations, (list, tuple)) and (not locations or _texts(list(locations)))
            and isinstance(given.get("jd_sha256"), str)):
        raise ValueError("Offices must be names, and the role must carry its JD's hash.")
    link = jd_url or given.get("jd_url") or ""
    lane = given.get("lane") if given.get("lane") in get_args(rc.Lane) else "other"
    role = {"id": given["id"], "title": title, "jd_url": public_url(link) if link else "",
            "locations": [o.strip() for o in locations], "lane": lane, "criteria": criteria, "required_criteria": required, "manager_preferences": [],
            "observed_on": date.today().isoformat(), "brief_version": 1, "brief_status": "proposed",
            "implementation_stage": DROPPED, "jd_sha256": given["jd_sha256"]}
    if why := clash(role):
        raise FileExistsError(why)
    rc.spec_path(role["id"]).write_text(spec.model_dump_json(indent=1) + "\n")  # never a role entry without a spec
    data = json.loads(ROLES.read_text())
    data["roles"].append(role)
    _write(data)
    return role


def remove(role_id):
    """Take a dropped role out: its config/roles.json entry and its spec. The roles GI configured by hand stay."""
    data = json.loads(ROLES.read_text())
    role = next((r for r in data["roles"] if r["id"] == role_id), None)
    if not role:
        raise KeyError(f"{role_id} is not a role in config/roles.json.")
    if role.get("implementation_stage") != DROPPED:
        raise ValueError(f"{role_id} was configured by hand, not dropped in: edit config/roles.json instead.")
    data["roles"] = [r for r in data["roles"] if r["id"] != role_id]
    _write(data)
    rc.spec_path(role_id).unlink(missing_ok=True)
    return role


def _write(data):
    """config/roles.json in its own format and mode, replaced whole so no reader sees half a file."""
    with tempfile.NamedTemporaryFile("w", dir=ROLES.parent, suffix=".tmp", delete=False) as out:
        out.write(json.dumps(data, indent=2) + "\n")
    os.chmod(out.name, ROLES.stat().st_mode & 0o777)
    os.replace(out.name, ROLES)
