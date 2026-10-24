"""Measure prompt caching: tokens, cached tokens, cost, and verdicts for the demo request.

Runs the same request several times on a throwaway copy of the frozen demo data in
tests/seed (Priya with a village of Sam, Diego, Mei, and Hannah) with a fixed clock: Priya asks for help with a 1pm coffee chat, Village checks her village, and the
first person asked replies "yes, happy to!". Every agent run's usage is recorded.

    python3 scripts/measure_caching.py --label before
    python3 scripts/measure_caching.py --label after --trials 4

Results are appended to output/caching_experiment.json (one entry per label). Costs are
computed from token counts with the prices in backend/config.py; after a price change,
`--resummarize` recomputes every saved run without calling the model again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
# Measure the model's own prompt caching, not Portkey replaying identical requests from its cache.
os.environ["VILLAGE_BYPASS_GATEWAY_CACHE"] = "1"

import config  # noqa: E402
import db  # noqa: E402
import runner  # noqa: E402

OUT = ROOT / "output" / "caching_experiment.json"
REQUEST = "I have a coffee chat at 1 for 30 min and need someone to hang out with my kid"


async def one_trial() -> dict:
    import coordinator

    db.reset()
    db.set_setting("demo_now", "2026-10-13T11:30")
    start = time.monotonic()
    await coordinator.handle_message("priya", REQUEST)
    req = db.one("SELECT id FROM requests ORDER BY id DESC")["id"]
    await coordinator.handle_button("priya", "confirm", req, None)
    asked = db.one("SELECT villager_id FROM asks WHERE request_id = ? AND status = 'asked' ORDER BY villager_id", req)
    if asked:
        await coordinator.handle_message(asked["villager_id"], "yes, happy to!")
    seconds = time.monotonic() - start
    usage = [dict(r) for r in db.all_("SELECT agent, subject, requests, input_tokens, cached_tokens, output_tokens, "
                                      "cost_usd, duration_ms FROM usage ORDER BY id")]
    verdicts = {a["villager_id"]: a["verdict"] for a in db.all_("SELECT * FROM asks WHERE request_id = ?", req)}
    status = db.one("SELECT status FROM requests WHERE id = ?", req)["status"]
    return {"seconds": round(seconds, 2), "status": status, "verdicts": verdicts, "runs": usage}


def summarize(trials: list[dict]) -> dict:
    by_agent: dict[str, dict] = {}
    for t in trials:
        for r in t["runs"]:
            a = by_agent.setdefault(r["agent"], {"runs": 0, "requests": 0, "input": 0, "cached": 0, "output": 0,
                                                 "cost": 0.0})
            a["runs"] += 1
            a["requests"] += r["requests"]
            a["input"] += r["input_tokens"]
            a["cached"] += r["cached_tokens"]
            a["output"] += r["output_tokens"]
            a["cost"] += config.cost_usd(r["input_tokens"], r["cached_tokens"], r["output_tokens"])
    n = len(trials)
    total_in = sum(a["input"] for a in by_agent.values())
    total_cached = sum(a["cached"] for a in by_agent.values())
    return {
        "trials": n,
        "per_request": {
            "input_tokens": round(total_in / n),
            "cached_tokens": round(total_cached / n),
            "cached_share": round(total_cached / total_in, 3) if total_in else 0,
            "output_tokens": round(sum(a["output"] for a in by_agent.values()) / n),
            "cost_usd": round(sum(a["cost"] for a in by_agent.values()) / n, 6),
            "seconds": round(sum(t["seconds"] for t in trials) / n, 2),
        },
        "by_agent": {k: {**v, "cost": round(v["cost"], 6),
                         "input_per_run": round(v["input"] / v["runs"]),
                         "cached_share": round(v["cached"] / v["input"], 3) if v["input"] else 0}
                     for k, v in by_agent.items()},
        "first_trial_cached_tokens": sum(r["cached_tokens"] for r in trials[0]["runs"]),
        "later_trials_cached_tokens": [sum(r["cached_tokens"] for r in t["runs"]) for t in trials[1:]],
        "verdicts": [t["verdicts"] for t in trials],
        "all_booked": all(t["status"] == "booked" for t in trials),
    }


async def main(label: str, trials: int, pause: float) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = config.DB_PATH = Path(tmp) / "village.db"
        db.SEED_DIR = ROOT / "tests" / "seed"   # the frozen demo data the measurements were made on
        runner.AUDIT_LOG = Path(tmp) / "audit.jsonl"
        results = []
        for i in range(trials):
            results.append(await one_trial())
            print(f"trial {i + 1}: {results[-1]['seconds']}s, "
                  f"cached {sum(r['cached_tokens'] for r in results[-1]['runs'])} of "
                  f"{sum(r['input_tokens'] for r in results[-1]['runs'])} input tokens")
            if i < trials - 1:
                await asyncio.sleep(pause)
    summary = summarize(results)
    print(json.dumps(summary["per_request"], indent=2))
    print(json.dumps({k: {"input_per_run": v["input_per_run"], "cached_share": v["cached_share"]}
                      for k, v in summary["by_agent"].items()}, indent=2))
    data = json.loads(OUT.read_text()) if OUT.exists() else {}
    data[label] = {"summary": summary, "trials": results,
                   "prices_per_million": {"input": config.PRICE_INPUT_PER_M,
                                          "cached_input": config.PRICE_CACHED_INPUT_PER_M,
                                          "output": config.PRICE_OUTPUT_PER_M}}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2) + "\n")
    print(f"saved to {OUT.relative_to(ROOT)} as '{label}'")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--trials", type=int, default=4)
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between trials")
    ap.add_argument("--resummarize", action="store_true",
                    help="recompute every saved summary (e.g. after a price change) without new model calls")
    args = ap.parse_args()
    if args.resummarize:
        data = json.loads(OUT.read_text())
        for label, entry in data.items():
            entry["summary"] = summarize(entry["trials"])
            entry["prices_per_million"] = {"input": config.PRICE_INPUT_PER_M,
                                           "cached_input": config.PRICE_CACHED_INPUT_PER_M,
                                           "output": config.PRICE_OUTPUT_PER_M}
            print(label, json.dumps(entry["summary"]["per_request"]))
        OUT.write_text(json.dumps(data, indent=2) + "\n")
    else:
        asyncio.run(main(args.label, args.trials, args.pause))
