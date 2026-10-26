"""Setup, as a conversation: are you a parent, your kids, your village, your calendar.

Channel-agnostic: it only sends messages through `channels.send`, with buttons,
a people picker, and form buttons that each channel draws its own way (Slack
Block Kit, or the web demo client). Someone's step is kept in users.onboarding:

    NULL -> role -> kid -> [kid_notes:<ids> | kid_medical:<ids>] -> village -> done
    (at most one of kid_notes / kid_medical, depending on what the first message already covered)
                \\-> calendar_helper (not a parent) -> done

Parents aren't asked when they're free until they join someone else's village
(step calendar_joined, which counts as done). Helpers are asked right away, since
that's the only thing Village needs from them.
"""

from __future__ import annotations

import json
import re

import channels
import db
from agents.setup import kids_prompt, setup_agent
from models import Button, KidsReading, ParsedAvailability, age_label
from runner import run_agent



SETTINGS_HELP = ("To change your calendars, written availability, kids, emergency contact, or village later, or to "
                 "pause for a while, open my Home tab: click Village in the sidebar, then Home at the top.")
DONE = ("done", "calendar_joined")   # setup finished; calendar_joined is a parent asked for free times later


def step(user_id: str) -> str | None:
    row = db.one("SELECT onboarding FROM users WHERE id = ?", user_id)
    return row["onboarding"] if row else None


def _set(user_id: str, value: str) -> None:
    db.run("UPDATE users SET onboarding = ? WHERE id = ?", value, user_id)


def _first(user_id: str) -> str:
    return db.one("SELECT name FROM users WHERE id = ?", user_id)["name"].split()[0]


def _kid_names(user_id: str) -> str:
    names = [k["name"] for k in db.all_("SELECT name FROM kids WHERE parent_id = ? ORDER BY rowid", user_id)]
    if not names:
        return "your kid"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _in_a_village(user_id: str) -> bool:
    return bool(db.one("SELECT 1 FROM village WHERE villager_id = ? AND status = 'accepted'", user_id))


def _first_kid(user_id: str) -> str | None:
    kid = db.one("SELECT name FROM kids WHERE parent_id = ? ORDER BY rowid LIMIT 1", user_id)
    return kid["name"] if kid else None


def has_free_times(user_id: str) -> bool:
    """A connected calendar or written availability: Village needs one to know when to ask someone."""
    row = db.one("SELECT availability_text FROM users WHERE id = ?", user_id)
    return bool(db.one("SELECT 1 FROM calendars WHERE user_id = ?", user_id) or (row and row["availability_text"]))


# --- demo answers ---------------------------------------------------------------------------
# The made-up people in data/seed go through setup like anyone else after Reset demo. Their
# calendar files can't be connected by link, so they're one tap away.


def _demo(user_id: str) -> dict | None:
    return next((u for u in db.seed()["users"] if u["id"] == user_id), None)


def _demo_calendar_button(user_id: str) -> list[Button]:
    me = _demo(user_id)
    if not me or not me.get("calendars") or db.one("SELECT 1 FROM calendars WHERE user_id = ?", user_id):
        return []
    n = len(me["calendars"])
    return [Button(label="Use my demo calendar" + ("s" if n > 1 else "") + " (demo only)", action="setup_demo_calendar")]


def _connect_demo_calendars(user_id: str, already: bool = False) -> None:
    """Connect a demo person's calendar files, and their written note if they have one."""
    me = _demo(user_id)
    for c in [] if already else me.get("calendars", []):
        db.run("INSERT INTO calendars (user_id, label, source, kind) VALUES (?,?,?, 'file')", user_id, c["label"],
               c["file"])
    labels = " and ".join(c["label"] for c in me.get("calendars", []))
    text = f"✅ Connected your demo calendar{'s' if len(me.get('calendars', [])) > 1 else ''}: *{labels}*."
    if me.get("availability_text"):
        parsed = me.get("availability_parsed")
        db.run("UPDATE users SET availability_text = ?, availability_parsed = ? WHERE id = ?",
               me["availability_text"], json.dumps(parsed) if parsed else None, user_id)
        text += f" Also saved your note: \"{me['availability_text']}\""
    channels.send(user_id, text)


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
                "with their kid for an hour or so, and I only ask people who are free.\n\n"
                "A few quick questions to set you up. *Are you a parent?*")
    channels.send(user_id, text, [Button(label="Yes, I'm a parent", action="setup_role:parent", style="primary"),
                                  Button(label="No, I'm here to help", action="setup_role:helper")])


def _answered(user_id: str, choice: str) -> None:
    """Mark the newest question with buttons as answered, as if the person had tapped `choice`."""
    row = db.one("SELECT id FROM messages WHERE user_id = ? AND direction = 'out' AND buttons IS NOT NULL "
                 "ORDER BY id DESC LIMIT 1", user_id)
    db.run("UPDATE messages SET buttons_used = 1, button_choice = ? WHERE id = ?", choice, row["id"])


def replay_setup(user_id: str) -> None:
    """For a demo person who starts already set up: write the setup conversation they would have had, using
    their profile from data/seed, so their chat shows how they got here. No model calls."""
    me = _demo(user_id)
    _set(user_id, None)
    start(user_id)
    if me["is_parent"]:
        _answered(user_id, "Yes, I'm a parent")
        channels.send(user_id, KIDS_QUESTION)
        kids = db.all_("SELECT * FROM kids WHERE parent_id = ? ORDER BY rowid", user_id)
        channels.store_incoming(user_id, "; ".join(
            f"{k['name']}, {k['age']} - {k['allergies']} {k['notes']}".strip() for k in kids))
        _summary(user_id)
        channels.send(user_id, EMERGENCY_QUESTION)
        channels.store_incoming(user_id, db.one("SELECT emergency_contact FROM users WHERE id = ?",
                                                user_id)["emergency_contact"] or "-")
        _ask_village(user_id)
        invited = [r["name"].split()[0] for r in db.all_(
            "SELECT u.name FROM village v JOIN users u ON u.id = v.villager_id WHERE v.parent_id = ? "
            "ORDER BY v.rank", user_id)]
        if invited:
            _answered(user_id, f"Invited {_join_and(invited)}")
    else:
        _answered(user_id, "No, I'm here to help")
        _ask_calendar(user_id, as_parent=False)
        _answered(user_id, "Use my demo calendar" + ("s" if len(me.get("calendars", [])) > 1 else "") + " (demo only)")
        _connect_demo_calendars(user_id, already=True)
    _finish(user_id)


def _join_and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


KIDS_QUESTION = "What are your kids' first names and ages? Just type them, like *Maya, 3*."


def _ask_kid(user_id: str) -> None:
    _set(user_id, "kid")
    kids = db.all_("SELECT name, age FROM kids WHERE parent_id = ?", user_id)
    if kids:
        listed = ", ".join(f"{k['name']} ({k['age']})" for k in kids)
        channels.send(user_id, f"I have {listed}. Want to add another kid? Type their first name and age, like "
                               "*Maya, 3*.", [Button(label="No, that's everyone", action="setup_kids_done")])
    else:
        channels.send(user_id, KIDS_QUESTION)


NO_RE = re.compile(r"^\s*(?:no(?:pe)?|none|nothing|n/?a|not really|nah)\b", re.I)
NONE = "None"


async def read_kids(user_id: str, text: str, question: str, known: list[str]) -> KidsReading | None:
    """The kids agent's reading of a setup answer, or None if it couldn't run."""
    try:
        return await run_agent(setup_agent, "setup", kids_prompt(text[:1500], known, question),
                               output_type=KidsReading, subject=user_id)
    except Exception:
        return None


def _add_to_kid(kid_id: str, notes: str = "", allergies: str = "") -> None:
    old = db.one("SELECT notes, allergies FROM kids WHERE id = ?", kid_id)
    db.run("UPDATE kids SET notes = ?, allergies = ? WHERE id = ?", f"{old['notes']} {notes}".strip()[:500],
           f"{old['allergies']} {allergies}".strip()[:300], kid_id)


def _apply(kids: list, reading: KidsReading, question: str) -> None:
    """Add what the agent read to the kids on file, matched by first name."""
    by_name = {k.name.strip().lower(): k for k in reading.kids}
    for row in kids:
        info = by_name.get(row["name"].lower())
        if info is None:
            continue
        allergies = info.allergies or (NONE if question == "allergies" else "")
        _add_to_kid(row["id"], notes=info.notes, allergies=allergies)
        if info.age and not row["age"] and age_label(info.age):
            db.run("UPDATE kids SET age = ? WHERE id = ?", age_label(info.age), row["id"])


def _summary(user_id: str) -> None:
    """What Village saved about each kid, so the parent can check it."""
    blocks = []
    for k in db.all_("SELECT * FROM kids WHERE parent_id = ? ORDER BY rowid", user_id):
        blocks.append(f"*Name:* {k['name']}\n*Age:* {k['age'] or 'Not given'}\n*Allergies:* {k['allergies'] or 'None mentioned'}\n"
                      f"*General notes:* {k['notes'] or 'None'}")
    channels.send(user_id, "Here's what I saved 🔒 I only share it with the classmate who says yes, after they say "
                           "yes.\n\n" + "\n\n".join(blocks) + "\n\nYou can edit this anytime on my Home tab.")


def _ask_about_kids(user_id: str) -> None:
    """At most one follow-up, depending on what the parent already said: nothing about a kid -> ask for anything
    important, especially allergies; notes but no allergies -> ask only about allergies; all covered -> no question."""
    kids = db.all_("SELECT id, name, age, notes, allergies FROM kids WHERE parent_id = ? ORDER BY rowid", user_id)
    ageless = [k for k in kids if not k["age"]]
    if ageless:                                    # an age that wasn't given or was unclear: ask, never guess
        _set(user_id, "kid_age:" + ",".join(k["id"] for k in ageless))
        names = _join_and([k["name"] for k in ageless])
        verb = "is" if len(ageless) == 1 else "are"
        channels.send(user_id, f"How old {verb} {names}? Years, or months for a baby, like *4* or *6 months*.")
        return
    needing = [k for k in kids if not k["allergies"]]
    if not needing:
        _summary(user_id)
        _ask_emergency(user_id)
        return
    names = _join_and([k["name"] for k in needing])
    many = len(needing) > 1
    if any(not k["notes"] for k in needing):
        _set(user_id, "kid_notes:" + ",".join(k["id"] for k in needing))
        text = (f"Anything important a helper should know about {names}? *Especially allergies or medical needs*, "
                "plus anything else that helps, like favorite things or what's in the bag."
                + (" You can cover all of them in one message." if many else ""))
    else:
        _set(user_id, "kid_medical:" + ",".join(k["id"] for k in needing))
        who = f"does {names}" if not many else f"do {names.replace(' and ', ' or ')}"
        text = f"Thanks! Last thing: {who} have any allergies or medical needs a helper should know about?"
    channels.send(user_id, f"{text} 🔒 I only share this with the classmate who says yes, after they say yes.")


def _waiting_kids(user_id: str) -> list[str]:
    """The kids a notes or allergy question is waiting on (kept in the step: kid_notes:<ids>, kid_medical:<ids>)."""
    current = step(user_id) or ""
    _, sep, ids = current.partition(":")
    return [k for k in ids.split(",") if k] if sep and current.startswith(("kid_age", "kid_notes", "kid_medical")) else []


EMERGENCY_QUESTION = ("*In an emergency, how should a helper reach you?* Share a phone number, and anything else "
                      "they should know (another person to call, a doctor, what to do first). 🔒 Only the classmate "
                      "who says yes gets this.")


def _ask_emergency(user_id: str) -> None:
    _set(user_id, "emergency")
    channels.send(user_id, EMERGENCY_QUESTION)


def _ask_village(user_id: str) -> None:
    _set(user_id, "village")
    channels.send(user_id, "*Who do you want in your village?* When you need someone to watch "
                           f"{_kid_names(user_id)}, these classmates get a message and can say yes or no. I only ask "
                           "the ones who are free, based on their calendar or the availability they've shared.",
                  [Button(label="Pick classmates…", action="setup_village", kind="people", style="primary"),
                   Button(label="Skip for now", action="setup_skip_village")])


def _ask_calendar(user_id: str, as_parent: bool) -> None:
    """Helpers during setup; parents once they're in someone else's village (see ask_parent_for_free_times)."""
    # The step remembers which answer they gave, so the wording follows their answer, not whatever
    # happens to be on file (someone can say "just helping" and still have a kid saved).
    _set(user_id, "calendar_joined" if as_parent else "calendar_helper")
    if as_parent:
        lead = ("*Connect your calendar*, or tell me when you're usually free. Now that you're in a classmate's "
                "village, I only ask you when you look free.")
    else:
        lead = ("*Last step: connect your calendar*, or tell me when you're usually free. When a classmate needs a "
                "hand, I only ask you if you look free.")
    # No skip: Village needs a calendar or a description, or it can't tell when to ask someone.
    channels.send(user_id, f"{lead} I only see busy times, never event names, and you can connect several.",
                  _demo_calendar_button(user_id) +
                  [Button(label="Connect a calendar", action="setup_calendar", kind="form", form="calendar",
                          style="primary"),
                   Button(label="Describe my availability instead", action="setup_describe", kind="form",
                          form="availability")])


def ask_parent_for_free_times(user_id: str) -> None:
    """A parent just joined someone's village: now Village needs to know when they're free."""
    if step(user_id) == "done" and not has_free_times(user_id):
        _ask_calendar(user_id, as_parent=True)


def _after_village(user_id: str) -> None:
    """Parents finish here, unless they're already in someone's village and haven't said when they're free."""
    if _in_a_village(user_id) and not has_free_times(user_id):
        _ask_calendar(user_id, as_parent=True)
    else:
        _finish(user_id)


def _finish(user_id: str) -> None:
    current = step(user_id)
    _set(user_id, "done")
    channels.retire_setup_questions(user_id)
    if current == "calendar_joined" and _first_kid(user_id) is None:
        text = "✅ *Thanks!* I'll only ask you when you look free, and you can always say no."
    elif current == "calendar_joined":
        text = ("✅ *Thanks!* I'll only ask you to help when you look free. And whenever you need a hand with "
                f"{_kid_names(user_id)}, just message me here.")
    elif current != "calendar_helper":
        text = ("✅ *You're all set!* Whenever you need a hand, just message me here, like \"I have a coffee chat at "
                f"1 for 30 min and need someone for {_first_kid(user_id) or 'my kid'}.\" I'll confirm the time with "
                "you before asking anyone. If you join a classmate's village, I'll ask when you're usually free.")
    else:
        text = ("✅ *You're all set!* When a parent adds you to their village, I'll ask you here, and only when you "
                "look free. You can always say no.")
    channels.send(user_id, f"{text}\n\n{SETTINGS_HELP}")


# --- answers ------------------------------------------------------------------------------


async def handle_text(user_id: str, text: str) -> bool:
    """A typed answer during setup. Returns True if the message was a setup answer."""
    current = step(user_id) or ""
    if current == "kid":
        known = [k["name"] for k in db.all_("SELECT name FROM kids WHERE parent_id = ? ORDER BY rowid", user_id)]
        reading = await read_kids(user_id, text, "kids", known)
        new = [k for k in (reading.kids if reading else []) if k.name.strip() and k.name.lower() not in
               {n.lower() for n in known}]
        if not new:
            channels.send(user_id, "Sorry, I didn't catch that. Try their first names and ages, like *Maya, 3*.")
            return True
        import coordinator

        for k in new:
            name = k.name.strip()[:40]
            coordinator.add_kid(user_id, name[:1].upper() + name[1:], (age_label(k.age) if k.age else None) or "",
                                k.notes.strip(), k.allergies.strip())
        _ask_about_kids(user_id)
        return True
    if current == "emergency":
        db.run("UPDATE users SET emergency_contact = ? WHERE id = ?", text.strip()[:300], user_id)
        channels.send(user_id, "Got it, saved. 🔒 You can change it anytime on my Home tab.")
        _ask_village(user_id)
        return True
    if current.startswith(("kid_age", "kid_notes", "kid_medical")):
        question = {"kid_age": "kids", "kid_notes": "notes", "kid_medical": "allergies"}[current.partition(":")[0]]
        kids = [db.one("SELECT * FROM kids WHERE id = ?", k) for k in _waiting_kids(user_id)]
        if question == "allergies" and NO_RE.match(text):
            for k in kids:
                _add_to_kid(k["id"], allergies=NONE)
        else:
            reading = await read_kids(user_id, text, question, [k["name"] for k in kids])
            if reading:
                _apply(kids, reading, question)
        if question == "kids":                     # ages: ask again only for any still missing
            _ask_about_kids(user_id)
            return True
        _summary(user_id)                          # one question about notes or allergies, then move on
        _ask_emergency(user_id)
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
    elif verb == "setup_kids_done":
        _ask_emergency(user_id)
    elif verb in ("setup_skip_notes", "setup_no_medical"):   # buttons from before setup asked one question
        _summary(user_id)
        _ask_emergency(user_id)
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
        _after_village(user_id)
    elif verb == "setup_skip_village":
        _after_village(user_id)
    elif verb == "setup_calendar_done" and has_free_times(user_id):
        _finish(user_id)
    elif verb == "setup_demo_calendar" and _demo(user_id) and not db.one(
            "SELECT 1 FROM calendars WHERE user_id = ?", user_id):
        _connect_demo_calendars(user_id)
        if step(user_id) in ("calendar", "calendar_helper", "calendar_joined"):
            _finish(user_id)


def calendar_connected(user_id: str, label: str) -> None:
    """Called whenever someone connects a calendar; continues setup if they're on that step."""
    if step(user_id) in ("calendar", "calendar_helper", "calendar_joined"):
        channels.send(user_id, f"✅ Connected *{label}*. Have another calendar, like a personal one? You can also add "
                               "a note about your availability, like \"I'm free most evenings, except during consulting recruiting events\" (optional).",
                      [Button(label="Connect another", action="setup_calendar", kind="form", form="calendar"),
                       Button(label="Add a note (optional)", action="setup_describe", kind="form",
                              form="availability"),
                       Button(label="That's all", action="setup_calendar_done", style="primary")])


def availability_saved(user_id: str, parsed: ParsedAvailability | None) -> None:
    """Called whenever someone saves written availability; finishes setup if they're on that step."""
    if step(user_id) in ("calendar", "calendar_helper", "calendar_joined"):
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
