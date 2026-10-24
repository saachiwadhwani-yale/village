"""Settings shared by the whole app: keys, the model, limits, prices, and paths.

* Keys: only PORTKEY_API_KEY is used. It is read from `.env` in the repo
  folder first, then from the folder one level above (where the course keys
  live). A value already set in the environment is never overwritten.
* Model: gpt-6-luna through Portkey, the only model allowed on the course key.
* Limits: per-agent and per-request caps so a run can't loop or overspend.
"""

from __future__ import annotations

import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pathlib import Path
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from pydantic_ai import ModelSettings, UsageLimits
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent
DATA_DIR = ROOT / "data"
SEED_DIR = DATA_DIR / "seed"
DB_PATH = DATA_DIR / "village.db"
PROMPTS_DIR = BACKEND / "prompts"
AUDIT_LOG = ROOT / "output" / "audit_log.jsonl"

# Everyone at SOM is on New Haven time.
TZ = ZoneInfo("America/New_York")

MODEL_NAME = "gpt-6-luna"

# --- Care rules -------------------------------------------------------------------
MAX_COMMITMENT_MINUTES = 80  # longest class/meeting Village covers (a full SOM class); handoffs add to it
HANDOFF_BUFFER_MIN = 5       # added before and after the parent's commitment
WAVE_SIZE = 3                # villagers asked at once
WAVE_TIMEOUT_MIN = 10        # no answer after this long -> ask the next wave
MAX_VILLAGERS_CHECKED = 15   # hard cap on availability checks per request
MAX_OPEN_REQUESTS = 3        # per parent, so one person can't flood the village
# A helper can back out through Village (which then finds a replacement automatically) only
# this long before the handoff. Closer than this, they're asked to contact the parent directly.
# Placeholder: revisit after talking to classmates.
BACKOUT_MIN_NOTICE_HOURS = 6
# Inside this window (but outside the cutoff above), backing out takes a second "are you sure?" tap.
# From a parent interview: "ideally the day before but minimum 4-6 hours".
BACKOUT_SOFT_NOTICE_HOURS = 24
# Under BACKOUT_MIN_NOTICE_HOURS a helper can't cancel, but Village quietly looks for cover. It stops this
# long before the start, and the helper talks to the parent directly instead.
COVER_CUTOFF_MIN = 20
# Once someone says no, backs out, or takes a shift on a day, don't ask them again that day (any parent).
ONE_ANSWER_PER_DAY = True
# Screening is rules first: clear cases are settled by code, only unclear ones go to an agent.
# Set True only to measure the cost of giving every villager an agent (for the tokenomics comparison).


# --- Token / cost limits ----------------------------------------------------------
MODEL_SETTINGS = ModelSettings(max_tokens=3_000)
# Per-agent overrides. The availability agent's cases are short timing calls, and in testing it sometimes
# reasoned for 3,000 tokens about a case its rules tool had already settled; low effort keeps it quick.
AGENT_SETTINGS = {"availability": {"openai_reasoning_effort": "low", "max_tokens": 1_200}}
AGENT_LIMITS = UsageLimits(request_limit=6, tool_calls_limit=8, total_tokens_limit=30_000)
REQUEST_TOKEN_BUDGET = 120_000   # all agent runs for one care request combined

# Dollars per 1M tokens for gpt-6-luna, from the course's price table.
PRICE_INPUT_PER_M = 0.10
PRICE_OUTPUT_PER_M = 0.50
# The course table doesn't list a cached-input price. OpenAI models typically bill cached input at a
# 90% discount, so this assumes $0.01. Confirm with the course before relying on it.
PRICE_CACHED_INPUT_PER_M = 0.01


def load_env() -> None:
    """Read .env from the repo folder, then the parent folder. Earlier files win."""
    for env_path in (ROOT / ".env", ROOT.parent / ".env"):
        if not env_path.exists():
            continue
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip("\"'")
            if value:
                os.environ.setdefault(key.strip(), value)


def build_model() -> OpenAIResponsesModel:
    """gpt-6-luna behind Portkey, shared by every agent."""
    load_env()
    key = os.environ.get("PORTKEY_API_KEY")
    if not key:
        raise RuntimeError(
            "PORTKEY_API_KEY is missing. Put it in .env in this folder or the folder above."
        )
    headers = {"x-portkey-api-key": key}
    # Portkey replays identical requests from its own cache. Measurements of the model's prompt caching
    # turn that off (scripts/measure_caching.py sets VILLAGE_BYPASS_GATEWAY_CACHE=1).
    if os.environ.get("VILLAGE_BYPASS_GATEWAY_CACHE") == "1":
        headers["x-portkey-cache-force-refresh"] = "true"
    client = AsyncOpenAI(api_key=key, base_url="https://api.portkey.ai/v1", default_headers=headers)
    return OpenAIResponsesModel(MODEL_NAME, provider=OpenAIProvider(openai_client=client))


def load_prompt(name: str) -> str:
    """System prompt from backend/prompts/<name>.md."""
    return (PROMPTS_DIR / f"{name}.md").read_text().strip()


def cost_usd(input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    """Dollar cost of one agent run. Cached input is billed at the cached rate."""
    fresh = max(input_tokens - cached_tokens, 0)
    return (
        fresh * PRICE_INPUT_PER_M
        + cached_tokens * PRICE_CACHED_INPUT_PER_M
        + output_tokens * PRICE_OUTPUT_PER_M
    ) / 1_000_000


model = build_model()
