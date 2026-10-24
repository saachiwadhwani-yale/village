"""Setup, as a conversation: are you a parent, your kids, your village, your calendar.

Channel-agnostic: it only sends messages through `channels.send`, with buttons,
a people picker, and form buttons that each channel draws its own way (Slack
Block Kit, or the web demo client). Someone's step is kept in users.onboarding:

    NULL -> role -> kid -> kid_notes:<ids> -> village -> done
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
from models import Button, ParsedAvailability, age_label

NOT_A_NAME = {"she's", "he's", "they're", "she is", "he is", "my kid", "my son", "my daughter", "age"}


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


def _connect_demo_calendars(user_id: str) -> None:
    """Connect a demo person's calendar files, and their written note if they have one."""
    me = _demo(user_id)
    for c in me.get("calendars", []):
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


def hello(user_id: str) -> None:
    """The first message for someone who's already set up (a demo person after a reset)."""
    kids = db.all_("SELECT name, age FROM kids WHERE parent_id = ? ORDER BY rowid", user_id)
    hi = (f"👋 Hi {_first(user_id)}! I'm Village. I help SOM parents find a classmate they trust to hang out with "
          "their kid for an hour or so, and I only ask people who are free.\n\n")
    if kids:
        village = [r["name"].split()[0] for r in db.all_(
            "SELECT u.name FROM village v JOIN users u ON u.id = v.villager_id "
            "WHERE v.parent_id = ? AND v.status = 'accepted' ORDER BY v.rank", user_id)]
        who = ", ".join(f"{k['name']} ({k['age']})" for k in kids)
        crew = f" and {_join_and(village)} in your village" if village else ""
        text = (f"You're set up with {who}{crew}. Whenever you need a hand, just message me here, like \"I have a "
                f"coffee chat at 1 for 30 min and need someone for {kids[0]['name']}.\"")
    else:
        parents = [r["name"].split()[0] for r in db.all_(
            "SELECT u.name FROM village v JOIN users u ON u.id = v.parent_id "
            "WHERE v.villager_id = ? AND v.status = 'accepted'", user_id)]
        text = (f"You're set up and in {_join_and([p + chr(39) + 's' for p in parents])} village. I'll message you "
                "when they need a hand and you look free. You can always say no." if parents else
                "You're set up. When a parent adds you to their village, I'll ask you here.")
    channels.send(user_id, f"{hi}{text}\n\nTo change anything, open my Home tab.")


def _join_and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _ask_kid(user_id: str) -> None:
    _set(user_id, "kid")
    kids = db.all_("SELECT name, age FROM kids WHERE parent_id = ?", user_id)
    if kids:
        listed = ", ".join(f"{k['name']} ({k['age']})" for k in kids)
        channels.send(user_id, f"I have {listed}. Want to add another kid? Type their first name and age, like "
                               "*Maya, 3*.", [Button(label="No, that's everyone", action="setup_kids_done")])
    else:
        channels.send(user_id, "What are your kids' first names and ages? Just type them, like *Maya, 3*.")


def parse_kids(text: str) -> list[tuple[str, int]]:
    """Every "name, age" pair in a message: "Maya, 3", "Maya 3 and Leo 5", "Ana, 6 months"."""
    kids = []
    pairs = re.findall(r"([A-Za-zÀ-ÿ'.-]+(?:\s[A-Za-zÀ-ÿ'.-]+)?)\s*[,:(-]?\s*(?:age\s*)?"
                       r"(\d{1,2}\s*(?:months?|mos?|mths?|years?|yrs?|y/?o)?)\b", text, flags=re.I)
    for name, age in pairs:
        name = re.sub(r"^(?:and|&|my|kids?|is|are)\s+", "", name.strip(), flags=re.I).strip()
        label = age_label(age)
        if not name or name.lower() in NOT_A_NAME or name.lower() in ("and", "age") or label is None:
            continue
        kids.append((" ".join(w[:1].upper() + w[1:] for w in name.split()), label))
    return kids


def _ask_notes(user_id: str, kid_ids: list[str]) -> None:
    """Ask for notes about the first kid in the list; the rest wait in the step."""
    if not kid_ids:
        _ask_village(user_id)
        return
    _set(user_id, "kid_notes:" + ",".join(kid_ids))
    kid = db.one("SELECT name FROM kids WHERE id = ?", kid_ids[0])
    channels.send(user_id, f"Anything a helper should know about {kid['name']}? Allergies, favorite things, what's "
                           "in the bag. 🔒 I only share this with the classmate who says yes, after they say yes.",
                  [Button(label="Skip", action="setup_skip_notes")])


def _waiting_kids(user_id: str) -> list[str]:
    current = step(user_id) or ""
    return [k for k in current.removeprefix("kid_notes:").split(",") if k] if current.startswith("kid_notes:") else []


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
    channels.send(user_id, f"{text}\n\nTo change anything later, open my Home tab.")


# --- answers ------------------------------------------------------------------------------


def handle_text(user_id: str, text: str) -> bool:
    """A typed answer during setup. Returns True if the message was a setup answer."""
    current = step(user_id)
    if current == "kid":
        kids = parse_kids(text)
        if not kids:
            channels.send(user_id, "Sorry, I didn't catch that. Try first names and ages, like *Maya, 3*.")
            return True
        import coordinator

        ids = [coordinator.add_kid(user_id, name, age, "") for name, age in kids]
        if len(kids) > 1:
            channels.send(user_id, "Added " + ", ".join(f"{n} ({a})" for n, a in kids) + ".")
        _ask_notes(user_id, ids)
        return True
    if current and current.startswith("kid_notes"):
        waiting = _waiting_kids(user_id)
        if waiting:
            kid = db.one("SELECT name FROM kids WHERE id = ?", waiting[0])
            db.run("UPDATE kids SET notes = ? WHERE id = ?", text[:500], waiting[0])
            channels.send(user_id, f"Got it, saved for {kid['name']}. 🔒")
        _ask_notes(user_id, waiting[1:])
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
        _ask_village(user_id)
    elif verb == "setup_skip_notes":
        _ask_notes(user_id, _waiting_kids(user_id)[1:])
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
