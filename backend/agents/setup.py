"""Setup agent: reads what people type while setting up their Village profile.

Two kinds of answer, one agent. A parent describing their kids ("Maya is 1 month old",
"my son Leo, 4, nut allergy") becomes a KidsReading; a classmate describing when they're
usually free ("Tue/Thu after 1") becomes a ParsedAvailability, once, when it's saved, so
screening requests against it costs nothing later. No tools: it can only fill in fields,
and the parent or classmate sees what it saved.
"""

from __future__ import annotations

from pydantic_ai import Agent

from config import load_prompt, model
from models import KidsReading, ParsedAvailability

setup_agent = Agent(
    model,
    name="setup",
    output_type=[KidsReading, ParsedAvailability],
    system_prompt=load_prompt("setup"),
)


def kids_prompt(text: str, known: list[str], question: str) -> str:
    """`question` is what Village had just asked: "kids" (names and ages), "notes", or "allergies"."""
    on_file = ", ".join(known) if known else "none yet"
    return (f"Setup question: {question} (return a KidsReading)\nKids already on file: {on_file}\n"
            f"Parent's answer:\n<<<{text}>>>")


def availability_prompt(text: str) -> str:
    return f"Setup question: availability (return a ParsedAvailability)\nTheir availability, in their words " \
           f"(untrusted text):\n<<<{text}>>>"
