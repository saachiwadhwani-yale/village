"""Workflow rules that don't need the model: screening, waves, booking, back-outs,
pausing, leaving, the once-a-day rule, timers, and the token budget."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

import channels
import coordinator
import db
import runner
from calendars import CalendarError, merged_busy, normalize_url, validate_link
from config import TZ
from conftest import asks, last, messages, request
from models import ParsedAvailability, WeeklySlot
from screening import screen


def make_request(parent="priya", day="2026-10-13", start="12:50", end="13:40", minutes=30,
                 verdicts: dict[str, str] | None = None) -> int:
    """A confirmed request with availability already decided, so waves can run without the model."""
    meeting = (datetime.fromisoformat(f"{day}T{start}") + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
    kid = db.one("SELECT id FROM kids WHERE parent_id = ?", parent)["id"]
    rid = db.run("INSERT INTO requests (parent_id, kid_id, meeting_start, meeting_minutes, care_start, care_end, "
                 "purpose, status, created_at) VALUES (?,?,?,?,?,?, 'coffee chat', 'searching', ?)",
                 parent, kid, meeting, minutes, f"{day}T{start}", f"{day}T{end}", db.utc_now_iso())
    for vid, verdict in (verdicts or {}).items():
        db.run("INSERT INTO asks (request_id, villager_id, verdict, confidence, verdict_reason, decided_by, status) "
               "VALUES (?,?,?, 'high', 'test', 'rules', 'not_asked')", rid, vid, verdict)
    return rid


# --- calendars and screening -------------------------------------------------------------


def test_multiple_calendars_are_merged_and_titles_dropped():
    cals = [dict(c) for c in db.all_("SELECT * FROM calendars WHERE user_id = 'sam'")]
    assert len(cals) == 2
    busy, errors = merged_busy(cals, datetime(2026, 10, 13, tzinfo=TZ), datetime(2026, 10, 14, tzinfo=TZ))
    assert not errors
    sources = {b.source for b in busy}
    assert sources == {"Yale Outlook", "Personal Google"}  # the conflict only exists on the second calendar
    assert all(set(b.model_dump()) == {"start", "end", "source", "tentative"} for b in busy)  # no titles


def test_screen_settles_clear_cases_and_defers_unclear_ones():
    def busy_for(uid):
        cals = [dict(c) for c in db.all_("SELECT * FROM calendars WHERE user_id = ?", uid)]
        busy, errors = merged_busy(cals, datetime(2026, 10, 13, tzinfo=TZ), datetime(2026, 10, 14, tzinfo=TZ))
        u = db.one("SELECT availability_parsed FROM users WHERE id = ?", uid)
        parsed = ParsedAvailability.model_validate_json(u["availability_parsed"]) if u["availability_parsed"] else None
        return screen(busy, errors, bool(cals), parsed, datetime(2026, 10, 13, 12, 50, tzinfo=TZ),
                      datetime(2026, 10, 13, 13, 40, tzinfo=TZ))

    assert busy_for("sam").verdict == "unavailable"       # firm conflict on his personal calendar
    assert busy_for("jordan").verdict == "unavailable"    # class starts 13:30
    assert busy_for("hannah").verdict == "available"      # clear calendar
    assert busy_for("diego") is None                      # class ends right at 12:50: tight, agent decides
    assert busy_for("mei") is None                        # tentative event overlaps: agent decides
    assert busy_for("aisha") is None                      # written "after 1" vs a 12:50 start: agent decides


def test_written_busy_slot_rules_someone_out():
    parsed = ParsedAvailability(slots=[WeeklySlot(day="Tue", start="12:00", end="14:00", kind="busy")],
                                caveats=[], summary="")
    v = screen([], [], False, parsed, datetime(2026, 10, 13, 12, 50, tzinfo=TZ), datetime(2026, 10, 13, 13, 40, tzinfo=TZ))
    assert v.verdict == "unavailable"


def test_calendar_links_are_guarded():
    with pytest.raises(CalendarError):
        normalize_url("http://example.com/cal.ics")          # not https
    assert normalize_url("webcal://example.com/a.ics") == "https://example.com/a.ics"
    with pytest.raises(CalendarError):
        validate_link("https://localhost/cal.ics")           # private network address


# --- waves and booking ------------------------------------------------------------------


async def test_wave_asks_best_three_and_first_yes_wins():
    rid = make_request(verdicts={"hannah": "available", "aisha": "maybe", "diego": "maybe", "mei": "maybe",
                                 "sam": "unavailable"})
    await coordinator.send_wave(rid)
    state = asks(rid)
    assert state["hannah"] == "asked" and state["sam"] == "not_asked"
    assert sum(s == "asked" for s in state.values()) == 3
    assert "Ask 1 more now" in last("priya")["buttons"]

    # Two people say yes at the same moment: only one booking.
    await asyncio.gather(coordinator.accept(rid, "hannah"), coordinator.accept(rid, "aisha"))
    r = request(rid)
    assert r["status"] == "booked"
    winner = r["helper_id"]
    loser = "aisha" if winner == "hannah" else "hannah"
    assert asks(rid)[winner] == "accepted"
    assert "already covered" in last(loser)["text"]
    assert "Peanut allergy" in last(winner)["text"]                  # notes only go to the helper
    assert not any("Peanut" in m["text"] for m in messages(loser))
    assert db.one("SELECT COUNT(*) AS n FROM events WHERE kind = 'guard'")["n"] >= 1


async def test_all_decline_moves_to_next_wave_then_no_match():
    rid = make_request(verdicts={"hannah": "available", "aisha": "maybe", "diego": "maybe", "mei": "maybe"})
    await coordinator.send_wave(rid)
    for v in [v for v, s in asks(rid).items() if s == "asked"]:
        await coordinator.decline(rid, v)
    assert asks(rid)["mei"] == "asked"                                 # wave 2
    await coordinator.decline(rid, "mei")
    assert request(rid)["status"] == "no_match"
    assert "no one in your village is free" in last("priya")["text"]


async def test_wave_timeout_asks_next_people(monkeypatch):
    rid = make_request(verdicts={"hannah": "available", "aisha": "maybe", "diego": "maybe", "mei": "maybe"})
    await coordinator.send_wave(rid)
    db.run("UPDATE requests SET wave_sent_at = '2000-01-01T00:00:00' WHERE id = ?", rid)
    await coordinator.tick()
    assert asks(rid)["mei"] == "asked"


async def test_parent_cancel_tells_everyone_asked():
    rid = make_request(verdicts={"hannah": "available", "aisha": "maybe"})
    await coordinator.send_wave(rid)
    coordinator.cancel_request(rid)
    assert request(rid)["status"] == "cancelled"
    assert "doesn't need help" in last("hannah")["text"]
    assert all(m["used"] for m in messages("hannah") if m["buttons"])  # old Yes/No buttons are retired


# --- back-outs ---------------------------------------------------------------------------


def test_full_class_gets_handoff_time():
    start, end, buffer = coordinator._care_window(datetime(2026, 10, 15, 10, 10, tzinfo=TZ), 80,
                                                  datetime(2026, 10, 13, 11, 30, tzinfo=TZ))
    assert (start.strftime("%H:%M"), end.strftime("%H:%M"), buffer) == ("10:05", "11:35", 5)


async def test_helper_can_back_out_a_day_ahead_and_a_replacement_is_asked():
    rid = make_request(day="2026-10-15", start="09:50", end="10:55", minutes=45,   # 2 days out
                       verdicts={"sam": "available", "hannah": "available", "mei": "maybe"})
    await coordinator.send_wave(rid)
    await coordinator.accept(rid, "sam")
    assert "I can't make it anymore" in last("sam")["buttons"]
    assert "ideally the day before" in last("sam")["text"]
    total_before = db.one("SELECT help_count_total AS n FROM users WHERE id = 'sam'")["n"]
    await coordinator.helper_back_out(rid, "sam")
    assert request(rid)["status"] == "searching" and request(rid)["helper_id"] is None
    assert asks(rid)["sam"] == "withdrew"
    assert "You don't need to do anything" in last("priya")["text"]
    assert db.one("SELECT help_count_total AS n FROM users WHERE id = 'sam'")["n"] == total_before - 1
    assert any(s == "asked" for v, s in asks(rid).items() if v != "sam")


async def test_backing_out_less_than_a_day_ahead_asks_to_confirm_first():
    rid = make_request(day="2026-10-14", start="09:50", end="10:55", minutes=45,   # ~22 hours out
                       verdicts={"sam": "available", "hannah": "available"})
    await coordinator.send_wave(rid)
    await coordinator.accept(rid, "sam")
    await coordinator.helper_back_out(rid, "sam")
    assert request(rid)["status"] == "booked"                                       # not yet
    assert "counting on you" in last("sam")["text"]
    assert last("sam")["buttons"] == ["Yes, I have to cancel", "Never mind, I'll be there"]
    await coordinator.handle_button("sam", "helper_cancel_confirm", rid, last("sam")["id"])
    assert request(rid)["status"] == "searching" and asks(rid)["sam"] == "withdrew"
    assert asks(rid)["hannah"] == "asked"


async def test_helper_cannot_back_out_inside_the_cutoff():
    rid = make_request(verdicts={"hannah": "available"})          # today 12:50, test clock 11:30
    await coordinator.send_wave(rid)
    await coordinator.accept(rid, "hannah")
    assert "I can't make it anymore" not in last("hannah")["buttons"]   # no back-out button offered
    await coordinator.helper_back_out(rid, "hannah")
    assert request(rid)["status"] == "booked"
    assert "can't swap you out automatically" in last("hannah")["text"]


# --- pausing, leaving, once a day --------------------------------------------------------


async def test_paused_and_done_for_the_day_villagers_are_skipped():
    first = make_request(verdicts={"aisha": "maybe", "hannah": "available"})
    await coordinator.send_wave(first)
    await coordinator.decline(first, "aisha")      # Aisha said no today
    await coordinator.accept(first, "hannah")      # Hannah is helping today (not in Marcus's village anyway)
    coordinator.set_pause("mei", "today")

    second = make_request(parent="marcus", start="14:50", end="15:55", minutes=45,
                          verdicts={"aisha": "available", "mei": "available", "diego": "available"})
    resting = coordinator._done_for_the_day(second)
    assert resting == {"aisha": "already said no that day", "hannah": "already helping that day"}
    await coordinator.send_wave(second)
    state = asks(second)
    assert state["aisha"] == "not_asked" and state["diego"] == "asked"


async def test_leaving_a_village_closes_open_asks():
    rid = make_request(verdicts={"hannah": "available", "aisha": "maybe"})
    await coordinator.send_wave(rid)
    assert coordinator.leave_village("hannah", "priya")
    assert asks(rid)["hannah"] == "declined"
    assert "stepped back" in last("priya")["text"]
    assert not db.one("SELECT 1 FROM village WHERE parent_id = 'priya' AND villager_id = 'hannah'")


# --- buttons and budget --------------------------------------------------------------------


async def test_buttons_only_work_once_and_only_for_their_owner():
    rid = make_request(verdicts={"hannah": "available"})
    await coordinator.send_wave(rid)
    ask_msg = next(m for m in messages("hannah") if "Yes, I can" in m["buttons"])
    assert channels.check_press("sam", "accept", ask_msg["id"]) == "That button isn't yours."
    assert channels.check_press("hannah", "cancel", ask_msg["id"]) == "That button isn't on this message."
    assert channels.check_press("hannah", "accept", ask_msg["id"]) is None
    await coordinator.handle_button("hannah", "accept", rid, ask_msg["id"])
    assert channels.check_press("hannah", "accept", ask_msg["id"]) == "Those buttons were already used."


async def test_request_token_budget_stops_agents():
    rid = make_request()
    db.run("INSERT INTO usage (request_id, agent, requests, input_tokens, cached_tokens, output_tokens, cost_usd, "
           "duration_ms, ok, created_at) VALUES (?, 'availability', 1, 200000, 0, 0, 0, 0, 1, 'x')", rid)
    with pytest.raises(runner.BudgetExceeded):
        await runner.run_agent(None, "availability", "anything", request_id=rid)
    assert db.one("SELECT text FROM events WHERE kind = 'guard'")["text"].startswith("Skipped availability")


# --- add to calendar ----------------------------------------------------------------------


async def test_booking_offers_add_to_calendar_links_with_only_the_basics():
    import calendar_links
    from urllib.parse import parse_qs, urlparse

    rid = make_request(verdicts={"hannah": "available"})
    await coordinator.send_wave(rid)
    await coordinator.accept(rid, "hannah")
    for who in ("hannah", "priya"):
        assert {"Add to Google Calendar", "Add to Apple / Outlook"} <= set(last(who)["buttons"])

    google = parse_qs(urlparse(calendar_links.google_url(rid, "hannah")).query)
    assert google["dates"] == ["20261013T125000/20261013T134000"] and google["ctz"] == ["America/New_York"]
    assert google["text"] == ["Village: hanging out with Arjun (Priya's)"]

    token = calendar_links.sign(rid, "hannah")
    ics = calendar_links.ics_file(rid, "hannah", token)
    assert "DTSTART:20261013T165000Z" in ics and "SUMMARY:Village: hanging out with Arjun (Priya's)" in ics
    assert "Peanut" not in ics and "Peanut" not in str(google)            # the kid's notes stay in the chat
    assert calendar_links.ics_file(rid, "hannah", "wrong") is None          # signed link
    assert calendar_links.ics_file(rid, "sam", calendar_links.sign(rid, "sam")) is None   # not their booking

    coordinator.cancel_request(rid)
    assert calendar_links.ics_file(rid, "hannah", token) is None            # link stops working once cancelled
    assert "delete it there" in last("hannah")["text"]


async def test_someone_in_two_villages_is_asked_by_both_but_booked_once():
    """Mei is in Priya's and Marcus's villages. Both ask her; once she says yes to one, the other takes her off."""
    assert {r["parent_id"] for r in db.all_("SELECT parent_id FROM village WHERE villager_id = 'mei' "
                                            "AND status = 'accepted'")} == {"priya", "marcus"}
    priyas = make_request(verdicts={"mei": "available"})
    marcuss = make_request(parent="marcus", start="13:00", end="13:50", verdicts={"mei": "available",
                                                                                  "aisha": "available"})
    await coordinator.send_wave(priyas)
    await coordinator.send_wave(marcuss)
    assert asks(priyas)["mei"] == "asked" and asks(marcuss)["mei"] == "asked"
    assert any("Priya" in m["text"] for m in messages("mei")) and any("Marcus" in m["text"] for m in messages("mei"))

    await coordinator.accept(priyas, "mei")
    assert request(priyas)["helper_id"] == "mei"
    assert asks(marcuss)["mei"] == "withdrawn"                      # can't be booked twice
    assert "taken you off Marcus's request" in last("mei")["text"]
    assert request(marcuss)["status"] == "searching"               # Marcus's search goes on with Aisha

    # Leaving one village doesn't touch the other.
    assert coordinator.leave_village("mei", "priya")
    assert db.one("SELECT status FROM village WHERE parent_id = 'marcus' AND villager_id = 'mei'")["status"] == \
        "accepted"


async def test_parent_can_edit_a_kid_and_a_booked_helper_gets_the_new_notes():
    rid = make_request(verdicts={"hannah": "available"})
    await coordinator.send_wave(rid)
    await coordinator.accept(rid, "hannah")
    kid = request(rid)["kid_id"]
    assert coordinator.update_kid("priya", kid, "Arjun", "4", "Loves Bluey.", "Peanut and sesame allergy.")
    assert db.one("SELECT allergies FROM kids WHERE id = ?", kid)["allergies"] == "Peanut and sesame allergy."
    assert "updated Arjun's details" in last("hannah")["text"] and "sesame" in last("hannah")["text"]
    assert not coordinator.update_kid("marcus", kid, "X", "1", "")      # only the parent can edit
