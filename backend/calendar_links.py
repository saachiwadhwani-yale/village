"""'Add to calendar' links for a booking: a Google Calendar link and a downloadable .ics file.

No sign-in or API keys: Google's link just opens a pre-filled event, and the .ics
file opens in Apple Calendar or Outlook. The event has only the basics (who, when,
where); the child's notes stay in the Village chat, since calendars often sync to
other devices or are shared with partners and assistants.

The .ics download is a signed link, so only the parent and the helper can fetch it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

import db
from config import TZ

PUBLIC_URL = os.environ.get("PUBLIC_URL") or f"http://localhost:{os.environ.get('PORT', '8000')}"


def _secret() -> bytes:
    """A random key, made once per database, for signing download links."""
    value = db.get_setting("link_secret")
    if not value:
        value = secrets.token_hex(16)
        db.set_setting("link_secret", value)
    return value.encode()


def sign(request_id: int, user_id: str) -> str:
    return hmac.new(_secret(), f"{request_id}:{user_id}".encode(), hashlib.sha256).hexdigest()[:24]


def _event(request_id: int, user_id: str) -> dict | None:
    """Title, times, place, and description for this person's view of a booking."""
    r = db.one("SELECT r.*, k.name AS kid, p.name AS parent, h.name AS helper FROM requests r "
               "JOIN kids k ON k.id = r.kid_id JOIN users p ON p.id = r.parent_id "
               "LEFT JOIN users h ON h.id = r.helper_id WHERE r.id = ?", request_id)
    if not r or r["status"] != "booked" or user_id not in (r["parent_id"], r["helper_id"]):
        return None
    if user_id == r["helper_id"]:
        title = f"Village: hanging out with {r['kid']} ({r['parent'].split()[0]}'s)"
        details = (f"You're hanging out with {r['kid']} while {r['parent'].split()[0]} is at their "
                   f"{r['purpose'] or 'commitment'}. {r['kid']}'s notes are in your Village messages.")
    else:
        title = f"Village: {r['helper'].split()[0]} has {r['kid']}"
        details = f"{r['helper']} is hanging out with {r['kid']} during your {r['purpose'] or 'commitment'}."
    start = datetime.fromisoformat(r["care_start"]).replace(tzinfo=TZ)
    end = datetime.fromisoformat(r["care_end"]).replace(tzinfo=TZ)
    return {"title": title, "start": start, "end": end, "location": r["location"] or "", "details": details}


def google_url(request_id: int, user_id: str) -> str | None:
    e = _event(request_id, user_id)
    if not e:
        return None
    fmt = "%Y%m%dT%H%M%S"
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": e["title"], "details": e["details"], "location": e["location"],
        "dates": f"{e['start'].strftime(fmt)}/{e['end'].strftime(fmt)}", "ctz": "America/New_York",
    })


def ics_url(request_id: int, user_id: str) -> str:
    return f"{PUBLIC_URL}/api/calendar/{request_id}.ics?u={user_id}&t={sign(request_id, user_id)}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_file(request_id: int, user_id: str, token: str) -> str | None:
    """The .ics file, or None if the link isn't valid (wrong person, bad signature, not booked)."""
    if not hmac.compare_digest(token, sign(request_id, user_id)):
        return None
    e = _event(request_id, user_id)
    if not e:
        return None
    utc = "%Y%m%dT%H%M%SZ"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Village//SOM//EN", "METHOD:PUBLISH", "BEGIN:VEVENT",
        f"UID:village-{request_id}-{user_id}@village",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime(utc)}",
        f"DTSTART:{e['start'].astimezone(timezone.utc).strftime(utc)}",
        f"DTEND:{e['end'].astimezone(timezone.utc).strftime(utc)}",
        f"SUMMARY:{_escape(e['title'])}", f"DESCRIPTION:{_escape(e['details'])}",
    ]
    if e["location"]:
        lines.append(f"LOCATION:{_escape(e['location'])}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"
