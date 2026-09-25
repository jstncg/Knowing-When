"""OpenReview (API v2): acceptance decisions with their dates and venue.

Decision notes live in a submission's forum; their `cdate` is when the decision
was posted. v1-era venues wrap content values differently, so `_value` reads
both shapes.
"""

import json
from datetime import datetime, timezone
from urllib.parse import urlencode

from .http import Fetcher
from .records import fetch_text, person_event

API = "https://api2.openreview.net/notes?"
REJECTED = ("reject", "desk reject", "withdraw")
PAGE = 1000


def _value(content: dict, key: str) -> str:
    raw = content.get(key, "")
    return str(raw.get("value", "") if isinstance(raw, dict) else raw or "")


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def notes(fetch, params: dict) -> tuple[list[dict], str]:
    text, version_hash = fetch_text(fetch, API + urlencode({**params, "limit": PAGE}))
    return json.loads(text).get("notes", []), version_hash


def decision(fetch, forum_id: str) -> tuple[dict | None, str]:
    """The accept/reject note for one forum, or None while undecided."""
    forum, version_hash = notes(fetch, {"forum": forum_id})
    for note in forum:
        text = _value(note.get("content", {}), "decision")
        if text and note.get("invitations", [note.get("invitation", "")])[0].endswith("/Decision"):
            return {"decision": text, "cdate": note["cdate"], "note_id": note["id"]}, version_hash
    return None, version_hash


def events(author_id: str, subject_id: str | None = None, fetch=None) -> list[dict]:
    """paper_accepted per accepted submission of an OpenReview profile (e.g. ~Jane_Doe1)."""
    fetch = fetch or Fetcher()
    subject = subject_id or f"openreview:{author_id}"
    submissions, _ = notes(fetch, {"content.authorids": author_id})
    out = []
    for note in submissions:
        content = note.get("content", {})
        verdict, version_hash = decision(fetch, note["forum"])
        if not verdict or verdict["decision"].lower().startswith(REJECTED):
            continue
        posted = _stamp(verdict["cdate"])
        venue = _value(content, "venue") or _value(content, "venueid")
        out.append(person_event(subject, "paper_accepted", posted[:10], "day", posted,
                                f"https://openreview.net/forum?id={note['forum']}", version_hash,
                                f"{_value(content, 'title')}: {verdict['decision']} ({venue})", "openreview_v2"))
    return out
