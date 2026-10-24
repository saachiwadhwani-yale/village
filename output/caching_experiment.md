# Prompt caching experiment

**Question:** can Village get gpt-6-luna's prompt caching to work, and does it change cost or quality?

**Method:** `python3 scripts/measure_caching.py` runs the same demo request 4 times on a fresh copy of the demo data with a fixed clock. Priya asks for help with a 1pm, 30-minute coffee chat, Village screens her 4 villagers (3 availability agents run), and the first person asked replies "yes, happy to!". Every agent run's tokens come from the model's own usage reports. Portkey's response cache was bypassed (`x-portkey-cache-force-refresh`), so these numbers are the model's prompt caching, not Portkey replaying earlier answers. Raw data: `output/caching_experiment.json`.

**Change:** the model only caches the part of a prompt that's identical from call to call, and only once that part is at least 1,024 tokens. Village's fixed instructions were about 600 tokens per agent, so nothing was cached. I added worked examples to the intake, availability, and reply agents (10 to 20 short cases each, such as a tight handoff, a tentative event, or details spread across two messages), plus rules for reading times in the reply agent. That took each agent's fixed part past 1,024 tokens. Everything that changes per request (time, villager data, the message) already comes after the fixed part.

## Results (average per request, 4 trials each)

| | Before | After (with examples) |
|---|---|---|
| Input tokens | 5,253 | 8,753 |
| Cached input tokens | 0 | 7,620 |
| Share of input cached | 0% | 87% |
| Output tokens | 437 | 429 |
| Cost per request | $0.000744 | $0.000404 |

**Cost per request fell 46%**, even though prompts got 67% longer.

| Agent | Input tokens per run, before → after | Cached, after |
|---|---|---|
| intake | 1,146 → 1,632 | 69% |
| availability | 1,696 → 2,908 | 91% |
| reply | 716 → 1,306 | 90% |

## Did quality change?

- Availability verdicts were identical in all 8 trials (before and after): Sam unavailable (a conflict on his second calendar, settled by rules), Diego maybe (tight handoff), Mei maybe (tentative event), Hannah available (rules). Every trial ended with a booking.
- The full test suite (34 tests, including 10 that call the model, among them the prompt-injection tests) passes with the new prompts.
- One intake example covers combining details across messages (e.g. "tomorrow at 2", then "30 min"). That backs up an earlier fix, which gave the intake agent the recent conversation; that fix, not the examples, is what solved the bug where a parent was asked again for the time.

## Caveats

- **Prices:** gpt-6-luna is $0.10 per 1M input tokens and $0.50 per 1M output tokens (course price table). The table has no cached-input price, so this assumes $0.01 per 1M (a 90% discount, which is typical for OpenAI models). If cached input were billed at the full price, the examples would cost more, not less, and caching would only bring the longer prompts back to roughly the original cost.
- **Warm cache:** the cache was already warm from earlier runs, so even trial 1 hit it. The very first request after a quiet period caches less. Within one request, each availability agent's second call and the later villagers' checks still reuse the cache.
- **Two layers of caching:** separately from the model's prompt caching, Portkey replays an identical request from its own cache, at no model cost. In normal use that happens when, for example, the same villager's first availability step comes up for the same time window more than once.
- **Speed** isn't compared: the model's response time varied from about 3 to 170 seconds per request during the test, for reasons unrelated to caching.
