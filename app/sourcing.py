"""Behind the scenes on Today's calls: what the engine watches, what came in and when, and for one person the trail
from each public item to what the post reader read off it, what the engine makes of it, and the call.

Read-only: it reads the store and makes the call Today's calls does (today.source, journey.assess, happened.moments),
and fetches, calls and sends nothing. Sources are named by what they are (public posts, code, papers, news,
filings), never by the tools that pull them.
"""

from urllib.parse import urlparse

from . import detectors, happened, journey, readiness, routing, today
from .scorecard import ReadOnce
from .detectors import READ_TYPES, item
from .models import parse_time
from .readiness import CONTEXT_ONLY, REASON_OF, SUPPRESSORS, _is_open, label

KINDS = {"x": "Public posts on X", "linkedin": "Public posts on LinkedIn", "github": "Public code on GitHub",
         "papers": "Papers", "news": "News", "filings": "Filings", "profile": "Public profiles",
         "notes": "GI's own notes", "gi": "GI's own work", "web": "Other public pages"}
PAPER_TYPES = {"paper_v1", "paper_revised", "publication", "paper_accepted", "paper_talk", "coauthor_link",
               "affiliation_change", "affiliation_seen"}
PAPER_HOSTS = ("arxiv.org", "openreview.net", "openalex.org", "doi.org", "semanticscholar.org")
# What an item is, or what the post reader read off it, in a few words; anything else reads as readiness.label.
WHAT = {"work_in_progress": "work in progress", "gi_topic": "on GI's topics", "coauthor_link": "a paper with a coauthor",
        "affiliation_change": "their affiliation on papers",
        "job_started": "started a job", "job_ended": "left a job", "contact_constraint": "when to get in touch",
        "project_release": "a release", "launch_announced": "a launch", "publication": "a paper out",
        "profile_job_started": "a job start on their profile", "profile_job_ended": "a job end on their profile",
        "profile_change": "a change on their profile"}
# What they shipped: a release, a launch or a paper, by what an item is or was read as, or by its public moment.
SHIPPED = {"project_release": "a release", "launch_announced": "a launch", "publication": "a paper",
           "paper_v1": "a paper", "paper_revised": "a paper", "paper_accepted": "a paper"}
SHIPPED_MOMENTS = {"launch": "a launch", "paper": "a paper"}  # happened.Happened.kind (a release is a launch's)
# A launch-week hold that is over, named for what it was (while it holds after the launch: today.held).
HELD = {"imminent_launch": "the hold around their launch week"}
UNREAD = routing.UNREAD  # a post nothing was read off as a sign (detectors.unread): dated and linked, never quoted
COUNTED = {"trigger", "listed", "counted"}  # on the call as a reason, not a hold or a wait
BEHIND = {"counts", "fading", "hold", "waits"}  # what the engine makes of an item that puts it behind the call
WEAK = {"closed", "seen"}  # lines left out beside any other line on the same item
OWN = {"notes", "gi"}  # GI's own records: in the table, never counted as public items
RUNS = 7  # the daily runs listed, newest first
OTHERS = 12  # items in view past the ones behind the call, newest first


def released(event):
    """A GitHub release of theirs (github.py's own line names it), not a build's: shipped work as it stands, read or
    not."""
    return detectors.released(event) and bool(happened.shipped(event["quote"]))


def plain(event_type):
    """What an item is, or what was read off it, in a few words."""
    return WHAT.get(event_type) or label(event_type)


def kind(event):
    """Which kind of public source an event came from (KINDS)."""
    host = urlparse(event.get("source_url") or "").netloc.lower().removeprefix("www.")
    extractor, etype = event.get("extractor") or "", event["event_type"]
    if extractor == "manual_note":
        return "notes"
    if event["subject_id"] == "gi":
        return "gi"
    if extractor.startswith(("sec_", "warn_")) or host.endswith("sec.gov"):
        return "filings"
    if extractor.startswith("linkedin_profile"):
        return "profile"
    if extractor.startswith("google_news"):
        return "news"
    if host in ("x.com", "twitter.com"):
        return "x"
    if host.endswith("linkedin.com"):
        return "profile" if "/in/" in (event.get("source_url") or "") else "linkedin"
    if host.endswith("github.com"):
        return "github"
    if etype in PAPER_TYPES or host.endswith(PAPER_HOSTS):
        return "papers"
    return "news" if event["subject_type"] == "org" else "web"


def _items(events):
    """Events grouped by the public item they came from (detectors.item): the item itself (a post, a repo, a paper,
    a filing) first, then what was read off it. A record with no link (a door note) is an item of its own."""
    groups = {}
    for e in events:
        groups.setdefault(item(e) if e.get("source_url") else e["id"], []).append(e)
    return [sorted(g, key=lambda e: (e["event_type"] not in READ_TYPES, e["observed_at"])) for g in groups.values()]


def _stored(group):
    return min((e.get("updated_at") or "" for e in group), default="") or None


def _watched(ctx, by_kind):
    """The public places the engine looks for them, each with what the store holds from it."""
    anchors = ctx.anchors if ctx else {}
    places = [("x", anchors.get("x")), ("linkedin", anchors.get("linkedin")), ("github", anchors.get("github")),
              ("papers", anchors.get("openalex") and f"https://openalex.org/{anchors['openalex'].rsplit('/', 1)[-1]}")]
    rows = [{"kind": k, "label": KINDS[k], "url": url, **by_kind.pop(k, {"items": 0, "newest": None})}
            for k, url in places if url]
    if ctx and ctx.employer:  # the daily run reads the employer's news and layoff notices (journey.employer_sources)
        news = [by_kind.pop(k, {"items": 0, "newest": None}) for k in ("news", "filings")]
        rows.append({"kind": "employer", "label": f"News and filings about {ctx.employer}", "url": "",
                     "items": sum(n["items"] for n in news), "newest": max((n["newest"] or "" for n in news),
                                                                           default="") or None})
    return rows + [{"kind": k, "label": KINDS[k], "url": "", **v} for k, v in by_kind.items()]


def trail(mode, subject_id, others=OTHERS):
    """One person's sources and their trail, or None when they are not in the workspace: {name, call, watched,
    items (the ones behind the call, then the newest ``others``), behind (how many lead), more}. Each item says what it
    is, when it became public (and, live, when the pull stored it), what was read off it, what the engine made of it
    now (a reason that counts, a hold, context only, a window still to open, or seen but not counted), any public
    moment it is, and whether it is the call's trigger or one of its signals (on_call)."""
    found = today.source(mode)
    if not found:
        return None
    store, as_of = ReadOnce(found[0]), found[1]
    if not (ctx := journey.load_context(store, subject_id)):
        return None
    view = journey.view(store, subject_id, as_of)
    call = journey.assess(store, subject_id, as_of, ctx.role or None)
    now = parse_time(as_of).isoformat()
    # as readiness.score reads them: their employer's reports of one deal as one window while open, none once closed
    activations = readiness._employer_news(journey.detect(store, subject_id, as_of, view, ctx.role or None), now) \
        if call else []
    by_id = {e["id"]: e for e in view}
    signals = routing.signals(call, by_id) if call else []
    trigger = (today.trigger_of(call, signals) or {}).get("id") if call else None
    moments = {h.event_id: h for h in happened.moments(view, subject_id, as_of,
                                                        profiles={} if mode == "simulation" else today._profiles())}
    counted = {(i, r) for r, ids in (call.evidence.items() if call else ()) for i in ids}
    launched = today.launched(activations, now)  # launch-week holds whose launch is out: "their launch week"
    made = {}  # event id: {(state, what): day}, what the engine makes of it as of now
    for a in activations:
        reason = REASON_OF.get(a["detector_id"], a["detector_id"])
        opens, closes, live_now = a["window_open"], a["window_close"], _is_open(a, now)
        for i in a["evidence_event_ids"]:
            if a["detector_id"] in CONTEXT_ONLY:  # time in a seat: listed while the engine lists it, never a reason
                if not live_now:
                    continue
                said, day = ("context", label(a["detector_id"])), None
            elif a["family"] in SUPPRESSORS or a["holds"]:
                state = "hold" if live_now else "later" if opens and opens > now else "closed"
                what = ", ".join(HELD.get(h, label(h)) if state == "closed" else today.held(h, launched)
                                 for h in a["holds"] or [a["detector_id"]])
                said, day = (state, what), opens if state == "later" else closes
            elif opens and opens > now:
                said, day = ("waits" if call and call.until == opens else "later", label(reason)), opens
            elif (i, reason) in counted:  # a closed window still counts as it fades
                said, day = ("counts" if live_now else "fading", label(reason)), closes
            else:
                closed = closes and closes < now
                said, day = ("closed" if closed else "seen", label(reason)), closes if closed else None
            made.setdefault(i, {})[said] = day
    live = mode != "simulation"
    listed, evidence = {s["id"] for s in signals}, {i for i, _ in counted}
    items, unread, by_kind = [], set(), {}
    untold = detectors.unread(view)  # the posts and code nothing was read off, as every page and card counts them
    for group in _items(view):
        head, ids = group[0], {e["id"] for e in group}
        k = kind(head)
        seen = by_kind.setdefault(k, {"items": 0, "newest": None})
        seen["items"] += 1
        seen["newest"] = max(seen["newest"] or "", head["observed_at"])
        engine = {k: day for e in group for k, day in made.get(e["id"], {}).items()}
        if any(st not in WEAK for st, _ in engine):  # "seen" or "closed" says nothing beside a line that counts
            engine = {k: day for k, day in engine.items() if k[0] not in WEAK}
        states = {st for st, _ in engine}
        moment = next((moments[i] for i in ids if i in moments), None)
        # what was read off it: the post reader's tags, never another raw item of the same post; a release of theirs
        # is one as GitHub writes it
        read = [plain(e["event_type"]) for e in group[1:] if e["event_type"] not in READ_TYPES]
        read += ["a release"] if (release := released(head)) else []
        post = head["event_type"] in READ_TYPES
        if post and item(head) in untold:
            unread.add(len(items))  # its words are never shown: it is a count, or dated and linked if behind the call
        items.append({
            "kind": k, "label": KINDS[k], "public": head["observed_at"], "stored": _stored(group) if live else None,
            "url": head.get("source_url") or "",
            "quote": UNREAD if post and item(head) in untold else routing.own_quote(head)[:400],
            "whose": "theirs" if head["subject_id"] == subject_id else "gi" if head["subject_id"] == "gi"
            else "employer" if head["subject_type"] == "org" else "someone close to them",
            "is": "" if post else plain(head["event_type"]),
            "read": list(dict.fromkeys(read)),
            "engine": [{"state": st, "what": what, "day": readiness._day(day) if day else None}
                       for (st, what), day in engine.items()],
            "moment": happened.line(moment) if moment else "",
            "shipped": "a release" if moment and moment.release or release else
            next((SHIPPED[e["event_type"]] for e in group if e["event_type"] in SHIPPED), "")
            or SHIPPED_MOMENTS.get(moment.kind if moment else "", ""),
            "on_call": "trigger" if trigger in ids else "listed" if ids & listed else "counted" if ids & evidence
            else "holds" if "hold" in states else "waits" if "waits" in states else "",
        })
    for i in items:  # a public moment the engine counts nothing on says so, and names a newer one of theirs it counts
        if i["moment"] and not i["engine"] and not i["on_call"]:
            newer = max((j for j in items if j["on_call"] in COUNTED and j["whose"] == i["whose"] and j["shipped"]
                         and j["shipped"] == i["shipped"] and j["public"] > i["public"]), key=lambda j: j["public"],
                        default=None)
            i["engine"] = [{"state": "aside", "what": i["shipped"].removeprefix("a ") if newer else "",
                            "day": readiness._day(newer["public"]) if newer else None}]
    order = sorted(range(len(items)), key=lambda n: items[n]["public"], reverse=True)
    behind = [n for n in order if items[n]["on_call"] or items[n]["moment"]
              or any(x["state"] in BEHIND for x in items[n]["engine"])]
    rest = [n for n in order if n not in behind and n not in unread]
    hidden = [n for n in unread if n not in behind]
    return {"name": ctx.name,
            "call": {"headline": today.HEADLINE[call.action if call else None],
                     "reasons": [label(r) for r in call.reasons] if call else [],
                     "holds": [today.held(h, launched) for h in call.holds] if call else []},
            "watched": _watched(ctx, by_kind), "items": [items[n] for n in behind + rest[:others]],
            "behind": len(behind), "more": max(0, len(rest) - others),
            "hidden": len(hidden)}  # posts the post reader read nothing off, not behind the call: counted, never shown


def overview(mode):
    """What the whole workspace watches and what came in: people, public items (GI's own records aside), items per
    kind of source (live: how many the last run that pulled stored), and the daily run's last runs. None when live
    has no timelines yet."""
    found = today.source(mode)
    if not found:
        return None
    store, where = found[0], found[4]
    people = {c["subject_id"] for c in store.all("person_context")}
    events = store.all("timeline_event")
    live = mode != "simulation"
    runs = today._runs(today.DAILY_LOG) if live else []
    pulled = next((r for r in reversed(runs) if r.get("pulled") and r.get("at")), None)
    since = parse_time(pulled["at"]).isoformat() if pulled else None
    rows = {}
    for group in _items(events):
        head = group[0]
        row = rows.setdefault(kind(head), {"items": 0, "people": set(), "newest": "", "new": 0})
        row["items"] += 1
        row["newest"] = max(row["newest"], head["observed_at"])
        if head["subject_id"] in people:
            row["people"].add(head["subject_id"])
        if since and (_stored(group) or "") >= since:
            row["new"] += 1
    kinds = [{"kind": k, "label": KINDS[k], "items": r["items"], "people": len(r["people"]),
              "newest": r["newest"] or None, "new": r["new"] if since else None}
             for k in KINDS if (r := rows.get(k))]
    return {"where": where, "live": live, "people": len(people),
            "items": sum(k["items"] for k in kinds if k["kind"] not in OWN),
            "kinds": kinds,
            "runs": [{"at": r.get("at"), "status": r.get("status", "unknown"), "pulled": r.get("new_events"),
                      "read": r.get("read_calls"), "usd": r.get("usd"),
                      # route.py's line when the morning list went ("Nothing new to reach: nothing posted." when not)
                      "posted": isinstance(r.get("slack"), str) and r["slack"].startswith("Posted")}
                     for r in reversed(runs[-RUNS:])]}
