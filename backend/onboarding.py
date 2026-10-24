"""Setup, as a conversation: are you a parent, your kid, who you'd trust, your calendar.

Channel-agnostic: it only sends messages through `channels.send`, with buttons,
a people picker, and form buttons that each channel draws its own way (Slack
Block Kit, or the web demo client). Someone's step is kept in users.onboarding:

    NULL -> role -> kid -> kid_notes -> village -> calendar -> done
                \\-> calendar_helper (not a parent) -------------/
"""

from __future__ import annotations

import re

import channels
import db
from models import Button, ParsedAvailability

NOT_A_NAME = {"she's", "he's", "they're", "she is", "he is", "my kid", "my son", "my daughter", "age"}


def step(user_id: str) -> str | None:
    row = db.one("SELECT onboarding FROM users WHERE id = ?", user_id)
    return row["onboarding"] if row else None


def _set(user_id: str, value: str) -> None:
    db.run("UPDATE users SET onboarding = ? WHERE id = ?", value, user_id)


def _first(user_id: str) -> str:
    return db.one("SELECT name FROM users WHERE id = ?", user_id)["name"].split()[0]


def _first_kid(user_id: str) -> str | None:
    kid = db.one("SELECT name FROM kids WHERE parent_id = ? ORDER BY rowid LIMIT 1", user_id)
    return kid["name"] if kid else None


def has_free_times(user_id: str) -> bool:
    """A connected calendar or written availability: Village needs one to know when to ask someone."""
    row = db.one("SELECT availability_text FROM users WHERE id = ?", user_id)
    return bool(db.one("SELECT 1 FROM calendars WHERE user_id = ?", user_id) or (row and row["availability_text"]))


# --- the questions -----------------------------------------------------------------------


def start(user_id: str, restart: bool = False) -> None:
    """Begin setup the first time we meet someone (or again, if they ask)."""
    if step(user_id) is not None and not restart:
        return
    _set(user_id, "role")
    if restart:
        text = "Let's go through setup again. *Are you a parent?*"
    else:
        text = (f"👋 Hi {_first(user_id)}! I'm Village. I help SOM parents find a classmate they trust to hang out "
                "with their kid for an hour or so, and I only ask people when their calendar says they're free.\n\n"
                "A few quick questions to set you up. *Are you a parent?*")
    channels.send(user_id, text, [Button(label="Yes, I'm a parent", action="setup_role:parent", style="primary"),
                                  Button(label="No, I'm here to help", action="setup_role:helper")])


def _ask_kid(user_id: str) -> None:
    _set(user_id, "kid")
    kids = db.all_("SELECT name, age FROM kids WHERE parent_id = ?", user_id)
    if kids:
        listed = ", ".join(f"{k['name']} ({k['age']})" for k in kids)
        channels.send(user_id, f"I have {listed}. Want to add another kid? Type their first name and age, like "
                               "*Maya, 3*.", [Button(label="No, that's everyone", action="setup_kids_done")])
    else:
        channels.send(user_id, "What's your kid's first name and age? Just type it, like *Maya, 3*. "
                               "(Got more than one? You can add the others after.)")


def _ask_notes(user_id: str, kid_name: str) -> None:
    _set(user_id, "kid_notes")
    channels.send(user_id, f"Anything a helper should know about {kid_name}? Allergies, favorite things, what's in "
                           "the bag. 🔒 I only share this with the classmate who says yes, after they say yes.",
                  [Button(label="Skip", action="setup_skip_notes")])


def _ask_village(user_id: str) -> None:
    _set(user_id, "village")
    channels.send(user_id, f"*Who would you trust with {_first_kid(user_id) or 'your kid'}?* Pick a few classmates. "
                           "They each get an invite and can say yes or no, and I only ask people when they look free.",
                  [Button(label="Pick classmates…", action="setup_village", kind="people", style="primary"),
                   Button(label="Skip for now", action="setup_skip_village")])


def _ask_calendar(user_id: str, as_parent: bool) -> None:
    # The step remembers which answer they gave, so the wording follows their answer, not whatever
    # happens to be on file (someone can say "just helping" and still have a kid saved).
    _set(user_id, "calendar" if as_parent else "calendar_helper")
    why = ("You might help other parents too, and I only ask people when they look free." if as_parent
           else "When a classmate needs a hand, I only ask you if your calendar says you're free.")
    # No skip: Village needs a calendar or a description, or it can't tell when to ask someone.
    channels.send(user_id, f"*Last step: connect your calendar*, or tell me when you're usually free. {why} I only "
                           "see busy times, never event names, and you can connect several.",
                  [Button(label="Connect a calendar", action="setup_calendar", kind="form", form="calendar",
                          style="primary"),
                   Button(label="Describe my availability instead", action="setup_describe", kind="form",
                          form="availability")])


def _finish(user_id: str) -> None:
    as_parent = step(user_id) != "calendar_helper"
    _set(user_id, "done")
    channels.retire_setup_questions(user_id)
    if as_parent:
        text = ("✅ *You're all set!* Whenever you need a hand, just message me here, like \"I have a coffee chat at "
                f"1 for 30 min and need someone for {_first_kid(user_id) or 'my kid'}.\" I'll confirm the time with "
                "you before asking anyone.")
    else:
        text = ("✅ *You're all set!* When a parent adds you to their village, I'll ask you here, and only when you "
                "look free. You can always say no.")
    channels.send(user_id, f"{text}\n\nTo change anything later, open my Home tab.")


# --- answers ------------------------------------------------------------------------------


def handle_text(user_id: str, text: str) -> bool:
    """A typed answer during setup. Returns True if the message was a setup answer."""
    current = step(user_id)
    if current == "kid":
        m = re.match(r"^\s*([A-Za-zÀ-ÿ' .-]{1,40}?)\s*[,:-]?\s*(?:age\s*)?(\d{1,2})\b", text)
        if not m or int(m.group(2)) > 17 or m.group(1).strip().lower() in NOT_A_NAME:
            channels.send(user_id, "Sorry, I didn't catch that. Try their first name and age, like *Maya, 3*.")
            return True
        import coordinator

        name = " ".join(w[:1].upper() + w[1:] for w in m.group(1).split())
        coordinator.add_kid(user_id, name, int(m.group(2)), "")
        _ask_notes(user_id, name)
        return True
    if current == "kid_notes":
        kid = db.one("SELECT id, name FROM kids WHERE parent_id = ? ORDER BY rowid DESC LIMIT 1", user_id)
        db.run("UPDATE kids SET notes = ? WHERE id = ?", text[:500], kid["id"])
        channels.send(user_id, f"Got it, saved for {kid['name']}. 🔒")
        _ask_village(user_id)
        return True
    return False


def handle_action(user_id: str, verb: str, arg: str, values: list[str], message_id: int | None) -> None:
    """A tap on a setup button or a pick from the people picker."""
    import coordinator

    if verb == "setup_role":
        if arg == "parent":
            _ask_kid(user_id)
        else:
            _ask_calendar(user_id, as_parent=False)
    elif verb in ("setup_kids_done", "setup_skip_notes"):
        _ask_village(user_id)
    elif verb == "setup_village":
        current = [r["villager_id"] for r in
                   db.all_("SELECT villager_id FROM village WHERE parent_id = ? ORDER BY rank", user_id)]
        added = [v for v in dict.fromkeys(values)
                 if v != user_id and v not in current and db.one("SELECT 1 FROM users WHERE id = ?", v)]
        if added:
            coordinator.set_village(user_id, current + added)
            if message_id:
                names = ", ".join(_first(v) for v in added)
                db.run("UPDATE messages SET button_choice = ? WHERE id = ?", f"Invited {names}", message_id)
                channels.refresh(message_id)
        _ask_calendar(user_id, as_parent=True)
    elif verb == "setup_skip_village":
        _ask_calendar(user_id, as_parent=True)
    elif verb == "setup_calendar_done" and has_free_times(user_id):
        _finish(user_id)


def calendar_connected(user_id: str, label: str) -> None:
    """Called whenever someone connects a calendar; continues setup if they're on that step."""
    if step(user_id) in ("calendar", "calendar_helper"):
        channels.send(user_id, f"✅ Connected *{label}*. Have another calendar, like a personal one? You can also add "
                               "a note about your availability, like \"I'm free most evenings, except during consulting recruiting events\" (optional).",
                      [Button(label="Connect another", action="setup_calendar", kind="form", form="calendar"),
                       Button(label="Add a note (optional)", action="setup_describe", kind="form",
                              form="availability"),
                       Button(label="That's all", action="setup_calendar_done", style="primary")])


def availability_saved(user_id: str, parsed: ParsedAvailability | None) -> None:
    """Called whenever someone saves written availability; finishes setup if they're on that step."""
    if step(user_id) in ("calendar", "calendar_helper"):
        if not has_free_times(user_id):
            channels.send(user_id, "I need either a calendar or a few words about when you're usually free, so I "
                                   "only ask you when it works.",
                          [Button(label="Connect a calendar", action="setup_calendar", kind="form", form="calendar",
                                  style="primary"),
                           Button(label="Describe my availability", action="setup_describe", kind="form",
                                  form="availability")])
            return
        channels.send(user_id, "Got it, saved.")
        _finish(user_id)
