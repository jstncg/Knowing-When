"""Which of GI's team a watchlist person follows on X: the one fact the channel needs, since an X message lands in
their inbox, not their message requests, only when they follow the sender (routing.channel).

The first step (social_pull.py follows --schema) read each candidate actor's pricing and input schema from Apify's API,
free. Only ACTOR published a price per item, so the paid pull uses it: one run per person, reading up to CAP of the
accounts they follow, and keeping only GI team handles, never the rest of anyone's list (the run's dataset on
Apify is deleted once read). Before any run the actor's schema and price are read again: every input field this
sends must still be there, and the price must still be PRICE per item.

What is kept, per person (FOLLOWS): the handle read, the team handles they follow, how many accounts were read, and
whether that was all of them. When it was not and no team handle turned up, or nothing was read, whether they follow
the team is unknown: ``follows`` is None, as if never read.
"""

import json
import math
import re
from pathlib import Path

# Candidates found on apify.com on 2026-09-24, with the price their store titles showed, unchecked until --schema.
CANDIDATES = {
    "kaitoeasyapi~premium-x-follower-scraper-following-data": "$0.10 per 1,000 (store title)",
    "fastcrawler~x-twitter-followers-following-extractor-ppr-0-08-1k": "$0.08 per 1,000 (store title)",
    "feedminer~x-follower-scraper": "$0.06 per 1,000 (store title)",
    "data-slayer~twitter-followings": "no price in the title",
}
DESCRIPTION_MAX = 160
ACTOR = "kaitoeasyapi~premium-x-follower-scraper-following-data"
PRICE = 0.00015  # per item, from the actor's own pricing data (follows --schema, 2026-09-24)
CAP = 1000  # accounts read per person; past it, "follows nobody on the team" is unknown
MAX_USD = 3.0  # the ceiling Justin approved for this pull (2026-09-24): --max-usd can only lower it
FOLLOWS = Path("research/private/social/x-follows.json")  # what the pull keeps; routing reads it
HANDLE_FIELDS = ("screen_name", "userName", "username", "user_name", "handle")


def _short(text):
    text = " ".join(str(text or "").split())
    return text if len(text) <= DESCRIPTION_MAX else text[:DESCRIPTION_MAX].rsplit(" ", 1)[0] + "…"


def _input(build):
    """The input schema from a build: its actor definition's, else the older inputSchema string; {} when neither."""
    schema = ((build.get("actorDefinition") or {}).get("input")) or build.get("inputSchema") or {}
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except ValueError:
            return {}
    return schema if isinstance(schema, dict) else {}


def _in_force(actor, now):
    """The pricing entries in force on ``now``: the latest started by then (Apify also lists past and scheduled
    ones), else the last listed."""
    infos = actor.get("pricingInfos") or []
    started = sorted((p for p in infos if (p.get("startedAt") or "") <= now), key=lambda p: p.get("startedAt") or "")
    return started[-1:] or infos[-1:]


def _pricing(actor, now):
    """The pricing in force on ``now``: the model and what one unit costs."""
    out = []
    for p in _in_force(actor, now):
        model = p.get("pricingModel", "?")
        if model == "FLAT_PRICE_PER_MONTH" and p.get("pricePerUnitUsd") is not None:
            out.append(f"{model}: ${p['pricePerUnitUsd']} per month")
        elif p.get("pricePerUnitUsd") is not None:
            out.append(f"{model}: ${p['pricePerUnitUsd']} per {p.get('unitName') or 'unit'}")
        elif events := ((p.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}):
            out.append(f"{model}: " + "; ".join(f"{name} ${e.get('eventPriceUsd')} ({_short(e.get('eventTitle'))})"
                                                for name, e in events.items()))
        else:
            out.append(model + (f": ${p['pricePerUnitUsd']}" if p.get("pricePerUnitUsd") else ""))
    return out


def summary(actor_id, actor, build, now="9999"):
    """What the pull needs to know about one actor: its pricing on ``now`` (an ISO time), its input fields and its
    output's fields."""
    schema = _input(build)
    fields = {name: {"type": spec.get("type"), "editor": spec.get("editor"), "title": _short(spec.get("title")),
                     "description": _short(spec.get("description")), "default": spec.get("default",
                                                                                       spec.get("prefill")),
                     "minimum": spec.get("minimum")}
              for name, spec in (schema.get("properties") or {}).items()}
    dataset = (((build.get("actorDefinition") or {}).get("storages") or {}).get("dataset")) or {}
    shown = sorted({f for view in (dataset.get("views") or {}).values()
                    for f in ((view.get("transformation") or {}).get("fields") or [])})
    return {"actor": actor_id, "title": actor.get("title"), "pricing": _pricing(actor, now),
            "required": schema.get("required") or [], "input": fields, "output_fields": shown}


def lines(s):
    """The summary as a person reads it."""
    out = [f"{s['actor']}: {s['title'] or '?'}", "  pricing: " + ("; ".join(s["pricing"]) or "none listed")]
    for name, f in s["input"].items():
        need = " (required)" if name in s["required"] else ""
        out.append(f"  input {name}{need}: {f['type']}" + (f", default {json.dumps(f['default'])}"
                                                           if f["default"] is not None else "")
                   + (f", at least {f['minimum']}" if f.get("minimum") is not None else "")
                   + (f" | {f['title']}" if f["title"] else "") + (f" | {f['description']}" if f["description"] else ""))
    if not s["input"]:
        out.append("  input: no schema found in the latest build")
    out.append("  output fields: " + (", ".join(s["output_fields"]) or "not described"))
    return out


# ------------------------------------------------------------------ the paid pull

# The actor requires maxFollowers even with getFollowers off, at least 200 (its schema, 2026-09-24). Each run is
# budgeted for this many follower rows on top of CAP follows, which are dropped, never kept; a higher floor refuses.
FOLLOWERS_MAX = 200


def fewest_followers(s=None):
    """The maxFollowers to send: the least the actor's schema allows, and never 0, which some actors read as no limit."""
    least = ((s or {}).get("input", {}).get("maxFollowers") or {}).get("minimum")
    return max(1, int(least)) if isinstance(least, (int, float)) else 1


def follows_input(handle, cap=CAP, followers=1):
    """One person's run: the accounts they follow, never their followers (maxFollowers is only there because the actor
    requires it, at its least)."""
    return {"user_names": [handle.lstrip("@")], "maxFollowings": cap, "getFollowing": True, "getFollowers": False,
            "maxFollowers": followers}


# An input named with one of these words means the actor reads X as someone logged in: never used, even when the
# field is optional. Whole word parts, so "Author details" passes and "ct0" (X's login cookie) or "apiKey" do not.
LOGIN = {"cookie", "cookies", "login", "logins", "session", "sessions", "sessionid", "password", "passwd", "auth",
         "authorization", "token", "tokens", "credential", "credentials", "csrf", "bearer", "ct0", "apikey", "secret"}


def _split(m):
    """"authToken" -> "auth Token", "XSRFToken" -> "XSRF Token"."""
    return f"{m[1]} {m[2]}" if m[1] else f"{m[3]} {m[4]}"


def _logs_in(*texts):
    """Whether any word part of the texts (camelCase and snake_case split, and each two parts run together) is
    a login word."""
    for text in texts:
        parts = [w.lower() for w in re.split(r"[^A-Za-z0-9]+", re.sub(r"([a-z0-9])([A-Z])|([A-Z]+)([A-Z][a-z])", _split, text or "")) if w]
        if LOGIN & {*parts, *(a + b for a, b in zip(parts, parts[1:]))}:
            return True
    return False


def schema_problems(s):
    """What stops a run: an input field this sends that the actor no longer has, a required one it doesn't send, or
    any input for a login (LOGIN), which the pull never gives and an actor that asks for one never gets run."""
    sent = set(follows_input("x"))
    return [f"the actor has no input {name}" for name in sorted(sent - set(s["input"]))] + \
        [f"the actor needs {name}, which is not sent" for name in s["required"] if name not in sent] + \
        [f"the actor takes a login input ({name}): no cookies or logins" for name, f in s["input"].items()
         if _logs_in(name, f.get("title"), f.get("editor"))] + \
        ([f"the actor reads at least {fewest_followers(s)} followers"] if fewest_followers(s) > FOLLOWERS_MAX else [])


ITEM_EVENT = "apify-default-dataset-item"  # Apify's per-event name for one dataset item


def _per_item(p):
    """What one item costs under a pricing entry, when that is all it charges for; else None. Per dataset item, or
    per event when the only priced event is the dataset item's."""
    if p.get("pricingModel") == "PRICE_PER_DATASET_ITEM":
        return p.get("pricePerUnitUsd")
    if p.get("pricingModel") == "PAY_PER_EVENT":
        events = (p.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}
        priced = {name: e.get("eventPriceUsd") for name, e in events.items() if e.get("eventPriceUsd") != 0}
        return priced[ITEM_EVENT] if list(priced) == [ITEM_EVENT] else None
    return None


def price_problems(actor, now):
    """What stops a run on price: anything but PRICE per item in force on ``now``, billed per dataset item or as the
    dataset-item event with nothing else charged. The worst case printed, and the cap Justin approved, assume it."""
    found = _in_force(actor, now)
    price = _per_item(found[0]) if len(found) == 1 else None
    if isinstance(price, (int, float)) and 0 < price <= PRICE:
        return []
    return [f"the price is now {'; '.join(_pricing(actor, now)) or 'not listed'}, not ${PRICE} per item"]


def run_usd(followers=FOLLOWERS_MAX, cap=CAP, price=PRICE):
    """One run's dollar cap: its items in full (CAP follows plus the follower rows it may add) and a cent over, up to
    the cent, so the cap never stops a run short and makes a partial read look complete."""
    return round(math.ceil(round((cap + followers) * price * 100, 6)) / 100 + 0.01, 2)


def worst_usd(people, followers=FOLLOWERS_MAX, cap=CAP, price=PRICE):
    """Every run at its dollar cap: what the pull costs at most."""
    return round(people * run_usd(followers, cap, price), 2)


def team_handles(team):
    return {h.strip().lstrip("@").lower() for m in team if (h := m.get("x_handle") or "") and h.strip().lstrip("@")}


def _handle(item):
    for holder in (item, item.get("user") or {}, item.get("legacy") or {}):
        for field in HANDLE_FIELDS:
            if isinstance(holder.get(field), str) and holder[field].strip():
                return holder[field].strip().lstrip("@").lower()
    return ""


def _marker(item):
    return str(item.get("type") or item.get("relation") or "").lower()


def _direction(item):
    """Which list a row says it is from: "following" (or friends, X's old word for it), "follower", or "" when its
    marker names no list ("user") or it has none."""
    m = _marker(item)
    return "following" if "following" in m or "friend" in m else "follower" if "follow" in m else ""


def marked(items):
    """Whether the run's rows say which list they are from. The actor requires a followers count even with followers
    off; if it sends follower rows anyway and they don't say so, a teammate who follows them would read as one they
    follow, so unmarked rows count only when a person has checked they are follows (``trust_unmarked``)."""
    return any(_direction(i) for i in items if isinstance(i, dict))


def result(handle, items, team, cap, read_on, limit=None, trust_unmarked=False):
    """One person's record: the team handles among the accounts they follow, and nothing else of the list (follower
    rows are dropped). Only rows marked as follows count, and unmarked ones when trusted (the actor may mark only
    its follower rows): an empty read, only error rows (a protected, suspended or renamed account), or rows none of
    which counts say nothing, so follows is None, as it is when the read may have stopped short (cap follows, near
    enough, or ``limit`` rows in all, the run's item cap) without a team handle."""
    rows = [i for i in items if isinstance(i, dict)]
    counted = ({"following"} if marked(rows) else set()) | ({""} if trust_unmarked else set())
    followed = [h for i in rows if _direction(i) in counted and (h := _handle(i))]
    found = sorted({h for h in followed if h in team})
    complete = len(followed) < cap - 1 and (limit is None or len(items) < limit)
    return {"x_handle": handle.lstrip("@"), "follows": found if found or (complete and followed) else None,
            "read": len(followed), "complete": complete, "marked": marked(rows), "read_on": read_on}


def named(items):
    """How many rows name an account."""
    return sum(1 for i in items if isinstance(i, dict) and _handle(i))


def shape(items):
    """What a run's rows were, counted, for the log: rows marked as follows, follower rows dropped, rows that don't
    say which list they are from, and others (no account, an error). Counts and field names only, never a handle."""
    rows = [i for i in items if isinstance(i, dict) and _handle(i)]
    by = {d: sum(1 for i in rows if _direction(i) == d) for d in ("following", "follower", "")}
    fields = sorted({k for i in items[:5] if isinstance(i, dict) for k in i})
    return (f"{by['following']:,} marked as follows, {by['follower']:,} follower rows dropped, {by['']:,} not saying "
            f"which list, {len(items) - len(rows):,} other rows; fields: {', '.join(fields) or 'none'}")


def load(path=FOLLOWS):
    """{person id: record} from the saved pull, or {} when there is none or it can't be read."""
    try:
        people = json.loads(Path(path).read_text()).get("people", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return people if isinstance(people, dict) else {}


def of(records, subject_id):
    """One person's record: theirs for this role, else the one read for them under another role, else None."""
    if subject_id in records:
        return records[subject_id]
    key = subject_id.partition(":")[2] or subject_id
    return next((r for pid, r in sorted(records.items()) if (pid.partition(":")[2] or pid) == key), None)
