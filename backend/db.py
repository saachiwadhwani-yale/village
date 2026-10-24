"""SQLite storage for Village: people, villages, calendars, requests, and messages.

`data/village.db` is built from `data/seed/people.json` the first time the app
starts (or on POST /reset). It's a working copy; the seed file never changes.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from config import DB_PATH, SEED_DIR, TZ

SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    program TEXT NOT NULL,
    color TEXT NOT NULL,
    is_parent INTEGER NOT NULL,
    help_count_week INTEGER NOT NULL DEFAULT 0,
    help_count_total INTEGER NOT NULL DEFAULT 0,
    paused_until TEXT,                -- local ISO time; not asked for care that starts before this
    slack_id TEXT UNIQUE,             -- set for people who use Village through Slack
    onboarding TEXT,                  -- setup step (see onboarding.py): NULL, role, kid, kid_notes:<ids>, kid_medical:<ids>, village, calendar_helper, calendar_joined, done
    availability_text TEXT NOT NULL DEFAULT '',
    availability_parsed TEXT          -- JSON ParsedAvailability, or NULL
);
CREATE TABLE kids (
    id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    age TEXT NOT NULL,                -- years ("4") or months for babies ("6 months")
    notes TEXT NOT NULL DEFAULT '',   -- sensitive: only shown to the villager who accepts
    allergies TEXT NOT NULL DEFAULT '' -- allergies and medical needs (or "None"); same privacy as notes
);
CREATE TABLE village (
    parent_id TEXT NOT NULL REFERENCES users(id),
    villager_id TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL,             -- pending | accepted | declined
    rank INTEGER NOT NULL,            -- parent's preference order, 1 = ask first
    PRIMARY KEY (parent_id, villager_id)
);
CREATE TABLE calendars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    label TEXT NOT NULL,
    source TEXT NOT NULL,             -- an https URL, or a file name in data/seed/calendars
    kind TEXT NOT NULL                -- url | file
);
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id TEXT NOT NULL REFERENCES users(id),
    kid_id TEXT REFERENCES kids(id),
    meeting_start TEXT,               -- local ISO time of the parent's commitment
    meeting_minutes INTEGER,
    care_start TEXT,                  -- meeting window plus handoff buffers
    care_end TEXT,
    purpose TEXT,
    location TEXT,
    status TEXT NOT NULL,             -- draft | searching | booked | no_match | cancelled | expired
    helper_id TEXT REFERENCES users(id),
    wave INTEGER NOT NULL DEFAULT 0,
    wave_sent_at TEXT,                -- real (wall-clock) UTC time the current wave went out
    created_at TEXT NOT NULL
);
CREATE TABLE asks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES requests(id),
    villager_id TEXT NOT NULL REFERENCES users(id),
    verdict TEXT,                     -- available | maybe | unavailable
    confidence TEXT,
    verdict_reason TEXT,
    decided_by TEXT,                  -- rules | agent
    wave INTEGER,                     -- NULL until asked
    status TEXT NOT NULL,             -- not_asked | asked | accepted | declined | countered | filled | withdrawn
    counter_start TEXT,
    counter_end TEXT,
    asked_at TEXT,
    answered_at TEXT,
    UNIQUE (request_id, villager_id)
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    direction TEXT NOT NULL,          -- in (user -> Village) | out (Village -> user)
    text TEXT NOT NULL,
    buttons TEXT,                     -- JSON list of Button
    buttons_used INTEGER NOT NULL DEFAULT 0,
    button_choice TEXT,               -- label of the button that was tapped, if any
    slack_channel TEXT,               -- where the Slack copy of this message was posted
    slack_ts TEXT,
    request_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER,
    kind TEXT NOT NULL,               -- step | agent | guard | error
    agent TEXT,
    text TEXT NOT NULL,
    data TEXT,                        -- JSON
    created_at TEXT NOT NULL
);
CREATE TABLE usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER,
    agent TEXT NOT NULL,
    subject TEXT,                     -- which villager / parent the run was about
    requests INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    duration_ms INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
"""


def utc_now_iso() -> str:
    """Timestamp for messages and trace lines, on the (demo) clock everyone sees."""
    return now().isoformat(timespec="seconds")


@contextmanager
def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def seed() -> dict:
    return json.loads((SEED_DIR / "people.json").read_text())


def reset(fresh_setup: bool = False) -> None:
    """Rebuild village.db from the seed file.

    fresh_setup (what Reset demo does): each demo person starts the way their "demo"
    field says: "ready" (set up), "invited" (set up, with a pending invite), or "new"
    (not set up; their calendar files are one tap away during setup). No made-up
    help history. People who joined through Slack keep their account but start
    setup over. Without it (tests, scripts), everyone is fully set up from the seed.

    The new database is built in a temp file and swapped in at once, so the app
    (which keeps polling during a reset) never sees a half-built database.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    data = seed()
    keep = _slack_people()
    new = set()
    if fresh_setup:
        new = {u["id"] for u in data["users"] if u.get("demo", "new") == "new"}
        data = {**data,
                "kids": [k for k in data["kids"] if k["parent_id"] not in new],
                "village": [v for v in data["village"] if not {v["parent_id"], v["villager_id"]} & new],
                "users": [{**u, "help_count_week": 0, "help_count_total": 0,
                           **({"calendars": [], "availability_text": "", "availability_parsed": None,
                               "is_parent": False} if u["id"] in new else {})} for u in data["users"]]}
        keep = {"users": [{**u, "onboarding": None, "availability_text": "", "availability_parsed": None,
                           "paused_until": None, "is_parent": 0, "help_count_week": 0, "help_count_total": 0}
                           for u in keep.get("users", [])]}
    with connect(tmp) as conn:
        conn.executescript(SCHEMA)
        for u in data["users"]:
            parsed = u.get("availability_parsed")
            conn.execute(
                "INSERT INTO users (id, name, program, color, is_parent, help_count_week, help_count_total,"
                " availability_text, availability_parsed, onboarding) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (u["id"], u["name"], u["program"], u["color"], int(u["is_parent"]),
                 u.get("help_count_week", 0), u.get("help_count_total", 0), u.get("availability_text", ""),
                 json.dumps(parsed) if parsed else None, None if u["id"] in new else "done"),
            )
            for c in u.get("calendars", []):
                conn.execute(
                    "INSERT INTO calendars (user_id, label, source, kind) VALUES (?,?,?, 'file')",
                    (u["id"], c["label"], c["file"]),
                )
        for k in data["kids"]:
            conn.execute("INSERT INTO kids (id, parent_id, name, age, notes, allergies) VALUES (?,?,?,?,?,?)",
                         (k["id"], k["parent_id"], k["name"], k["age"], k["notes"], k.get("allergies", "")))
        for v in data["village"]:
            conn.execute("INSERT INTO village VALUES (?,?,?,?)",
                         (v["parent_id"], v["villager_id"], v["status"], v["rank"]))
        for key, value in data["settings"].items():
            conn.execute("INSERT INTO settings VALUES (?,?)", (key, value))
        _restore(conn, keep)
    os.replace(tmp, DB_PATH)


def _slack_people() -> dict[str, list[dict]]:
    """Real people who joined through Slack, so a demo reset doesn't erase their profiles."""
    if not DB_PATH.exists():
        return {}
    try:
        with connect() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE slack_id IS NOT NULL")]
            if not ids:
                return {}
            marks = ",".join("?" * len(ids))
            return {
                "users": [dict(r) for r in conn.execute(f"SELECT * FROM users WHERE id IN ({marks})", ids)],
                "kids": [dict(r) for r in conn.execute(f"SELECT * FROM kids WHERE parent_id IN ({marks})", ids)],
                "calendars": [dict(r) for r in conn.execute(
                    f"SELECT user_id, label, source, kind FROM calendars WHERE user_id IN ({marks})", ids)],
                "village": [dict(r) for r in conn.execute(
                    f"SELECT * FROM village WHERE parent_id IN ({marks}) OR villager_id IN ({marks})", ids + ids)],
            }
    except sqlite3.OperationalError:  # an old database without the slack_id column
        return {}


def _restore(conn, keep: dict[str, list[dict]]) -> None:
    known = {r["id"] for r in conn.execute("SELECT id FROM users")}
    for table in ("users", "kids", "calendars", "village"):
        for row in keep.get(table, []):
            if table == "users":
                known.add(row["id"])
            if table == "village" and not {row["parent_id"], row["villager_id"]} <= known:
                continue
            cols = ",".join(row)
            conn.execute(f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})",
                         list(row.values()))


def ensure() -> None:
    if not DB_PATH.exists():
        reset()


# Columns added after the first version, so an existing village.db keeps working.
_ADDED_COLUMNS = {
    "users": {"help_count_total": "INTEGER NOT NULL DEFAULT 0", "paused_until": "TEXT", "slack_id": "TEXT",
              "onboarding": "TEXT"},
    "kids": {"allergies": "TEXT NOT NULL DEFAULT ''"},
    "messages": {"button_choice": "TEXT", "slack_channel": "TEXT", "slack_ts": "TEXT"},
}


def migrate() -> None:
    with connect() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_slack_id ON users(slack_id)")
        # The demo clock and screening switch were removed from the app: always real time, rules first.
        conn.execute("DELETE FROM settings WHERE key IN ('demo_now', 'demo_set_at', 'screen_mode')")
        # Demo people are already set up; people who join (Slack, or "w_" from the web demo) go through setup.
        conn.execute("UPDATE users SET onboarding = 'done' WHERE onboarding IS NULL AND slack_id IS NULL "
                     "AND id NOT LIKE 'w\\_%' ESCAPE '\\'")


# --- Settings and the demo clock ------------------------------------------------


def get_setting(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str | None) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
        if key == "demo_now":  # the demo clock runs forward from the moment it's set
            conn.execute("INSERT OR REPLACE INTO settings VALUES ('demo_set_at', ?)",
                         (datetime.now(TZ).isoformat(),))


def now() -> datetime:
    """Current New Haven time.

    Tests pin the clock with the `demo_now` setting (it then ticks forward in
    real time from there); the app itself always runs on real time.
    """
    demo, set_at = get_setting("demo_now"), get_setting("demo_set_at")
    real = datetime.now(TZ)
    if demo:
        start = datetime.fromisoformat(demo).replace(tzinfo=TZ)
        elapsed = real - datetime.fromisoformat(set_at) if set_at else real - real
        return (start + elapsed).replace(microsecond=0)
    return real.replace(microsecond=0)


# --- Small query helpers ------------------------------------------------------------


def one(sql: str, *args) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(sql, args).fetchone()


def all_(sql: str, *args) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(sql, args).fetchall()


def run(sql: str, *args) -> int:
    """Execute a write. Returns lastrowid for inserts, rowcount otherwise."""
    with connect() as conn:
        cur = conn.execute(sql, args)
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount


def log_event(request_id: int | None, kind: str, text: str,
              agent: str | None = None, data: dict | None = None) -> None:
    """Add one line to the 'behind the scenes' trace."""
    run("INSERT INTO events (request_id, kind, agent, text, data, created_at) VALUES (?,?,?,?,?,?)",
        request_id, kind, agent, text, json.dumps(data, default=str) if data else None, utc_now_iso())
