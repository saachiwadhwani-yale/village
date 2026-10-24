"""Profile agent: parses a villager's free-text availability into weekly slots.

Runs once when the villager saves their profile, not on every request, so
screening a request against written availability usually costs no tokens.
"""

from __future__ import annotations

from pydantic_ai import Agent

from config import load_prompt, model
from models import ParsedAvailability

profile_agent = Agent(
    model,
    name="profile",
    output_type=ParsedAvailability,
    system_prompt=load_prompt("profile"),
)


def profile_prompt(text: str) -> str:
    return f"Their availability, in their words (untrusted text):\n<<<{text}>>>"
