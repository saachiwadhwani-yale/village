"""The coordinator: Village's workflow, written as plain code.

The steps are fixed, so they're code. Each step that needs judgment calls an agent:

    parent message ──► intake agent ──► confirm with parent (buttons)
                                              │
                                              ▼
            for every villager: rules screen ──► (unclear?) villager availability agent
                                              │       (one per villager, in parallel)
                                              ▼
            ask the best few (a "wave") ──► villager replies: button, or reply agent for free text
                                              │
                                              ▼
            first yes wins (one atomic database update) ──► notify parent, helper, everyone else

Agents never send messages, book anyone, or see a child's notes. Only this
module does, and only to people in the parent's approved village.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta

import calendar_links
import channels
import db
import onboarding
from agents.availability import VillagerDeps, availability_agent, villager_prompt
from agents.intake import IntakeDeps, intake_agent
from agents.profile import profile_agent, profile_prompt
from agents.reply import reply_agent, reply_prompt
from calendars import day_bounds, merged_busy
from config import (AGENT_FOR_EVERY_VILLAGER, BACKOUT_MIN_NOTICE_HOURS, BACKOUT_SOFT_NOTICE_HOURS, HANDOFF_BUFFER_MIN, ONE_ANSWER_PER_DAY,
                    MAX_COMMITMENT_MINUTES, MAX_OPEN_REQUESTS, MAX_VILLAGERS_CHECKED,
                    TZ, WAVE_SIZE, WAVE_TIMEOUT_MIN)
from models import AvailabilityVerdict, Button, ParsedAvailability
from runner import ContentFiltered, run_agent
from screening import screen

AGENT_CONCURRENCY = 5


# --- Formatting --------------------------------------------------------------------


def hm(dt: datetime) -> str:
    return dt.strftime("%-I:%M%p").lower()


def span(start: datetime, end: datetime) -> str:
    return f"{hm(start)}–{hm(end)}"


def day_label(dt: datetime) -> str:
    today = db.now().date()
    if dt.date() == today:
        return "today"
    if dt.date() == today + timedelta(days=1):
        return "tomorrow"
    return dt.strftime("%a %b %-d")


def first(user_id: str) -> str:
    row = db.one("SELECT name FROM users WHERE id = ?", user_id)
    return row["name"].split()[0] if row else user_id


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).replace(tzinfo=TZ) if value else None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _request(request_id: int):
    return db.one("SELECT * FROM requests WHERE id = ?", request_id)


def _kid(kid_id: str | None):
    return db.one("SELECT * FROM kids WHERE id = ?", kid_id) if kid_id else None


def _real_now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _care_window(meeting: datetime, minutes: int, now: datetime) -> tuple[datetime, datetime, int]:
    """The parent's commitment plus handoff time on each side, starting no earlier than now."""
    buffer = HANDOFF_BUFFER_MIN
    start = max(meeting - timedelta(minutes=buffer), now)
    return start, meeting + timedelta(minutes=minutes + buffer), buffer


# --- Incoming messages ---------------------------------------------------------------


async def handle_message(user_id: str, text: str) -> None:
    """Anyone typed something to Village."""
    channels.typing.add(user_id)
    try:
        if onboarding.step(user_id) is None:          # first time we've met: set up before anything else
            onboarding.start(user_id)
            return
        if onboarding.handle_text(user_id, text):
            return
        ask = _open_ask_for(user_id)
        if ask and await _villager_reply(ask, user_id, text):
            return
        booking = _upcoming_booking_for(user_id)
        if booking and await _helper_message(booking, user_id, text):
            return
        user = db.one("SELECT is_parent FROM users WHERE id = ?", user_id)
        if user and user["is_parent"]:
            await _parent_message(user_id, text)
        elif _recently_closed_ask(user_id):
            channels.send(user_id, "Thank you! That one's already covered, so no need. "
                                   "I'll message you next time someone needs a hand.")
        else:
            channels.send(user_id, "Hi! I'll message you here when a classmate in your village needs a hand. "
                                   "You can update your calendars and availability on your Village profile (my Home tab in Slack).")
    except Exception as exc:
        db.log_event(None, "error", f"Failed handling a message from {user_id}: {type(exc).__name__}: {exc}")
        channels.send(user_id, "Sorry, something went wrong on my end. Please try again in a moment.")
    finally:
        channels.typing.discard(user_id)


def _recently_closed_ask(villager_id: str) -> bool:
    """They were asked about a request that has since been filled or cancelled."""
    row = db.one("SELECT status FROM asks WHERE villager_id = ? AND asked_at IS NOT NULL "
                 "ORDER BY asked_at DESC LIMIT 1", villager_id)
    return bool(row and row["status"] in ("filled", "withdrawn"))


def _upcoming_booking_for(helper_id: str):
    """A request this person already said yes to that hasn't ended yet."""
    for req in db.all_("SELECT * FROM requests WHERE helper_id = ? AND status = 'booked' ORDER BY care_start",
                       helper_id):
        if _dt(req["care_end"]) > db.now():
            return req
    return None


def _open_ask_for(villager_id: str):
    return db.one(
        "SELECT a.* FROM asks a JOIN requests r ON r.id = a.request_id "
        "WHERE a.villager_id = ? AND a.status = 'asked' AND r.status = 'searching' "
        "ORDER BY a.asked_at DESC LIMIT 1", villager_id)


async def _parent_message(parent_id: str, text: str) -> None:
    draft = db.one("SELECT id FROM requests WHERE parent_id = ? AND status = 'draft' ORDER BY id DESC LIMIT 1",
                   parent_id)
    draft_id = draft["id"] if draft else None
    now = db.now()
    kids = db.all_("SELECT id, name, age FROM kids WHERE parent_id = ?", parent_id)
    kid_list = "; ".join(f"{k['name']} ({k['age']}, id {k['id']})" for k in kids) or "none"
    earlier = _recent_conversation(parent_id, text)
    history = f"Conversation so far about this request (oldest first):\n{earlier}\n" if earlier else ""
    prompt = (f"Now: {now:%A %Y-%m-%d %H:%M} (New Haven time).\nTheir kids: {kid_list}\n{history}"
              f"Parent's new message:\n<<<{text}>>>")
    try:
        out = await run_agent(intake_agent, "intake", prompt, IntakeDeps(parent_id, draft_id),
                              request_id=draft_id, subject=parent_id)
    except ContentFiltered:
        channels.send(parent_id, "That message looked like it had instructions for me in it, so I didn't act on "
                                 "it. Could you just tell me the time and how long, like: \"I have a coffee chat at "
                                 "1 for 30 min and need someone for my kid\"?")
        return
    except Exception as exc:
        db.log_event(draft_id, "error", f"Intake failed: {type(exc).__name__}", "intake")
        channels.send(parent_id, "Sorry, I couldn't read that. Could you try again, like: "
                                 "\"I have a coffee chat at 1 for 30 min and need someone for my kid\"?")
        return

    if out.kind in ("clarify", "other"):
        channels.send(parent_id, out.reply or "Could you tell me the time and how long you need?")
        return
    if out.kind == "cancel":
        target = draft_id or _latest_active(parent_id)
        if target:
            cancel_request(target)
        else:
            channels.send(parent_id, "You don't have any open requests right now.")
        return

    # kind == "request": the code, not the model, does the time math and the limits.
    meeting = datetime.fromisoformat(f"{out.meeting_date}T{out.meeting_start}").replace(tzinfo=TZ)
    minutes = out.meeting_minutes
    if meeting + timedelta(minutes=minutes) <= now:
        channels.send(parent_id, f"That time ({day_label(meeting)} at {hm(meeting)}) has already passed. "
                                 "Which day did you mean?")
        return
    if minutes > MAX_COMMITMENT_MINUTES:
        channels.send(parent_id, f"Village is for short stretches: up to {MAX_COMMITMENT_MINUTES} minutes with a "
                                 f"classmate. Your {out.purpose or 'commitment'} is {minutes} min. Could you "
                                 "split it, or get help for part of it?")
        return
    active = db.one("SELECT COUNT(*) AS n FROM requests WHERE parent_id = ? AND status = 'searching'",
                    parent_id)["n"]
    if active >= MAX_OPEN_REQUESTS:
        channels.send(parent_id, f"You already have {active} open requests. Cancel one before adding another.")
        return

    care_start, care_end, buffer = _care_window(meeting, minutes, now)
    fields = (out.kid_id, _iso(meeting), minutes, _iso(care_start), _iso(care_end), out.purpose, out.location)
    if draft_id:
        db.run("UPDATE requests SET kid_id=?, meeting_start=?, meeting_minutes=?, care_start=?, care_end=?,"
               " purpose=?, location=? WHERE id = ?", *fields, draft_id)
        channels.retire_buttons(draft_id, parent_id)
        request_id = draft_id
    else:
        request_id = db.run("INSERT INTO requests (parent_id, kid_id, meeting_start, meeting_minutes, care_start,"
                            " care_end, purpose, location, status, created_at) VALUES (?,?,?,?,?,?,?,?, 'draft', ?)",
                            parent_id, *fields, db.utc_now_iso())
        # The intake run happened before the request existed; count it toward this request.
        db.run("UPDATE usage SET request_id = ? WHERE id = (SELECT MAX(id) FROM usage WHERE agent = 'intake' "
               "AND subject = ? AND request_id IS NULL)", request_id, parent_id)
    db.log_event(request_id, "agent", f"Intake read the request: {out.purpose or 'commitment'} "
                 f"{day_label(meeting)} {hm(meeting)} for {minutes} min.", "intake", out.model_dump())

    kid = _kid(out.kid_id)
    meeting_end = meeting + timedelta(minutes=minutes)
    where = f" Handoff at {out.location}." if out.location else ""
    pad = f" plus {buffer} min on each side for the handoff" if buffer else ""
    channels.send(
        parent_id,
        f"Got it: someone to hang out with {kid['name']} ({kid['age']}) {day_label(care_start)}, "
        f"{span(care_start, care_end)}. That covers your {out.purpose or 'commitment'} "
        f"({span(meeting, meeting_end)}){pad}.{where}\n\nWant me to check your village?",
        [Button(label="Find someone", action="confirm", request_id=request_id, style="primary"),
         Button(label="Cancel", action="cancel", request_id=request_id)],
        request_id,
    )


def _recent_conversation(parent_id: str, text: str, minutes: int = 30, limit: int = 8) -> str:
    """The back-and-forth leading up to this message, so answers like "30 min" build on "tomorrow at 2".

    Only the current exchange: messages from the last half hour, stopping at anything that belongs to a
    request that was already sent out, booked, or cancelled.
    """
    since = (db.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    rows = db.all_("SELECT m.direction, m.text, r.status FROM messages m LEFT JOIN requests r ON r.id = m.request_id "
                   "WHERE m.user_id = ? AND m.created_at >= ? ORDER BY m.id DESC LIMIT ?", parent_id, since, limit + 1)
    lines = []
    for i, r in enumerate(rows):
        if i == 0 and r["direction"] == "in" and r["text"] == text:
            continue  # the message being answered right now
        if r["status"] and r["status"] != "draft":
            break     # an earlier request that's already underway or finished
        speaker = "Parent" if r["direction"] == "in" else "Village"
        lines.append(f"{speaker}: {r['text'][:300]}")
    return "\n".join(reversed(lines))


def _latest_active(parent_id: str) -> int | None:
    row = db.one("SELECT id FROM requests WHERE parent_id = ? AND status IN ('draft','searching','booked') "
                 "ORDER BY id DESC LIMIT 1", parent_id)
    return row["id"] if row else None


# --- Buttons -------------------------------------------------------------------------


async def handle_button(user_id: str, action: str, request_id: int | None, message_id: int | None,
                        values: list[str] | None = None) -> None:
    channels.typing.add(user_id)
    try:
        if message_id:
            channels.mark_pressed(user_id, message_id, action)
        verb, _, arg = action.partition(":")

        if verb.startswith("setup_"):
            onboarding.handle_action(user_id, verb, arg, values or [], message_id)
            return

        if verb in ("village_accept", "village_decline"):
            await _answer_invite(user_id, arg, verb == "village_accept")
            return

        req = _request(request_id) if request_id else None
        if req is None:
            return
        is_parent = req["parent_id"] == user_id
        if is_parent and verb == "confirm" and req["status"] == "draft":
            db.run("UPDATE requests SET status = 'searching' WHERE id = ?", request_id)
            channels.send(user_id, "On it! Checking who in your village is free…", request_id=request_id)
            await run_matching(request_id)
        elif is_parent and verb == "cancel":
            cancel_request(request_id)
        elif is_parent and verb == "ask_more":
            await send_wave(request_id, everyone=True)
        elif is_parent and verb == "accept_counter":
            await _accept_counter(request_id, arg)
        elif is_parent and verb == "decline_counter":
            await _decline_counter(request_id, arg)
        elif verb == "helper_cancel":
            await helper_back_out(request_id, user_id)
        elif verb == "helper_cancel_confirm":
            await helper_back_out(request_id, user_id, confirmed=True)
        elif verb == "helper_keep" and req["helper_id"] == user_id:
            channels.send(user_id, f"Great, thank you! You're still on for {_when(req)}.", request_id=request_id)
        elif verb in ("accept", "decline"):
            ask = db.one("SELECT * FROM asks WHERE request_id = ? AND villager_id = ? AND status = 'asked'",
                         request_id, user_id)
            if ask is None:
                channels.send(user_id, "Thanks! This one's already been sorted out.", request_id=request_id)
            elif verb == "accept":
                await accept(request_id, user_id)
            else:
                await decline(request_id, user_id)
    except Exception as exc:
        db.log_event(request_id, "error", f"Button {action} failed: {type(exc).__name__}: {exc}")
        channels.send(user_id, "Sorry, something went wrong on my end. Please try again.")
    finally:
        channels.typing.discard(user_id)


# --- Matching -------------------------------------------------------------------------


async def run_matching(request_id: int) -> None:
    """Check every villager's availability, then ask the first wave."""
    req = _request(request_id)
    care_start, care_end = _dt(req["care_start"]), _dt(req["care_end"])
    members = db.all_(
        "SELECT u.* FROM village v JOIN users u ON u.id = v.villager_id "
        "WHERE v.parent_id = ? AND v.status = 'accepted' ORDER BY u.name", req["parent_id"])
    # Paused villagers aren't checked or asked at all.
    paused = [m for m in members if m["paused_until"] and m["paused_until"] > req["care_start"]]
    resting = _done_for_the_day(request_id)
    villagers = [m for m in members if m not in paused and m["id"] not in resting][:MAX_VILLAGERS_CHECKED]
    if paused:
        db.log_event(request_id, "step", f"Skipped {_join([first(m['id']) for m in paused])} (paused).")
    for reason in ("already helping that day", "already said no that day"):
        names = [first(m["id"]) for m in members if resting.get(m["id"]) == reason]
        if names:
            db.log_event(request_id, "step", f"Skipped {_join(names)} ({reason}).")
    mode = "all_agents" if AGENT_FOR_EVERY_VILLAGER else "prefilter"
    db.log_event(request_id, "step", f"Checking {len(villagers)} villagers for {span(care_start, care_end)} "
                 f"({'rules first, agents for unclear cases' if mode == 'prefilter' else 'an agent for every villager'}).")
    if not villagers:
        db.run("UPDATE requests SET status = 'no_match' WHERE id = ?", request_id)
        channels.send(req["parent_id"], "Your village is empty. Add a few classmates you trust on your Village profile (my Home tab in Slack), "
                                        "and I'll ask them next time.", request_id=request_id)
        return

    day_start, day_end = day_bounds(care_start.date())
    sem = asyncio.Semaphore(AGENT_CONCURRENCY)

    async def check(v) -> tuple[str, AvailabilityVerdict, str]:
        cals = [dict(c) for c in db.all_("SELECT * FROM calendars WHERE user_id = ?", v["id"])]
        busy, errors = await asyncio.to_thread(merged_busy, cals, day_start, day_end)
        parsed = ParsedAvailability.model_validate_json(v["availability_parsed"]) if v["availability_parsed"] else None
        if mode == "prefilter":
            verdict = screen(busy, errors, bool(cals), parsed, care_start, care_end)
            if verdict:
                return v["id"], verdict, "rules"
        deps = VillagerDeps(
            villager_id=v["id"], busy=busy, calendar_labels=[c["label"] for c in cals], calendar_errors=errors,
            availability_text=v["availability_text"], parsed=parsed, help_count_week=v["help_count_week"],
            care_start=care_start, care_end=care_end,
        )
        async with sem:
            try:
                verdict = await run_agent(availability_agent, "availability",
                                          villager_prompt(deps, req["location"]), deps,
                                          request_id=request_id, subject=v["id"])
                return v["id"], verdict, "agent"
            except Exception as exc:
                kind = "guard" if isinstance(exc, ContentFiltered) else "error"
                db.log_event(request_id, kind, f"Availability check for {first(v['id'])} failed "
                             f"({type(exc).__name__}); will ask them anyway as a maybe.", "availability")
                return v["id"], AvailabilityVerdict(verdict="maybe", confidence="low",
                                                    reason="Couldn't check automatically."), "fallback"

    results = await asyncio.gather(*(check(v) for v in villagers))
    for vid, verdict, by in results:
        db.run("INSERT INTO asks (request_id, villager_id, verdict, confidence, verdict_reason, decided_by, status)"
               " VALUES (?,?,?,?,?,?, 'not_asked')",
               request_id, vid, verdict.verdict, verdict.confidence, verdict.reason, by)
        db.log_event(request_id, "agent" if by == "agent" else "step",
                     f"{first(vid)}: {verdict.verdict} — {verdict.reason}",
                     "availability" if by == "agent" else None,
                     {"villager": vid, "decided_by": by, **verdict.model_dump()})
    await send_wave(request_id)


def _done_for_the_day(request_id: int) -> dict[str, str]:
    """Villagers who already took a shift, said no, or backed out on this request's day, for any parent."""
    if not ONE_ANSWER_PER_DAY:
        return {}
    day = _request(request_id)["care_start"][:10]
    rows = db.all_(
        "SELECT a.villager_id, a.status FROM asks a JOIN requests r ON r.id = a.request_id "
        "WHERE r.id != ? AND substr(r.care_start, 1, 10) = ? AND ("
        " (a.status = 'accepted' AND r.status = 'booked') OR a.status IN ('declined', 'withdrew'))",
        request_id, day)
    done: dict[str, str] = {}
    for r in rows:
        if r["status"] == "accepted":
            done[r["villager_id"]] = "already helping that day"
        else:
            done.setdefault(r["villager_id"], "already said no that day")
    return done


def _candidates(request_id: int):
    """Villagers not yet asked, best first: free before maybe, then whoever has helped least lately
    (this week, then ever), so the same reliable person isn't always the one asked."""
    resting = _done_for_the_day(request_id)
    return [c for c in _ranked_candidates(request_id) if c["villager_id"] not in resting]


def _ranked_candidates(request_id: int):
    return db.all_(
        "SELECT a.villager_id, a.verdict, u.help_count_week FROM asks a "
        "JOIN requests r ON r.id = a.request_id "
        "JOIN users u ON u.id = a.villager_id "
        "JOIN village v ON v.parent_id = r.parent_id AND v.villager_id = a.villager_id "
        "WHERE a.request_id = ? AND a.status = 'not_asked' AND a.verdict IN ('available','maybe') "
        "ORDER BY (a.verdict = 'maybe'), u.help_count_week, u.help_count_total, u.name", request_id)


def _outstanding(request_id: int) -> int:
    return db.one("SELECT COUNT(*) AS n FROM asks WHERE request_id = ? AND status IN ('asked','countered')",
                  request_id)["n"]


async def send_wave(request_id: int, everyone: bool = False, replacing: str | None = None) -> None:
    """Ask the next few villagers. `replacing` is the name of a helper who just backed out."""
    req = _request(request_id)
    if req["status"] != "searching":
        return
    parent_id = req["parent_id"]
    candidates = _candidates(request_id)
    if not candidates:
        if _outstanding(request_id) == 0:
            db.run("UPDATE requests SET status = 'no_match' WHERE id = ?", request_id)
            channels.retire_buttons(request_id, parent_id)
            db.log_event(request_id, "step", "No one left to ask.")
            lead = (f"Heads up: {replacing} can't make it anymore, and I couldn't find anyone else in your village "
                    "who's free" if replacing else "I'm sorry, no one in your village is free")
            channels.send(parent_id, f"{lead} {_when(req)}. You could add more classmates to your village, "
                                     "or try a different time.", request_id=request_id)
        elif everyone:
            channels.send(parent_id, "Everyone who looks free has already been asked. I'll tell you as soon as "
                                     "someone answers.", request_id=request_id)
        return

    batch = candidates if everyone else candidates[:WAVE_SIZE]
    wave = req["wave"] + 1
    kid = _kid(req["kid_id"])
    care_start, care_end = _dt(req["care_start"]), _dt(req["care_end"])
    parent = first(parent_id)
    where = f" at {req['location']}" if req["location"] else ""
    why = f" {parent} has a {req['purpose']}." if req["purpose"] else ""
    for c in batch:
        vid = c["villager_id"]
        db.run("UPDATE asks SET status = 'asked', wave = ?, asked_at = ? WHERE request_id = ? AND villager_id = ?",
               wave, _real_now(), request_id, vid)
        channels.send(
            vid,
            f"Hi {first(vid)}! {parent} could use a hand: can you hang out with {kid['name']} ({kid['age']}) "
            f"{day_label(care_start)}, {span(care_start, care_end)}{where}?{why}",
            [Button(label="Yes, I can", action="accept", request_id=request_id, style="primary"),
             Button(label="Can't this time", action="decline", request_id=request_id)],
            request_id,
        )
    db.run("UPDATE requests SET wave = ?, wave_sent_at = ? WHERE id = ?", wave, _real_now(), request_id)
    names = _join([first(c["villager_id"]) for c in batch])
    db.log_event(request_id, "step", f"Wave {wave}: asked {names}.")
    remaining = len(candidates) - len(batch)
    buttons = [Button(label=f"Ask {remaining} more now", action="ask_more", request_id=request_id)] if remaining else []
    buttons.append(Button(label="Cancel request", action="cancel", request_id=request_id, style="danger"))
    channels.retire_buttons(request_id, parent_id)
    text = (f"Heads up: {replacing} can't make it anymore. You don't need to do anything: I'm already asking "
            f"{names} and will let you know as soon as someone says yes." if replacing
            else f"Asking {names} now. I'll let you know as soon as someone says yes.")
    channels.send(parent_id, text, buttons, request_id)


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


# --- Villager answers ----------------------------------------------------------------


async def _villager_reply(ask, villager_id: str, text: str) -> bool:
    """A free-text reply to an open ask. Returns False if it wasn't about the ask."""
    request_id = ask["request_id"]
    req = _request(request_id)
    try:
        intent = await run_agent(reply_agent, "reply",
                                 reply_prompt(_dt(req["care_start"]), _dt(req["care_end"]), text, booked=False),
                                 request_id=request_id, subject=villager_id)
    except ContentFiltered:
        channels.send(villager_id, "I couldn't use that reply. Tap Yes or No above, or tell me a time that works.")
        return True
    except Exception:
        channels.send(villager_id, "Sorry, I didn't catch that. Tap Yes or No above, or tell me a time that works.")
        return True
    db.log_event(request_id, "agent", f"Reply from {first(villager_id)} read as: {intent.intent}.", "reply",
                 {"villager": villager_id, **intent.model_dump()})
    parent = first(req["parent_id"])
    if intent.intent == "accept":
        await accept(request_id, villager_id)
    elif intent.intent == "decline":
        await decline(request_id, villager_id)
    elif intent.intent == "counter" and intent.counter_start and intent.counter_end:
        db.run("UPDATE asks SET status = 'countered', counter_start = ?, counter_end = ?, answered_at = ? "
               "WHERE id = ?", intent.counter_start, intent.counter_end, _real_now(), ask["id"])
        channels.retire_buttons(request_id, villager_id)
        meeting = _dt(req["meeting_start"])
        meeting_end = meeting + timedelta(minutes=req["meeting_minutes"])
        cs, ce = (_on(meeting.date(), intent.counter_start), _on(meeting.date(), intent.counter_end))
        gaps = [span(a, b) for a, b in ((meeting, cs), (ce, meeting_end)) if b > a]
        gap_note = (f" That leaves {' and '.join(gaps)} of your {req['purpose'] or 'commitment'} uncovered."
                    if gaps else "")
        channels.send(
            req["parent_id"],
            f"{first(villager_id)} can't do the whole time but offered {span(cs, ce)}. Your "
            f"{req['purpose'] or 'commitment'} is {span(meeting, meeting_end)}.{gap_note} "
            f"Want {first(villager_id)} to cover that?",
            [Button(label=f"Yes, {first(villager_id)} works", action=f"accept_counter:{villager_id}",
                     request_id=request_id, style="primary"),
             Button(label="Keep looking", action=f"decline_counter:{villager_id}", request_id=request_id)],
            request_id,
        )
        channels.send(villager_id, f"Thanks! I've asked {parent} whether {span(cs, ce)} works.",
                      request_id=request_id)
        if _outstanding(request_id) <= 1:
            await send_wave(request_id)
    elif intent.intent == "question":
        channels.send(req["parent_id"], f"{first(villager_id)} asked: “{intent.question}” "
                                        f"You can message {first(villager_id)} directly to answer.",
                      request_id=request_id)
        channels.send(villager_id, f"I've passed your question to {parent}.", request_id=request_id)
    else:
        user = db.one("SELECT is_parent FROM users WHERE id = ?", villager_id)
        if user["is_parent"]:
            return False  # probably a parent starting their own request
        channels.send(villager_id, "Sorry, I didn't catch that. Tap Yes or No above, or tell me a time that works.")
    return True


def _on(d: date, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{d.isoformat()}T{hhmm}").replace(tzinfo=TZ)


async def accept(request_id: int, villager_id: str) -> None:
    """First yes wins: a single conditional UPDATE, so two yeses can't both book."""
    claimed = db.run("UPDATE requests SET status = 'booked', helper_id = ? WHERE id = ? AND status = 'searching'",
                     villager_id, request_id)
    req = _request(request_id)
    parent_id = req["parent_id"]
    if not claimed:
        db.run("UPDATE asks SET status = 'filled' WHERE request_id = ? AND villager_id = ? "
               "AND status IN ('asked','countered')", request_id, villager_id)
        db.log_event(request_id, "guard", f"{first(villager_id)} said yes after the request was {req['status']}; "
                     "not booked twice.")
        channels.send(villager_id, "Thank you so much! This one's already covered, so you're off the hook.",
                      request_id=request_id)
        return

    db.run("UPDATE asks SET status = 'accepted', answered_at = ? WHERE request_id = ? AND villager_id = ?",
           _real_now(), request_id, villager_id)
    db.run("UPDATE users SET help_count_week = help_count_week + 1, help_count_total = help_count_total + 1 "
           "WHERE id = ?", villager_id)
    channels.retire_buttons(request_id)
    db.log_event(request_id, "step", f"Booked: {first(villager_id)} said yes first.")

    care_start, care_end = _dt(req["care_start"]), _dt(req["care_end"])
    when = f"{day_label(care_start)}, {span(care_start, care_end)}"
    for other in db.all_("SELECT villager_id FROM asks WHERE request_id = ? AND status IN ('asked','countered')",
                         request_id):
        db.run("UPDATE asks SET status = 'filled' WHERE request_id = ? AND villager_id = ?",
               request_id, other["villager_id"])
        channels.send(other["villager_id"], f"Update: {first(parent_id)} found someone for {when}, so no need. "
                                            "Thanks for being in the village!", request_id=request_id)

    kid = _kid(req["kid_id"])
    where = f" at {req['location']}" if req["location"] else ""
    channels.send(parent_id, f"{first(villager_id)} said yes! They'll meet you{where} at {hm(care_start)} "
                             f"and hang out with {kid['name']} until {hm(care_end)}.",
                  _calendar_buttons(request_id, parent_id)
                  + [Button(label="Cancel booking", action="cancel", request_id=request_id, style="danger")],
                  request_id)
    # The child's notes go only to the person who said yes, only now.
    notes = f"\n• Notes from {first(parent_id)}: {kid['notes']}" if kid["notes"] else ""
    can_back_out = _can_back_out(req)
    deadline = _dt(req["care_start"]) - timedelta(hours=BACKOUT_MIN_NOTICE_HOURS)
    out_note = (f"If you can't make it after all, please tell me as early as you can, ideally the day before "
                f"(and no later than {day_label(deadline)} at {hm(deadline)}), and I'll find someone else."
                if can_back_out else
                f"If anything comes up, message {first(parent_id)} right away.")
    channels.send(villager_id, f"Thank you! You're on for {when}.\n• Meet {first(parent_id)}{where} at "
                               f"{hm(care_start)}\n• {kid['name']} is {kid['age']}{notes}\n{out_note}",
                  _calendar_buttons(request_id, villager_id)
                  + ([Button(label="I can't make it anymore", action="helper_cancel", request_id=request_id)]
                     if can_back_out else []),
                  request_id)
    await _withdraw_other_asks(villager_id, request_id, care_start, care_end)


def _calendar_buttons(request_id: int, user_id: str) -> list[Button]:
    """Add-to-calendar links for a booking (no sign-in: a Google link and a signed .ics download)."""
    google = calendar_links.google_url(request_id, user_id)
    if not google:
        return []
    return [Button(label="Add to Google Calendar", action="calendar_google", kind="link", url=google,
                   request_id=request_id),
            Button(label="Add to Apple / Outlook", action="calendar_ics", kind="link",
                   url=calendar_links.ics_url(request_id, user_id), request_id=request_id)]


CALENDAR_NOTE = " If you added it to your calendar, you can delete it there."


def _can_back_out(req) -> bool:
    """Enough notice left for Village to swap the helper out automatically."""
    return _dt(req["care_start"]) - db.now() >= timedelta(hours=BACKOUT_MIN_NOTICE_HOURS)


def _short_notice(req) -> bool:
    """Less than a day before: backing out still works, but takes a second tap."""
    return _dt(req["care_start"]) - db.now() < timedelta(hours=BACKOUT_SOFT_NOTICE_HOURS)


def _short_notice_check(req, helper_id: str) -> None:
    parent = first(req["parent_id"])
    channels.send(helper_id, f"It's less than a day until {_when(req)}, and {parent} is counting on you. Please "
                             "only cancel if you really have to. If you do, I'll find someone else and let "
                             f"{parent} know.",
                  [Button(label="Yes, I have to cancel", action="helper_cancel_confirm", request_id=req["id"],
                          style="danger"),
                   Button(label="Never mind, I'll be there", action="helper_keep", request_id=req["id"],
                          style="primary")],
                  req["id"])


def _too_late_to_back_out(req, helper_id: str) -> None:
    db.log_event(req["id"], "guard", f"{first(helper_id)} asked to back out with less than "
                 f"{BACKOUT_MIN_NOTICE_HOURS} hours' notice; not swapped automatically.")
    parent = first(req["parent_id"])
    channels.send(helper_id, f"It's less than {BACKOUT_MIN_NOTICE_HOURS} hours until {hm(_dt(req['care_start']))}, "
                             f"so I can't swap you out automatically. {parent} is counting on you. If it's a real "
                             f"emergency, please message {parent} directly right away.", request_id=req["id"])


def _when(req) -> str:
    return f"{day_label(_dt(req['care_start']))}, {span(_dt(req['care_start']), _dt(req['care_end']))}"


async def _helper_message(req, helper_id: str, text: str) -> bool:
    """Free text from someone already booked. Never cancels on its own; backing out needs a tap."""
    request_id = req["id"]
    try:
        intent = await run_agent(reply_agent, "reply",
                                 reply_prompt(_dt(req["care_start"]), _dt(req["care_end"]), text, booked=True),
                                 request_id=request_id, subject=helper_id)
    except Exception:
        return False
    db.log_event(request_id, "agent", f"Message from helper {first(helper_id)} read as: {intent.intent}.", "reply",
                 {"villager": helper_id, "booked": True, **intent.model_dump()})
    parent = first(req["parent_id"])
    if intent.intent in ("decline", "counter") and not _can_back_out(req):
        _too_late_to_back_out(req, helper_id)
    elif intent.intent in ("decline", "counter") and _short_notice(req):
        _short_notice_check(req, helper_id)
    elif intent.intent in ("decline", "counter"):
        channels.send(helper_id, f"Sounds like {_when(req)} doesn't work anymore. That's completely okay! "
                                 f"Want me to find someone else for {parent}?",
                      [Button(label="Yes, find someone else", action="helper_cancel", request_id=request_id,
                              style="primary"),
                       Button(label="No, I'm still on", action="helper_keep", request_id=request_id)],
                      request_id)
    elif intent.intent == "question":
        channels.send(req["parent_id"], f"{first(helper_id)} asked: \u201c{intent.question}\u201d "
                                        f"You can message {first(helper_id)} directly to answer.",
                      request_id=request_id)
        channels.send(helper_id, f"I've passed your question to {parent}.", request_id=request_id)
    else:
        user = db.one("SELECT is_parent FROM users WHERE id = ?", helper_id)
        if user["is_parent"] and intent.intent == "unclear":
            return False  # probably a parent starting their own request
        channels.send(helper_id, f"You're all set for {_when(req)}. If anything changes, just tell me and "
                                 "I'll find someone else.", request_id=request_id)
    return True


async def helper_back_out(request_id: int, helper_id: str, confirmed: bool = False) -> None:
    """The booked helper can't make it. Reopen the request and find a replacement without the parent lifting a finger."""
    req = _request(request_id)
    if req["status"] != "booked" or req["helper_id"] != helper_id:
        channels.send(helper_id, "Thanks! This one's already been taken care of.", request_id=request_id)
        return
    now = db.now()
    if _dt(req["care_end"]) <= now:
        channels.send(helper_id, "That time has already passed, so there's nothing to change.", request_id=request_id)
        return
    if not _can_back_out(req):
        _too_late_to_back_out(req, helper_id)
        return
    if _short_notice(req) and not confirmed:
        db.log_event(request_id, "step", f"{first(helper_id)} asked to back out less than a day ahead; "
                     "asked them to confirm.")
        _short_notice_check(req, helper_id)
        return

    # Back to the full original window (a counter-offer may have shortened it), starting no earlier than now.
    care_start, care_end, _ = _care_window(_dt(req["meeting_start"]), req["meeting_minutes"], now)
    reopened = db.run("UPDATE requests SET status = 'searching', helper_id = NULL, care_start = ?, care_end = ? "
                      "WHERE id = ? AND status = 'booked' AND helper_id = ?",
                      _iso(care_start), _iso(care_end), request_id, helper_id)
    if not reopened:
        return
    db.run("UPDATE asks SET status = 'withdrew', answered_at = ? WHERE request_id = ? AND villager_id = ?",
           _real_now(), request_id, helper_id)
    # People told "no longer needed" never said no, so they can be asked again.
    db.run("UPDATE asks SET status = 'not_asked' WHERE request_id = ? AND status = 'filled'", request_id)
    db.run("UPDATE users SET help_count_week = MAX(help_count_week - 1, 0), "
           "help_count_total = MAX(help_count_total - 1, 0) WHERE id = ?", helper_id)
    channels.retire_buttons(request_id)
    db.log_event(request_id, "step", f"{first(helper_id)} can't make it anymore; looking for a replacement "
                 "automatically.")

    channels.send(helper_id, f"No problem at all, thanks for letting me know. I'll find someone else and let "
                             f"{first(req['parent_id'])} know. You don't need to do anything else.{CALENDAR_NOTE}",
                  request_id=request_id)
    await send_wave(request_id, replacing=first(helper_id))


async def _withdraw_other_asks(villager_id: str, booked_id: int, care_start: datetime, care_end: datetime) -> None:
    """Someone in several villages just said yes to one parent: take back their open asks from other parents
    that day (or, without the once-a-day rule, ones that overlap), so they can't be booked twice."""
    rows = db.all_("SELECT a.request_id, r.parent_id, r.care_start, r.care_end FROM asks a "
                   "JOIN requests r ON r.id = a.request_id WHERE a.villager_id = ? AND a.request_id != ? "
                   "AND a.status IN ('asked','countered') AND r.status = 'searching'", villager_id, booked_id)
    for r in rows:
        start, end = _dt(r["care_start"]), _dt(r["care_end"])
        clash = start.date() == care_start.date() if ONE_ANSWER_PER_DAY else start < care_end and care_start < end
        if not clash:
            continue
        db.run("UPDATE asks SET status = 'withdrawn' WHERE request_id = ? AND villager_id = ?", r["request_id"],
               villager_id)
        channels.retire_buttons(r["request_id"], villager_id)
        channels.send(villager_id, f"Since you're helping {first(_request(booked_id)['parent_id'])} that day, I've "
                                   f"taken you off {first(r['parent_id'])}'s request. No need to answer it.",
                      request_id=r["request_id"])
        db.log_event(r["request_id"], "step", f"{first(villager_id)} is already helping someone else that day.")
        if _outstanding(r["request_id"]) == 0:
            await send_wave(r["request_id"])


async def decline(request_id: int, villager_id: str) -> None:
    db.run("UPDATE asks SET status = 'declined', answered_at = ? WHERE request_id = ? AND villager_id = ?",
           _real_now(), request_id, villager_id)
    channels.retire_buttons(request_id, villager_id)
    channels.send(villager_id, "No problem, thanks for letting me know!", request_id=request_id)
    db.log_event(request_id, "step", f"{first(villager_id)} can't help.")
    if _outstanding(request_id) == 0:
        await send_wave(request_id)


async def _accept_counter(request_id: int, villager_id: str) -> None:
    ask = db.one("SELECT * FROM asks WHERE request_id = ? AND villager_id = ? AND status = 'countered'",
                 request_id, villager_id)
    req = _request(request_id)
    if not ask or req["status"] != "searching":
        channels.send(req["parent_id"], "That offer isn't open anymore.", request_id=request_id)
        return
    d = _dt(req["care_start"]).date()
    db.run("UPDATE requests SET care_start = ?, care_end = ? WHERE id = ?",
           _iso(_on(d, ask["counter_start"])), _iso(_on(d, ask["counter_end"])), request_id)
    await accept(request_id, villager_id)


async def _decline_counter(request_id: int, villager_id: str) -> None:
    db.run("UPDATE asks SET status = 'declined' WHERE request_id = ? AND villager_id = ? AND status = 'countered'",
           request_id, villager_id)
    req = _request(request_id)
    channels.send(villager_id, f"Thanks for offering! {first(req['parent_id'])} is going to keep looking.",
                  request_id=request_id)
    if _outstanding(request_id) == 0:
        await send_wave(request_id)


def cancel_request(request_id: int) -> None:
    req = _request(request_id)
    if req["status"] in ("cancelled", "expired", "no_match"):
        return
    db.run("UPDATE requests SET status = 'cancelled' WHERE id = ?", request_id)
    channels.retire_buttons(request_id)
    db.log_event(request_id, "step", "Cancelled by the parent.")
    if req["care_start"]:
        when = f"{day_label(_dt(req['care_start']))}, {span(_dt(req['care_start']), _dt(req['care_end']))}"
        notify = db.all_("SELECT villager_id FROM asks WHERE request_id = ? AND status IN ('asked','countered','accepted')",
                         request_id)
        for n in notify:
            note = CALENDAR_NOTE if req["helper_id"] == n["villager_id"] else ""
            channels.send(n["villager_id"], f"Update: {first(req['parent_id'])} doesn't need help {when} anymore. "
                                            f"Thanks!{note}", request_id=request_id)
        db.run("UPDATE asks SET status = 'withdrawn' WHERE request_id = ? AND status IN ('asked','countered')",
               request_id)
    note = CALENDAR_NOTE if req["status"] == "booked" else ""
    channels.send(req["parent_id"], f"Cancelled.{note} Message me any time you need a hand.", request_id=request_id)


# --- Village membership ------------------------------------------------------------------


def send_invite(parent_id: str, villager_id: str) -> None:
    kids = [k["name"] for k in db.all_("SELECT name FROM kids WHERE parent_id = ?", parent_id)]
    kid_names = _join(kids) if kids else "their kid"
    parent = db.one("SELECT name, program FROM users WHERE id = ?", parent_id)
    channels.send(
        villager_id,
        f"{parent['name']} ({parent['program']}) would like to add you to their village: classmates they trust "
        f"to hang out with {kid_names} for an hour or so when there's no other childcare. You'll only be asked "
        "when you look free, and you can always say no. Join?",
        [Button(label="Join", action=f"village_accept:{parent_id}", style="primary"),
         Button(label="Not right now", action=f"village_decline:{parent_id}")],
    )


async def _answer_invite(villager_id: str, parent_id: str, yes: bool) -> None:
    changed = db.run("UPDATE village SET status = ? WHERE parent_id = ? AND villager_id = ? AND status = 'pending'",
                     "accepted" if yes else "declined", parent_id, villager_id)
    if not changed:
        return
    if yes:
        channels.send(villager_id, f"You're in {first(parent_id)}'s village. Thank you!")
        onboarding.ask_parent_for_free_times(villager_id)   # parents share their free times when they first join one
        channels.send(parent_id, f"{first(villager_id)} joined your village.")
    else:
        channels.send(villager_id, "No problem. You won't get requests from them.")
        channels.send(parent_id, f"{first(villager_id)} can't join your village right now.")


def set_village(parent_id: str, villager_ids: list[str]) -> None:
    """Save a parent's village in order. New people get an invite; removed people are dropped."""
    villager_ids = [v for v in dict.fromkeys(villager_ids) if v != parent_id]
    existing = {r["villager_id"]: r["status"] for r in db.all_("SELECT * FROM village WHERE parent_id = ?", parent_id)}
    for vid in existing:
        if vid not in villager_ids:
            db.run("DELETE FROM village WHERE parent_id = ? AND villager_id = ?", parent_id, vid)
    for rank, vid in enumerate(villager_ids, start=1):
        if not db.one("SELECT 1 FROM users WHERE id = ?", vid):
            continue
        if vid in existing:
            db.run("UPDATE village SET rank = ? WHERE parent_id = ? AND villager_id = ?", rank, parent_id, vid)
        else:
            db.run("INSERT INTO village VALUES (?,?, 'pending', ?)", parent_id, vid, rank)
            send_invite(parent_id, vid)


def leave_village(villager_id: str, parent_id: str) -> bool:
    """A villager steps out of a parent's village. Open asks from that parent are declined for them;
    a booking they already said yes to stays (they can back out of it separately)."""
    left = db.run("DELETE FROM village WHERE parent_id = ? AND villager_id = ?", parent_id, villager_id)
    if not left:
        return False
    db.log_event(None, "step", f"{first(villager_id)} left {first(parent_id)}'s village.")
    open_asks = db.all_("SELECT a.request_id FROM asks a JOIN requests r ON r.id = a.request_id "
                        "WHERE a.villager_id = ? AND r.parent_id = ? AND a.status IN ('asked','countered') "
                        "AND r.status = 'searching'", villager_id, parent_id)
    for a in open_asks:
        db.run("UPDATE asks SET status = 'declined', answered_at = ? WHERE request_id = ? AND villager_id = ?",
               _real_now(), a["request_id"], villager_id)
        channels.retire_buttons(a["request_id"], villager_id)
    channels.send(parent_id, f"{first(villager_id)} has stepped back from your village. Thanks for understanding! "
                             "You can add other classmates on your Village profile (my Home tab in Slack) anytime.")
    return True


async def after_leave(villager_id: str, parent_id: str) -> None:
    """If leaving emptied a wave, keep the parent's search going."""
    for r in db.all_("SELECT id FROM requests WHERE parent_id = ? AND status = 'searching'", parent_id):
        if _outstanding(r["id"]) == 0:
            await send_wave(r["id"])


PAUSE_FOREVER = "9999-12-31T00:00"


def set_pause(user_id: str, mode: str) -> str | None:
    """Pause for today, for a week, until turned back on ("indefinite"), or resume. Returns paused_until."""
    now = db.now()
    if mode == "resume":
        until = None
    elif mode == "today":
        until = _iso(datetime(now.year, now.month, now.day, tzinfo=TZ) + timedelta(days=1))
    elif mode == "week":
        until = _iso(now + timedelta(days=7))
    else:
        until = PAUSE_FOREVER
    db.run("UPDATE users SET paused_until = ? WHERE id = ?", until, user_id)
    return until


def add_kid(parent_id: str, name: str, age: str, notes: str) -> str:
    """Adding a child makes someone a parent, with their own village."""
    n = db.one("SELECT COUNT(*) AS n FROM kids WHERE parent_id = ?", parent_id)["n"]
    kid_id = f"{parent_id}_kid{n + 1}"
    while db.one("SELECT 1 FROM kids WHERE id = ?", kid_id):
        n += 1
        kid_id = f"{parent_id}_kid{n + 1}"
    db.run("INSERT INTO kids VALUES (?,?,?,?,?)", kid_id, parent_id, name.strip(), str(age), notes.strip())
    db.run("UPDATE users SET is_parent = 1 WHERE id = ?", parent_id)
    return kid_id


def remove_kid(parent_id: str, kid_id: str) -> str | None:
    """Returns why the child can't be removed, or None if they were."""
    if db.one("SELECT 1 FROM requests WHERE kid_id = ? AND status IN ('draft','searching','booked')", kid_id):
        return "There's an open request for them. Cancel it first."
    if db.one("SELECT 1 FROM requests WHERE kid_id = ?", kid_id):
        db.run("UPDATE kids SET notes = '' WHERE id = ? AND parent_id = ?", kid_id, parent_id)
        return "They're on past requests, so I kept their name but cleared their notes."
    db.run("DELETE FROM kids WHERE id = ? AND parent_id = ?", kid_id, parent_id)
    if not db.one("SELECT 1 FROM kids WHERE parent_id = ?", parent_id):
        db.run("UPDATE users SET is_parent = 0 WHERE id = ?", parent_id)
    return None


def welcome_everyone() -> None:
    """After a reset: new people get the first setup question (in the web demo or Slack), set-up demo
    people get a short hello, and pending village invites go out."""
    for row in db.all_("SELECT id, onboarding FROM users ORDER BY is_parent DESC, name"):
        if row["onboarding"] is None:
            onboarding.start(row["id"])
        else:
            onboarding.hello(row["id"])
    for row in db.all_("SELECT parent_id, villager_id FROM village WHERE status = 'pending'"):
        send_invite(row["parent_id"], row["villager_id"])


async def update_availability(user_id: str, text: str) -> ParsedAvailability | None:
    """Save free-text availability and parse it once with the profile agent."""
    text = text.strip()
    parsed = None
    if text:
        parsed = await run_agent(profile_agent, "profile", profile_prompt(text), subject=user_id)
    db.run("UPDATE users SET availability_text = ?, availability_parsed = ? WHERE id = ?",
           text, parsed.model_dump_json() if parsed else None, user_id)
    return parsed


# --- Timers --------------------------------------------------------------------------------


async def tick() -> None:
    """Every 30s: ask the next wave if nobody answered in time; expire past requests."""
    now = db.now()
    cutoff = (datetime.now(TZ) - timedelta(minutes=WAVE_TIMEOUT_MIN)).isoformat(timespec="seconds")
    for req in db.all_("SELECT * FROM requests WHERE status IN ('draft','searching')"):
        if _dt(req["care_end"]) and _dt(req["care_end"]) < now:
            db.run("UPDATE requests SET status = 'expired' WHERE id = ?", req["id"])
            channels.retire_buttons(req["id"])
            continue
        if (req["status"] == "searching" and req["wave_sent_at"] and req["wave_sent_at"] < cutoff
                and _candidates(req["id"])):
            db.log_event(req["id"], "step", f"No answer in {WAVE_TIMEOUT_MIN} min, asking the next people.")
            await send_wave(req["id"])


async def ticker() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await tick()
        except Exception as exc:
            db.log_event(None, "error", f"Timer failed: {type(exc).__name__}: {exc}")
