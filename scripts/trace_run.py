"""Trace one real run of the demo request, step by step, for the report.

Same setup as measure_caching.py (frozen demo data in tests/seed, fixed clock, Portkey's
response cache bypassed): Priya asks for help with a 1pm coffee chat, taps Find someone,
and the first classmate asked replies in free text. Saves every trace event, each
classmate's verdict, and every agent run's tokens and cost.

    python3 scripts/trace_run.py        -> output/traced_run.json and output/traced_run.md
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["VILLAGE_BYPASS_GATEWAY_CACHE"] = "1"

import calendars  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import runner  # noqa: E402

REQUEST = "I have a coffee chat at 1 for 30 min and need someone to hang out with my kid"
REPLY = "yes, happy to! I'll meet you outside Evans Hall"


async def main() -> None:
    import coordinator

    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = config.DB_PATH = Path(tmp) / "village.db"
        db.SEED_DIR = calendars.SEED_DIR = ROOT / "tests" / "seed"
        runner.AUDIT_LOG = Path(tmp) / "audit.jsonl"
        db.reset()
        db.set_setting("demo_now", "2026-10-13T11:30")
        await coordinator.handle_message("priya", REQUEST)
        rid = db.one("SELECT id FROM requests ORDER BY id DESC")["id"]
        draft = dict(db.one("SELECT meeting_start, meeting_minutes, care_start, care_end, purpose FROM requests "
                            "WHERE id = ?", rid))
        await coordinator.handle_button("priya", "confirm", rid, None)
        asked = db.one("SELECT villager_id FROM asks WHERE request_id = ? AND status = 'asked' "
                       "ORDER BY asked_at, villager_id", rid)["villager_id"]
        await coordinator.handle_message(asked, REPLY)
        out = {
            "request": REQUEST, "reply": {"from": asked, "text": REPLY}, "draft": draft,
            "final": dict(db.one("SELECT status, helper_id, care_start, care_end FROM requests WHERE id = ?", rid)),
            "asks": [dict(r) for r in db.all_("SELECT a.villager_id, a.verdict, a.decided_by, a.verdict_reason, "
                                              "a.status, a.wave FROM asks a WHERE request_id = ? ORDER BY villager_id",
                                              rid)],
            "events": [dict(r) for r in db.all_("SELECT kind, agent, text FROM events WHERE request_id = ? "
                                                "ORDER BY id", rid)],
            "usage": [dict(r) for r in db.all_("SELECT agent, subject, requests, input_tokens, cached_tokens, "
                                               "output_tokens, cost_usd, duration_ms FROM usage ORDER BY id")],
            "messages": [dict(r) for r in db.all_("SELECT user_id, direction, text FROM messages ORDER BY id")],
        }
    (ROOT / "output" / "traced_run.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    u = out["usage"]
    lines = ["# One traced run", "", f"Request: *{REQUEST}*", "",
             "| Agent | For | Input | Cached | Output | Cost | Seconds |", "|---|---|---|---|---|---|---|"]
    lines += [f"| {r['agent']} | {r['subject'] or ''} | {r['input_tokens']:,} | {r['cached_tokens']:,} | "
              f"{r['output_tokens']:,} | ${r['cost_usd']:.6f} | {r['duration_ms'] / 1000:.1f} |" for r in u]
    lines += [f"| **Total** | | {sum(r['input_tokens'] for r in u):,} | {sum(r['cached_tokens'] for r in u):,} | "
              f"{sum(r['output_tokens'] for r in u):,} | ${sum(r['cost_usd'] for r in u):.6f} | |", "",
              "| Classmate | Verdict | Decided by | Reason | Status |", "|---|---|---|---|---|"]
    lines += [f"| {a['villager_id']} | {a['verdict']} | {a['decided_by']} | {a['verdict_reason']} | {a['status']} |"
              for a in out["asks"]]
    lines += ["", "Trace:", ""] + [f"1. [{e['kind']}{'/' + e['agent'] if e['agent'] else ''}] {e['text']}"
                                   for e in out["events"]]
    (ROOT / "output" / "traced_run.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(main())
