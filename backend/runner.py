"""Run an agent with limits, and record what it cost.

Every agent run goes through `run_agent`. It:
* refuses to start if the care request has already used its token budget,
* runs the agent under AGENT_LIMITS (requests, tool calls, tokens),
* writes one row to the `usage` table and one line to output/audit_log.jsonl
  with input, cached input, and output tokens, cost, time, and tool calls,
* adds a line to the request's trace so the app can show what happened.
"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIResponsesModelSettings

import db
from config import AGENT_LIMITS, AUDIT_LOG, MODEL_NAME, MODEL_SETTINGS, REQUEST_TOKEN_BUDGET, cost_usd


class BudgetExceeded(Exception):
    """This care request already spent its token budget."""


class ContentFiltered(Exception):
    """The model provider refused the input (Azure's filter catches prompt-injection attempts)."""


def _is_content_filter(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 400 and "content_filter" in str(getattr(exc, "body", exc))


def request_tokens_used(request_id: int | None) -> int:
    if request_id is None:
        return 0
    row = db.one("SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS t FROM usage WHERE request_id = ?",
                 request_id)
    return row["t"]


def _tool_calls(messages) -> list[dict]:
    calls = []
    for m in messages:
        for part in getattr(m, "parts", []):
            if getattr(part, "part_kind", "") == "tool-call" and part.tool_name != "final_result":
                calls.append({"tool": part.tool_name, "args": part.args_as_dict()})
    return calls


async def run_agent(agent: Agent, name: str, prompt: str, deps: Any = None, *,
                    request_id: int | None = None, subject: str | None = None):
    """Run one agent and return its structured output. Raises on failure."""
    if request_id is not None and request_tokens_used(request_id) >= REQUEST_TOKEN_BUDGET:
        db.log_event(request_id, "guard", f"Skipped {name}: this request used its token budget.", name)
        raise BudgetExceeded(name)

    started = time.monotonic()
    result, error = None, None
    try:
        # One cache key per agent, so runs of the same agent share a prompt cache.
        settings = OpenAIResponsesModelSettings(**MODEL_SETTINGS, openai_prompt_cache_key=f"village-{name}")
        result = await agent.run(prompt, deps=deps, usage_limits=AGENT_LIMITS, model_settings=settings)
        return result.output
    except UsageLimitExceeded as exc:
        error = f"usage limit: {exc}"
        db.log_event(request_id, "guard", f"{name} hit its usage limit and was stopped.", name)
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if _is_content_filter(exc):
            db.log_event(request_id, "guard", f"The model provider blocked the input to {name} as a likely "
                         "prompt-injection attempt; nothing was done with it.", name, {"subject": subject})
            raise ContentFiltered(name) from exc
        raise
    finally:
        ms = int((time.monotonic() - started) * 1000)
        usage = result.usage if result is not None else None
        inp = usage.input_tokens if usage else 0
        cached = usage.cache_read_tokens if usage else 0
        out = usage.output_tokens if usage else 0
        reqs = usage.requests if usage else 0
        cost = cost_usd(inp, cached, out)
        db.run(
            "INSERT INTO usage (request_id, agent, subject, requests, input_tokens, cached_tokens,"
            " output_tokens, cost_usd, duration_ms, ok, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            request_id, name, subject, reqs, inp, cached, out, cost, ms, int(error is None), db.utc_now_iso(),
        )
        entry = {
            "at": db.utc_now_iso(), "request_id": request_id, "agent": name, "subject": subject,
            "model": MODEL_NAME, "requests": reqs, "input_tokens": inp, "cached_input_tokens": cached,
            "output_tokens": out, "cost_usd": round(cost, 6), "duration_ms": ms,
            "tool_calls": _tool_calls(result.all_messages()) if result is not None else [],
            "output": result.output.model_dump() if result is not None else None,
            "error": error,
        }
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
