"""Rules screen: settle the obvious availability cases without a model call.

Returns a verdict when the answer is clear from the times alone (a firm
calendar conflict, or a fully clear calendar). Returns None for anything
judgment-dependent: a tight handoff, a tentative event, vague written
availability, a calendar that failed to load. Those go to a villager agent.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from models import AvailabilityVerdict, BusyBlock, ParsedAvailability

EDGE_MARGIN = timedelta(minutes=15)


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def screen(busy: list[BusyBlock], calendar_errors: list[str], has_calendars: bool,
           parsed: ParsedAvailability | None, care_start: datetime, care_end: datetime
           ) -> AvailabilityVerdict | None:
    weekday = care_start.strftime("%a")
    slots = [s for s in (parsed.slots if parsed else []) if s.day == weekday]

    # 1. Firm conflicts: definitely don't ask.
    for b in busy:
        if not b.tentative and b.start < care_end and b.end > care_start:
            return AvailabilityVerdict(verdict="unavailable", confidence="high",
                                       reason=f"Busy {_hm(b.start)}-{_hm(b.end)} on their {b.source} calendar.")
    for s in slots:
        if s.kind == "busy" and s.start < _hm(care_end) and s.end > _hm(care_start):
            return AvailabilityVerdict(verdict="unavailable", confidence="high",
                                       reason=f"Said they're not free {weekday} {s.start}-{s.end}.")

    # 2. Anything that needs judgment goes to the villager's agent.
    if calendar_errors:
        return None
    if any(b.tentative and b.start < care_end and b.end > care_start for b in busy):
        return None
    if any(care_start - EDGE_MARGIN < b.end <= care_start or care_end <= b.start < care_end + EDGE_MARGIN
           for b in busy):
        return None
    if parsed and parsed.caveats:
        return None
    free_slots = [s for s in slots if s.kind == "free"]
    inside_free = any(s.start <= _hm(care_start) and s.end >= _hm(care_end) for s in free_slots)
    if free_slots and not inside_free:
        return None

    # 3. Clear cases.
    if has_calendars:
        return AvailabilityVerdict(verdict="available", confidence="high",
                                   reason="No conflicts on any connected calendar.")
    if inside_free:
        return AvailabilityVerdict(verdict="available", confidence="medium",
                                   reason="Inside the times they said they're usually free.")
    return None  # no calendar and nothing written that covers this time
