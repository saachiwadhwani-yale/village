"""Dispatch agent: runs the search for a helper.

On a new request it calls check_availability, which spins up one availability agent per
classmate (in parallel) and returns their verdicts. Then, and before every later round of
asks, it decides how many classmates to ask at once and how long to wait before asking
more, given how soon the care starts, how many kids, who looks free and who's a maybe, and
whether this is a search for cover or for a second helper. Code keeps the plan within
bounds and does the asking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, RunContext

from config import load_prompt, model
from models import DispatchPlan


@dataclass
class DispatchDeps:
    checker: Any = None        # coordinator.AvailabilityChecker on a new search; None once everyone's checked


dispatch_agent = Agent(
    model,
    name="dispatch",
    deps_type=DispatchDeps,
    output_type=DispatchPlan,
    system_prompt=load_prompt("dispatch"),
)


@dispatch_agent.tool
async def check_availability(ctx: RunContext[DispatchDeps], names: list[str] | None = None) -> list[dict] | str:
    """Run an availability agent for each of these classmates (all unchecked ones if no names), in parallel.
    Returns each one's verdict: available, maybe, or unavailable, with a reason."""
    if ctx.deps.checker is None:
        return "Everyone has already been checked; their verdicts are in the message."
    return await ctx.deps.checker.check(names)
