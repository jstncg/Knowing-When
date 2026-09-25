import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import contact, providers, role_drop
from .engine import decide, digest, person_key, review_hash
from .fixtures import scenarios
from .models import Action, Candidate, Review, iso, parse_time, utcnow
from .journey import assess
from .store import Conflict, Store

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
store = Store()
wake = threading.Event()  # set by enqueue so the idle worker starts new jobs promptly
secret_names = {
    "exa_key": "EXA_API_KEY",
    "anthropic_key": "ANTHROPIC_API_KEY",
    "crustdata_key": "CRUSTDATA_API_KEY",
}


def settings():
    local = dotenv_values(ROOT / ".env")
    config = store.get("settings") or {}
    return {
        "model": config.get("model", "claude-opus-5"),
        # Extraction and the kill pass use a cheaper model; research uses "model".
        "small_model": os.getenv("SMALL_MODEL") or local.get("SMALL_MODEL") or "",
        "anthropic_workspace_id": os.getenv("ANTHROPIC_WORKSPACE_ID")
        or local.get("ANTHROPIC_WORKSPACE_ID")
        or "",
        "monitoring_enabled": config.get("monitoring_enabled", False),
        # A value saved under an older, wider range shows as the cap the Budget applies.
        "max_calls_per_run": min(config.get("max_calls_per_run", 8), providers.MAX_CALLS_PER_RUN[1]),
        "exa_run_budget": 5,
        **{
            k: os.getenv(env) or local.get(env) or "" for k, env in secret_names.items()
        },
    }


def safe_settings():
    s = settings()
    return {
        **{k: v for k, v in s.items() if k not in secret_names},
        **{k.replace("_key", "_configured"): bool(s.get(k)) for k in secret_names},
        "storage": store.engine.dialect.name,
        "max_calls_range": list(providers.MAX_CALLS_PER_RUN),
    }


def role_for(key):
    role = store.get("role:" + key)
    if not role:
        raise HTTPException(404, "Role not found")
    from .role_compiler import build_signal_plan

    return {
        **role,
        "id": key,
        "signal_plan": role.get("signal_plan") or build_signal_plan(role),
    }


def candidate_for(key):
    c = store.get(key)
    if not c or not key.startswith("candidate:"):
        raise HTTPException(404, "Candidate not found")
    return c


def log(kind, data):
    return store.put(
        "audit:" + uuid4().hex, "audit", {"kind": kind, "created_at": iso(), **data}
    )


def save_candidate(c, trusted=False, original=None):
    clean = Candidate.model_validate(c).model_dump()
    role_for(clean["role_id"])
    if not trusted:
        clean["identity_status"] = (
            "conflict" if clean["identity_status"] == "conflict" else "unknown"
        )
        for e in clean["evidence"]:
            e["verified"] = False
    pid = original["person_id"] if original else person_key(clean)
    key = original["id"] if original else f"candidate:{digest([pid, clean['role_id']])}"
    if not original and store.get(key):
        raise Conflict(
            "This profile is already on this role’s watchlist. Open the existing record."
        )
    clean["person_id"] = pid
    clean["next_check_at"] = iso()
    if original:
        for field in (
            "dismissed",
            "snooze_until",
            "research_history",
            "distinct_people",
            "source_change_pending",
            "proposal_hold",
        ):
            if field in original:
                clean[field] = original[field]
        log(
            "candidate_revision",
            {"candidate_id": key, "before": original, "mode": clean["mode"]},
        )
    if trusted:
        for field in ("review_hash", "reviewed_at"):
            if c.get(field):
                clean[field] = c[field]
    store.put(
        pid,
        "person",
        {
            "mode": clean["mode"],
            "profile_url": clean["profile_url"],
            "name": clean["name"],
        },
        only_new=True,
    )
    return store.put(
        key, "candidate", clean, expected=original["revision"] if original else None
    )


def merge_research(original, proposal):
    """A web refresh cannot erase authorized offline evidence or a human referral."""
    merged = {
        **original,
        **proposal,
        **{k: original[k] for k in ("name", "profile_url", "role_id", "mode")},
    }
    manual = {
        e["id"]: e
        for e in original["evidence"]
        if e["source_kind"]
        in (
            "manual_source",
            "authorized_referral",
            "candidate_supplied_update",
            "owned_recruiting_history",
            "authorized_talent_network",
        )
    }
    merged["evidence"] = list(
        {
            **manual,
            **{e["id"]: e for e in proposal.get("evidence", original["evidence"])},
        }.values()
    )
    fit = {f["criterion"]: f for f in proposal.get("fit", original["fit"])}
    for f in original["fit"]:
        manual_refs = [eid for eid in f["evidence_ids"] if eid in manual]
        if (
            manual_refs
            and fit.get(f["criterion"], {}).get("status", "unknown") == "unknown"
        ):
            fit[f["criterion"]] = {**f, "evidence_ids": manual_refs}
    merged["fit"] = list(fit.values())
    events = {e["id"]: e for e in proposal.get("events", original["events"])}
    for e in original["events"]:
        if (
            e["evidence_ids"]
            and set(e["evidence_ids"]).issubset(manual)
            and e["id"] not in events
        ):
            events[e["id"]] = e
    merged["events"] = list(events.values())
    if original["route"]["evidence_ids"] and set(
        original["route"]["evidence_ids"]
    ).issubset(manual):
        merged["route"] = original["route"]
    if original["draft"].get("sender_name"):
        merged["draft"] = {
            **merged["draft"],
            "sender_name": original["draft"]["sender_name"],
            "sender_function": original["draft"]["sender_function"],
        }
    return merged


def seed_simulation():
    roles = {
        r["id"].removeprefix("role:"): {**r, "id": r["id"].removeprefix("role:")}
        for r in store.all("role")
    }
    for c in scenarios(roles):
        try:
            save_candidate(c, trusted=True)
        except Conflict:
            pass


def seed_role(r):
    """A config/roles.json role into the web app's store, for the pages that still read roles there."""
    from .role_compiler import build_signal_plan

    r["signal_plan"] = build_signal_plan(r)
    r["manager_notes"] = "\n".join(r.get("manager_preferences", []))
    r["required_criteria"] = r.get("required_criteria") or r["criteria"][:2]  # as update_role falls back
    store.put("role:" + r["id"], "role", r, only_new=True)


def initialize():
    configured = json.loads((ROOT / "config/roles.json").read_text())["roles"]
    for r in configured:
        seed_role(r)
    for r in store.all("role"):  # a dropped role taken out while the app was down (scripts/roles.py remove)
        if r.get("implementation_stage") == role_drop.DROPPED and r["id"].removeprefix("role:") not in {
                c["id"] for c in configured}:
            store.remove(r["id"])
    contact.migrate(store)
    if not store.get("simulation-initialized"):
        seed_simulation()
        store.put("simulation-initialized", "meta", {"created_at": iso()})
    private_seed = ROOT / "research/private/live-seeds.json"
    if private_seed.exists() and not store.get("live-seeded"):
        payload = json.loads(private_seed.read_text())
        rows = (
            payload
            if isinstance(payload, list)
            else payload.get("candidates", payload.get("prospects", []))
        )
        for c in rows:
            try:
                save_candidate(c)
            except (Conflict, ValidationError):
                continue
        store.put("live-seeded", "meta", {"created_at": iso()})
    from . import company_context, crustdata

    # Idempotent: existing timeline events are no-ops.
    crustdata.backfill_timeline(store)
    company_context.backfill_timeline(store)
    # A single local worker resumes queued work; interrupted jobs are visible/retryable.
    for job in store.all("job"):
        if job["status"] == "running":
            store.put(
                job["id"],
                "job",
                {
                    **job,
                    "status": "queued" if job.get("remote_run_id") else "failed",
                    "error": None
                    if job.get("remote_run_id")
                    else "Worker stopped during this run. Review usage before retrying.",
                },
            )


def enriched(c):
    now = utcnow()
    d = decide(c, role_for(c["role_id"]), contact.history(store, c["person_id"]), now,
               assess(store, c["person_id"], iso(now), c["role_id"]))
    # Surface possible alias collisions rather than silently merging by name.
    aliases = [
        x["id"]
        for x in store.all("candidate")
        if x["mode"] == c["mode"]
        and x["name"].casefold() == c["name"].casefold()
        and x["person_id"] != c["person_id"]
        and x["person_id"] not in c.get("distinct_people", [])
    ]
    if aliases and d["state"] == "ready":
        d.update(state="needs_review", ping=None)
        d["reasons"].append(
            "Another profile has the same name. Link the identities before contacting."
        )
    return {**c, "decision": d, "possible_aliases": aliases}


def evaluate(mode):
    """Refresh the internal timing proposals for one workspace (the single delivery path)."""
    from .sequence import refresh_alerts

    return {"received": refresh_alerts(store, mode, role_for), "channel": "local_timing_workspace"}


async def execute_exa_discovery(job, s):
    """Create once, persist the remote ID, and yield between individual polls."""
    from . import exa_agent

    current = store.get(job["id"]) or job
    role = current.get("role_snapshot") or role_for(job["role_id"])
    if current.get("remote_run_id"):
        run = await exa_agent.poll_run(current["remote_run_id"], s)
    else:
        if current.get("creation_started_at"):
            raise ValueError(
                "Exa creation has an uncertain outcome. Check the Exa dashboard before starting a replacement; automatic creation is disabled."
            )
        current = store.put(
            job["id"],
            "job",
            {
                **current,
                "creation_started_at": iso(),
                "role_snapshot": role,
            },
        )
        exclusions = [
            c
            for c in store.all("candidate")
            if c["mode"] == "live" and c["role_id"] == role["id"]
        ]
        try:
            run = await exa_agent.create_run(role, s, exclusions)
        except exa_agent.RequestRejected:
            store.put(job["id"], "job", {**current, "creation_started_at": None})
            raise
    current = store.put(
        job["id"],
        "job",
        {
            **current,
            "remote_run_id": run["id"],
            "remote_status": run["status"],
            "remote_run": run,
            "last_polled_at": iso(),
        },
    )
    if run["status"] not in exa_agent.TERMINAL_STATUSES:
        return {
            "_pending": True,
            "remote_run_id": run["id"],
            "remote_status": run["status"],
            "note": "Exa is researching; this run will resume after a restart.",
        }
    proposals = exa_agent.parse_completed(run, role)
    added, duplicates, invalid, source_ids = [], 0, [], set()
    for proposal in proposals:
        for doc in proposal.get("_documents", []):
            key = "source:" + digest([doc["url"], role["id"]])
            existing = store.get(key)
            if existing and existing.get("text"):
                doc = {**doc, **existing, "grounding": doc.get("grounding", [])}
            store.put(key, "source", {**doc, "role_id": role["id"], "mode": "live"})
            source_ids.add(key)
        try:
            saved = save_candidate(proposal)
            added.append(saved["id"])
        except Conflict:
            duplicates += 1
        except ValidationError:
            invalid.append(proposal.get("name", "Unknown"))
    return {
        "provider": "exa_agent",
        "remote_run_id": run["id"],
        "candidates_added": len(added),
        "candidate_ids": added,
        "duplicates_skipped": duplicates,
        "invalid_proposals": invalid,
        "sources": len(source_ids),
        "costDollars": run.get("costDollars"),
        "stopReason": run.get("stopReason"),
        "warnings": run.get("warnings", []),
        "role_brief_changed": role["brief_version"]
        != role_for(role["id"])["brief_version"],
        "note": "Cited discovery hypotheses saved. Deep research and human review are required before a ping.",
    }


async def check_research(proposal, candidate, role, s):
    """The small-model steps after research, each on its own bounded budget (separate from
    research's) so one never starves another. This run's pages and requests to wait go on the
    timeline first (append-only facts, kept even if a later write conflicts), so the kill pass
    and the decision on the saved candidate read them; then the kill pass. Returns the checked
    proposal, the timeline update and the calls each step made."""
    from . import extraction, verify

    pages = providers.Budget({**s, "max_calls_per_run": 8})  # one call per page, up to 8
    timeline_update = await extraction.record_documents(
        proposal.get("_input_documents", []), candidate, settings=s, store=store, budget=pages)
    follow_ups = extraction.record_follow_ups(proposal, candidate, store)
    timeline_update |= {"follow_ups": follow_ups["follow_ups"], "errors": timeline_update["errors"] + follow_ups["errors"]}
    budget = providers.Budget({**s, "max_calls_per_run": 16})  # 2 searches, up to 6 source reads, one check per hook

    async def search(query, after):
        if not s.get("exa_key"):
            raise providers.ProviderError("Exa is not configured; supersession search skipped.")
        docs = await providers._search(query, role, s, budget)
        return [{"url": d["url"], "text": d["text"], "published_at": d.get("published_at")} for d in docs]

    proposal = await verify.kill_pass(proposal, candidate, settings=s, store=store, search_fn=search, budget=budget,
                                      readiness=assess(store, candidate["person_id"], iso(), candidate["role_id"]))
    return proposal, timeline_update, {"extraction": pages.used, "kill_pass": budget.used}


async def execute_job(job):
    s = settings()
    kind = job["kind"]
    if kind == "evaluate":
        return evaluate(job["mode"])
    if job["mode"] != "live":
        raise ValueError("Simulation never calls external providers. Use Evaluate.")
    if kind == "company_refresh":
        from . import company_context
        # GI changes land on the timeline as org events; detectors match them to people. No per-watch research
        # fan-out on every page change.
        return await company_context.refresh(store, s)
    if kind in ("crustdata_connect", "crustdata_sync"):
        from . import crustdata

        client = crustdata.Client(s.get("crustdata_key"))
        store.put("crustdata:schedule", "meta", {"next_poll_at": iso(utcnow() + timedelta(minutes=15))})
        result = await (crustdata.connect(store, client) if kind == "crustdata_connect" else crustdata.sync(store, client))
        # Recover unqueued observations after a crash; one research job per candidate.
        pending = [o for o in store.all("crustdata_observation") if o["status"] == "pending_research"]
        for cid in sorted({cid for o in pending for cid in o["candidate_ids"] if cid not in o.get("researched_candidate_ids", [])}):
            if any(j.get("candidate_id") == cid and j["status"] in ("queued", "running")
                   and j["kind"] in ("research", "watch") for j in store.all("job")):
                continue
            c = candidate_for(cid)
            if enriched(c)["decision"]["state"] in ("suppressed", "snoozed"):
                continue
            c["review_hash"] = None
            c["source_change_pending"] = {"provider": "crustdata", "detected_at": iso()}
            c["research_question"] = "Investigate the supplied Crustdata observations. Establish actual event dates, this person's involvement, why a GI conversation has value NOW versus waiting, and counterevidence. A new post or profile change alone is not a timing recommendation."
            store.put(cid, "candidate", c, expected=c["revision"])
            queued = enqueue("research", "live", candidate_id=cid)
            batch = sorted([o for o in pending if cid in o["candidate_ids"] and cid not in o.get("researched_candidate_ids", [])], key=lambda o: o["detected_at"])[:20]
            for observation in batch:
                if cid in observation["candidate_ids"]:
                    current = store.get(observation["id"])
                    jobs = {**current.get("research_jobs", {}), cid: queued["id"]}
                    done = set(jobs) >= set(current["candidate_ids"])
                    store.put(current["id"], "crustdata_observation", {**current, "research_jobs": jobs,
                        "status": "research_queued" if done else "pending_research"})
        evaluate("live")
        return result
    if kind == "discover":
        if s.get("exa_key") or job.get("remote_run_id"):
            return await execute_exa_discovery(job, s)
        role = role_for(job["role_id"])
        budget_settings = {**s, "_budget": providers.Budget(s)}
        docs = await providers.discover(role, budget_settings)
        for doc in docs:
            key = "source:" + digest([doc["url"], role["id"]])
            store.put(key, "source", {**doc, "role_id": role["id"], "mode": "live"})
        count = 0
        if s["anthropic_key"]:
            for proposal in await providers.extract_prospects(
                docs, role, budget_settings
            ):
                try:
                    save_candidate({**proposal, "mode": "live", "role_id": role["id"]})
                    count += 1
                except (Conflict, ValidationError):
                    pass
        return {
            "sources": len(docs),
            "candidates_added": count,
            "provider_calls": budget_settings["_budget"].used,
            "warnings": budget_settings["_budget"].warnings,
            "note": "Source discovery completed. New people require deep research and review.",
        }
    if kind in ("research", "watch"):
        original = candidate_for(job["candidate_id"])
        previous = store.get("research-baseline:" + original["id"])
        warnings, changed, established = [], [], []
        watched_calls = 0
        watch_budget = providers.Budget(s)
        role = role_for(original["role_id"])
        if (
            kind == "watch"
            and previous
            and previous.get("brief_version") == role["brief_version"]
            and parse_time(previous["researched_at"]) > utcnow() - timedelta(days=7)
        ):
            # One source observation is shared by all people and roles referencing it. Only re-run expensive
            # interpretation on a changed source; weekly broader research still searches beyond known pages for new
            # events.
            watch = store.get("watch:" + original["id"])
            urls = (
                watch["urls"]
                if watch
                else list(
                    dict.fromkeys(
                        [original["profile_url"]]
                        + [e["url"] for e in original["evidence"]]
                    )
                )
            )[: min(s["max_calls_per_run"], 8)]
            for url in urls:
                key = "observation:" + digest(url)
                cached = store.get(key)
                method = previous.get("extraction_methods", {}).get(url, "native")
                if (
                    cached
                    and cached.get("extraction_method", "native") == method
                    and parse_time(cached["checked_at"]) > utcnow() - timedelta(hours=1)
                ):
                    current = cached
                else:
                    try:
                        watched_calls += 1
                        if method == "exa_contents":
                            if not s.get("exa_key"):
                                raise providers.ProviderError(
                                    "Exa is required to refresh this source with the original extraction method."
                                )
                            fetched = await providers.fetch_current_documents(
                                [url], s, watch_budget, role["id"]
                            )
                            if not fetched["documents"]:
                                raise providers.ProviderError(
                                    "Exa could not refresh a watched source; coverage is incomplete."
                                )
                            doc = fetched["documents"][0]
                        else:
                            watch_budget.take()
                            doc = await providers.fetch_document(url)
                        method = doc.get("extraction_method", method)
                        current = {
                            "url": url,
                            "checked_at": iso(),
                            "content_hash": digest(doc["text"]),
                            "text": doc["text"],
                            "title": doc["title"],
                            "extraction_method": method,
                        }
                        store.put(key, "observation", current)
                    except providers.ProviderError as e:
                        warnings.append(str(e))
                        continue
                if url not in previous.get("sources", {}) or previous.get(
                    "extraction_methods", {}
                ).get(url, "native") != current.get("extraction_method", "native"):
                    previous.setdefault("sources", {})[url] = current["content_hash"]
                    previous.setdefault("extraction_methods", {})[url] = method
                    established.append(url)
                    # First observation is a baseline, not evidence of a change.
                    continue
                if previous["sources"][url] != current["content_hash"]:
                    changed.append(url)
                else:
                    for e in original["evidence"]:
                        if e["url"] == url:
                            e["last_checked_at"] = current["checked_at"]
            if established:
                store.put(
                    previous["id"], "baseline", previous, expected=previous["revision"]
                )
            if not changed:
                original["next_check_at"] = enriched(original)["decision"][
                    "next_check_at"
                ]
                store.put(
                    original["id"], "candidate", original, expected=original["revision"]
                )
                if warnings:
                    raise providers.ProviderError(
                        "Coverage degraded: some watched sources failed. "
                        + "; ".join(warnings[:2])
                    )
                return {
                    "candidate_id": original["id"],
                    "note": "Comparable sources unchanged. First observations establish baselines, not changes; broader search runs at least weekly.",
                    "baselines_established": established,
                    "provider_model_calls": 0,
                }
            # Discovery is its own durable bounded job. A change invalidates earlier review immediately, even if the
            # interpretation provider is unavailable.
            original["review_hash"] = None
            original["source_change_pending"] = {"urls": changed, "detected_at": iso()}
            original["research_question"] = "Verify the change on " + "; ".join(changed)
            store.put(
                original["id"], "candidate", original, expected=original["revision"]
            )
            evaluate(original["mode"])
            enqueue("research", "live", candidate_id=original["id"])
            return {
                "candidate_id": original["id"],
                "changed_sources": changed,
                "source_calls": watched_calls,
                "note": "Changed evidence invalidated the old review. Deep research is queued as a separate bounded job.",
                "warnings": warnings,
            }
        unknown = [
            x["criterion"]
            for x in original["fit"]
            if x["status"] == "unknown"
            and x["criterion"] in role.get("required_criteria", [])
        ]
        question = (
            "Find attributable evidence for " + "; ".join(unknown[:2])
            if unknown
            else "Find a materially new professional conversation opportunity; distinguish changes from previously observed work."
        )
        watch = store.get("watch:" + original["id"]) or {}
        assigned_observations = sorted([o for o in store.all("crustdata_observation")
            if original["id"] in o.get("candidate_ids", []) and not o.get("baseline")
            and o.get("research_jobs", {}).get(original["id"]) == job.get("id")], key=lambda o: o["detected_at"])
        # Raw provider payloads stay in the audit store. Only bounded leads go to the model; source documents still
        # have to establish the actual claims.
        leads = [{k: o.get(k) for k in ("id", "kind", "source_url", "event_date", "event_date_basis", "detected_at", "provider_run_at", "timing_claim")}
                 | {"summary": o.get("summary", "")[:2400]} for o in assigned_observations]
        context = {
            **original,
            "provider_observations": leads,
            "watch_context": {
                "fit_basis": watch.get("fit_basis"),
                "organization": watch.get("organization"),
                "organization_is_unverified_context": True,
                "urls": list(dict.fromkeys([o["source_url"] for o in assigned_observations if o.get("source_url")][-4:]
                    + watch.get("urls", []))),
            },
            "contact_history": store.get(original["person_id"]),
            "reviewer_feedback": [
                f for f in store.all("feedback") if f["candidate_id"] == original["id"]
            ][-20:],
        }
        from . import company_context
        gi_context = company_context.context(store)
        context["company_context_coverage"] = gi_context.get("coverage")
        context["company_context_trust"] = gi_context.get("trust")
        proposal = await providers.research(
            context,
            role,
            {**s, "research_question": job.get("research_question") or original.get("research_question") or question,
             "company_documents": gi_context.get("documents", [])},
        )
        # Keep paid output before anything below can fail or an optimistic write can reject it, then again with the
        # kill pass's checks and the calls the steps made. It is an audit and review record, never an instruction to
        # replay stale judgment.
        research_job_id = job.get("id") or "job:" + uuid4().hex
        artifact = {
            "candidate_id": original["id"],
            "input_revision": original["revision"],
            "role_brief_version": role["brief_version"],
            "generated_at": iso(),
        }
        store.put("research-artifact:" + research_job_id, "research_artifact", {**artifact, "proposal": proposal})
        proposal, timeline_update, step_calls = await check_research(proposal, original, role, s)
        store.put("research-artifact:" + research_job_id, "research_artifact",
                  {**artifact, "proposal": proposal, "step_calls": step_calls})
        retained_message = (
            "Research input changed while the run was in progress. The proposal, "
            "sources and usage were retained for review at "
            f"/api/jobs/{research_job_id}/research-proposal; nothing was applied. "
            "Review the new context before requesting fresh research."
        )
        current = store.get(original["id"])
        current_role = store.get("role:" + original["role_id"])
        if (
            not current
            or current["revision"] != original["revision"]
            or not current_role
            or current_role["brief_version"] != role["brief_version"]
        ):
            raise Conflict(retained_message)
        # Never let a model silently alter identity, mode, or role ownership.
        merged = merge_research(original, proposal)
        try:
            saved = save_candidate(merged, original=original)
        except Conflict as error:
            raise Conflict(retained_message) from error
        saved["source_change_pending"] = None
        saved["proposal_hold"] = None
        saved["next_check_at"] = enriched(saved)["decision"]["next_check_at"]
        store.put(saved["id"], "candidate", saved, expected=saved["revision"])
        hashes, extraction_methods = {}, {}
        for doc in proposal.get("_documents", []):
            # Search highlights are query-dependent excerpts; diff full pages only.
            if doc.get("content_kind") == "highlights":
                continue
            hashes[doc["url"]] = digest(doc["text"])
            extraction_methods[doc["url"]] = doc.get("extraction_method", "native")
            store.put(
                "observation:" + digest(doc["url"]),
                "observation",
                {
                    "url": doc["url"],
                    "checked_at": iso(),
                    "content_hash": hashes[doc["url"]],
                    "text": doc["text"],
                    "title": doc["title"],
                    "extraction_method": extraction_methods[doc["url"]],
                },
            )
        store.put(
            "research-baseline:" + original["id"],
            "baseline",
            {
                "candidate_id": original["id"],
                "researched_at": iso(),
                "sources": hashes,
                "extraction_methods": extraction_methods,
                "brief_version": role["brief_version"],
            },
        )
        evaluate(original["mode"])
        for observation in assigned_observations:
            current_observation = store.get(observation["id"])
            completed = list(set(current_observation.get("researched_candidate_ids", []) + [original["id"]]))
            store.put(observation["id"], "crustdata_observation", {**current_observation,
                "researched_candidate_ids": completed,
                "status": "researched" if set(completed) >= set(current_observation["candidate_ids"]) else current_observation["status"]})
        return {
            "candidate_id": saved["id"],
            "note": "Research updated. Review sources and proposed timing before release.",
            "usage": proposal.get("_usage", {}),
            "step_calls": step_calls,  # the calls after research's own (usage): page extraction, the kill pass
            "timeline": timeline_update,
        }
    raise ValueError("Unknown operation")


async def run_job(job):
    store.put(job["id"], "job", {**job, "status": "running", "started_at": iso()})
    try:
        result = await execute_job(job)
        pending = result.pop("_pending", False)
        if job["kind"] in ("watch", "research") and not pending:
            record_watch_check(job, result=result)
        store.put(job["id"], "job", {
            **(store.get(job["id"]) or job),
            "status": "queued" if pending else "complete",
            "result": result,
            "finished_at": None if pending else iso(),
            "next_poll_at": iso(utcnow() + timedelta(seconds=15)) if pending else None,
            "error": None,
        })
    except Exception as e:
        # ProviderError text is sanitized by adapters; never expose raw request headers.

        error = (
            str(e)
            if isinstance(e, (providers.ProviderError, Conflict, ValueError))
            else f"Run failed ({type(e).__name__}). No ping was released; retry after checking setup."
        )[:1500]
        if job["kind"] in ("watch", "research"):
            record_watch_check(job, error=error)
        store.put(job["id"], "job", {**(store.get(job["id"]) or job), "status": "failed",
                                     "error": error, "finished_at": iso()})


def schedule_monitoring(now):
    """Enqueue due monitoring work; return when the next item becomes due."""
    from . import company_context, journey, moments

    evaluate("live")  # time-based withdrawals (stale evidence, due follow-ups)
    upcoming = [now + timedelta(minutes=15)]
    # Only people still on an active live watch, and not dismissed or opted out, get moment rechecks.
    watched = [c for w in store.all("watch") if w["mode"] == "live" and w["status"] == "active"
               if (c := store.get(w["candidate_id"])) and enriched(c)["decision"]["state"] != "suppressed"]
    # journey.related adds the person's employer ids (WARN name, SEC CIK) to "gi".
    targets = [(c["person_id"], {"id": c["role_id"]}, tuple(journey.related(store, c["person_id"]))) for c in watched]
    _, recheck_at = moments.due_rechecks(store, now, targets)
    if recheck_at:
        upcoming.append(recheck_at)
    if company_context.due(store):
        enqueue("company_refresh", "live")
    if settings().get("crustdata_key") and any(s.get("remote_id") for s in store.all("crustdata_subscription")):
        due = parse_time((store.get("crustdata:schedule") or {}).get("next_poll_at") or iso(now))
        if due <= now:
            enqueue("crustdata_sync", "live")
        else:
            upcoming.append(due)
    busy = {j.get("candidate_id") for j in store.all("job") if j["status"] in ("queued", "running")}
    for watch in store.all("watch"):
        if watch["mode"] != "live" or watch["status"] != "active" or watch["candidate_id"] in busy:
            continue
        due = parse_time(watch.get("next_check_at") or iso(now))
        if due > now:
            upcoming.append(due)
            continue
        c = store.get(watch["candidate_id"])
        d = enriched(c)["decision"] if c else {"state": "suppressed"}
        if d["state"] in ("suppressed", "snoozed"):
            continue
        enqueue("watch", "live", candidate_id=c["id"])
        store.put(watch["id"], "watch", {**watch, "next_check_at": d["next_check_at"]}, expected=watch["revision"])
    return min(upcoming)


async def worker():
    """One sequential job runner. It sleeps until the next due item instead of polling."""
    next_scan = utcnow()
    while True:
        try:
            now = utcnow()
            queued = sorted(
                (j for j in store.all("job") if j["status"] == "queued"),
                key=lambda j: j["created_at"],
            )
            due = [j for j in queued if not j.get("next_poll_at") or parse_time(j["next_poll_at"]) <= now]
            if due:
                await run_job(due[0])
                await asyncio.sleep(0)
                continue
            if settings()["monitoring_enabled"] and now >= next_scan:
                next_scan = schedule_monitoring(now)
            wake_at = min([next_scan] + [parse_time(j["next_poll_at"]) for j in queued if j.get("next_poll_at")])
            wake.clear()
            # Sleep in 1 s steps so a new enqueue starts promptly.
            for _ in range(max(1, min(60, int((wake_at - utcnow()).total_seconds())))):
                await asyncio.sleep(1)
                if wake.is_set():
                    break
        except asyncio.CancelledError:
            return
        except Exception as e:
            log("worker_error", {"error": f"{type(e).__name__}: {str(e)[:500]}"})
            await asyncio.sleep(5)


def enqueue(kind, mode, role_id=None, candidate_id=None):
    for j in store.all("job"):
        if j["status"] in ("queued", "running") and (
            j["kind"],
            j["mode"],
            j.get("role_id"),
            j.get("candidate_id"),
        ) == (kind, mode, role_id, candidate_id):
            return j
    # A retry resumes a known remote run instead of buying the same discovery twice.
    for j in reversed(store.all("job")):
        if j["status"] == "failed" and (
            j["kind"],
            j["mode"],
            j.get("role_id"),
            j.get("candidate_id"),
        ) == (kind, mode, role_id, candidate_id):
            if j.get("remote_run_id") and j.get("remote_status") not in (
                "failed",
                "cancelled",
                "completed",
            ):
                return store.put(
                    j["id"],
                    "job",
                    {**j, "status": "queued", "next_poll_at": iso(), "error": None},
                )
            if j.get("creation_started_at") and not j.get("remote_run_id"):
                raise HTTPException(
                    409,
                    "An earlier Exa creation has an uncertain outcome. Check Exa runs before starting a replacement.",
                )
    wake.set()
    return store.put(
        "job:" + uuid4().hex,
        "job",
        {
            "kind": kind,
            "mode": mode,
            "role_id": role_id,
            "candidate_id": candidate_id,
            "status": "queued",
            "created_at": iso(),
            "result": None,
            "error": None,
        },
    )


@asynccontextmanager
async def lifespan(app):
    initialize()
    evaluate("simulation")
    evaluate("live")
    task = asyncio.create_task(worker())
    yield
    task.cancel()
    await task


app = FastAPI(title="GI Timing Engine", lifespan=lifespan)


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.url.hostname
    if host not in ("127.0.0.1", "localhost", "testserver"):
        return JSONResponse({"detail": "This pilot is local-only."}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse(
                {"detail": "Cross-origin writes are disabled."}, status_code=403
            )
        if "application/json" not in request.headers.get("content-type", ""):
            return JSONResponse({"detail": "Use application/json."}, status_code=415)
        if int(request.headers.get("content-length", "0")) > 1_000_000:
            return JSONResponse({"detail": "Request too large."}, status_code=413)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


@app.exception_handler(Conflict)
async def conflict_handler(_, e):
    return JSONResponse({"detail": str(e)}, status_code=409)


@app.exception_handler(ValidationError)
async def validation_handler(_, e):
    return JSONResponse({"detail": str(e)}, status_code=422)


@app.get("/")
def index():
    return FileResponse(ROOT / "app/static/index.html")


@app.get("/api/company-context")
def get_company_context():
    from . import company_context
    return {**company_context.status(store), "refresh_due": company_context.due(store),
            "pending_jobs": [j["id"] for j in store.all("job") if j.get("kind") == "company_refresh" and j.get("status") in ("queued", "running")]}


@app.post("/api/company-context/refresh")
def refresh_company_context(body: dict):
    return enqueue("company_refresh", "live")


@app.get("/api/state")
def state(mode: str = "simulation"):
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    from .timeline import private_notes

    candidates = [
        {**enriched(c), "private_notes": private_notes(store, c["person_id"])}
        for c in store.all("candidate")
        if c["mode"] == mode
    ]
    def rank(d):
        # Someone readiness says to reach now waits only on research: listed before those who wait.
        if d["state"] == "research" and (d.get("readiness") or {}).get("action") == "reach_now":
            return 1.5
        return {"ready": 0, "needs_review": 1, "watch": 2, "research": 3}.get(d["state"], 4)

    candidates.sort(key=lambda c: (rank(c["decision"]), -c["decision"]["fit_score"]))
    from . import company_context
    return {
        "now": iso(),
        "mode": mode,
        "company_context": company_context.status(store) if mode == "live" else None,
        "roles": [
            {**r, "id": (key := r["id"].removeprefix("role:")),
             "watch": (spec := role_drop.spec_of(key)) and role_drop.watch(spec, r)}
            for r in store.all("role")
        ],
        "drop_price": role_drop.PRICE,  # said before a dropped JD is read
        "candidates": candidates,
        "jobs": sorted(
            [j for j in store.all("job") if j["mode"] == mode],
            key=lambda j: j["created_at"],
            reverse=True,
        )[:50],
        "sources": [
            {k: v for k, v in s.items() if k != "text"} for s in store.all("source")
        ],
        "settings": safe_settings(),
    }


@app.get("/api/calls")
def todays_calls(mode: str = "simulation"):
    """The engine's call for everyone in the workspace's timelines, each as a seven-part ping (app/today.py)."""
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    from . import today

    return today.calls(mode, companies=True)


@app.get("/api/calls/sources")
def calls_sources(mode: str = "simulation"):
    """Behind the scenes on Today's calls: what the workspace watches and what came in (app/sourcing.py)."""
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    from . import sourcing

    return sourcing.overview(mode) or {"missing": "No timelines yet: run the pull first."}


@app.get("/api/calls/{subject_id}/trail")
def call_trail(subject_id: str, mode: str = "simulation"):
    """One person's sources and the trail from each public item to their call (app/sourcing.py)."""
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    from . import sourcing

    if not (found := sourcing.trail(mode, subject_id)):
        raise HTTPException(404, "Not in this workspace's timelines.")
    return found


@app.post("/api/calls/{subject_id}/ask")
async def ask_about(subject_id: str, body: dict):
    """A question about one person: a suggested one answered free, any other by the model from their stored facts
    only (app/ask.py)."""
    mode = body.get("mode")
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    from . import ask

    try:
        if (key := body.get("suggested")) is not None:
            if not isinstance(key, str) or key not in ask.SUGGESTED:
                raise ValueError(f"Pick one of: {', '.join(ask.SUGGESTED)}.")
            found = ask.suggested(mode, subject_id, key)
        else:
            found = await ask.ask(mode, subject_id, body.get("question"), settings())
    except (ValueError, providers.ProviderError) as e:
        raise HTTPException(422, str(e)) from e
    if not found:
        raise HTTPException(404, "Not in this workspace's timelines.")
    return found


@app.get("/api/scorecard")
def scorecard(mode: str = "simulation"):
    """The newest early-sign scorecard on real people; simulation has none, since invented people prove nothing."""
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    from . import today

    return {"report": today.latest_scorecard() if mode == "live" else None, "folder": str(today.SCORECARDS)}


@app.get("/api/events")
def events_page(mode: str = "simulation"):
    """GI's own events as a hiring sensor: the next event's plan, the last one's door notes and follow-ups (app/events.py)."""
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    from . import events

    return events.page(mode)


@app.get("/api/week")
def this_week(mode: str = "simulation"):
    """The Monday brief for GI's ops team: the week's decisions, most urgent first, and the detail behind them, as the
    Monday post names them (weekly.brief)."""
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    from . import inbox, routing, weekly

    week = weekly.brief(mode)  # the Slack post's week: no one the post would leave out
    # The Block Kit preview carries the card in a link, so only the invented week gets one.
    card = inbox.card(week) if mode == "simulation" and not week["missing"] else None
    preview = routing.builder_url(card) if card else None
    return {**week, "preview": preview}


@app.get("/api/hiring")
def hiring_page(mode: str = "simulation"):
    """The hiring budget: each surfaced person's first-year cost if hired, and a plan against the year (app/hiring.py)."""
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    from . import hiring

    return hiring.page(mode)


@app.get("/api/month")
def month_page(mode: str = "simulation"):
    """The founders' one-pager: pipeline per role, spend against budget, what's slipping (app/month.py)."""
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    from . import month

    m = month.month(mode)
    return {**m, "text": month.text(m)}


@app.get("/api/starts")
def starts_page(mode: str = "simulation"):
    """New starts: per accepted offer, what must be ready by the first day, who owns it and when (app/starts.py)."""
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    from . import starts

    return starts.page(mode)


def _starts_call(fn, *args, **kwargs):
    from pydantic import ValidationError

    try:
        return fn(*args, **kwargs)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValidationError as e:
        raise HTTPException(422, "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:3]))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/starts")
def starts_add(body: dict):
    """An accepted offer: add the hire and their checklist. Sends nothing."""
    from . import starts

    mode = body.get("mode", "simulation")
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    hire = _starts_call(starts.add, mode, str(body.get("name") or ""), str(body.get("role_id") or ""),
                        str(body.get("office") or ""), str(body.get("start") or ""), body.get("subject_id") or None,
                        str(body.get("offer_on") or "") or None)
    return {"id": hire.id}


@app.patch("/api/starts/{hire_id}")
def starts_update(hire_id: str, body: dict):
    """Change one checklist item: its owner, its state, or what the candidate said at the offer stage."""
    from . import starts

    mode = body.get("mode", "simulation")
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    fields = {k: body[k] for k in ("owner", "state", "note") if isinstance(body.get(k), str)}
    _starts_call(starts.update, mode, hire_id, str(body.get("key") or ""), **fields)
    return {"ok": True}


@app.delete("/api/starts/{hire_id}")
def starts_remove(hire_id: str, body: dict):
    from . import starts

    mode = body.get("mode", "simulation")
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    _starts_call(starts.remove, mode, hire_id)
    return {"ok": True}


@app.put("/api/hiring")
def hiring_plan(body: dict):
    """Save the hiring plan and its assumptions; the projection comes back."""
    from pydantic import ValidationError

    from . import hiring

    mode = body.get("mode", "simulation")
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Select live or simulation")
    try:
        return hiring.update(mode, body)
    except ValidationError as e:
        raise HTTPException(422, "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:3]))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/events/{event_id}/notes")
def event_note(event_id: str, body: dict):
    """A door note from a GI event: it lands on the person's timeline, and the engine's new call comes back."""
    from . import events

    mode = body.get("mode", "simulation")
    if mode not in ("live", "simulation") or body.get("kind") not in ("open", "wait", "context"):
        raise HTTPException(422, "Select live or simulation, and a note that is open, wait or context")
    text = str(body.get("text") or "").strip()
    if not text or len(text) > 1000:
        raise HTTPException(422, "Write what they said, in under 1000 characters")
    if not isinstance(body.get("wait_until") or "", str) or (body["kind"] == "wait") != bool(body.get("wait_until")):
        raise HTTPException(422, "A wait needs the day they asked us to wait until, YYYY-MM-DD; only a wait has one")
    try:
        return events.add_note(mode, event_id, str(body.get("subject_id", "")), body["kind"], text,
                               str(body.get("author") or "GI host")[:80], body.get("wait_until") or None)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/events/{event_id}/ledger")
def event_ledger(event_id: str, body: dict):
    """A host sent this person the invite or the follow-up (nothing is sent from here): it goes in the contact ledger."""
    from . import events

    mode = body.get("mode", "simulation")
    if mode not in ("live", "simulation") or body.get("kind") not in ("invited", "sent"):
        raise HTTPException(422, "Select live or simulation, and an invite or a follow-up")
    try:
        return events.mark(mode, event_id, str(body.get("subject_id", "")), body["kind"],
                           str(body.get("by") or "GI host")[:80], also_invite=str(body.get("also_invite") or "") or None)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/jobs/{key}/research-proposal")
def research_proposal(key: str):
    artifact = store.get("research-artifact:" + key) if key.startswith("job:") else None
    if not artifact:
        raise HTTPException(404, "No retained research proposal for this job")
    return artifact


@app.post("/api/candidates")
def create_candidate(body: Candidate):
    return save_candidate(body.model_dump())


@app.put("/api/candidates/{key}")
def update_candidate(key: str, body: dict):
    c = candidate_for(key)
    if body.get("revision") != c["revision"]:
        raise Conflict("Candidate changed. Refresh before saving.")
    if (
        body.get("mode", c["mode"]) != c["mode"]
        or body.get("role_id", c["role_id"]) != c["role_id"]
    ):
        raise HTTPException(
            422, "Role and mode cannot be changed on an existing candidate."
        )
    saved = save_candidate({**c, **body}, original=c)
    evaluate(c["mode"])
    return enriched(saved)


@app.post("/api/candidates/{key}/review")
def review_candidate(key: str, body: Review):
    c = candidate_for(key)
    if body.revision != c["revision"]:
        raise Conflict("Research changed since you opened it. Review the new revision.")
    if not all((body.identity_confirmed, body.evidence_confirmed, body.fit_confirmed)):
        raise HTTPException(
            422,
            "Confirm identity, source/date accuracy, and fit/draft review explicitly.",
        )
    if c["identity_status"] == "conflict":
        raise HTTPException(
            422, "Resolve the identity conflict in the record before reviewing."
        )
    c["identity_status"] = "verified"
    for e in c["evidence"]:
        e["verified"] = True
    c["proposal_hold"] = None
    c["reviewed_at"] = iso()
    c["review_hash"] = review_hash(c, role_for(c["role_id"]))
    c["review_note"] = body.note
    saved = store.put(key, "candidate", c, expected=body.revision)
    log(
        "review",
        {
            "candidate_id": key,
            "mode": c["mode"],
            "review_hash": c["review_hash"],
            "note": body.note,
        },
    )
    evaluate(c["mode"])
    return enriched(saved)


@app.post("/api/candidates/{key}/action")
def candidate_action(key: str, body: Action):
    c = candidate_for(key)
    if body.action == "snooze":
        if not body.until or parse_time(body.until) <= utcnow():
            raise HTTPException(422, "Choose a future recheck date")
        c["snooze_until"] = iso(parse_time(body.until))
    elif body.action == "dismiss":
        c["dismissed"] = True
    elif body.action == "reopen":
        c["dismissed"] = False
        c["snooze_until"] = None
        # Reopen is not consent to reverse an opt-out or an active conversation.
    elif body.action in ("opt_out", "mark_sent"):
        contact.record(store, c["person_id"], {"opt_out": "never", "mark_sent": "sent"}[body.action],
                       role_id=c["role_id"], note=body.note)
    saved = store.put(key, "candidate", c, expected=c["revision"])
    log(
        body.action,
        {
            "candidate_id": key,
            "person_id": c["person_id"],
            "mode": c["mode"],
            "note": body.note,
            "until": body.until,
        },
    )
    evaluate(c["mode"])
    return enriched(saved)


@app.post("/api/candidates/{key}/feedback")
def feedback(key: str, body: dict):
    c = candidate_for(key)
    label = body.get("label")
    if label not in (
        "useful",
        "wrong_fit",
        "wrong_time",
        "wrong_identity",
        "weak_route",
        "weak_draft",
        "missed_signal",
    ):
        raise HTTPException(422, "Choose a supported feedback label")
    note = str(body.get("note", "")).strip()
    if len(note) > 4000:
        raise HTTPException(422, "Keep feedback under 4000 characters")
    record = store.put(
        "feedback:" + uuid4().hex,
        "feedback",
        {
            "candidate_id": key,
            "mode": c["mode"],
            "role_id": c["role_id"],
            "brief_version": role_for(c["role_id"])["brief_version"],
            "candidate_revision": c["revision"],
            "label": label,
            "note": note,
            "created_at": iso(),
            "policy_effect": "Recorded for the next research pass. Negative feedback invalidates this opportunity’s review; no automatic role or policy change.",
        },
    )
    if label not in ("useful", "missed_signal"):
        c["proposal_hold"] = label
        c["review_hash"] = None
        if label == "wrong_identity":
            c["identity_status"] = "conflict"
        store.put(key, "candidate", c, expected=c["revision"])
        evaluate(c["mode"])
    return record


@app.put("/api/roles/{key}")
def update_role(key: str, body: dict):
    old = role_for(key)
    if body.get("brief_version") != old["brief_version"]:
        raise Conflict("Role brief changed. Refresh before saving.")
    criteria = body.get("criteria", old["criteria"])
    if (
        not isinstance(criteria, list)
        or not criteria
        or not all(isinstance(x, str) and x.strip() for x in criteria)
    ):
        raise HTTPException(422, "Provide a nonempty list of role criteria.")
    criteria = list(dict.fromkeys(x.strip() for x in criteria))
    required = body.get(
        "required_criteria", [x for x in old["required_criteria"] if x in criteria]
    )
    if not set(required).issubset(criteria) or not required:
        required = criteria[:2]
    new = {
        **old,
        "criteria": criteria,
        "required_criteria": required,
        "manager_notes": str(body.get("manager_notes", old.get("manager_notes", ""))),
        "title": str(body.get("title", old["title"])),
        "closed": bool(body.get("closed", old.get("closed", False))),
        "brief_version": old["brief_version"] + 1,
    }
    log("role_revision", {"role_id": key, "before": old, "after": new})
    store.put("role:" + key, "role", new, expected=old["revision"])
    for c in store.all("candidate"):
        if c["role_id"] == key:
            c["next_check_at"] = iso()
            store.put(c["id"], "candidate", c, expected=c["revision"])
    evaluate("live")
    evaluate("simulation")
    return {**new, "id": key}


@app.get("/api/settings")
def get_settings():
    return safe_settings()


@app.get("/api/crustdata")
def crustdata_status():
    from .crustdata import status
    return status(store, bool(settings().get("crustdata_key")))


@app.post("/api/crustdata/connect")
def crustdata_connect():
    if not settings().get("crustdata_key"):
        raise HTTPException(422, "Add a Crustdata key in Setup.")
    return enqueue("crustdata_connect", "live")


@app.post("/api/crustdata/sync")
def crustdata_sync():
    if not settings().get("crustdata_key"):
        raise HTTPException(422, "Add a Crustdata key in Setup.")
    return enqueue("crustdata_sync", "live")


@app.post("/api/settings")
def set_settings(body: dict):
    config = store.get("settings") or {}
    if "model" in body:
        model = str(body["model"]).strip()
        if not model or len(model) > 100:
            raise HTTPException(422, "Enter a valid model ID")
        config["model"] = model
    if "max_calls_per_run" in body:
        limit = int(body["max_calls_per_run"])
        low, high = providers.MAX_CALLS_PER_RUN
        if not low <= limit <= high:
            raise HTTPException(422, f"Use {low}–{high} requests per run")
        config["max_calls_per_run"] = limit
    local = dict(dotenv_values(ROOT / ".env"))
    changed = False
    env_fields = {**secret_names, "anthropic_workspace_id": "ANTHROPIC_WORKSPACE_ID"}
    for key, env in env_fields.items():
        if body.get(key):
            value = str(body[key]).strip()
            if (
                any(char in value for char in ("\n", "\r", "'", '"'))
                or len(value) > 1000
            ):
                raise HTTPException(422, "Invalid API key format")
            local[env] = value
            changed = True
    if changed:
        target = ROOT / ".env"
        # .env is never served, logged or returned. Owner-only permissions from creation.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(
                "\n".join(f"{k}='{v}'" for k, v in local.items() if v is not None)
                + "\n"
            )
        os.chmod(target, 0o600)
    if "monitoring_enabled" in body:
        enabled = bool(body["monitoring_enabled"])
        if enabled and not (
            os.getenv("ANTHROPIC_API_KEY") or local.get("ANTHROPIC_API_KEY")
        ):
            raise HTTPException(
                422, "Add a research-model key before enabling monitoring."
            )
        config["monitoring_enabled"] = enabled
    store.put("settings", "settings", config)
    return safe_settings()


@app.post("/api/run")
def run(body: dict):
    kind, mode = body.get("operation"), body.get("mode", "live")
    if kind not in ("evaluate", "discover", "research") or mode not in (
        "live",
        "simulation",
    ):
        raise HTTPException(
            422, "Choose evaluate, discover, or research and a valid mode"
        )
    if kind == "discover":
        role_for(body.get("role_id", ""))
    if kind == "research":
        c = candidate_for(body.get("candidate_id", ""))
        if c["mode"] != mode:
            raise HTTPException(422, "Candidate belongs to a different mode")
        if not settings()["anthropic_key"]:
            raise HTTPException(
                422,
                "Add an Anthropic API key in Setup for live research. Simulation requires no key.",
            )
    if kind != "evaluate" and mode == "simulation":
        raise HTTPException(
            422,
            "Simulation uses fictional fixtures. Switch to Live for provider calls.",
        )
    return enqueue(kind, mode, body.get("role_id"), body.get("candidate_id"))


@app.post("/api/simulation/reset")
def reset_simulation():
    for kind in (
        "candidate",
        "person",
        "job",
        "audit",
        "feedback",
        "watch",
        "watch_observation",
        "review_alert",
    ):
        for row in store.all(kind):
            if row.get("mode") == "simulation":
                store.remove(row["id"])
                if kind == "person":
                    contact.forget(store, row["id"])
    from . import hiring, starts, today

    today._demo_store.cache_clear()  # Today's calls and Events: notes, invites and follow-ups logged in the demo
    hiring.reset()  # the Budget tab's plan
    starts.reset()  # New starts
    seed_simulation()
    return evaluate("simulation")


@app.post("/api/people/link")
def link_people(body: dict):
    a, b = (
        candidate_for(body.get("candidate_id", "")),
        candidate_for(body.get("same_person_as", "")),
    )
    if a["mode"] != b["mode"]:
        raise HTTPException(422, "Cannot merge simulation and live identities")
    if a["person_id"] == b["person_id"]:
        return {"linked": True}
    contact.relink(store, a["person_id"], b["person_id"])
    for c in store.all("candidate"):
        if c["person_id"] == a["person_id"]:
            c["person_id"] = b["person_id"]
            c["review_hash"] = None
            store.put(c["id"], "candidate", c, expected=c["revision"])
    log(
        "identity_link",
        {"from_person": a["person_id"], "to_person": b["person_id"], "mode": a["mode"]},
    )
    evaluate(a["mode"])
    return {"linked": True}


@app.post("/api/people/distinct")
def distinct_people(body: dict):
    a, b = (
        candidate_for(body.get("candidate_id", "")),
        candidate_for(body.get("other_candidate_id", "")),
    )
    note = str(body.get("note", "")).strip()
    if a["mode"] != b["mode"] or a["person_id"] == b["person_id"] or len(note) < 10:
        raise HTTPException(
            422, "Provide evidence explaining why these profiles are different people"
        )
    for first, second in ((a, b), (b, a)):
        first["distinct_people"] = list(
            set(first.get("distinct_people", []) + [second["person_id"]])
        )
        store.put(first["id"], "candidate", first, expected=first["revision"])
    log(
        "distinct_people",
        {"first": a["id"], "second": b["id"], "note": note, "mode": a["mode"]},
    )
    evaluate(a["mode"])
    return {"distinct": True}


@app.post("/api/people/{person_id}/notes")
def add_note(person_id: str, body: dict):
    """Tier-0 private context (for example a referral or a conversation), kept append-only.

    Its author types it: "open" to a move or "wait" until a later day counts for timing; untyped is context.
    """
    from . import timeline

    if not store.get(person_id) or not person_id.startswith("person:"):
        raise HTTPException(404, "Person not found")
    text, author = str(body.get("text", "")).strip(), str(body.get("author", "")).strip()
    supersedes = body.get("supersedes") or None
    note_type, wait_until = str(body.get("note_type") or "") or None, str(body.get("wait_until") or "") or None
    if not 1 <= len(text) <= 4000 or not 1 <= len(author) <= 120:
        raise HTTPException(422, "Add the note text and your name.")
    if supersedes and supersedes not in {n["id"] for n in timeline.private_notes(store, person_id)}:
        raise HTTPException(422, "Only a current note for this person can be edited.")
    try:
        return timeline.add_private_note(store, person_id, text, author, supersedes, note_type, wait_until)
    except ValueError as e:
        raise HTTPException(422, "Mark the note open to a move, not until a day from today on, or context only.") from e


app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")


# Explicit watch enrollment keeps broad discovery out of the notification budget.
@app.post("/api/watches")
def create_watch(body: dict):
    from .models import public_url

    c = candidate_for(body.get("candidate_id", ""))
    premise = str(body.get("fit_basis", "")).strip()
    if not 20 <= len(premise) <= 4000:
        raise HTTPException(
            422, "Explain why this person is worth monitoring (20–4000 characters)."
        )
    urls = body.get("urls") or [c["profile_url"]] + [
        e["url"] for e in c["evidence"][:3]
    ]
    if not isinstance(urls, list) or not 1 <= len(urls) <= 8:
        raise HTTPException(422, "Choose one to eight public source URLs.")
    try:
        urls = list(dict.fromkeys(public_url(u) for u in urls))
    except (ValueError, TypeError):
        raise HTTPException(422, "Use public http(s) source URLs.")
    key = "watch:" + c["id"]
    old = store.get(key) or {}
    watch = store.put(
        key,
        "watch",
        {
            **old,
            "candidate_id": c["id"],
            "name": c["name"],
            "role_id": c["role_id"],
            "mode": c["mode"],
            "status": "active",
            "fit_basis": premise,
            "organization": str(body.get("organization", ""))[:300],
            "urls": urls,
            "brief_version": role_for(c["role_id"])["brief_version"],
            "enrolled_at": old.get("enrolled_at") or iso(),
            "next_check_at": iso(),
            "coverage_status": "not_checked",
            "last_outcome": "Enrolled for observation. This is not outreach approval.",
        },
    )
    log(
        "watch_enrollment",
        {"candidate_id": c["id"], "mode": c["mode"], "fit_basis": premise},
    )
    evaluate(c["mode"])
    return watch


@app.patch("/api/watches/{key}")
def update_watch(key: str, body: dict):
    watch = store.get(key)
    if not watch or not key.startswith("watch:"):
        raise HTTPException(404, "Watch not found")
    if body.get("status") not in ("active", "paused"):
        raise HTTPException(422, "Choose active or paused")
    saved = store.put(
        key, "watch", {**watch, "status": body["status"]}, expected=watch["revision"]
    )
    from .sequence import refresh_alerts

    refresh_alerts(store, watch["mode"], role_for)
    return saved


def record_watch_check(job, result=None, error=None):
    watch = store.get("watch:" + str(job.get("candidate_id")))
    if not watch:
        return
    c = candidate_for(watch["candidate_id"])
    result = result or {}
    warnings = result.get("warnings", []) or result.get("usage", {}).get("warnings", [])
    outcome = (
        error
        or result.get("note")
        or "Observation completed; no new opportunity established."
    )
    next_at = enriched(c)["decision"]["next_check_at"]
    plan = role_for(c["role_id"])["signal_plan"]
    cadences = [
        signal.get("recheck_hours", 72)
        for signal in plan.get("signals", [])
        if isinstance(signal, dict)
    ]
    if cadences:
        next_at = min(next_at, iso(utcnow() + timedelta(hours=max(6, min(cadences)))))
    coverage = "error" if error else "partial" if warnings else "checked"
    if watch["brief_version"] != role_for(c["role_id"])["brief_version"]:
        coverage = "brief_changed"
        outcome = (
            "Hiring brief changed. Re-enroll after reviewing the watch premise. "
            + outcome
        )
    store.put(
        watch["id"],
        "watch",
        {
            **watch,
            "last_checked_at": iso(),
            "next_check_at": next_at,
            "coverage_status": coverage,
            "last_outcome": outcome,
        },
        expected=watch["revision"],
    )
    store.put(
        "watch-observation:" + uuid4().hex,
        "watch_observation",
        {
            "candidate_id": c["id"],
            "mode": c["mode"],
            "checked_at": iso(),
            "job_id": job.get("id"),
            "changed_sources": result.get("changed_sources", []),
            "summary": outcome,
            "coverage_status": coverage,
            "warnings": warnings,
            "candidate_revision": c["revision"],
        },
    )
    from .sequence import refresh_alerts

    refresh_alerts(store, c["mode"], role_for)


@app.post("/api/watches/{key}/check")
def check_watch(key: str):
    watch = store.get(key)
    if not watch or not key.startswith("watch:"):
        raise HTTPException(404, "Watch not found")
    if watch["status"] != "active":
        raise HTTPException(422, "Resume this watch before checking it.")
    if watch["mode"] == "simulation":
        raise HTTPException(
            422, "Use the timing demonstration for fictional source changes."
        )
    return enqueue("watch", "live", candidate_id=watch["candidate_id"])


@app.post("/api/sequence/run")
def run_sequence(body: dict):
    mode = body.get("mode", "live")
    if mode != "live":
        raise HTTPException(422, "Use the timing demonstration for simulation.")
    jobs = []
    for watch in store.all("watch"):
        if watch["mode"] == mode and watch["status"] == "active":
            jobs.append(check_watch(watch["id"]))
    return {
        "jobs": jobs,
        "note": "Only enrolled watches are checked; discovery is separate.",
    }


@app.get("/api/sequence")
def sequence_state(mode: str = "live"):
    if mode not in ("live", "simulation"):
        raise HTTPException(422, "Choose live or simulation")
    from .role_compiler import build_signal_plan

    alerts = [a for a in store.all("review_alert") if a["mode"] == mode]
    return {
        "watches": [w for w in store.all("watch") if w["mode"] == mode],
        "alerts": sorted(alerts, key=lambda a: a["created_at"], reverse=True),
        "observations": sorted(
            [o for o in store.all("watch_observation") if o["mode"] == mode],
            key=lambda o: o["checked_at"],
            reverse=True,
        )[:100],
        "plans": [
            {
                **r,
                "id": r["id"].removeprefix("role:"),
                "signal_plan": r.get("signal_plan") or build_signal_plan(r),
            }
            for r in store.all("role")
        ],
        "monitoring_enabled": settings()["monitoring_enabled"],
        "channel": "local_timing_workspace",
        "note": "Internal proposals need review; receiving a ping does not authorize outreach. Monitoring runs while this local server is alive.",
    }


@app.post("/api/roles/preview")
async def preview_role(body: dict):
    """A dropped JD as a role and what the engine would watch for it; nothing is written. The sample is free; any
    other JD makes two model calls (role_drop.PRICE)."""
    try:
        if body.get("sample"):
            return role_drop.sample()
        return await role_drop.preview(str(body.get("jd_text", "")), settings(), str(body.get("jd_url", "")))
    except (ValueError, providers.ProviderError) as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/roles/accept")
def accept_role(body: dict):
    """A previewed role into config/roles.json and config/role-specs, where every path reads it."""
    try:
        role = role_drop.accept(body.get("preview") or {}, str(body.get("jd_url", "")), body.get("locations") or [])
    except FileExistsError as e:
        raise Conflict(str(e)) from e
    except (ValueError, KeyError, TypeError, ValidationError) as e:
        raise HTTPException(422, str(e)) from e
    seed_role(dict(role))
    log("role_added", {"role_id": role["id"], "jd_sha256": role["jd_sha256"]})
    return {**role, "next": role_drop.ADDED}  # what the page says next: nobody watched yet, how, and when


@app.delete("/api/roles/{key}")
def remove_role(key: str):
    try:
        role = role_drop.remove(key)
    except KeyError as e:  # already out of config (scripts/roles.py remove): only this page's copy is left
        if (role := store.get("role:" + key) or {}).get("implementation_stage") != role_drop.DROPPED:
            raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    store.remove("role:" + key)
    log("role_removed", {"role_id": key})
    return role


@app.post("/api/sequence/demo")
def sequence_demo():
    """Replay known fictional observations through the real persistence and inbox path."""
    from copy import deepcopy
    from .sequence import refresh_alerts

    roles = {
        r["id"].removeprefix("role:"): role_for(r["id"].removeprefix("role:"))
        for r in store.all("role")
    }
    fixtures = scenarios(roles)[:2]
    backend = deepcopy(fixtures[0])
    backend.update(name="Sam River", role_id="backend", profile_url="https://example.org/fictional/sam-river")
    backend["evidence"][0]["url"] = backend["profile_url"]
    backend["fit"] = [
        {"criterion": c, "status": "supported", "evidence_ids": ["work"]}
        for c in roles["backend"]["criteria"]
    ]
    backend["events"][0].update(
        kind="engineering_milestone",
        title="Published a Java service migration postmortem",
        recipient_value="Compare the documented zero-downtime migration and database bottleneck analysis with production service ownership in the Backend role.",
        why_now="The fictional engineer has just published the migration postmortem and explicitly invited practitioners to discuss the rollback tradeoffs.",
        why_wait="Wait if the migration is still rolling out and Sam is on call for it.",
        falsifier="Sam's postmortem names another engineer as the migration lead, or Sam says the rollback work is done and closed.",
        confidence="medium",
    )
    backend["evidence"][0]["excerpt"] = (
        "Fictional source: Sam led a Java service migration, removed a relational database bottleneck, and now invites peer discussion of the rollback design."
        + " " + backend["events"][0]["timing_assessment"]["person_impact"]["quote"]
    )
    backend["draft"].update(
        subject="Your migration rollback design",
        body="Sam — the rollback boundary in your Java migration postmortem is the part I’d like to understand: how did you separate database changes from the service cutover? Our Senior / Lead Backend Engineer role involves scalable services and database performance. Would comparing those design tradeoffs be useful?",
        sender_function="CTO / engineering leader",
    )
    fixtures.append(backend)
    records = []
    for fixture in fixtures:  # the seeded Alex Rowan and Morgan Vale, reused, and Sam River: no second record of anyone
        rid = fixture["role_id"]
        quiet = {**fixture, "events": []}
        key = "candidate:" + digest([person_key(quiet), rid])
        old = store.get(key)
        if old:
            old = {**old, "dismissed": False, "snooze_until": None}
        saved = save_candidate(quiet, trusted=True, original=old)
        contact.forget(store, saved["person_id"])
        create_watch(
            {
                "candidate_id": saved["id"],
                "fit_basis": "Fictional evaluated role match for a labeled three-role timing replay; not a real hiring assessment.",
                "urls": [fixture["profile_url"]],
            }
        )
        records.append((fixture, saved))
    baseline_new = refresh_alerts(store, "simulation", role_for)
    steps = [
        {
            "stage": "baseline",
            "new_alerts": baseline_new,
            "outcome": "Standing role fit alone produces no timing alert.",
        }
    ]
    unchanged = refresh_alerts(store, "simulation", role_for)
    steps.append(
        {
            "stage": "unchanged",
            "new_alerts": unchanged,
            "outcome": "Reading unchanged evidence creates no alert.",
        }
    )
    for fixture, quiet in records:
        saved = save_candidate(fixture, trusted=True, original=store.get(quiet["id"]))
        record_watch_check(
            {"id": "demo:" + uuid4().hex, "candidate_id": saved["id"]},
            {
                "changed_sources": [fixture["profile_url"]],
                "note": "Fictional replay: a dated, person-attributed milestone and invitation appeared after the standing-fit baseline.",
            },
        )
    ids = {c["id"] for _, c in records}
    active = [
        a
        for a in store.all("review_alert")
        if a["candidate_id"] in ids and a["status"] == "proposed"
    ]
    steps.append(
        {
            "stage": "meaningful_change",
            "persisted": len(active),
            "outcome": "Three complete, unreviewed fictional pings persisted to the actual local inbox.",
        }
    )
    repeated = refresh_alerts(store, "simulation", role_for)
    steps.append(
        {
            "stage": "repeat",
            "new_alerts": repeated,
            "outcome": "The same occasions do not create duplicate alerts.",
        }
    )
    # Prove opt-out on one fixture, then restore the fictional inbox for inspection.
    test = records[0][1]["person_id"]
    contact.record(store, test, "never")
    refresh_alerts(store, "simulation", role_for)
    suppressed = all(
        a["status"] == "withdrawn"
        for a in store.all("review_alert")
        if a["candidate_id"] == records[0][1]["id"]
    )
    contact.forget(store, test)
    refresh_alerts(store, "simulation", role_for)
    steps.append(
        {
            "stage": "opt_out",
            "suppressed": suppressed,
            "outcome": "An opt-out withdraws the opportunity across senders; fictional state restored for inspection.",
        }
    )
    result = {
        "mode": "simulation",
        "persisted": len(active),
        "candidate_ids": list(ids),
        "steps": steps,
        "note": "Deterministic fictional replay through the actual decision/store/receipt channel. No provider calls, real signals or candidate messages.",
    }
    store.put(
        "sequence-demo:" + uuid4().hex, "demo_run", {**result, "created_at": iso()}
    )
    return result


@app.post("/api/sequence/receipts")
def acknowledge_sequence(body: dict):
    ids = body.get("alert_ids", [])
    if not isinstance(ids, list) or len(ids) > 100:
        raise HTTPException(422, "Provide up to100 displayed alert IDs")
    timestamp = iso()
    acknowledged = []
    for key in ids:
        alert = store.get(str(key))
        if (
            not alert
            or not str(key).startswith("alert:")
            or alert["status"] == "withdrawn"
        ):
            continue
        if not alert.get("seen_at"):
            store.put(
                key,
                "review_alert",
                {
                    **alert,
                    "seen_at": timestamp,
                    "receipt_basis": "browser_display_acknowledgment_not_human_review",
                },
                expected=alert["revision"],
            )
        acknowledged.append(key)
    return {"acknowledged": acknowledged, "displayed_at": timestamp}
