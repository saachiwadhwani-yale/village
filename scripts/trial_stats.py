"""Timings from real requests in data/village.db, for the report's Impact section.

For each request: who asked, how long from the parent's first message to the first ask going out and
to a confirmed helper, how many people were asked and said no, and what the agents cost.

    python3 scripts/trial_stats.py              # requests from today
    python3 scripts/trial_stats.py --all        # every request
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "village.db"


def t(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def mins(a: datetime | None, b: datetime | None) -> str:
    return f"{(b - a).total_seconds() / 60:.1f} min" if a and b else "-"


def main(show_all: bool) -> None:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    today = datetime.now().astimezone().date().isoformat()
    rows = db.execute("SELECT r.*, p.name AS parent, h.name AS helper FROM requests r JOIN users p ON p.id = r.parent_id "
                      "LEFT JOIN users h ON h.id = r.helper_id ORDER BY r.id").fetchall()
    for r in rows:
        if not show_all and not r["created_at"].startswith(today):
            continue
        created = t(r["created_at"])
        # The parent's first message about it: their last message at or before the request was created.
        first_msg = db.execute("SELECT created_at FROM messages WHERE user_id = ? AND direction = 'in' AND created_at <= ? "
                               "ORDER BY id DESC LIMIT 1", (r["parent_id"], r["created_at"])).fetchone()
        start = t(first_msg["created_at"]) if first_msg else created
        asks = db.execute("SELECT * FROM asks WHERE request_id = ?", (r["id"],)).fetchall()
        asked = [a for a in asks if a["asked_at"]]
        first_ask = min((t(a["asked_at"]) for a in asked), default=None)
        accepted = [t(a["answered_at"]) for a in asks if a["status"] == "accepted" and a["answered_at"]]
        cost = db.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c, COUNT(*) AS n FROM usage WHERE request_id = ?",
                          (r["id"],)).fetchone()
        print(f"#{r['id']} {r['parent']} · {r['purpose'] or 'request'} · {r['status']}"
              f"{' · helper ' + r['helper'] if r['helper'] else ''}")
        print(f"   parent's first message → first ask out: {mins(start, first_ask)}")
        print(f"   parent's first message → confirmed helper: {mins(start, min(accepted) if accepted else None)}")
        print(f"   classmates checked {len(asks)}, asked {len(asked)}, said no "
              f"{sum(a['status'] == 'declined' for a in asks)}, rounds {r['wave']}")
        print(f"   agents: {cost['n']} runs, ${cost['c']:.4f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="every request, not just today's")
    main(ap.parse_args().all)
