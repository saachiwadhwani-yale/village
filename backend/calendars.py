"""Read villagers' calendars and turn them into plain busy blocks.

Each villager can connect any number of calendars: a private iCal (.ics) link
from Google, Outlook, or Apple, or (for the demo) a file in
data/seed/calendars. All of them are merged into one list of busy blocks.

Privacy: event titles, descriptions, locations, and attendees are dropped the
moment a calendar is read. Only start/end times, the calendar's label, and
whether the event is tentative leave this module, so no agent ever sees what
someone is doing, only that they're busy.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import httpx
import icalendar
import recurring_ical_events

from config import SEED_DIR, TZ
from models import BusyBlock

MAX_ICS_BYTES = 5_000_000
FETCH_TIMEOUT_S = 8
CACHE_SECONDS = 120  # one matching run reads each link once, not once per check

_cache: dict[str, tuple[float, bytes]] = {}


class CalendarError(Exception):
    """A calendar link that can't be used (bad URL, unreachable, not iCal)."""


def normalize_url(url: str) -> str:
    """Accept webcal:// links (Apple/Outlook give these out) and require https."""
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise CalendarError("Calendar links must start with https:// or webcal://")
    return url


def _check_public_host(host: str) -> None:
    """Refuse links that point inside the server's own network (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(host, 443)
    except socket.gaierror as exc:
        raise CalendarError(f"Couldn't find {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise CalendarError("That link points to a private network address.")


def _fetch(url: str) -> bytes:
    hit = _cache.get(url)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    _check_public_host(urlparse(url).hostname or "")
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                if str(resp.url).split(":", 1)[0] != "https":
                    raise CalendarError("The link redirected away from https.")
                resp.raise_for_status()
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_ICS_BYTES:
                        raise CalendarError("That calendar is too large (over 5 MB).")
    except httpx.HTTPError as exc:
        raise CalendarError(f"Couldn't download the calendar ({type(exc).__name__}).") from exc
    if b"BEGIN:VCALENDAR" not in body[:2000]:
        raise CalendarError("That link didn't return an iCal (.ics) calendar.")
    _cache[url] = (time.monotonic(), body)
    return body


def _read_source(source: str, kind: str) -> bytes:
    if kind == "file":
        path = (SEED_DIR / "calendars" / source).resolve()
        if path.parent != (SEED_DIR / "calendars").resolve() or not path.exists():
            raise CalendarError(f"Demo calendar file {source} not found.")
        return path.read_bytes()
    return _fetch(normalize_url(source))


def _as_local(value) -> datetime | None:
    """Timed events become aware New Haven datetimes. All-day events return None."""
    if isinstance(value, datetime):
        return value.astimezone(TZ) if value.tzinfo else value.replace(tzinfo=TZ)
    return None  # a plain date = all-day event (birthdays, holidays); not treated as busy


def busy_blocks(source: str, kind: str, label: str,
                start: datetime, end: datetime) -> list[BusyBlock]:
    """Busy times from one calendar between start and end. Titles are discarded here."""
    cal = icalendar.Calendar.from_ical(_read_source(source, kind))
    blocks: list[BusyBlock] = []
    for event in recurring_ical_events.of(cal).between(start, end):
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if str(event.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT":
            continue  # the owner marked this event as "free"
        s = _as_local(event.get("DTSTART").dt)
        e_prop = event.get("DTEND")
        e = _as_local(e_prop.dt) if e_prop else (s + timedelta(hours=1) if s else None)
        if s is None or e is None:
            continue
        blocks.append(BusyBlock(
            start=s, end=e, source=label,
            tentative=str(event.get("STATUS", "")).upper() == "TENTATIVE",
        ))
    return blocks


def merged_busy(calendars: list[dict], start: datetime, end: datetime
                ) -> tuple[list[BusyBlock], list[str]]:
    """Busy blocks from all of a person's calendars, sorted, plus any read errors."""
    blocks: list[BusyBlock] = []
    errors: list[str] = []
    for cal in calendars:
        try:
            blocks += busy_blocks(cal["source"], cal["kind"], cal["label"], start, end)
        except CalendarError as exc:
            errors.append(f"{cal['label']}: {exc}")
        except Exception as exc:  # malformed .ics
            errors.append(f"{cal['label']}: couldn't read it ({type(exc).__name__}).")
    blocks.sort(key=lambda b: b.start)
    return blocks, errors


def validate_link(url: str) -> int:
    """Download a new link once to make sure it works. Returns how many events it has."""
    body = _fetch(normalize_url(url))
    cal = icalendar.Calendar.from_ical(body)
    return sum(1 for _ in cal.walk("VEVENT"))


def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    return start, start + timedelta(days=1)
