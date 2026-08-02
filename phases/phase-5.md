# Phase 5: Testing & Telemetry

Status: done and verified, 2026-08-02.

## What got built

```
semantic-queue/
├── config/
│   └── logging_config.py     # shared JSON log formatter, used by api + worker
├── app/
│   ├── main.py                 # request-logging middleware, QpsTracker
│   └── cache.py                 # structured cache_lookup log lines
├── workers/
│   └── inference_worker.py      # structured batch_processed log lines
├── tests/
│   ├── conftest.py               # shared fixtures: client, redis_client, cleanup helpers
│   ├── test_rate_limiter.py
│   ├── test_cache.py
│   ├── test_concurrency.py       # regression test for the Phase 4 blocking bug
│   └── test_batching.py
└── requirements.txt             # pytest, httpx promoted from transitive to explicit pins
```

## Structured JSON logging

**What changed and why.** Every log line the app emits is now one JSON
object instead of a plain formatted string. `config/logging_config.py`
holds a `JsonFormatter` and a `configure_logging()` function, imported by
both `app/main.py` and `workers/inference_worker.py` so the two separate
processes emit one consistent format instead of drifting apart. Fields
passed via `logger.info(msg, extra={...})` get flattened straight into
the JSON object — `logger.info("cache hit", extra={"cache_status":
"HIT", "score": 0.96})` produces
`{"timestamp": ..., "message": "cache hit", "cache_status": "HIT", "score": 0.96}`,
not a string you'd need a regex to parse back apart.

**Where each of plan.md's four fields actually shows up:**
- **`cache_status`** — every `cache_lookup` event (`app/cache.py`, one
  per `/v1/predict` request), and again on the `request_completed` event
  once the response is ready.
- **`batch_size`** — every `batch_processed` event
  (`workers/inference_worker.py`), one per batch the worker actually ran.
- **`latency_ms`** — new: a `request_completed` event, logged once per
  request by a new FastAPI middleware in `app/main.py`, timing from the
  moment the request enters the app to the moment the response leaves.
  This field didn't exist before Phase 5 — nothing was tracking
  end-to-end per-request latency.
- **`qps`** — also new: a small in-process `QpsTracker` counts requests
  in a trailing 1-second window and stamps the current value onto every
  `request_completed` line.

**Plain-language: what this actually buys you.** A plain-text log line
like `CACHE_HIT method=exact entry_id=abc123` is fine for a human
scrolling `docker compose logs` in real time, but useless the moment you
want to ask a *question* of your logs — "what's the p95 latency for
cache misses in the last hour," "how often does the cosine-similarity
path actually fire vs. exact match" — because answering that means
writing a regex to tear the string back apart, which breaks the instant
someone tweaks the message wording. A JSON line is already a parsed
record: pipe it into `jq`, load it into any log aggregation tool, or
just `grep '"cache_status": "HIT"'` — the *data* was never encoded into
prose in the first place. This is also why the middleware design
matters: computing `latency_ms`/`qps` once, centrally, for every route,
means adding a new endpoint later doesn't require remembering to
duplicate timing code into it.

**Verified live** (not just "should work") — a real request, its
matching structured log lines:
```
$ curl -s -X POST http://localhost:8001/v1/predict -H "X-Client-ID: logging-smoke-test" \
    -d '{"text": "structured logging smoke test sentence unique marker abc123"}'

$ docker compose logs api --since 10s | grep -E '"event": "(cache_lookup|request_completed)"'
{"timestamp": "2026-08-02T20:01:07.437709+00:00", "level": "INFO", "logger": "semantic_queue.cache", "message": "cache miss", "event": "cache_lookup", "cache_status": "MISS", "entry_id": "c818e386...", "best_score": 0.5267, "threshold": 0.92}
{"timestamp": "2026-08-02T20:01:07.554505+00:00", "level": "INFO", "logger": "semantic_queue.api", "message": "request completed", "event": "request_completed", "method": "POST", "path": "/v1/predict", "status_code": 200, "latency_ms": 666.23, "qps": 1.0, "cache_status": "MISS", "client_id": "logging-smoke-test"}

$ docker compose logs worker --since 15s | grep '"event": "batch_processed"'
{"timestamp": "2026-08-02T20:01:07.553365+00:00", "level": "INFO", "logger": "semantic_queue.worker", "message": "batch processed", "event": "batch_processed", "batch_size": 1, "duration_ms": 47.8, "task_ids": ["32e76ba1-..."]}
```
Full request journey (rate limit → cache lookup → worker batch → result)
reconstructable from three log lines, tied together by `client_id` and
timing — this is literally Phase 5's own "done when."

## The test suite

**Design principle: hit the real running stack, not mocks.** Every test
either makes a real HTTP call to the live `api` service (`httpx.Client`
against `http://localhost:8000`, run via `docker compose exec api
python -m pytest`) or exercises the worker's real, unmocked functions
against a real Redis connection. No part of the system under test is
replaced with a stand-in. This was an explicit instruction, and it's not
a formality — a test that mocks `SemanticCache.lookup()` or patches
`model.encode` to return a canned array would have passed cleanly on the
*broken* Phase 4 code (the un-offloaded `json.loads`/NumPy block that
blocked the event loop for ~58ms per request), because a mock removes
exactly the real, timing-sensitive code path where that bug lived.

**`tests/test_rate_limiter.py`** — missing `X-Client-ID` → 400; a
genuine concurrent burst (`asyncio.gather`, matching how this was
manually verified in Phase 2) past `RATE_LIMIT_CAPACITY` produces both
`429`s and `200`s; after a few seconds' wait, the same client_id
succeeds again, proving continuous refill rather than a permanent
lockout. Every request in the burst uses identical text on purpose —
after the first request populates the cache, the rest take the fast
exact-match path, so the test measures rate limiting, not caching.

**`tests/test_cache.py`** — uses the **real** embedding model via real
HTTP calls, not mocked vectors, per the explicit instruction and per
Phase 3's own lesson: assumed-similar phrasing doesn't reliably clear
0.92. Before writing these tests, candidate sentence pairs were checked
against the live model the same way Phase 3 was (see `phases/phase-3.md`
for that precedent):
```
0.9750  "Can you tell me what the weather is like today" <-> "Could you tell me what today weather is like"
0.0027  "The chef prepared a three course meal..." <-> "The submarine descended into..."
```
Comfortable margins either side of the threshold, so the tests aren't
sitting on a knife-edge that model updates could flip. A `conftest.py`
helper (`clear_cache_entry`) deletes any pre-existing entry for a test's
exact text before it runs — the cache has no TTL by design, so without
this, re-running the suite a second time would find prior runs' entries
still cached and see false `HIT`s regardless of what's being tested.

**`tests/test_concurrency.py`** — not one of the three explicitly
requested categories, added because it's the most direct way to test for
*exactly* the class of bug Phase 4 found and fixed: fires a slow,
cache-miss `/v1/predict` (forces a real embedding computation) and a
`/health` request concurrently, and asserts `/health` isn't stuck behind
it. This is a more precise, deterministic regression test than trying to
assert on a req/sec number (which Phase 4's own investigation showed is
noisy and environment-dependent) — it directly answers "is the event
loop free to do other work right now," the actual question the original
bug was about.

**`tests/test_batching.py`** — per explicit permission, uses direct
`ml_task_queue` injection (same technique as Phase 4's manual isolation
test) rather than trying to reproduce a size-16 batch through realistic
HTTP concurrency, which Phase 4 already established doesn't reliably
happen. Imports and calls the real `_collect_batch()` from
`workers/inference_worker.py` directly — real code, not a
reimplementation of the batching logic for test purposes. Runs against
Redis **DB 15**, not DB 0, so injected tasks are invisible to the actual
running `worker` container (which is continuously draining DB 0's
`ml_task_queue` in the live stack) — without this, the test and the
production worker would race over the same list and results would be
flaky. Three tests: a burst larger than `batch_size` caps at exactly 16
and returns fast; the same burst mirrors Phase 4's manual "16 then 9"
finding across two consecutive calls; a burst smaller than `batch_size`
waits out `max_delay` before returning.

## Deviations from `plan.md`, and why

1. **`pytest` and `httpx` promoted from transitive to explicit pins.**
   Both were already present in the image (pulled in incidentally by
   `sentence-transformers`/`huggingface_hub` and `fastapi`), but Phase 1's
   explicit requirements list didn't include them despite the tech-stack
   section naming them — a gap from that phase, closed now that they're
   first used directly by project code (`tests/`).

2. **JSON logging is a custom `logging.Formatter`, not a third-party
   library** (e.g. `python-json-logger`). The formatter is ~20 lines and
   `requirements.txt` is already carrying enough weight (torch,
   sentence-transformers); didn't want a new dependency for something
   this small.

3. **`latency_ms`/`qps` computed centrally in middleware, not per-route.**
   `plan.md` doesn't specify where these are measured. Centralizing in
   one middleware means every current and future route gets consistent
   timing for free, instead of each handler needing its own
   `time.perf_counter()` bookkeeping.

4. **`qps` is a simple in-process trailing-1-second counter, not a real
   metrics system.** No cross-replica aggregation, no percentiles — just
   enough to satisfy "the logs show it" per Phase 5's literal ask.
   Anything more (Prometheus, a proper metrics backend) is out of scope
   for a portfolio project's logging phase.

5. **No dedicated pytest test asserts on the JSON log *format* itself**
   (e.g. capturing stdout and parsing it). Verified manually instead (see
   above) — capturing and parsing cross-container log streams from
   inside a pytest run would add real complexity for low marginal value
   over what a direct `docker compose logs | grep` already proves.

6. **`tests/test_concurrency.py` added beyond the three requested
   categories** (rate limiting, cache correctness, batching accuracy) —
   reasoning above. Flagging it explicitly as an addition, not silently
   folding it into "cache correctness" where it doesn't really belong.

## Verification

```
$ docker compose exec api python -m pytest tests/ -v
============================= test session starts ==============================
platform linux -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0 -- /usr/local/bin/python
rootdir: /code
plugins: anyio-4.14.2, locust-2.44.4
collected 9 items

tests/test_batching.py::test_batch_size_cap_triggers PASSED              [ 11%]
tests/test_batching.py::test_batch_splits_across_size_and_remainder PASSED [ 22%]
tests/test_batching.py::test_max_delay_triggers PASSED                   [ 33%]
tests/test_cache.py::test_exact_match_hit PASSED                         [ 44%]
tests/test_cache.py::test_cosine_similarity_hit_above_threshold PASSED   [ 55%]
tests/test_cache.py::test_dissimilar_text_misses PASSED                  [ 66%]
tests/test_concurrency.py::test_health_stays_responsive_during_cache_lookup PASSED [ 77%]
tests/test_rate_limiter.py::test_missing_client_id_returns_400 PASSED    [ 88%]
tests/test_rate_limiter.py::test_exceeding_limit_returns_429_then_refills PASSED [100%]

============================== 9 passed in 17.58s ==============================
```
Re-ran immediately after to confirm repeatability, not a lucky first
pass — second run: `9 passed in 4.90s` (faster once caches/connections
were already warm).

## How to re-verify this later

```bash
cd ~/Projects/semantic-queue
docker compose up -d --build
docker compose logs api --tail 5     # "Application startup complete"
docker compose logs worker --tail 5  # "worker ready: ..."

# full suite
docker compose exec api python -m pytest tests/ -v

# spot-check structured logging directly
curl -s -X POST http://localhost:8001/v1/predict \
  -H "Content-Type: application/json" -H "X-Client-ID: logging-check" \
  -d '{"text": "a fresh sentence to check structured logging"}' > /dev/null
docker compose logs api --since 10s | grep -E '"event": "(cache_lookup|request_completed)"'
docker compose logs worker --since 10s | grep '"event": "batch_processed"'
```
