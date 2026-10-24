"""Shared setup for the tests.

Every test gets a fresh copy of the demo database in a temp folder, so the
app's real data/village.db is never touched. Tests marked `live` call
gpt-6-luna through Portkey (they need PORTKEY_API_KEY and cost a few cents).

Run from the repo folder:
    python3 -m pytest              # everything
    python3 -m pytest -m "not live"   # only the tests that don't call the model
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import channels  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import runner  # noqa: E402


import calendars  # noqa: E402

# The tests use their own frozen copy of the demo data (tests/seed), so changing the demo people in
# data/seed doesn't change what the tests check.
TEST_SEED = Path(__file__).resolve().parent / "seed"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    for module in (config, db, calendars):
        monkeypatch.setattr(module, "SEED_DIR", TEST_SEED)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "village.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "village.db")
    monkeypatch.setattr(runner, "AUDIT_LOG", tmp_path / "audit_log.jsonl")
    monkeypatch.setattr(channels, "_adapters", [])
    channels.typing.clear()
    db.reset()
    db.set_setting("demo_now", "2026-10-13T11:30")   # a fixed Tuesday late morning, so results don't depend on today
    yield


@pytest.fixture(autouse=True)
def simple_dispatch(request, monkeypatch):
    """Offline tests size each round of asks with the simple rule (3 at a time, everyone for cover).
    Live tests use the real dispatch agent."""
    if request.node.get_closest_marker("live"):
        return
    import coordinator
    from models import DispatchPlan

    async def plan(req, candidates, kind, checker=None):
        return DispatchPlan(ask_now=len(candidates) if kind == "cover" else 3, wait_minutes=10, reason="test rule")
    monkeypatch.setattr(coordinator, "plan_wave", plan)


# --- helpers ---------------------------------------------------------------------------


def messages(user_id: str) -> list[dict]:
    """Everything Village sent this person, oldest first, with button labels."""
    rows = db.all_("SELECT * FROM messages WHERE user_id = ? AND direction = 'out' ORDER BY id", user_id)
    return [{"id": r["id"], "text": r["text"], "request_id": r["request_id"], "used": bool(r["buttons_used"]),
             "buttons": [b["label"] for b in json.loads(r["buttons"] or "[]")],
             "actions": [b["action"] for b in json.loads(r["buttons"] or "[]")]} for r in rows]


def last(user_id: str) -> dict:
    return messages(user_id)[-1]


def asks(request_id: int) -> dict[str, str]:
    return {a["villager_id"]: a["status"] for a in db.all_("SELECT * FROM asks WHERE request_id = ?", request_id)}


def request(request_id: int):
    return db.one("SELECT * FROM requests WHERE id = ?", request_id)


def latest_request_id() -> int:
    return db.one("SELECT id FROM requests ORDER BY id DESC LIMIT 1")["id"]


async def ask_and_confirm(coordinator, parent: str, text: str) -> int:
    """Parent sends a request and taps Find someone. Returns the request id."""
    await coordinator.handle_message(parent, text)
    req = latest_request_id()
    assert request(req)["status"] == "draft", last(parent)["text"]
    await coordinator.handle_button(parent, "confirm", req, None)
    return req
