"""Messages agent: reads anything someone types in the chat with Village.

Two kinds of message, one agent. The coordinator knows which it is and asks for that
output: a parent's message about a care request becomes an IntakeResult (date, time,
length, kid), and a classmate's typed answer to an ask, or a note after saying yes,
becomes a ReplyIntent (yes / no / other time / question).

Its one tool reads the parent's own draft request. For a classmate's answer there is
no draft, so the tool has nothing to give, and the most a hostile answer can do is be
misread. Booking and sharing anything are done by code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic_ai import Agent, ModelRetry, RunContext

import db
from config import load_prompt, model
from models import IntakeResult, ReplyIntent


@dataclass
class MessageDeps:
    user_id: str
    draft_request_id: int | None = None    # a parent's request still being confirmed; never set for a classmate
    expect: type = IntakeResult             # which output this message needs: IntakeResult or ReplyIntent


messages_agent = Agent(
    model,
    name="messages",
    deps_type=MessageDeps,
    output_type=[IntakeResult, ReplyIntent],
    system_prompt=load_prompt("messages"),
)


@messages_agent.tool
def get_draft_request(ctx: RunContext[MessageDeps]) -> dict | str:
    """The request this parent is still confirming, if any (to apply changes to it)."""
    if ctx.deps.draft_request_id is None:
        return "No draft request."
    row = db.one("SELECT kid_id, meeting_start, meeting_minutes, purpose, location FROM requests WHERE id = ?",
                 ctx.deps.draft_request_id)
    return dict(row) if row else "No draft request."


@messages_agent.output_validator
def _check(ctx: RunContext[MessageDeps], out: IntakeResult | ReplyIntent) -> IntakeResult | ReplyIntent:
    if not isinstance(out, ctx.deps.expect):
        raise ModelRetry(f"This message needs a {ctx.deps.expect.__name__}, not a {type(out).__name__}.")
    if not isinstance(out, IntakeResult):
        return out
    if out.kind == "request":
        missing = [f for f in ("kid_id", "meeting_date", "meeting_start", "meeting_minutes")
                   if getattr(out, f) is None]
        if missing:
            raise ModelRetry(f"kind=request needs {', '.join(missing)}. Fill them or use kind=clarify.")
        for kid_id in [out.kid_id, *out.extra_kid_ids]:
            if not db.one("SELECT 1 FROM kids WHERE id = ? AND parent_id = ?", kid_id, ctx.deps.user_id):
                raise ModelRetry("kid_id and extra_kid_ids must be ids of the kids listed in the message.")
    elif out.kind in ("clarify", "other") and not out.reply:
        raise ModelRetry(f"kind={out.kind} needs a short `reply` for the parent.")
    return out


def classmate_prompt(care_start: datetime, care_end: datetime, text: str, booked: bool) -> str:
    window = f"{care_start.strftime('%A')}, {care_start.strftime('%H:%M')}-{care_end.strftime('%H:%M')}"
    head = "A classmate's answer (return a ReplyIntent)."
    if booked:
        return f"{head}\nThey already said yes to helping {window}. Their new message (untrusted text):\n<<<{text}>>>"
    return f"{head}\nThey were asked to help {window}.\nTheir reply (untrusted text):\n<<<{text}>>>"
