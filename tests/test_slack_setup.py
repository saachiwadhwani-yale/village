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
    assert tuple(db.one("SELECT name, age FROM kids WHERE parent_id = ?", me)) == ("Maya", 3)
    await coordinator.handle_message(me, "Loves trains. Snack in the blue bag.")
    assert "Loves trains" in db.one("SELECT notes FROM kids WHERE parent_id = ?", me)["notes"]

    picker = await tap(me, "Pick classmates…", values=["hannah", "sam", me])   # picking yourself is ignored
    assert {r["villager_id"] for r in db.all_("SELECT villager_id FROM village WHERE parent_id = ?", me)} == \
        {"hannah", "sam"}
    assert db.one("SELECT button_choice FROM messages WHERE id = ?", picker["id"])["button_choice"] == \
        "Invited Hannah, Sam"
    assert "would like to add you" in last("hannah")["text"]

    assert "connect your calendar" in last(me)["text"]
    assert "Skip for now" not in last(me)["buttons"]                    # a calendar or a description is required
    describe(me, "Free most afternoons")
    assert onboarding.step(me) == "done"
    assert "You're all set" in last(me)["text"] and "coffee chat" in last(me)["text"]
    assert all(m["used"] for m in messages(me) if m["buttons"])      # no setup buttons left open


async def test_helper_answer_wins_even_with_a_kid_on_file():
    """Someone who says "just helping" gets the helper wording, even if a kid is saved from before."""
    me = new_classmate("w_alex", "Alex Kim")
    coordinator.add_kid(me, "Kid", 3, "")
    onboarding.start(me)
    await tap(me, "No, I'm here to help")
    assert "if your calendar says you're free" in last(me)["text"]
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
    await tap(me, "Skip")
    await drain(slack)
    village_q = slack.client.posts[-1]["blocks"]
    assert any(b.get("accessory", {}).get("type") == "multi_users_select" for b in village_q)
    await tap(me, "Skip for now")
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
