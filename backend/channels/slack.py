"""Slack channel: classmates use Village by DMing a Slack bot.

Runs inside the FastAPI app over Socket Mode (no public URL needed), and only
when SLACK_BOT_TOKEN and SLACK_APP_TOKEN are set. The web app keeps working
either way.

* Messages: a Slack user's DMs go to coordinator.handle_message; Village's
  replies to them are posted back as DMs, with buttons as Block Kit buttons.
* Setup: the shared conversation in onboarding.py (are you a parent, your kid,
  who you'd trust, your calendar). It starts when someone joins the workspace
  or first opens the app. Its people picker and form buttons become a Slack
  multi-user select and Slack modals here.
* Home tab: a short summary of someone's profile, with a menu to edit each part.
* People: anyone Village meets in Slack gets a profile from their Slack name.
  Demo people (no Slack account) keep using the web phones, so a Slack parent
  can test with demo villagers and answer for them in the web app.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import zlib
from typing import Any

import certifi
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

import channels
import db
import onboarding
from calendars import CalendarError, normalize_url, validate_link
from models import Button, age_label

log = logging.getLogger("village.slack")

COLORS = ["#c2410c", "#7c3aed", "#0369a1", "#047857", "#be185d", "#a16207", "#0f766e", "#4d7c0f", "#b91c1c"]
CALENDAR_HELP = (
    "*Google:* Settings → your calendar → Integrate calendar → _Secret address in iCal format_\n"
    "*Outlook / Yale:* Settings → Calendar → Shared calendars → Publish a calendar → "
    "_Can view when I'm busy_ → copy the ICS link\n"
    "*Apple:* right-click the calendar → Share → Public calendar → copy the link"
)


def _esc(text: str) -> str:
    """Slack mrkdwn treats &, <, > as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mrkdwn(text: str) -> str:
    """Escape a message for Slack, keeping the one kind of link Village writes itself (to its Home tab)."""
    return re.sub(r"&lt;(slack://app\?[^|\s]*)\|([^&<>]*)&gt;",
                  lambda m: f"<{m.group(1).replace('&amp;', '&')}|{m.group(2)}>", _esc(text))


def _clean_name(raw: str) -> str:
    """Slack names can be handles ("saachi.wadhwani") with invisible characters; make them readable."""
    name = re.sub(r"[​-‏⁠﻿]", "", raw).strip()
    if name and " " not in name and re.search(r"[._]", name):
        name = " ".join(part.capitalize() for part in re.split(r"[._]+", name) if part)
    if name == name.lower():
        name = name.title()
    return name or "Classmate"


def _is_bot(slack_id: str) -> bool:
    return slack_id in ("USLACKBOT", "")


def _coordinator():
    import coordinator  # imported late: coordinator imports channels

    return coordinator


class SlackChannel:
    def __init__(self, bot_token: str, app_token: str) -> None:
        # Use certifi's certificates: python.org installs on macOS don't trust any by default.
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self.app = AsyncApp(client=AsyncWebClient(token=bot_token, ssl=ssl_ctx))
        self.handler = AsyncSocketModeHandler(self.app, app_token)
        self.client = self.app.client
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue | None = None
        self.worker: asyncio.Task | None = None
        self.dm_channels: dict[str, str] = {}
        self.team_id: str | None = None
        self.app_id: str | None = None
        self._home_pending: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._register_listeners()

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.worker = asyncio.create_task(self._work())
        auth = await self.client.auth_test()
        self.team_id = auth.get("team_id")
        try:
            self.app_id = (await self.client.bots_info(bot=auth["bot_id"]))["bot"]["app_id"]
        except Exception:
            self.app_id = None  # messages then say "Home tab" without a link
        await self.handler.connect_async()
        channels.register(self)
        log.info("Slack connected")

    async def stop(self) -> None:
        if self.worker:
            self.worker.cancel()
        await self.handler.close_async()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- channel adapter (called from coordinator code, possibly from a worker thread) ---------

    def send(self, user_id: str, text: str, buttons: list[Button], request_id: int | None, msg_id: int) -> None:
        slack_id = self._slack_id(user_id)
        if slack_id:
            self._enqueue(("post", msg_id))
            self._refresh_home(slack_id)

    def retire(self, msg_ids: list[int]) -> None:
        for msg_id in msg_ids:
            self._enqueue(("update", msg_id))

    def _refresh_home(self, slack_id: str) -> None:
        """Something changed for them (an invite answered, setup progress), so refresh their Home tab."""
        if slack_id not in self._home_pending:
            self._home_pending.add(slack_id)
            self._enqueue(("home", slack_id))

    def _enqueue(self, job: tuple) -> None:
        if self.loop and self.queue is not None:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, job)

    async def _work(self) -> None:
        """Post and update Slack messages one at a time, so they arrive in order."""
        while True:
            kind, ref = await self.queue.get()
            try:
                if kind == "post":
                    await self._post(ref)
                elif kind == "update":
                    await self._update(ref)
                else:
                    self._home_pending.discard(ref)
                    await self.publish_home(ref)
            except Exception:
                log.exception("Slack %s failed for %s", kind, ref)

    @staticmethod
    def _slack_id(user_id: str) -> str | None:
        row = db.one("SELECT slack_id FROM users WHERE id = ?", user_id)
        return row["slack_id"] if row else None

    async def _dm(self, slack_id: str) -> str:
        if slack_id not in self.dm_channels:
            resp = await self.client.conversations_open(users=slack_id)
            self.dm_channels[slack_id] = resp["channel"]["id"]
        return self.dm_channels[slack_id]

    def _blocks(self, msg) -> list[dict]:
        """A stored message as Block Kit: text, then its buttons, people picker, or form buttons."""
        text = _mrkdwn(msg["text"])
        if self.team_id and self.app_id:
            text = text.replace("my Home tab", f"my <slack://app?team={self.team_id}&id={self.app_id}&tab=home|Home tab>")
        blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        buttons = json.loads(msg["buttons"] or "[]")
        if buttons and not msg["buttons_used"]:
            elements = []
            for i, b in enumerate(buttons):
                el = {"type": "button", "text": {"type": "plain_text", "text": b["label"][:75]}}
                if b.get("kind") == "people":
                    # A button that opens Slack's people picker in a pop-up, so it sits in the same row.
                    el["action_id"] = f"village_pick_{i}"
                    el["value"] = json.dumps({"a": b["action"], "m": msg["id"]})
                elif b.get("kind") == "link":
                    el["action_id"] = f"village_link_{i}"
                    el["url"] = b["url"]
                elif b.get("kind") == "form":
                    el["action_id"] = f"village_form_{i}"
                    el["value"] = json.dumps({"f": b["form"], "m": msg["id"]})
                else:
                    el["action_id"] = f"village_btn_{i}"
                    el["value"] = json.dumps({"a": b["action"], "r": b["request_id"], "m": msg["id"]})
                if b["style"] in ("primary", "danger"):
                    el["style"] = b["style"]
                elements.append(el)
            if elements:
                blocks.append({"type": "actions", "elements": elements})
        elif buttons and msg["button_choice"]:
            blocks.append(_context(f"✓ {_esc(msg['button_choice'])}"))
        return blocks

    async def _post(self, msg_id: int) -> None:
        msg = db.one("SELECT * FROM messages WHERE id = ?", msg_id)
        slack_id = self._slack_id(msg["user_id"])
        if not slack_id:
            return
        channel = await self._dm(slack_id)
        resp = await self.client.chat_postMessage(channel=channel, text=msg["text"], blocks=self._blocks(msg))
        db.run("UPDATE messages SET slack_channel = ?, slack_ts = ? WHERE id = ?", channel, resp["ts"], msg_id)

    async def _update(self, msg_id: int) -> None:
        msg = db.one("SELECT * FROM messages WHERE id = ?", msg_id)
        if not msg or not msg["slack_ts"]:
            return
        await self.client.chat_update(channel=msg["slack_channel"], ts=msg["slack_ts"], text=msg["text"],
                                      blocks=self._blocks(msg))

    # --- people ----------------------------------------------------------------------------

    async def user_for(self, slack_id: str) -> tuple[str, bool]:
        """The Village user for a Slack user, creating one on first contact. Returns (id, is_new)."""
        row = db.one("SELECT id FROM users WHERE slack_id = ?", slack_id)
        if row:
            return row["id"], False
        info = (await self.client.users_info(user=slack_id))["user"]
        profile = info.get("profile", {})
        name = _clean_name(profile.get("real_name") or profile.get("display_name") or info.get("name") or "")
        program = (profile.get("title") or "SOM").strip()[:40]
        user_id = "s_" + slack_id.lower()
        color = COLORS[zlib.crc32(slack_id.encode()) % len(COLORS)]
        db.run("INSERT OR IGNORE INTO users (id, name, program, color, is_parent, slack_id) VALUES (?,?,?,?,0,?)",
               user_id, name, program, color, slack_id)
        db.log_event(None, "step", f"{name} joined Village through Slack.")
        return user_id, True

    async def _is_bot_account(self, slack_id: str) -> bool:
        """Bots and apps can be picked in Slack's people picker; they can't watch a kid."""
        if db.one("SELECT 1 FROM users WHERE slack_id = ?", slack_id):
            return False
        info = (await self.client.users_info(user=slack_id))["user"]
        return bool(info.get("is_bot") or info.get("is_app_user") or info.get("deleted"))

    async def greet(self, slack_id: str) -> None:
        """First contact (joining the workspace or opening the app): start setup in Messages."""
        user_id, _ = await self.user_for(slack_id)
        onboarding.start(user_id)

    # --- listeners -------------------------------------------------------------------------

    def _register_listeners(self) -> None:
        app = self.app

        @app.event("message")
        async def on_message(event):
            if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
                return
            text = (event.get("text") or "").strip()[:1000]
            if not text:
                return
            if _is_bot(event.get("user", "")):
                return
            user_id, _ = await self.user_for(event["user"])
            channels.store_incoming(user_id, text)
            channels.typing.add(user_id)
            self._spawn(_coordinator().handle_message(user_id, text))

        @app.event("app_home_opened")
        async def on_home(event):
            await self.greet(event["user"])
            if event.get("tab") == "home":
                await self.publish_home(event["user"])

        @app.event("team_join")
        async def on_team_join(event):
            member = event.get("user", {})
            if not member.get("is_bot") and member.get("id"):
                await self.greet(member["id"])

        @app.action(re.compile(r"^village_btn_\d+$"))
        async def on_button(ack, body, action, respond):
            await ack()
            value = json.loads(action["value"])
            user_id, _ = await self.user_for(body["user"]["id"])
            refusal = channels.check_press(user_id, value["a"], value["m"])
            if refusal:
                await respond(text=f"{refusal} Nothing changed.", response_type="ephemeral", replace_original=False)
                return
            channels.typing.add(user_id)
            self._spawn(_coordinator().handle_button(user_id, value["a"], value["r"], value["m"]))

        @app.action(re.compile(r"^village_pick_\d+$"))
        async def on_pick_people(ack, body, action):
            await ack()
            await self.client.views_open(trigger_id=body["trigger_id"], view=_add_people_modal(action["value"]))

        @app.action(re.compile(r"^village_link_\d+$"))
        async def on_link(ack):
            await ack()  # Slack opens the link itself; nothing to do here

        @app.action(re.compile(r"^village_form_\d+$"))
        async def on_form_button(ack, body, action):
            await ack()
            value = json.loads(action["value"])
            user_id, _ = await self.user_for(body["user"]["id"])
            await self._open_form(body["trigger_id"], value["f"], user_id)

        @app.action(re.compile(r"^home_"))
        async def on_home_action(ack, body, action):
            await ack()
            await self._home_action(body, action)

        @app.view("add_kid")
        async def on_add_kid(ack, body, view):
            values = view["state"]["values"]
            name = values["name"]["v"]["value"].strip()
            age_text = values["age"]["v"]["value"].strip()
            age = age_label(age_text)
            if age is None:
                await ack(response_action="errors", errors={"age": "Enter an age in years, like 4, or months, "
                                                                    "like 6 months."})
                return
            await ack()
            user_id, _ = await self.user_for(body["user"]["id"])
            notes = values["notes"]["v"].get("value") or ""
            allergies = (values["allergies"]["v"].get("value") or "").strip()
            if allergies.lower() in ("none", "no", "n/a", "nothing"):
                allergies = "None"
            if view.get("private_metadata"):          # editing an existing kid
                _coordinator().update_kid(user_id, view["private_metadata"], name, age, notes, allergies)
            else:
                _coordinator().add_kid(user_id, name, age, notes, allergies)
            await self.publish_home(body["user"]["id"])

        @app.view("add_villagers")
        async def on_add_villagers(ack, body, view):
            await ack()
            slack_id = body["user"]["id"]
            user_id, _ = await self.user_for(slack_id)
            picked = []
            for sid in view["state"]["values"]["people"]["v"].get("selected_users", []):
                if sid == slack_id or _is_bot(sid) or await self._is_bot_account(sid):
                    continue
                picked.append((await self.user_for(sid))[0])
            if view.get("private_metadata"):     # opened from a setup question in the chat
                meta = json.loads(view["private_metadata"])
                if picked and channels.check_press(user_id, meta["a"], meta["m"]) is None:
                    self._spawn(_coordinator().handle_button(user_id, meta["a"], None, meta["m"], values=picked))
                return
            current = [r["villager_id"] for r in
                       db.all_("SELECT villager_id FROM village WHERE parent_id = ? ORDER BY rank", user_id)]
            _coordinator().set_village(user_id, current + [v for v in picked if v not in current])
            await self.publish_home(slack_id)

        @app.view("add_calendar")
        async def on_add_calendar(ack, body, view):
            values = view["state"]["values"]
            label = (values["label"]["v"].get("value") or "Calendar").strip()[:40]
            url = values["url"]["v"]["value"].strip()
            try:
                url = normalize_url(url)
                await asyncio.wait_for(asyncio.to_thread(validate_link, url), timeout=2.5)
            except CalendarError as exc:
                await ack(response_action="errors", errors={"url": str(exc)})
                return
            except asyncio.TimeoutError:
                await ack(response_action="errors", errors={"url": "That link took too long to load. Try again?"})
                return
            except Exception:
                await ack(response_action="errors", errors={"url": "That link didn't return a calendar I can read."})
                return
            await ack()
            slack_id = body["user"]["id"]
            user_id, _ = await self.user_for(slack_id)
            db.run("INSERT INTO calendars (user_id, label, source, kind) VALUES (?,?,?, 'url')", user_id, label, url)
            onboarding.calendar_connected(user_id, label)
            await self.publish_home(slack_id)

        @app.view("edit_availability")
        async def on_availability(ack, body, view):
            await ack()
            text = view["state"]["values"]["text"]["v"].get("value") or ""
            slack_id = body["user"]["id"]
            user_id, _ = await self.user_for(slack_id)

            async def parse():
                try:
                    parsed = await _coordinator().update_availability(user_id, text[:1500])
                    onboarding.availability_saved(user_id, parsed)
                    await self.publish_home(slack_id)
                except Exception:
                    await self.publish_home(slack_id, note="⚠️ I couldn't read that availability. Try rewording it?")

            self._spawn(parse())

    async def _open_form(self, trigger_id: str, form: str, user_id: str) -> None:
        if form == "kid":
            view = _kid_modal()
        elif form == "calendar":
            view = _calendar_modal()
        else:
            row = db.one("SELECT availability_text FROM users WHERE id = ?", user_id)
            view = _availability_modal(row["availability_text"])
        await self.client.views_open(trigger_id=trigger_id, view=view)

    # --- Home tab: a short summary with a menu per line ------------------------------------

    async def _home_action(self, body: dict, action: dict) -> None:
        coordinator = _coordinator()
        slack_id = body["user"]["id"]
        user_id, _ = await self.user_for(slack_id)
        action_id: str = action["action_id"]
        value = action.get("value") or action.get("selected_option", {}).get("value", "")
        verb, _, arg = value.partition(":")
        note = None

        if action_id == "home_redo_setup":
            onboarding.start(user_id, restart=True)
            note = "Setup started again in the Messages tab."
        elif verb in ("add_kid", "add_calendar", "edit_availability"):
            await self._open_form(body["trigger_id"], {"add_kid": "kid", "add_calendar": "calendar",
                                                       "edit_availability": "availability"}[verb], user_id)
            return
        elif verb == "edit_kid":
            kid = db.one("SELECT * FROM kids WHERE id = ? AND parent_id = ?", arg, user_id)
            if kid:
                await self.client.views_open(trigger_id=body["trigger_id"], view=_kid_modal(dict(kid)))
            return
        elif verb == "remove_kid":
            note = coordinator.remove_kid(user_id, arg)
        elif verb == "add_villagers":
            await self.client.views_open(trigger_id=body["trigger_id"], view=_add_people_modal())
            return
        elif verb == "remove_villager":
            current = [r["villager_id"] for r in
                       db.all_("SELECT villager_id FROM village WHERE parent_id = ? ORDER BY rank", user_id)]
            coordinator.set_village(user_id, [v for v in current if v != arg])
        elif verb == "remove_calendar":
            db.run("DELETE FROM calendars WHERE id = ? AND user_id = ?", int(arg), user_id)
        elif action_id == "home_pause_menu":
            coordinator.set_pause(user_id, value)
        elif action_id.startswith("home_leave"):
            if coordinator.leave_village(user_id, value):
                self._spawn(coordinator.after_leave(user_id, value))
        elif action_id == "home_add_demo_villagers":
            current = [r["villager_id"] for r in
                       db.all_("SELECT villager_id FROM village WHERE parent_id = ? ORDER BY rank", user_id)]
            picked = [o["value"] for o in action.get("selected_options", [])
                      if db.one("SELECT 1 FROM users WHERE id = ? AND slack_id IS NULL", o["value"])]
            added = [v for v in picked if v not in current]
            coordinator.set_village(user_id, current + added)
            if added:
                note = "Invited. Accept for them in the web demo (localhost:8000)."
        await self.publish_home(slack_id, note=note)

    async def publish_home(self, slack_id: str, note: str | None = None) -> None:
        user_id, _ = await self.user_for(slack_id)
        await self.client.views_publish(user_id=slack_id, view={"type": "home", "blocks": _home_blocks(user_id, note)})


# --- Block Kit builders ----------------------------------------------------------------------


def _section(text: str, accessory: dict | None = None) -> dict:
    block: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    if accessory:
        block["accessory"] = accessory
    return block


def _button(text: str, action_id: str, value: str = "", style: str | None = None,
            confirm: dict | None = None) -> dict:
    b: dict[str, Any] = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id,
                         "value": value or action_id}
    if style:
        b["style"] = style
    if confirm:
        b["confirm"] = confirm
    return b


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _menu(action_id: str, options: list[tuple[str, str]]) -> dict:
    """A small "⋯" menu on the right of a summary line (Slack allows up to 5 options)."""
    return {"type": "overflow", "action_id": action_id,
            "options": [{"text": {"type": "plain_text", "text": t[:75]}, "value": v} for t, v in options[:5]]}


def _home_blocks(user_id: str, note: str | None) -> list[dict]:
    coordinator = _coordinator()
    u = db.one("SELECT * FROM users WHERE id = ?", user_id)
    now = db.now().strftime("%Y-%m-%dT%H:%M")
    kids = db.all_("SELECT * FROM kids WHERE parent_id = ? ORDER BY rowid", user_id)
    members = db.all_("SELECT v.*, u.name, u.paused_until FROM village v JOIN users u ON u.id = v.villager_id "
                      "WHERE v.parent_id = ? ORDER BY v.rank", user_id)
    cals = db.all_("SELECT * FROM calendars WHERE user_id = ?", user_id)
    in_villages = db.all_("SELECT v.parent_id, u.name FROM village v JOIN users u ON u.id = v.parent_id "
                          "WHERE v.villager_id = ? AND v.status = 'accepted'", user_id)

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "🏡 Your Village profile"}},
        _context("To ask for help, just message me in the *Messages* tab."),
    ]
    if note:
        blocks.append(_section(f"*{_esc(note)}*"))
    if u["onboarding"] not in onboarding.DONE:
        blocks.append(_section("👋 *Finish setting up in the Messages tab.* I'm asking you a few quick questions there.",
                               _button("Start over", "home_redo_setup")))
    elif not onboarding.has_free_times(user_id) and (not u["is_parent"] or in_villages):
        blocks.append(_section("⚠️ *Connect a calendar or describe when you're usually free*, so I know when to ask "
                               "you.", _button("Connect a calendar", "home_add_cal_warn", "add_calendar")))
    blocks.append({"type": "divider"})

    if kids:
        blocks.append(_section("*Kids*", _button("Add a kid", "home_add_kid", "add_kid")))
        for k in kids:
            notes = "  ·  🔒 allergies and notes saved" if k["notes"] or k["allergies"] else ""
            blocks.append(_section(f"{_esc(k['name'])}, {k['age']}{notes}",
                                   _menu(f"home_kid_{k['id']}", [(f"Edit {k['name']}", f"edit_kid:{k['id']}"),
                                                                  (f"Remove {k['name']}", f"remove_kid:{k['id']}")])))

        blocks += [{"type": "divider"},
                   _section("*Your village*", _button("Add people", "home_add_people", "add_villagers")),
                   _context("I only ask people who look free, and spread asks around so no one is asked too often.")]
        labels = {"accepted": "✅ Joined", "pending": "⏳ Invited", "declined": "✖️ Said not right now"}
        for m in members:
            status = labels[m["status"]]
            if m["status"] == "accepted" and m["paused_until"] and m["paused_until"] > now:
                status = "⏸ Paused"
            blocks.append(_section(f"{_esc(m['name'])}  ·  {status}",
                                   _menu(f"home_villager_{m['villager_id']}",
                                         [("Remove from village", f"remove_villager:{m['villager_id']}")])))
        if not members:
            blocks.append(_context("No one yet. Add classmates you'd trust with your kid."))

    if blocks[-1]["type"] != "divider":
        blocks.append({"type": "divider"})
    blocks += [_section("*Calendars*", _button("Connect a calendar", "home_add_cal", "add_calendar")),
               _context("I only see busy times, never event names.")]
    for c in cals:
        blocks.append(_section(_esc(c["label"]), _menu(f"home_cal_{c['id']}",
                                                       [("Disconnect", f"remove_calendar:{c['id']}")])))
    if not cals:
        blocks.append(_context("None connected."))

    blocks.append({"type": "divider"})
    if u["availability_text"]:
        avail = _esc(u["availability_text"])   # their exact words, not the model's summary
    else:
        avail = "_Nothing written. Optional, e.g. \"free Tue/Thu after 1\"._"
    blocks.append(_section(f"*Availability in your words*\n{avail}",
                           _button("Edit" if u["availability_text"] else "Write", "home_edit_avail",
                                   "edit_availability")))
    blocks.append({"type": "divider"})

    paused = bool(u["paused_until"]) and u["paused_until"] > now
    if u["paused_until"] == coordinator.PAUSE_FOREVER:
        status = "⏸ Paused until you turn it back on"
    elif paused:
        status = (f"⏸ Paused until {coordinator.day_label(coordinator._dt(u['paused_until']))} at "
                  f"{coordinator.hm(coordinator._dt(u['paused_until']))}")
    else:
        status = "🟢 Taking requests"
    options = ([("Start taking requests", "resume")] if paused else []) + [
        ("Pause for today", "today"), ("Pause for a week", "week"), ("Pause until I turn it back on", "indefinite")]
    helped = f"helped {u['help_count_week']} this week, {u['help_count_total']} all-time"
    blocks.append(_section(f"*{'Helping other parents' if kids else 'Helping parents'}*\n{status}  ·  {helped}", {
        "type": "static_select", "action_id": "home_pause_menu",
        "placeholder": {"type": "plain_text", "text": "Change…"},
        "options": [{"text": {"type": "plain_text", "text": t}, "value": v} for t, v in options]}))
    for v in in_villages:
        parent_first = v["name"].split()[0]
        blocks.append(_section(f"In *{_esc(v['name'])}*'s village", _button(
            "Leave", f"home_leave_{v['parent_id']}", v["parent_id"],
            confirm=_confirm(f"Leave {parent_first}'s village?",
                             f"You won't get their requests anymore, and {parent_first} will be told you've stepped "
                             "back. A time you already said yes to stays booked.", "Leave"))))

    if u["onboarding"] in onboarding.DONE:
        blocks.append({"type": "actions", "elements": [_button("Redo setup questions", "home_redo_setup")]})

    # Testing with demo people (they live in the web app, so Slack's people picker can't show them)
    if kids:
        in_village = {m["villager_id"] for m in members}
        demo = [d for d in db.all_("SELECT id, name FROM users WHERE slack_id IS NULL ORDER BY name")
                if d["id"] not in in_village]
        if demo:
            blocks += [{"type": "divider"},
                       _section("🧪 *Testing only:* add demo classmates. They live in the web app "
                                "(localhost:8000), where you answer for them.", {
                                    "type": "multi_static_select", "action_id": "home_add_demo_villagers",
                                    "placeholder": {"type": "plain_text", "text": "Add demo people…"},
                                    "options": [{"text": {"type": "plain_text", "text": d["name"][:75]},
                                                 "value": d["id"]} for d in demo[:100]]})]
    return blocks


def _confirm(title: str, text: str, yes: str) -> dict:
    return {"title": {"type": "plain_text", "text": title[:100]}, "text": {"type": "mrkdwn", "text": text},
            "confirm": {"type": "plain_text", "text": yes}, "deny": {"type": "plain_text", "text": "Cancel"},
            "style": "danger"}


def _input(block_id: str, label: str, element: dict, optional: bool = False, hint: str | None = None) -> dict:
    block = {"type": "input", "block_id": block_id, "label": {"type": "plain_text", "text": label},
             "element": {**element, "action_id": "v"}, "optional": optional}
    if hint:
        block["hint"] = {"type": "plain_text", "text": hint}
    return block


def _modal(callback_id: str, title: str, blocks: list[dict], submit: str = "Save", metadata: str = "") -> dict:
    return {"type": "modal", "callback_id": callback_id, "title": {"type": "plain_text", "text": title},
            "submit": {"type": "plain_text", "text": submit}, "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": blocks, "private_metadata": metadata}


def _kid_modal(kid: dict | None = None) -> dict:
    """Add a kid, or edit one (kid given: fields filled in, its id kept in the modal's metadata)."""
    def filled(element: dict, key: str) -> dict:
        return {**element, "initial_value": str(kid[key])} if kid and kid[key] not in (None, "") else element
    return _modal("add_kid", f"Edit {kid['name']}"[:24] if kid else "Add a kid", [
        _input("name", "First name", filled({"type": "plain_text_input", "max_length": 40}, "name")),
        _input("age", "Age", filled({"type": "plain_text_input", "max_length": 12,
                                     "placeholder": {"type": "plain_text", "text": "e.g. 4 or 6 months"}}, "age")),
        _input("allergies", "Allergies or medical needs",
               filled({"type": "plain_text_input", "max_length": 300,
                       "placeholder": {"type": "plain_text", "text": "e.g. Peanut allergy, EpiPen in backpack. Or: none"}},
                      "allergies"),
               hint="Only shared with the classmate who says yes, after they say yes."),
        _input("notes", "General notes", filled({"type": "plain_text_input", "multiline": True, "max_length": 500},
                                                "notes"),
               optional=True, hint="Favorite things, naps, what's in the bag."),
    ], metadata=kid["id"] if kid else "")


def _add_people_modal(metadata: str = "") -> dict:
    return _modal("add_villagers", "Your village", [
        _input("people", "Who do you want in your village?",
               {"type": "multi_users_select", "placeholder": {"type": "plain_text", "text": "Pick classmates…"}},
               hint="Each person gets a message asking if they want to join, and can say yes or no."),
    ], submit="Invite", metadata=metadata)


def _calendar_modal(metadata: str = "") -> dict:
    return _modal("add_calendar", "Connect a calendar", [
        _input("label", "Label", {"type": "plain_text_input", "max_length": 40,
                                  "placeholder": {"type": "plain_text", "text": "e.g. Yale Outlook"}}, optional=True),
        _input("url", "Private iCal link", {"type": "plain_text_input",
                                            "placeholder": {"type": "plain_text", "text": "https://… or webcal://…"}}),
        _context(CALENDAR_HELP),
    ], submit="Connect", metadata=metadata)


def _availability_modal(current: str, metadata: str = "") -> dict:
    element = {"type": "plain_text_input", "multiline": True, "max_length": 1500,
               "placeholder": {"type": "plain_text", "text": "e.g. Usually free Tue/Thu afternoons after 1. Never Fridays."}}
    if current:
        element["initial_value"] = current
    return _modal("edit_availability", "Your availability", [
        _input("text", "When are you usually free?", element, optional=True,
               hint="I turn this into weekly time slots once, when you save."),
    ], metadata=metadata)
