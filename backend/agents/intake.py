"""Intake agent: turns a parent's chat message into a structured care request."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent, ModelRetry, RunContext

import db
from config import load_prompt, model
from models import IntakeResult


@dataclass
class IntakeDeps:
    parent_id: str
    draft_request_id: int | None = None


intake_agent = Agent(
    model,
    name="intake",
    deps_type=IntakeDeps,
    output_type=IntakeResult,
    system_prompt=load_prompt("intake"),
)


@intake_agent.tool
def get_draft_request(ctx: RunContext[IntakeDeps]) -> dict | str:
    """The request this parent is still confirming, if any (to apply changes to it)."""
    if ctx.deps.draft_request_id is None:
        return "No draft request."
    row = db.one("SELECT kid_id, meeting_start, meeting_minutes, purpose, location FROM requests WHERE id = ?",
                 ctx.deps.draft_request_id)
    return dict(row) if row else "No draft request."


@intake_agent.output_validator
def _check(ctx: RunContext[IntakeDeps], out: IntakeResult) -> IntakeResult:
    if out.kind == "request":
        missing = [f for f in ("kid_id", "meeting_date", "meeting_start", "meeting_minutes")
                   if getattr(out, f) is None]
        if missing:
            raise ModelRetry(f"kind=request needs {', '.join(missing)}. Fill them or use kind=clarify.")
        kid = db.one("SELECT 1 FROM kids WHERE id = ? AND parent_id = ?", out.kid_id, ctx.deps.parent_id)
        if not kid:
            raise ModelRetry("kid_id must be the id of one of the kids listed in the message.")
    elif out.kind in ("clarify", "other") and not out.reply:
        raise ModelRetry(f"kind={out.kind} needs a short `reply` for the parent.")
    return out
