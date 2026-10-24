"""Messaging channels.

Every message Village sends goes through `send()`. It is always stored in the
`messages` table, which the web app shows as each person's chat. Other
channels (Slack, see channels/slack.py) register an adapter with `register()`
and deliver their own users' copies; incoming messages from any channel go to
coordinator.handle_message / handle_button, so the agents don't care where a
message came from.
"""

from __future__ import annotations

import json
from typing import Protocol

import db
from models import Button


class Adapter(Protocol):
    def send(self, user_id: str, text: str, buttons: list[Button], request_id: int | None, msg_id: int) -> None:
        """Deliver a copy of a stored message (only for people on this channel)."""

    def retire(self, msg_ids: list[int]) -> None:
        """Remove the buttons from messages whose question has been answered."""


_adapters: list[Adapter] = []

# People Village is currently working for, so the chat can show "typing…".
typing: set[str] = set()


def register(adapter: Adapter) -> None:
    _adapters.append(adapter)


def store_incoming(user_id: str, text: str) -> int:
    return db.run("INSERT INTO messages (user_id, direction, text, created_at) VALUES (?, 'in', ?, ?)",
                  user_id, text, db.utc_now_iso())


def send(user_id: str, text: str, buttons: list[Button] | None = None,
         request_id: int | None = None) -> int:
    buttons = buttons or []
    if any(b.action.startswith("setup_") for b in buttons):
        retire_setup_questions(user_id)
    msg_id = db.run(
        "INSERT INTO messages (user_id, direction, text, buttons, request_id, created_at)"
        " VALUES (?, 'out', ?, ?, ?, ?)",
        user_id, text, json.dumps([b.model_dump() for b in buttons]) if buttons else None,
        request_id, db.utc_now_iso(),
    )
    for adapter in _adapters:
        adapter.send(user_id, text, buttons, request_id, msg_id)
    return msg_id


def check_press(user_id: str, action: str, message_id: int | None) -> str | None:
    """Why a button press must be refused, or None. Buttons only work for the person they were
    sent to, only once, and only with the actions that message actually offered."""
    if message_id is None:
        return None
    msg = db.one("SELECT buttons, buttons_used FROM messages WHERE id = ? AND user_id = ?", message_id, user_id)
    if not msg:
        return "That button isn't yours."
    if msg["buttons_used"]:
        return "Those buttons were already used."
    if action not in {b["action"] for b in json.loads(msg["buttons"] or "[]")}:
        return "That button isn't on this message."
    return None


def mark_pressed(user_id: str, message_id: int, action: str) -> None:
    msg = db.one("SELECT buttons FROM messages WHERE id = ? AND user_id = ?", message_id, user_id)
    label = next((b["label"] for b in json.loads(msg["buttons"] or "[]") if b["action"] == action), None) if msg else None
    db.run("UPDATE messages SET buttons_used = 1, button_choice = ? WHERE id = ? AND user_id = ?",
           label, message_id, user_id)
    for adapter in _adapters:
        adapter.retire([message_id])


def refresh(message_id: int) -> None:
    """A message's stored state changed (e.g. what was picked); let channels redraw it."""
    for adapter in _adapters:
        adapter.retire([message_id])


def retire_setup_questions(user_id: str) -> None:
    """Close earlier setup questions once someone moves on, so their old buttons can't be reused."""
    ids = [r["id"] for r in db.all_("SELECT id FROM messages WHERE user_id = ? AND request_id IS NULL AND "
                                    "buttons LIKE '%setup_%' AND buttons_used = 0", user_id)]
    if ids:
        db.run(f"UPDATE messages SET buttons_used = 1 WHERE id IN ({','.join('?' * len(ids))})", *ids)
        for adapter in _adapters:
            adapter.retire(ids)


def retire_buttons(request_id: int, user_id: str | None = None) -> None:
    """Grey out old buttons once their question has been answered."""
    sql = "SELECT id FROM messages WHERE request_id = ? AND buttons IS NOT NULL AND buttons_used = 0"
    args: list = [request_id]
    if user_id:
        sql += " AND user_id = ?"
        args.append(user_id)
    ids = [r["id"] for r in db.all_(sql, *args)]
    if not ids:
        return
    db.run(f"UPDATE messages SET buttons_used = 1 WHERE id IN ({','.join('?' * len(ids))})", *ids)
    for adapter in _adapters:
        adapter.retire(ids)
