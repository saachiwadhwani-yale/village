"""The setup conversation (shared by Slack and the web demo), and how Slack draws it.

No network and no model: Slack is a fake client that records what the bot would send.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import channels
import coordinator
import db
import onboarding
from channels.slack import SlackChannel, _home_blocks
from conftest import last, messages
from models import ParsedAvailability


def describe(user_id: str, text: str) -> None:
    """What saving the availability form does (minus the model call that parses it)."""
    db.run("UPDATE users SET availability_text = ? WHERE id = ?", text, user_id)
    onboarding.availability_saved(user_id, ParsedAvailability(slots=[], caveats=[], summary=text) if text else None)


def new_classmate(user_id: str, name: str) -> str:
    db.run("INSERT INTO users (id, name, program, color, is_parent) VALUES (?,?, 'SOM', '#000', 0)", user_id, name)
    return user_id


async def tap(user_id: str, label: str, values: list[str] | None = None):
    """Tap a button on the newest message that has it, like a person would."""
    msg = next(m for m in reversed(messages(user_id)) if label in m["buttons"] and not m["used"])
    action = msg["actions"][msg["buttons"].index(label)]
    assert channels.check_press(user_id, action, msg["id"]) is None
    await coordinator.handle_button(user_id, action, None, msg["id"], values)
    return msg


# --- the shared conversation ---------------------------------------------------------------


async def test_parent_setup_conversation():
    me = new_classmate("w_saachi", "Saachi Wadhwani")
    await coordinator.handle_message(me, "hi")                        # first contact starts setup
    assert "Are you a parent?" in last(me)["text"]

    await tap(me, "Yes, I'm a parent")
    await coordinator.handle_message(me, "she's 3")                   # not a name: asks again
    assert "didn't catch that" in last(me)["text"]
    await coordinator.handle_message(me, "Maya, 3")
    assert tuple(db.one("SELECT name, age FROM kids WHERE parent_id = ?", me)) == ("Maya", "3")
    await coordinator.handle_message(me, "Loves trains. Snack in the blue bag.")
    assert "Loves trains" in db.one("SELECT notes FROM kids WHERE parent_id = ?", me)["notes"]
    summary = messages(me)[-2]["text"]                               # one question only, then the summary
    assert "*Name:* Maya\n*Age:* 3\n*Allergies:* None mentioned\n*General notes:* Loves trains. Snack in the blue bag." \
        in summary

    picker = await tap(me, "Pick classmates…", values=["hannah", "sam", me])   # picking yourself is ignored
    assert {r["villager_id"] for r in db.all_("SELECT villager_id FROM village WHERE parent_id = ?", me)} == \
        {"hannah", "sam"}
    assert db.one("SELECT button_choice FROM messages WHERE id = ?", picker["id"])["button_choice"] == \
        "Invited Hannah, Sam"
    assert "would like to add you" in last("hannah")["text"]

    # Parents aren't asked when they're free until they're in someone else's village.
    assert onboarding.step(me) == "done"
    assert "You're all set" in last(me)["text"] and "coffee chat" in last(me)["text"]
    assert all(m["used"] for m in messages(me) if m["buttons"])      # no setup buttons left open


async def test_several_kids_in_one_message_get_one_notes_question():
    me = new_classmate("w_dana", "Dana Ruiz")
    onboarding.start(me)
    await tap(me, "Yes, I'm a parent")
    await coordinator.handle_message(me, "Maya 3 and leo, 5")
    assert [tuple(k) for k in db.all_("SELECT name, age FROM kids WHERE parent_id = ? ORDER BY rowid", me)] == \
        [("Maya", "3"), ("Leo", "5")]
    assert "about Maya and Leo" in last(me)["text"]                    # one question for both
    await coordinator.handle_message(me, "Maya loves trains, Leo is allergic to nuts. Snacks are in the blue bag.")
    kids = {k["name"]: (k["allergies"], k["notes"]) for k in db.all_("SELECT * FROM kids WHERE parent_id = ?", me)}
    assert kids == {"Maya": ("", "Maya loves trains. Snacks are in the blue bag."),
                    "Leo": ("Leo is allergic to nuts.", "Snacks are in the blue bag.")}
    assert "Who do you want in your village?" in last(me)["text"] and "Maya or Leo" in last(me)["text"]


async def test_notes_given_with_the_names_are_kept_and_not_asked_again():
    me = new_classmate("w_ola", "Ola Berg")
    onboarding.start(me)
    await tap(me, "Yes, I'm a parent")
    await coordinator.handle_message(me, "Maya 3 - no allergies, loves trains, naps at 1; Leo 5 - nut allergy")
    kids = {k["name"]: (k["allergies"], k["notes"]) for k in db.all_("SELECT * FROM kids WHERE parent_id = ?", me)}
    assert kids == {"Maya": ("No allergies.", "Loves trains. Naps at 1."), "Leo": ("Nut allergy.", "")}
    assert "Who do you want in your village?" in last(me)["text"]      # everything given at once: nothing to ask
    assert not any("?" in m["text"] and "allerg" in m["text"].lower() for m in messages(me))
    assert "*Name:* Leo\n*Age:* 5\n*Allergies:* Nut allergy.\n*General notes:* None" in messages(me)[-2]["text"]


def test_parse_kids():
    assert onboarding.parse_kids("Maya, 3") == [("Maya", "3")]
    assert onboarding.parse_kids("Maya (3), Leo (5) and Ana 1") == [("Maya", "3"), ("Leo", "5"), ("Ana", "1")]
    assert onboarding.parse_kids("Mary Ann, 4") == [("Mary Ann", "4")]
    assert onboarding.parse_kids("Ana, 6 months and Leo 2 years") == [("Ana", "6 months"), ("Leo", "2")]
    assert onboarding.parse_kids("Ana 1 month") == [("Ana", "1 month")]
    assert onboarding.parse_kids("she's 3") == []


async def test_parent_is_asked_for_free_times_on_joining_a_village():
    me = new_classmate("w_lee", "Lee Park")
    onboarding.start(me)
    await tap(me, "Yes, I'm a parent")
    await coordinator.handle_message(me, "Ana, 2")
    await coordinator.handle_message(me, "none")
    await tap(me, "Skip for now")
    assert onboarding.step(me) == "done" and not onboarding.has_free_times(me)
    home = _home_blocks(me, None)
    assert "Connect a calendar or describe" not in json.dumps(home)   # no nagging before they're in a village

    coordinator.set_village("priya", ["sam", "diego", "mei", "hannah", me])
    await tap(me, "Join")
    assert onboarding.step(me) == "calendar_joined"
    assert "Now that you're in a classmate's village" in last(me)["text"]
    describe(me, "Free Fridays")
    assert onboarding.step(me) == "done"
    assert "Thanks!" in last(me)["text"] and "Ana" in last(me)["text"]

    # Someone who already shared free times isn't asked again when they join another village.
    coordinator.set_village("marcus", ["hannah", "mei", "priya", me])
    await tap(me, "Join")
    assert onboarding.step(me) == "done"


async def test_helper_answer_wins_even_with_a_kid_on_file():
    """Someone who says "just helping" gets the helper wording, even if a kid is saved from before."""
    me = new_classmate("w_alex", "Alex Kim")
    coordinator.add_kid(me, "Kid", 3, "")
    onboarding.start(me)
    await tap(me, "No, I'm here to help")
    assert "Last step" in last(me)["text"] and "only ask you if you look free" in last(me)["text"]
    describe(me, "Tuesdays after 2")
    final = last(me)["text"]
    assert "When a parent adds you to their village" in final and "coffee chat" not in final


async def test_empty_description_without_a_calendar_asks_again():
    me = new_classmate("w_sam2", "Sam Two")
    onboarding.start(me)
    await tap(me, "No, I'm here to help")
    describe(me, "")
    assert onboarding.step(me) == "calendar_helper"
    assert "I need either a calendar or a few words" in last(me)["text"]


async def test_with_a_calendar_the_description_is_optional():
    me = new_classmate("w_kim", "Kim Lee")
    onboarding.start(me)
    await tap(me, "No, I'm here to help")
    db.run("INSERT INTO calendars (user_id, label, source, kind) VALUES (?, 'Google', 'x.ics', 'file')", me)
    onboarding.calendar_connected(me, "Google")
    assert "Add a note (optional)" in last(me)["buttons"]
    await tap(me, "That's all")
    assert onboarding.step(me) == "done"


async def test_demo_people_skip_setup():
    await coordinator.handle_message("priya", "hi")
    assert "Are you a parent?" not in last("priya")["text"]


# --- how Slack draws it -----------------------------------------------------------------------


class FakeSlack:
    def __init__(self):
        self.posts, self.updates, self.homes = [], [], []

    async def users_info(self, user):
        return {"user": {"name": "x", "profile": {"real_name": "saachi.​wadhwani", "title": ""}}}

    async def conversations_open(self, users):
        return {"channel": {"id": "D" + users}}

    async def chat_postMessage(self, channel, text, blocks):
        self.posts.append({"channel": channel, "text": text, "blocks": blocks})
        return {"ts": str(len(self.posts))}

    async def chat_update(self, channel, ts, text, blocks):
        self.updates.append({"ts": ts, "blocks": blocks})

    async def views_publish(self, user_id, view):
        self.homes.append(view)


@pytest.fixture
async def slack():
    s = SlackChannel.__new__(SlackChannel)
    s.client, s.dm_channels, s._tasks, s._home_pending = FakeSlack(), {}, set(), set()
    s.team_id, s.app_id = "T1", "A1"
    s.loop, s.queue = asyncio.get_running_loop(), asyncio.Queue()
    s.worker = asyncio.create_task(s._work())
    channels.register(s)
    yield s
    s.worker.cancel()


async def drain(s):
    for _ in range(100):
        await asyncio.sleep(0.005)
        if s.queue.empty():
            return


async def test_slack_draws_setup_with_a_people_picker_and_form_buttons(slack):
    await slack.greet("UME")
    me, _ = await slack.user_for("UME")
    assert db.one("SELECT name FROM users WHERE id = ?", me)["name"] == "Saachi Wadhwani"   # handle cleaned up
    await tap(me, "Yes, I'm a parent")
    await coordinator.handle_message(me, "Maya, 3")
    await coordinator.handle_message(me, "no")
    await drain(slack)
    village_q = slack.client.posts[-1]["blocks"]
    row = next(b for b in village_q if b["type"] == "actions")["elements"]
    assert [e["text"]["text"] for e in row] == ["Pick classmates…", "Skip for now"]   # one row; picker opens a pop-up
    assert row[0]["action_id"].startswith("village_pick_")
    await tap(me, "Skip for now")
    coordinator.set_village("priya", ["sam", "diego", "mei", "hannah", me])
    await tap(me, "Join")                                          # in a village now: asked for free times
    await drain(slack)
    calendar_q = slack.client.posts[-1]["blocks"]
    actions = next(b for b in calendar_q if b["type"] == "actions")["elements"]
    assert [json.loads(e["value"])["f"] for e in actions if e["action_id"].startswith("village_form_")] == \
        ["calendar", "availability"]
    describe(me, "Free most afternoons")
    await drain(slack)
    final = slack.client.posts[-1]["blocks"][0]["text"]["text"]
    assert "<slack://app?team=T1&id=A1&tab=home|Home tab>" in final
    assert any("✓" in json.dumps(u["blocks"], ensure_ascii=False) for u in slack.client.updates)


async def test_home_tab_summary_is_valid_for_every_kind_of_person(slack):
    me, _ = await slack.user_for("UME")
    coordinator.add_kid(me, "Maya", 3, "")
    coordinator.set_village(me, ["hannah", "sam"])
    for uid in [me, "priya", "hannah", "tom"]:
        blocks = _home_blocks(uid, None)
        assert len(blocks) <= 100                                              # Slack's limit
        for b in blocks:
            acc = b.get("accessory", {})
            if acc.get("type") == "overflow":
                assert 1 <= len(acc["options"]) <= 5                           # Slack's limits for ⋯ menus
    text = json.dumps(_home_blocks(me, None), ensure_ascii=False)
    assert "Hannah Weiss  ·  ⏳ Invited" in text and "Finish setting up" in text
    assert "order" not in text                                                 # no ranking of villagers
    assert "Helping parents" in json.dumps(_home_blocks("hannah", None), ensure_ascii=False)   # not a parent


async def test_reset_starts_everyone_at_setup_with_demo_answers():
    """Reset demo: demo people start brand-new, with their seed answers as hints and their calendars one tap away."""
    db.reset(fresh_setup=True)
    coordinator.welcome_everyone()
    assert db.one("SELECT COUNT(*) AS n FROM kids")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM village")["n"] == 0
    assert db.one("SELECT MAX(help_count_total) AS n FROM users")["n"] == 0   # no made-up history either
    assert "Are you a parent?" in last("priya")["text"] and "Are you a parent?" in last("mei")["text"]

    await tap("mei", "No, I'm here to help")
    assert last("mei")["buttons"][:3] == ["Use my demo calendars (demo only)", "Connect a calendar",
                                          "Describe my availability instead"]
    await tap("mei", "Use my demo calendars (demo only)")
    assert db.one("SELECT COUNT(*) AS n FROM calendars WHERE user_id = 'mei'")["n"] == 2
    assert "skip a club meeting" in db.one("SELECT availability_text FROM users WHERE id = 'mei'")["availability_text"]
    assert onboarding.step("mei") == "done"

    await tap("priya", "Yes, I'm a parent")
    await coordinator.handle_message("priya", "Arjun, 6 months")
    assert db.one("SELECT age FROM kids WHERE parent_id = 'priya'")["age"] == "6 months"


async def test_reset_with_the_real_demo_data_starts_people_as_described(monkeypatch):
    """data/seed: Priya set up with Sam and Mei, Hannah set up and invited, Marcus/Diego/Jordan brand-new."""
    from pathlib import Path
    monkeypatch.setattr(db, "SEED_DIR", Path(__file__).resolve().parents[1] / "data" / "seed")
    db.reset(fresh_setup=True)
    coordinator.welcome_everyone()
    assert {r["villager_id"]: r["status"] for r in db.all_("SELECT * FROM village WHERE parent_id = 'priya'")} == \
        {"sam": "accepted", "mei": "accepted", "hannah": "pending"}
    assert {r["id"] for r in db.all_("SELECT id FROM users WHERE onboarding = 'done'")} == {"priya", "sam", "mei", "hannah"}
    assert "You're set up with Arjun (4) and Sam and Mei in your village" in last("priya")["text"]
    assert "would like to add you" in last("hannah")["text"]
    for new in ("marcus", "diego", "jordan"):
        assert "Are you a parent?" in last(new)["text"]
        assert not db.one("SELECT 1 FROM calendars WHERE user_id = ?", new)


async def test_notes_without_allergies_get_one_allergy_question():
    me = new_classmate("w_kai", "Kai Wu")
    onboarding.start(me)
    await tap(me, "Yes, I'm a parent")
    await coordinator.handle_message(me, "Lily 2 - loves the stroller, naps around 1")
    assert "Last thing: does Lily have any allergies or medical needs" in last(me)["text"]
    await coordinator.handle_message(me, "no")
    assert "*Allergies:* None\n*General notes:* Loves the stroller, naps around 1." in messages(me)[-2]["text"]
    assert "Who do you want in your village?" in last(me)["text"]
