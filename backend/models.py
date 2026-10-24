"""Structured types for Village.

The first half are the agents' structured outputs: every agent returns one of
these, so the coordinator gets typed, checkable answers instead of free text.
The second half are the shapes the API sends to the React app.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Weekday = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HHMM = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="24-hour time, HH:MM.")


# --------------------------------------------------------------------------
# Agent outputs
# --------------------------------------------------------------------------


class IntakeResult(BaseModel):
    """What the intake agent understood from a parent's message."""

    kind: Literal["request", "clarify", "cancel", "other"] = Field(
        description=(
            "request: enough detail to look for help. clarify: something essential is "
            "missing or ambiguous. cancel: the parent wants to cancel their open request. "
            "other: not a care request (greeting, thanks, question about the app)."
        )
    )
    kid_id: str | None = Field(default=None, description="Id of the child from list_my_kids.")
    meeting_date: str | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Date of the commitment, YYYY-MM-DD."
    )
    meeting_start: str | None = Field(
        default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="When the parent's own commitment starts (not the handoff), 24-hour HH:MM.",
    )
    meeting_minutes: int | None = Field(
        default=None, ge=1, le=600, description="Length of the parent's commitment in minutes."
    )
    purpose: str | None = Field(
        default=None, max_length=60, description="Short label, e.g. 'coffee chat', 'Accounting class'."
    )
    location: str | None = Field(
        default=None, max_length=60, description="Where the handoff should happen, if the parent said."
    )
    reply: str | None = Field(
        default=None, max_length=400,
        description="For clarify/other: the one short message to send back to the parent.",
    )


class WeeklySlot(BaseModel):
    day: Weekday
    start: str = HHMM
    end: str = HHMM
    kind: Literal["free", "busy"]


class ParsedAvailability(BaseModel):
    """A villager's free-text availability turned into weekly time slots."""

    slots: list[WeeklySlot] = Field(
        default_factory=list, description="Recurring weekly windows stated in the text."
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="Conditions that can't be expressed as a fixed weekly slot, in plain words.",
    )
    summary: str = Field(max_length=200, description="One-line plain summary for the profile page.")


class AvailabilityVerdict(BaseModel):
    """One villager agent's call on whether to ask this villager."""

    verdict: Literal["available", "maybe", "unavailable"]
    confidence: Literal["low", "medium", "high"]
    reason: str = Field(
        max_length=200,
        description="One short sentence. Never mention what an event is, only that time is busy.",
    )


class ReplyIntent(BaseModel):
    """What a villager meant by a free-text reply to a care ask."""

    intent: Literal["accept", "decline", "counter", "question", "unclear"] = Field(
        description=(
            "accept: clearly says yes to the whole time asked. decline: no. counter: offers a "
            "different or partial time. question: asks something before deciding. unclear: anything else."
        )
    )
    counter_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    counter_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    question: str | None = Field(default=None, max_length=300, description="The villager's question, restated.")


# --------------------------------------------------------------------------
# Calendar data (never includes event titles, descriptions, or attendees)
# --------------------------------------------------------------------------


class BusyBlock(BaseModel):
    start: datetime
    end: datetime
    source: str = Field(description="Which calendar or profile rule this came from, e.g. 'Yale Outlook'.")
    tentative: bool = False


# --------------------------------------------------------------------------
# API shapes
# --------------------------------------------------------------------------


class Button(BaseModel):
    """A control under a message. Each channel draws it its own way (Slack Block Kit, the web client)."""

    label: str
    action: str
    request_id: int | None = None
    style: Literal["primary", "secondary", "danger"] = "secondary"
    kind: Literal["button", "people", "form", "link"] = Field(
        default="button",
        description="button: a plain tap. people: pick classmates, sent back as `values`. "
                    "form: opens a form (see `form`) instead of acting right away. link: opens `url`.",
    )
    form: Literal["kid", "calendar", "availability"] | None = None
    url: str | None = None


class Message(BaseModel):
    id: int
    user_id: str
    direction: Literal["in", "out"]
    text: str
    buttons: list[Button] = []
    buttons_used: bool = False
    button_choice: str | None = None
    request_id: int | None = None
    created_at: str


class Kid(BaseModel):
    id: str
    name: str
    age: int
    notes: str = ""


class Calendar(BaseModel):
    id: int
    label: str
    source: str
    kind: Literal["url", "file"]


class VillageMember(BaseModel):
    villager_id: str
    name: str
    status: Literal["pending", "accepted", "declined"]
    rank: int


class User(BaseModel):
    id: str
    name: str
    program: str
    color: str
    is_parent: bool
    kids: list[Kid] = []
    village: list[VillageMember] = []
    in_villages_of: list[str] = []
    calendars: list[Calendar] = []
    availability_text: str = ""
    availability_parsed: ParsedAvailability | None = None
    help_count_week: int = 0
    help_count_total: int = 0
    paused: bool = False
    onboarding: str | None = None
    on_slack: bool = False
    paused_until: str | None = Field(default=None, description="Local ISO time, or 'indefinite'.")


class SendMessage(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class ButtonPress(BaseModel):
    action: str
    request_id: int | None = None
    message_id: int | None = None
    values: list[str] = Field(default_factory=list, description="Picked user ids, for people pickers.")


class AddKid(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    age: int = Field(ge=0, le=17)
    notes: str = Field(default="", max_length=500)


class NewUser(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    program: str = Field(default="SOM", max_length=40)


class AddCalendar(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=8, max_length=1000)


class SetAvailability(BaseModel):
    text: str = Field(max_length=1500)


class SetPause(BaseModel):
    mode: Literal["today", "week", "indefinite", "resume"]


class SetVillage(BaseModel):
    villager_ids: list[str]


class Ask(BaseModel):
    villager_id: str
    villager_name: str
    wave: int | None
    verdict: str | None
    verdict_reason: str | None
    decided_by: str | None
    status: str


class CareRequest(BaseModel):
    id: int
    parent_id: str
    parent_name: str
    kid_name: str | None
    meeting_start: str | None
    meeting_minutes: int | None
    care_start: str | None
    care_end: str | None
    purpose: str | None
    location: str | None
    status: str
    helper_name: str | None
    created_at: str
    asks: list[Ask] = []


class TraceEvent(BaseModel):
    id: int
    request_id: int | None
    kind: str
    agent: str | None
    text: str
    data: dict | None
    created_at: str


class UsageRow(BaseModel):
    agent: str
    runs: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float


class UsageSummary(BaseModel):
    by_agent: list[UsageRow]
    total: UsageRow
    by_request: dict[int, UsageRow]
