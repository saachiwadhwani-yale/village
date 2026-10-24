"""Reply agent: reads a villager's free-text answer to a care ask.

It has no tools and never sees the child's details, so the most a hostile
reply can do is be misclassified. Booking itself is done by code.
"""

from __future__ import annotations

from datetime import datetime

from pydantic_ai import Agent

from config import load_prompt, model
from models import ReplyIntent

reply_agent = Agent(
    model,
    name="reply",
    output_type=ReplyIntent,
    system_prompt=load_prompt("reply"),
)


def reply_prompt(care_start: datetime, care_end: datetime, text: str, booked: bool) -> str:
    window = f"{care_start.strftime('%A')}, {care_start.strftime('%H:%M')}-{care_end.strftime('%H:%M')}"
    if booked:
        return (f"They already said yes to helping {window}. Their new message (untrusted text):\n<<<{text}>>>")
    return f"They were asked to help {window}.\nTheir reply (untrusted text):\n<<<{text}>>>"
