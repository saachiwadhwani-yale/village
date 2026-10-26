"""Village API (FastAPI).

Run from the repo folder:
    python3 backend/main.py

Serves the API under /api and, if frontend/dist exists, the built React app at /.

Routes:
    GET  /api/users                         everyone, with kids, village, calendars, availability
    GET  /api/inbox                         newest message per person (unread dots)
    GET  /api/users/{id}/messages           that person's chat (+ whether Village is "typing")
    POST /api/users/{id}/messages           they send a message
    POST /api/users/{id}/buttons            they tap a button
    POST /api/users                         web demo: join as a new classmate
    POST /api/users/{id}/open               first time opening Village: start setup
    POST /api/users/{id}/setup/restart      go through the setup questions again
    POST /api/users/{id}/kids, DELETE /api/users/{id}/kids/{kid}   add or remove a kid
    PUT  /api/users/{id}/village            a parent saves their village (ordered)
    DELETE /api/users/{id}/villages/{parent} a villager leaves a parent's village
    PUT  /api/users/{id}/pause              pause or resume getting asked
    POST /api/users/{id}/calendars          connect a calendar link
    DELETE /api/users/{id}/calendars/{cid}  disconnect one
    PUT  /api/users/{id}/availability       save written availability (parsed by the profile agent)
    GET  /api/calendar/{id}.ics             a booking as a calendar file (signed link)
    GET  /api/requests                      care requests with each villager's verdict and status
    GET  /api/events                        the behind-the-scenes trace
    GET  /api/usage                         tokens and cost by agent and by request
    POST /api/reset                         start everyone over from setup
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import calendar_links
import channels
import coordinator
import db
import onboarding
from calendars import CalendarError, normalize_url, validate_link
from config import ROOT
from models import (AddCalendar, AddKid, Ask, ButtonPress, Calendar, CareRequest, Kid, Message, NewUser,
                    ParsedAvailability, SendMessage, SetAvailability, SetPause, SetVillage, TraceEvent, UsageRow, UsageSummary, User,
                    VillageMember)

_tasks: set[asyncio.Task] = set()


def _background(coro) -> None:
    """Run agent work after the request returns; the chat polls for the result."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not db.DB_PATH.exists():
        db.reset(fresh_setup=True)
        coordinator.welcome_everyone()
    db.migrate()
    ticker = asyncio.create_task(coordinator.ticker())
    slack = await _start_slack()
    yield
    ticker.cancel()
    if slack:
        await slack.stop()


async def _start_slack():
    """Connect the Slack bot if both Slack tokens are in .env. Optional: the web app works without it."""
    bot, app_token = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_APP_TOKEN")
    if not (bot and app_token):
        print("Slack: off (add SLACK_BOT_TOKEN and SLACK_APP_TOKEN to .env to turn it on)")
        return None
    if bot.startswith("xapp-") and app_token.startswith("xoxb-"):
        print("Slack: the two tokens look swapped in .env (xoxb- is the bot token); using them the right way round.")
        bot, app_token = app_token, bot
    from channels.slack import SlackChannel

    slack = SlackChannel(bot, app_token)
    try:
        # Bolt retries forever on connection errors; don't let that block the web app from starting.
        await asyncio.wait_for(slack.start(), timeout=20)
        print("Slack: connected. Classmates can DM the Village bot.")
        return slack
    except Exception as exc:
        reason = "timed out" if isinstance(exc, asyncio.TimeoutError) else f"{type(exc).__name__}: {exc}"
        print(f"Slack: couldn't connect ({reason}). The web app still works.")
        try:
            await slack.stop()
        except Exception:
            pass
        return None


app = FastAPI(title="Village", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"],
                   allow_headers=["*"])

SLACK_ONLY = re.compile(r"^/api/users/([^/]+)/")


@app.middleware("http")
async def no_acting_as_slack_users(request, call_next):
    """The web demo plays made-up people. Real people who use Village in Slack can't be read or acted for here."""
    m = SLACK_ONLY.match(request.url.path)
    if m and db.one("SELECT 1 FROM users WHERE id = ? AND slack_id IS NOT NULL", m.group(1)):
        return JSONResponse({"detail": "This person uses Village in Slack, so the web demo can't act for them."},
                            status_code=403)
    return await call_next(request)



def _user_or_404(user_id: str):
    row = db.one("SELECT * FROM users WHERE id = ?", user_id)
    if not row:
        raise HTTPException(404, f"No user {user_id}")
    return row


def _user(row) -> User:
    uid = row["id"]
    return User(
        id=uid, name=row["name"], program=row["program"], color=row["color"], is_parent=bool(row["is_parent"]),
        kids=[Kid(**dict(k)) for k in db.all_("SELECT id, name, age, notes, allergies FROM kids WHERE parent_id = ?", uid)],
        village=[VillageMember(villager_id=v["villager_id"], name=v["name"], status=v["status"], rank=v["rank"])
                 for v in db.all_("SELECT v.*, u.name FROM village v JOIN users u ON u.id = v.villager_id "
                                  "WHERE v.parent_id = ? ORDER BY v.rank", uid)],
        in_villages_of=[r["parent_id"] for r in db.all_(
            "SELECT parent_id FROM village WHERE villager_id = ? AND status = 'accepted'", uid)],
        calendars=[Calendar(id=c["id"], label=c["label"], kind=c["kind"],
                            source=c["source"] if c["kind"] == "file" else _mask(c["source"]))
                   for c in db.all_("SELECT * FROM calendars WHERE user_id = ?", uid)],
        availability_text=row["availability_text"],
        availability_parsed=ParsedAvailability.model_validate_json(row["availability_parsed"])
        if row["availability_parsed"] else None,
        help_count_week=row["help_count_week"], help_count_total=row["help_count_total"],
        paused=bool(row["paused_until"]) and row["paused_until"] > db.now().strftime("%Y-%m-%dT%H:%M"),
        paused_until=("indefinite" if row["paused_until"] == coordinator.PAUSE_FOREVER else row["paused_until"]),
        onboarding=row["onboarding"], on_slack=bool(row["slack_id"]), emergency_contact=row["emergency_contact"],
        invited_to=[r["parent_id"] for r in db.all_(
            "SELECT parent_id FROM village WHERE villager_id = ? AND status = 'pending'", uid)],
    )


def _mask(url: str) -> str:
    """Private calendar links are secrets; only show the host and the last few characters."""
    host = url.split("/")[2] if "//" in url else url
    return f"{host}/…{url[-6:]}"


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/users")
def new_user(body: NewUser) -> User:
    """Web demo: join as a brand-new classmate, to go through setup from the first question."""
    base = "w_" + ("".join(ch for ch in body.name.lower() if ch.isalnum())[:20] or "classmate")
    user_id, n = base, 1
    while db.one("SELECT 1 FROM users WHERE id = ?", user_id):
        n += 1
        user_id = f"{base}{n}"
    colors = ["#c2410c", "#7c3aed", "#0369a1", "#047857", "#be185d", "#a16207", "#0f766e"]
    db.run("INSERT INTO users (id, name, program, color, is_parent) VALUES (?,?,?,?,0)",
           user_id, body.name.strip(), body.program.strip() or "SOM", colors[len(user_id) % len(colors)])
    return _user(_user_or_404(user_id))


@app.post("/api/users/{user_id}/open")
def open_village(user_id: str) -> dict:
    """Someone opened Village for the first time: start setup (like a Slack user opening the app)."""
    _user_or_404(user_id)
    onboarding.start(user_id)
    return {"ok": True}


@app.post("/api/users/{user_id}/setup/restart")
def restart_setup(user_id: str) -> dict:
    _user_or_404(user_id)
    onboarding.start(user_id, restart=True)
    return {"ok": True}


@app.post("/api/users/{user_id}/kids")
def add_kid(user_id: str, body: AddKid) -> User:
    _user_or_404(user_id)
    coordinator.add_kid(user_id, body.name, body.age, body.notes, body.allergies)
    return _user(_user_or_404(user_id))


@app.put("/api/users/{user_id}/kids/{kid_id}")
def update_kid(user_id: str, kid_id: str, body: AddKid) -> User:
    _user_or_404(user_id)
    if not coordinator.update_kid(user_id, kid_id, body.name, body.age, body.notes, body.allergies):
        raise HTTPException(404, "No such kid.")
    return _user(_user_or_404(user_id))


@app.delete("/api/users/{user_id}/kids/{kid_id}")
def remove_kid(user_id: str, kid_id: str) -> User:
    _user_or_404(user_id)
    problem = coordinator.remove_kid(user_id, kid_id)
    if problem and "Cancel it first" in problem:
        raise HTTPException(409, problem)
    return _user(_user_or_404(user_id))


@app.get("/api/users")
def users() -> list[User]:
    """Everyone, but for real Slack users only what's needed to show their name in a village (no kids, notes,
    calendars, or availability)."""
    return [_user(r) if not r["slack_id"] else
            User(id=r["id"], name=r["name"], program=r["program"], color=r["color"], is_parent=bool(r["is_parent"]),
                 onboarding=r["onboarding"], on_slack=True)
            for r in db.all_("SELECT * FROM users ORDER BY is_parent DESC, name")]


@app.get("/api/users/{user_id}/messages")
def messages(user_id: str, after: int = 0) -> dict:
    _user_or_404(user_id)
    rows = db.all_("SELECT * FROM messages WHERE user_id = ? AND id > ? ORDER BY id", user_id, after)
    return {
        "messages": [Message(id=r["id"], user_id=r["user_id"], direction=r["direction"], text=r["text"],
                             buttons=json.loads(r["buttons"]) if r["buttons"] else [],
                             buttons_used=bool(r["buttons_used"]), button_choice=r["button_choice"],
                             request_id=r["request_id"],
                             created_at=r["created_at"]).model_dump() for r in rows],
        "typing": user_id in channels.typing,
    }


@app.get("/api/inbox")
def inbox() -> dict[str, int]:
    """Newest message Village sent each person, so the app can show unread dots."""
    return {r["user_id"]: r["last"] for r in db.all_(
        "SELECT m.user_id, MAX(m.id) AS last FROM messages m JOIN users u ON u.id = m.user_id "
        "WHERE m.direction = 'out' AND u.slack_id IS NULL GROUP BY m.user_id")}


@app.post("/api/users/{user_id}/messages")
async def send_message(user_id: str, body: SendMessage) -> dict:
    _user_or_404(user_id)
    msg_id = channels.store_incoming(user_id, body.text.strip())
    channels.typing.add(user_id)
    _background(coordinator.handle_message(user_id, body.text.strip()))
    return {"id": msg_id}


@app.post("/api/users/{user_id}/buttons")
async def press_button(user_id: str, body: ButtonPress) -> dict:
    _user_or_404(user_id)
    refusal = channels.check_press(user_id, body.action, body.message_id)
    if refusal:
        raise HTTPException(409, refusal)
    channels.typing.add(user_id)
    _background(coordinator.handle_button(user_id, body.action, body.request_id, body.message_id, body.values))
    return {"ok": True}


@app.put("/api/users/{user_id}/village")
def save_village(user_id: str, body: SetVillage) -> User:
    row = _user_or_404(user_id)
    if not row["is_parent"]:
        raise HTTPException(400, "Only parents have a village.")
    coordinator.set_village(user_id, body.villager_ids)
    return _user(row)


@app.delete("/api/users/{user_id}/villages/{parent_id}")
async def leave_village(user_id: str, parent_id: str) -> User:
    _user_or_404(user_id)
    if not coordinator.leave_village(user_id, parent_id):
        raise HTTPException(404, "You're not in that village.")
    _background(coordinator.after_leave(user_id, parent_id))
    return _user(_user_or_404(user_id))


@app.put("/api/users/{user_id}/pause")
def pause(user_id: str, body: SetPause) -> User:
    _user_or_404(user_id)
    coordinator.set_pause(user_id, body.mode)
    return _user(_user_or_404(user_id))


@app.post("/api/users/{user_id}/calendars")
async def add_calendar(user_id: str, body: AddCalendar) -> User:
    row = _user_or_404(user_id)
    try:
        url = normalize_url(body.url)
        await asyncio.to_thread(validate_link, url)
    except CalendarError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        raise HTTPException(400, "That link didn't return a calendar Village can read.")
    db.run("INSERT INTO calendars (user_id, label, source, kind) VALUES (?,?,?, 'url')", user_id, body.label, url)
    onboarding.calendar_connected(user_id, body.label)
    return _user(row)


@app.delete("/api/users/{user_id}/calendars/{cal_id}")
def remove_calendar(user_id: str, cal_id: int) -> User:
    row = _user_or_404(user_id)
    db.run("DELETE FROM calendars WHERE id = ? AND user_id = ?", cal_id, user_id)
    return _user(row)


@app.put("/api/users/{user_id}/emergency")
def set_emergency(user_id: str, body: SendMessage) -> User:
    """A parent's emergency instructions for whoever helps (only shared with a helper who said yes)."""
    _user_or_404(user_id)
    db.run("UPDATE users SET emergency_contact = ? WHERE id = ?", body.text.strip()[:300], user_id)
    return _user(_user_or_404(user_id))


@app.put("/api/users/{user_id}/availability")
async def save_availability(user_id: str, body: SetAvailability) -> User:
    _user_or_404(user_id)
    try:
        parsed = await coordinator.update_availability(user_id, body.text)
        onboarding.availability_saved(user_id, parsed)
    except Exception as exc:
        raise HTTPException(502, f"Couldn't read that availability ({type(exc).__name__}). Try rewording it.")
    return _user(_user_or_404(user_id))


@app.get("/api/calendar/{request_id}.ics")
def booking_ics(request_id: int, u: str, t: str) -> Response:
    """A booking as a calendar file (Apple Calendar / Outlook). Signed: only the parent and helper can fetch it."""
    body = calendar_links.ics_file(request_id, u, t)
    if body is None:
        raise HTTPException(404, "That calendar link isn't valid anymore.")
    return Response(body, media_type="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="village-{request_id}.ics"'})


@app.get("/api/requests")
def requests() -> list[CareRequest]:
    out = []
    for r in db.all_("SELECT r.*, p.name AS parent_name, k.name AS kid_name, h.name AS helper_name, "
                     "s.name AS support_name "
                     "FROM requests r JOIN users p ON p.id = r.parent_id LEFT JOIN kids k ON k.id = r.kid_id "
                     "LEFT JOIN users h ON h.id = r.helper_id LEFT JOIN users s ON s.id = r.support_id WHERE p.slack_id IS NULL ORDER BY r.id DESC"):
        asks = db.all_("SELECT a.*, u.name FROM asks a JOIN users u ON u.id = a.villager_id "
                       "JOIN village v ON v.villager_id = a.villager_id AND v.parent_id = ? "
                       "WHERE a.request_id = ? ORDER BY v.rank", r["parent_id"], r["id"])
        out.append(CareRequest(
            id=r["id"], parent_id=r["parent_id"], parent_name=r["parent_name"],
            kid_name=coordinator._join([k["name"] for k in coordinator._kids(r)]) or r["kid_name"],
            support_name=r["support_name"],
            meeting_start=r["meeting_start"], meeting_minutes=r["meeting_minutes"], care_start=r["care_start"],
            care_end=r["care_end"], purpose=r["purpose"], location=r["location"], status=r["status"],
            helper_name=r["helper_name"], created_at=r["created_at"],
            asks=[Ask(villager_id=a["villager_id"], villager_name=a["name"], wave=a["wave"], verdict=a["verdict"],
                      verdict_reason=a["verdict_reason"], decided_by=a["decided_by"], status=a["status"])
                  for a in asks],
        ))
    return out


@app.get("/api/events")
def events(after: int = 0, request_id: int | None = None) -> list[TraceEvent]:
    sql = "SELECT * FROM events WHERE id > ?"
    args: list = [after]
    if request_id is not None:
        sql += " AND request_id = ?"
        args.append(request_id)
    sql += " AND (request_id IS NULL OR request_id NOT IN (SELECT r.id FROM requests r JOIN users p ON " \
           "p.id = r.parent_id WHERE p.slack_id IS NOT NULL))"           # real Slack parents' requests stay private
    return [TraceEvent(id=e["id"], request_id=e["request_id"], kind=e["kind"], agent=e["agent"], text=e["text"],
                       data=json.loads(e["data"]) if e["data"] else None, created_at=e["created_at"])
            for e in db.all_(sql + " ORDER BY id", *args)]


def _usage_row(agent: str, r) -> UsageRow:
    return UsageRow(agent=agent, runs=r["runs"] or 0, input_tokens=r["i"] or 0, cached_tokens=r["c"] or 0,
                    output_tokens=r["o"] or 0, cost_usd=round(r["cost"] or 0, 6))


_SUMS = ("COUNT(*) AS runs, SUM(input_tokens) AS i, SUM(cached_tokens) AS c, SUM(output_tokens) AS o, "
         "SUM(cost_usd) AS cost")


@app.get("/api/usage")
def usage() -> UsageSummary:
    by_agent = [_usage_row(r["agent"], r) for r in db.all_(f"SELECT agent, {_SUMS} FROM usage GROUP BY agent")]
    total = _usage_row("total", db.one(f"SELECT {_SUMS} FROM usage"))
    by_request = {r["request_id"]: _usage_row("request", r) for r in db.all_(
        f"SELECT request_id, {_SUMS} FROM usage WHERE request_id IS NOT NULL GROUP BY request_id")}
    return UsageSummary(by_agent=by_agent, total=total, by_request=by_request)


@app.post("/api/reset")
async def reset() -> dict:
    """Everyone, including Slack users, starts over from the first setup question."""
    if _tasks:
        raise HTTPException(409, "Village is still working on something. Try again in a few seconds.")
    db.reset(fresh_setup=True)
    coordinator.welcome_everyone()
    channels.typing.clear()
    return {"ok": True}


# --- The built React app (npm run build) -----------------------------------------------

DIST = ROOT / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        file = (DIST / path).resolve()
        if path and file.is_file() and file.is_relative_to(DIST.resolve()):
            return FileResponse(file)  # favicon and other files from public/
        return FileResponse(DIST / "index.html")


if __name__ == "__main__":
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    print(f"Village is running at http://localhost:{port}")
    uvicorn.run("main:app", host="127.0.0.1", port=port, app_dir=str(Path(__file__).resolve().parent))
