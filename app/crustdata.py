"""Crustdata observations feed the existing researcher; they never authorize outreach.

Versioned API contract: docs.crustdata.com, checked 2026-09-21. Pull run history
instead of requiring a public webhook receiver for the local pilot. Provider
watch creation is reconciled before retrying an uncertain request.
"""

import asyncio
import json
import time
from datetime import timedelta
from urllib.parse import urlsplit

import httpx

from . import contact
from .engine import digest
from .models import iso, parse_time, utcnow

BASE = "https://api.crustdata.com"
VERSION = "2025-11-01"
_last_watch_call = {}
TRACK = {"op": "or", "conditions": [
    {"field": "experience.employment_details.current", "type": "added"},
    {"field": "basic_profile.current_title", "type": "changed"},
    {"field": "basic_profile.headline", "type": "changed"},
]}


class CrustdataError(Exception):
    pass


class Client:
    def __init__(self, key):
        if not key:
            raise CrustdataError("Add a Crustdata API key in Setup.")
        self.key = key

    async def request(self, method, path, body=None, params=None):
        if path.startswith("/watch/"):
            bucket = path.split("/")[2]
            await asyncio.sleep(max(0, 6.1 - (time.monotonic() - _last_watch_call.get(bucket, 0))))
            _last_watch_call[bucket] = time.monotonic()
        # No redirects: never forward the credential to a payload/source URL.
        async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
            try:
                r = await client.request(method, BASE + path, json=body, params=params,
                    headers={"Authorization": "Bearer " + self.key,
                             "x-api-version": VERSION})
            except httpx.HTTPError:
                raise CrustdataError("Crustdata connection failed; retry reads, reconcile creations.") from None
        if not r.is_success:
            # Provider bodies can echo credentials or arbitrary input. Do not persist them.
            reasons = {401: "credential rejected", 403: "dataset or field access not enabled",
                       402: "insufficient credits", 429: "rate limited", 400: "request rejected; check current API contract"}
            raise CrustdataError(f"Crustdata {r.status_code}: {reasons.get(r.status_code, 'provider request failed')} ({path}).")
        if r.status_code == 204:
            return {}
        try:
            return r.json()
        except ValueError:
            raise CrustdataError("Crustdata returned non-JSON data.") from None


def linkedin_url(value):
    try:
        u = urlsplit(value)
        if u.scheme in ("https", "http") and u.hostname in ("linkedin.com", "www.linkedin.com") and u.path.startswith("/in/"):
            slug = u.path.strip("/").split("/")
            if len(slug) == 2 and slug[1]:
                return "https://www.linkedin.com/in/" + slug[1].lower()
    except (TypeError, ValueError):
        pass
    return None


def subjects(store):
    """Resolve URLs already present in dossiers, never guess a handle from a name."""
    found, gaps = {}, []
    for watch in store.all("watch"):
        if watch.get("mode") != "live" or watch.get("status") != "active":
            continue
        c = store.get(watch["candidate_id"])
        if not c:
            continue
        if c.get("dismissed") or contact.opted_out(contact.history(store, c.get("person_id", ""))):
            continue
        # Prefer the person's primary/enrolled URL. Evidence may link to a
        # colleague, introducer or namesake and must not broaden identity.
        primary = linkedin_url(c["profile_url"])
        enrolled = {u for v in watch.get("urls", []) if (u := linkedin_url(v))}
        matches = {primary} if primary else enrolled
        if len(matches) != 1 or c.get("identity_status") == "conflict":
            gaps.append({"candidate_id": c["id"], "name": c["name"], "reason": "Need one attributable LinkedIn profile URL; no name-only matching."})
            continue
        url = next(iter(matches))
        found.setdefault(url, []).append(c["id"])
    return found, gaps


async def account(store, client):
    key = "crustdata:account"
    try:
        balance = await client.request("GET", "/account/credits")
        permissions = await client.request("GET", "/account/endpoints")
        data = {"credits": balance["account"]["credits"], "checked_at": iso(), "error": None,
                "permissions": [{"path": e["path"], "status": e["status"]}
                                for e in permissions.get("endpoints", [])]}
    except (CrustdataError, KeyError, TypeError) as e:
        data = {**(store.get(key) or {}), "error": str(e), "attempted_at": iso()}
        store.put(key, "crustdata_account", data)
        raise
    return store.put(key, "crustdata_account", data)


def definition(dataset, url):
    config = {"trigger": {"type": "interval", "every_hours": 1},
              "max_results_per_run": 10, "payload_delivery_type": "inline"}
    if dataset == "person":
        return {"entities": {"professional_network_profile_urls": [url]}, "track": TRACK,
                "fields": ["basic_profile", "social_handles", "experience"],
                "config": {**config, "refresh_frequency_days": 1}, "notifications": []}
    return {"filters": {"op": "and", "conditions": [
                {"field": "actor.actor_type", "type": "=", "value": "person"},
                {"field": "actor.professional_network_url", "type": "=", "value": url}]},
            "config": config, "notifications": [], "overflow_policy": "redeliver"}


def watch_path(dataset, remote_id=None):
    path = f"/watch/{dataset}" + ("/search" if dataset == "social_post" else "")
    return path + (f"/{remote_id}" if remote_id is not None else "")


async def connect(store, client):
    await account(store, client)
    urls, gaps = subjects(store)
    report = {"created": 0, "reused": 0, "errors": [], "gaps": gaps}
    for dataset in ("person", "social_post"):
        remote = await client.request("GET", watch_path(dataset))
        for url, ids in urls.items():
            spec = definition(dataset, url)
            key = "crustdata-sub:" + digest([dataset, url])
            old = store.get(key) or {}
            match = next((r for r in remote if r.get("status") not in ("cancelled", "expired")
                          and all(r.get(k) == spec[k] for k in ("entities", "track") if k in spec)
                          and (dataset == "person" or r.get("filters") == spec["filters"])), None)
            try:
                if old.get("remote_id"):
                    match = next((r for r in remote if r["id"] == old["remote_id"]), None)
                    if not match:
                        raise CrustdataError("Saved remote watch missing; investigate before creating a replacement.")
                if match:
                    report["reused"] += 1
                else:
                    if old.get("creation_started_at"):
                        raise CrustdataError("Creation outcome uncertain; no duplicate created. Check Crustdata before retrying.")
                    old = store.put(key, "crustdata_subscription", {**old, "dataset": dataset,
                        "profile_url": url, "candidate_ids": ids, "creation_started_at": iso(), "status": "creating"})
                    match = await client.request("POST", watch_path(dataset), spec)
                    report["created"] += 1
                    remote.append(match)
                store.put(key, "crustdata_subscription", {**old, "dataset": dataset,
                    "profile_url": url, "candidate_ids": ids, "remote_id": match["id"],
                    "status": match["status"], "config": match["config"], "error": None,
                    "creation_started_at": None, "created_at": match.get("created_at"),
                    "next_poll_at": None})  # a (re)connected watch polls on the next sync
            except CrustdataError as e:
                # A clear HTTP rejection cannot have created a watch; transport errors can.
                clear = any(f"Crustdata {code}:" in str(e) for code in (400, 401, 402, 403, 429))
                store.put(key, "crustdata_subscription", {**old, "dataset": dataset,
                    "profile_url": url, "candidate_ids": ids, "status": "error", "error": str(e),
                    "creation_started_at": None if clear else old.get("creation_started_at")})
                report["errors"].append(str(e))
    return report


def records_from_run(run):
    if run.get("status") != "SUCCESS":
        return []
    if run.get("payload_delivery", {}).get("type") == "link":
        raise CrustdataError("Linked run payload requires investigation; only inline configured. No records silently skipped.")
    rows = []
    for delivery in run.get("notifications", []):
        payload = delivery.get("payload", {})
        rows.extend(payload.get("notifications", payload.get("results", [])))
    if run.get("new_records_count", 0) and not rows:
        raise CrustdataError("Run reports records but has no readable inline payload; coverage incomplete.")
    return rows


def ingest_run(store, sub, run):
    """Persist observations idempotently; first discovery sample is never a trigger."""
    rows = records_from_run(run)
    if run.get("status") != "SUCCESS":
        raise CrustdataError("Crustdata run not successful: " + str(run.get("failure_reason") or run.get("status")))
    # Free discovery samples are baselines. Explicitly do not conflate first local
    # receipt with first remote run (we may have been offline for days).
    baseline = sub["dataset"] == "social_post" and bool(rows) and run.get("credits_deducted") == 0
    current_subjects, _ = subjects(store)
    candidate_ids = current_subjects.get(sub["profile_url"], [])
    added = []
    for row in rows:
        record = row.get("record", row)
        if sub["dataset"] == "person":
            # This watch contains exactly one supplied URL. Preserve provider ID;
            # research must still verify attribution, never match by name.
            source = sub["profile_url"]
            event_date = None
            changes = row.get("changes", [])
            stable = ["person", source, run["id"], changes]
            summary = json.dumps(changes, ensure_ascii=False)
        else:
            actor = record.get("actor", {})
            author_url = linkedin_url(actor.get("professional_network_url"))
            if author_url != sub["profile_url"]:
                candidate_ids_for_row = []
            else:
                candidate_ids_for_row = candidate_ids
            source = record.get("share_url") or record.get("url")
            event_date = record.get("date_posted")
            changes = []
            stable = ["social_post", source or record.get("urn") or row.get("uid"), record.get("text", "")]
            summary = record.get("text") or "Post has no text. Inspect the source; no timing inference established."
        ids = candidate_ids if sub["dataset"] == "person" else candidate_ids_for_row
        key = "crustdata-observation:" + digest(stable)
        if store.get(key):
            continue
        observation = {"provider": "crustdata", "subscription_id": sub["id"],
            "remote_watch_id": sub["remote_id"], "remote_run_id": run["id"],
            "candidate_ids": ids, "kind": sub["dataset"], "source_url": source,
            "summary": summary[:16000], "record": record, "changes": changes,
            "event_date": event_date, "event_date_basis": "post_publication_only" if event_date else "unknown",
            "detected_at": iso(), "provider_run_at": run.get("completed_at"),
            "baseline": baseline, "status": "baseline" if baseline else "needs_identity" if not ids else "pending_research",
            "timing_claim": "Observation only. Neither a profile change nor a post proves a reason to contact now."}
        added.append(store.put(key, "crustdata_observation", observation, only_new=True))
    for observation in added:
        to_timeline(store, observation)
    return added


def to_timeline(store, observation):
    """Map one attributed observation to tier-1 timeline events, one per person."""
    from .timeline import add_event, source_version

    if not observation.get("source_url") or not observation.get("candidate_ids"):
        return []
    post = observation["kind"] == "social_post"
    text = observation["summary"] if post else json.dumps(observation["changes"], sort_keys=True)
    version = source_version(store, observation["source_url"], text, observation["detected_at"])
    day, precision = (observation.get("event_date") or "")[:10] or None, None
    if day:
        precision = "day"
        # Crustdata derives some post times from relative labels ("2y"), leaving
        # identical clock times across one run. Keep only the precision we know.
        clock = observation["event_date"][10:]
        if any(o["id"] != observation["id"] and o.get("remote_run_id") == observation.get("remote_run_id")
               and (o.get("event_date") or "")[10:] == clock for o in store.all("crustdata_observation")):
            recent = utcnow() - parse_time(observation["event_date"]) < timedelta(days=365)
            day, precision = (day[:7], "month") if recent else (day[:4], "year")
    events = []
    for candidate_id in observation["candidate_ids"]:
        if (candidate := store.get(candidate_id)) and candidate.get("person_id"):
            events.append(add_event(store, {
                "subject_type": "person", "subject_id": candidate["person_id"],
                "event_type": "linkedin_post" if post else "profile_change",
                "event_date": day, "date_precision": precision, "observed_at": observation["detected_at"],
                "source_url": observation["source_url"], "source_version_hash": version["content_hash"],
                "quote": text[:4000], "tier": 1, "extractor": "crustdata_v1",
            }))
    return events


def backfill_timeline(store):
    return sum(len(to_timeline(store, o)) for o in store.all("crustdata_observation"))


async def sync(store, client):
    """Poll each watch whose next_poll_at has passed; no call at all when none is due.

    A minute of grace: next_poll_at is stamped after the watch's own calls, a
    little after the 15-minute tick, and would otherwise wait a whole extra tick.
    """
    cutoff = utcnow() + timedelta(minutes=1)
    due = [s for s in store.all("crustdata_subscription")
           if s.get("remote_id") and (not s.get("next_poll_at") or parse_time(s["next_poll_at"]) <= cutoff)]
    if not due:
        return {"observations_added": 0, "errors": []}
    await account(store, client)
    new, errors = [], []
    live_subjects, _ = subjects(store)
    for sub in due:
        pending = list(sub.get("backfill_cursors") or [])
        if not pending and sub.get("backfill_cursor") is not None:
            pending.append(sub["backfill_cursor"])
        cursor = None
        try:
            remote = await client.request("GET", watch_path(sub["dataset"], sub["remote_id"]))
            if sub["profile_url"] not in live_subjects:
                if remote.get("status") == "active":
                    await client.request("PATCH", watch_path(sub["dataset"], sub["remote_id"]), {"status": "paused"})
                store.put(sub["id"], "crustdata_subscription", {**sub, "status": "paused",
                    "error": "Paused: no active attributable local subject.", "next_poll_at": iso(utcnow() + timedelta(hours=24))})
                continue
            # Latest page always wins. Page two advances the newest gap; page
            # three advances the oldest saved gap so neither workstream starves.
            for page_index in range(3):
                historical_turn = page_index == 2 and len(pending) > 1
                if page_index:
                    if not pending:
                        break
                    cursor = pending.pop(-1 if historical_turn else 0)
                page = await client.request("GET", f"/watch/{sub['dataset']}/{sub['remote_id']}/runs",
                                            params={"limit": 10, **({"cursor": cursor} if cursor is not None else {})})
                runs = page.get("runs", [])
                was_seen = bool(runs) and all(
                    store.get(f"crustdata-run:{sub['dataset']}:{sub['remote_id']}:{r['id']}")
                    for r in runs
                )
                for run in reversed(runs):
                    key = f"crustdata-run:{sub['dataset']}:{sub['remote_id']}:{run['id']}"
                    if store.get(key):
                        continue
                    if run.get("status") == "RUNNING":
                        continue
                    if run.get("status") != "SUCCESS":
                        reason = "Remote run failed or skipped: " + str(run.get("failure_reason") or run.get("status"))
                        errors.append(reason)
                        store.put(key, "crustdata_run", {"subscription_id": sub["id"], "summary": run, "coverage_error": reason}, only_new=True)
                        continue
                    detail = await client.request("GET", f"/watch/{sub['dataset']}/{sub['remote_id']}/runs/{run['id']}/summary")
                    new.extend(ingest_run(store, sub, detail))
                    store.put(key, "crustdata_run", {"subscription_id": sub["id"], "summary": detail}, only_new=True)
                next_cursor = page.get("next_cursor")
                # A saved frontier already represents earlier incomplete pages.
                # Reaching a fully processed page closes this scan, without
                # discarding independent frontiers from previous outages.
                if not was_seen and next_cursor is not None and next_cursor not in pending and next_cursor != cursor:
                    if historical_turn:
                        pending.append(next_cursor)
                    else:
                        pending.insert(0, next_cursor)
                cursor = None
            store.put(sub["id"], "crustdata_subscription", {**sub, "status": remote["status"],
                "config": remote["config"], "credits_consumed": remote.get("credits_consumed"),
                "last_polled_at": iso(), "backfill_cursors": pending,
                "backfill_cursor": pending[0] if pending else None, "error": None,
                "next_poll_at": iso(utcnow() + timedelta(minutes=15))})
        except (CrustdataError, KeyError, TypeError) as e:
            errors.append(str(e))
            if cursor is not None and cursor not in pending:
                pending.insert(0, cursor)
            store.put(sub["id"], "crustdata_subscription", {**sub, "error": str(e),
                "backfill_cursors": pending, "backfill_cursor": pending[0] if pending else None,
                "last_attempt_at": iso(), "next_poll_at": iso(utcnow() + timedelta(minutes=30))})
    return {"observations_added": len(new), "errors": errors}


def status(store, configured):
    _, gaps = subjects(store)
    return {"configured": configured, "account": store.get("crustdata:account") or {},
            "subscriptions": store.all("crustdata_subscription"),
            "observations": sorted(store.all("crustdata_observation"), key=lambda x: x["detected_at"], reverse=True)[:50],
            "coverage_errors": [r for r in store.all("crustdata_run") if r.get("coverage_error")][-20:],
            "gaps": gaps, "note": "Provider watches run remotely. The local app imports their history every 15 minutes while running; a watch that failed waits 30 minutes, and a paused one 24 hours or until Connect. Observations require timing research; no candidate contact is sent."}
