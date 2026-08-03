# SemanticQueue — Progress Tracker

Instructions for Claude Code: after completing a task below and confirming it works, check it off by changing `[ ]` to `[x]`. Do not check off a task you have not actually verified runs/works. Add a one-line note under a task if something deviated from the plan (e.g. a different library version, a config change) so there's a record of what actually happened, not just what was planned. Commit to git after each checked-off item with a descriptive commit message.

Instructions for me (human): skim this file any time I want to know exactly how far along the project is, without re-reading the whole conversation history.

---

## Environment Setup
- [ ] WSL2 + Ubuntu installed
- [ ] Docker Desktop installed with WSL2 backend enabled
- [ ] Project cloned/created inside WSL2 native filesystem (not `/mnt/c/...`)
- [ ] Git initialized, remote set to github.com/ak5hay19/semantic-queue

---

## Phase 1: Environment & Container Setup
- [x] Project structure created (`/app`, `/config`, `/workers`, `/tests`)
- [x] Dockerfile written
- [x] docker-compose.yml written (api + redis services)
- [x] requirements.txt written with pinned versions
- [x] `docker-compose up` boots both containers cleanly
- [x] Verified Redis reachable from api container (`redis-cli ping` or equivalent)

**Notes (deviations from plan.md — full detail in `phases/phase-1.md`):**
- Added a minimal placeholder `app/main.py` (single `GET /`) purely so the `api` container has something to boot with — the real gateway is Phase 2 scope.
- `api` is published on host port **8001**, not 8000 — port 8000 was already taken by an unrelated `k3d-storm-local` cluster on this machine.
- `docker-compose.yml` bind-mounts the project dir and runs uvicorn with `--reload` for live-reload during development.
- Added a Redis healthcheck + `depends_on: condition: service_healthy` so `api` doesn't start before Redis is actually ready.
- Verified Redis connectivity via the `redis` Python client (`r.ping()`) rather than the `redis-cli` binary, since `redis-tools` isn't installed in the slim base image — functionally equivalent, and closer to how the app itself will talk to Redis.
- Follow-up pass: re-checked port mapping is still only `8001:8000`, confirmed. Also found `--reload` was writing `__pycache__` into the bind-mounted project dir as root — fixed by adding a non-root `appuser` (UID/GID 1000:1000, matching the host user) to the Dockerfile via `USER appuser`. Re-verified `docker compose up` still boots cleanly, Redis still reachable, and a forced reload now writes host-owned files.

**Status:** Done, verified 2026-08-02 — see `phases/phase-1.md`

---

## Phase 2: Async API Gateway & Token Bucket Rate Limiter
- [x] `app/main.py` FastAPI entry point created
- [x] TokenBucketRateLimiter implemented using `redis.asyncio`
- [x] `client_id` read from `X-Client-ID` header
- [x] `/health` endpoint working
- [x] `/v1/predict` endpoint working
- [x] `/v1/predict` implemented as synchronous hold-open (no polling endpoint in base)
- [x] Verified: exceeding rate limit returns HTTP 429
- [x] Verified: limit resets after time window
- [x] Verified: missing X-Client-ID header returns 400

**Notes (deviations from plan.md — full detail in `phases/phase-2.md`):**
- Rate limiter refills continuously (a real token-bucket algorithm) rather than resetting to full at fixed window boundaries — `plan.md`'s "100 req/min" phrasing didn't fully disambiguate the two, and continuous refill avoids a boundary-burst problem a fixed-window counter would have.
- Atomic check-and-decrement implemented via a Redis Lua script (`EVAL`), not `WATCH`/`MULTI` — simpler for a single-key read-modify-write.
- Rate-limit keys auto-expire (`EXPIRE`, 2x the window) so idle client_id buckets don't accumulate in Redis forever — not specified in plan.md.
- Added `config/settings.py` for env-driven settings (`REDIS_URL`, rate limit capacity/window) — the `config/` folder was reserved for this in Phase 1.
- Added a 10s timeout on `/v1/predict`'s wait for a result, returning `504` if exceeded — bounds the failure mode where nothing ever resolves the Future.
- `/v1/predict` is wired against a temporary stub (`_fake_worker` in `app/main.py`) that sleeps 0.5s and returns a canned result, since Phase 4's real queue/worker don't exist yet. Clearly marked in code for removal in Phase 4.
- Open design question flagged (not resolved) for Phase 4: the real worker runs as a separate OS process and can't directly resolve an `asyncio.Future` living in the API process — Phase 4 will need something inside the API process that notices the worker's result (e.g. watching a Redis key/list) and resolves the Future itself.
- Burst-tested with 110 concurrent requests against a 100-capacity bucket: 103 succeeded, 7 got 429 — the 3 extra are expected continuous-refill behavior during the concurrent dispatch window, not a bug (see `phases/phase-2.md` for the full explanation and command output).

**Status:** Done, verified 2026-08-02 — see `phases/phase-2.md`

---

## Phase 3: Redis Semantic Vector Cache
- [x] `app/cache.py` created
- [x] Exact-string match check implemented
- [x] Embedding generation via sentence-transformers implemented
- [x] Cosine similarity check implemented (NumPy)
- [x] Threshold logic (0.92) implemented
- [x] Verified: two differently-worded similar prompts return same cached result
- [x] Verified: logs clearly show CACHE_HIT vs CACHE_MISS

**Notes (deviations from plan.md — full detail in `phases/phase-3.md`):**
- Cache keys derived from `sha256(text)` directly (`cache:entry:{id}`), so exact-match is a single O(1) lookup with no separate index.
- Embeddings stored in their own bulk hash (`cache:embeddings`, one HGETALL fetches all of them for the vectorized similarity step) rather than bundled into each entry's hash — keeps the bulk fetch lean as the cache grows.
- Embeddings stored as JSON-encoded float lists, not raw bytes — the shared Redis client uses `decode_responses=True` (from Phase 2), which would corrupt raw binary.
- Added `cache_status` (`HIT`/`MISS`) to the `/v1/predict` response body, not just logs — makes verification possible from curl output alone. `task_id` is `null` on a hit (no Future/task machinery touched).
- Logging is plain `key=value` text (standard `logging` module), not JSON — full JSON structured logging is explicitly Phase 5's deliverable.
- Added `HF_HOME` + a named Docker volume (`hf_cache`) so the ~90MB embedding model persists across `--reload` restarts — `appuser` (added in the Phase 1 follow-up) has no home directory to cache into by default.
- **Finding:** the originally suggested test pair ("what's the weather like today" vs "how's the weather today") scores 0.89 cosine similarity — below the 0.92 threshold, so it would not actually hit. Verified against the real model and used a different, empirically-confirmed paraphrase pair (0.9651) for the demo instead. Full numbers in `phases/phase-3.md`.

**Status:** Done, verified 2026-08-02 — see `phases/phase-3.md`

---

## Phase 4: Async Task Queue & Dynamic Batching Worker
- [x] Producer pushes to Redis List (`LPUSH ml_task_queue`) implemented
- [x] `workers/inference_worker.py` created
- [x] Worker pops tasks via `BRPOP`
- [x] Dynamic batcher implemented (batch_size=16 OR max_delay=20ms)
- [x] Batched PyTorch forward pass implemented
- [x] Results + embeddings written back to semantic cache
- [x] Task state updated to COMPLETED
- [x] Verified: concurrent requests visibly get grouped into batches in logs

**Notes (deviations from plan.md — full detail in `phases/phase-4.md`):**
- Worker recomputes the embedding the API already computed for the cache-similarity check (deliberate duplicate compute) — otherwise the "batched forward pass" would only batch the near-free classifier step, and the genuinely expensive step (MiniLM encoding) would never get the batching benefit at all.
- "Mark task COMPLETED" has no separate status field — the worker's `LPUSH` onto `predict_results` (consumed via a destructive `BRPOP`) is itself the completion signal, per the settled Phase 3 design already recorded in `plan.md`.
- One batching loop per worker process, not an explicit multi-worker pool — matches the Phase 4 spec's own text (a single `inference_worker.py`); `docker compose up --scale worker=N` would give a real pool for free since they'd share `ml_task_queue`, not needed at this scale.
- No hot-reload for the worker (unlike the api service's `--reload`) — edits require `docker compose restart worker`.
- **Real bug found and fixed:** redis-py 8.0.1's client-side `socket_timeout` (default 5s) was killing indefinitely-blocking `BRPOP` calls (both the worker's task wait and the API's result listener) after 5s of no traffic, even though `timeout=0` asks Redis to block forever. Fixed with a dedicated Redis connection (`socket_timeout=None`) for those two specific calls, separate from the general pooled client — also just correct Redis practice regardless of the library quirk.
- **Test-methodology finding:** first batching demo attempt used near-duplicate templated sentences ("...sentence number 0/1/2...") — 49 of 50 turned out to be legitimate cosine-similarity cache hits (correctly deduplicated by Phase 3's cache) rather than worker traffic. Fixed the test (genuinely distinct topics), not the code. Real batches observed: 2, 5, 13, 4 (24 total) and a 40-request run summing correctly across 21 batches — largest single batch was 13, not the full 16 cap; explained in `phases/phase-4.md`.
- **Follow-up fix:** `SemanticCache.lookup()`'s decode (`json.loads` per cached embedding) + matrix build + cosine-similarity compute ran inline on the event loop, unthreaded — measured ~58ms of blocking CPU work per request at a 266-entry cache. Offloaded via `asyncio.to_thread` (new `_best_similarity_match()` function in `app/cache.py`), same pattern as `_embed()`. Re-measured: cost per call is unchanged (~45ms — the fix moves *where* it runs, not how much work it is), and the req/sec ceiling did **not** improve (9.9/sec vs. the ~13/sec baseline) — root-caused to Python's GIL: `json.loads` in a loop is GIL-bound pure-Python work, so concurrent `to_thread` calls contend rather than parallelize (measured 10 concurrent calls taking *longer* than 10 sequential ones, 0.56x "speedup"). The fix is still correct and kept — it stops this step from blocking the event loop's ability to service other requests' I/O — but a real throughput fix needs a `ProcessPoolExecutor`, a faster embedding serialization format, or multiple API worker processes. Full numbers in `phases/phase-4.md`.

**Status:** Done, verified 2026-08-02 — see `phases/phase-4.md`

---

## Phase 5: Testing & Telemetry
- [x] Structured JSON logging implemented (latency_ms, cache_status, batch_size, qps)
- [x] pytest + httpx tests written for rate limiting
- [x] pytest + httpx tests written for cache hit/miss correctness
- [x] pytest + httpx tests written for batching accuracy
- [x] Full test suite passes

**Notes (deviations from plan.md — full detail in `phases/phase-5.md`):**
- `pytest`/`httpx` promoted from transitive to explicit `requirements.txt` pins — a Phase 1 gap (tech-stack section named them, the explicit pin list didn't), closed now that `tests/` uses them directly.
- Custom `logging.Formatter` (`config/logging_config.py`, shared by api + worker) for JSON logs instead of a third-party JSON-logging library — small enough not to warrant a new dependency.
- `latency_ms`/`qps` computed centrally by a new FastAPI middleware in `app/main.py`, not per-route — one place to get consistent timing for every route, current and future.
- `qps` is a simple in-process trailing-1-second counter (`QpsTracker`), not a real metrics system — satisfies "the logs show it," nothing more.
- No dedicated pytest test asserts on the JSON log format itself — verified manually via `docker compose logs | grep`, shown in `phases/phase-5.md`.
- Added `tests/test_concurrency.py` beyond the three requested categories (rate limiting, cache, batching) — a regression test specifically targeting the Phase 4 event-loop-blocking bug class (asserts `/health` isn't stuck behind a concurrent cache-miss lookup). Flagged as an addition, not folded silently into another category.
- Batching tests inject directly into `ml_task_queue` on an isolated Redis DB (15, not 0) and call the real `_collect_batch()` — avoids racing the actual live `worker` container, which continuously drains DB 0 in the running stack.

**Status:** Done, verified 2026-08-02 — see `phases/phase-5.md`

---

## Phase 6: Load Benchmarking
- [x] locustfile.py written, simulates 500+ concurrent clients
- [x] Cold run completed (0% cache hits) — numbers recorded
- [x] Warm run completed (high cache hits) — numbers recorded
- [x] Overload run completed (429 enforcement verified) — numbers recorded
- [x] Peak QPS recorded
- [x] p50/p95/p99 latency recorded
- [x] % latency reduction (cache hit vs miss) calculated

**Notes (deviations from plan.md — full detail in `phases/phase-6.md`):**
- Locust runs from a host-side virtualenv (`.venv/`), not inside the `api` container — keeps the load generator's own CPU use from competing with the system under test.
- Three separate Locust `User` classes (`ColdUser`/`WarmUser`/`OverloadUser`), selected on the CLI, one per scenario, rather than one parameterized file.
- **Real bugs found and fixed, both connection-handling artifacts, not the actual measured bottleneck:** (1) redis-py's async client pool defaults to 100 connections — exhausted immediately at 500 concurrent requests (`MaxConnectionsError` on 40%+ of requests); fixed via a new `REDIS_MAX_CONNECTIONS=512` setting. (2) The same "redis-py silently applies a 5s client-side timeout" quirk Phase 4 found for the two dedicated `BRPOP` connections turned out to also apply to the general-purpose pooled client under 500-concurrent congestion (confirmed by inspecting a live `Connection` object's actual attributes) — fixed with explicit `socket_timeout=None, socket_connect_timeout=None` on `app.state.redis`.
- **Cold scenario's request generator went through two failed, empirically-measured attempts before landing on random word-salad text** — natural-language slot-fill templates (even with 50625 combinatorial, no-replacement variations) still produced a 20% (then 9%, after adding template-shape variation) false cache-hit rate at scale, because MiniLM's embeddings picked up on shared sentence structure across "distinct" generated content. Random unordered word draws from an 80-word vocabulary measured 0/599 false hits and is what's actually used.
- **Overload scenario's first draft used expensive (cache-miss) request bodies** and only generated 178 total requests across 500 users in 30s — nowhere near enough per-client_id volume to exhaust a 100-token bucket. Fixed by having Overload reuse one fixed, pre-cached sentence so it measures the rate limiter specifically, not the cache-miss path a second time.
- **A related, not-fixed finding:** under 500-concurrent Cold load, 67.7% of requests legitimately exceeded the app's own existing 10s SLA timeout (504, not a crash) — the same GIL-bound cache-scan ceiling Phase 4 documented, now large enough at this concurrency to cause outright timeouts rather than just slow throughput. Left alone per this phase's explicit instructions.
- **Key numbers:** Cold (500 users, cache-miss only): 3.87 req/s completed, p50/p95/p99 = 13.0s/25.0s/35.0s, 67.7% hit the 10s SLA timeout. Warm (500 users, 100% cache hit): 70.23 req/s, p50/p95/p99 = 4.0s/15.0s/42.0s, 0 failures. Overload (500 users, 5 shared client_ids): 604 req/s aggregate, 95.3% correctly rejected with HTTP 429. Latency reduction, cache hit vs miss: ~69% at p50 under full load, ~96% at best-case/uncongested single requests.

**Status:** Done, verified 2026-08-03 — see `phases/phase-6.md`

---

## BASE PROJECT COMPLETE
- [x] All phases above checked off
- [x] README written explaining architecture, how to run it, and key numbers
- [ ] Can explain every design decision out loud without notes

---

## Stretch Goals (only attempt after base project complete)

### Stretch 1: Adaptive Similarity Threshold
- [ ] Not started

### Stretch 2: Cost-Aware Dynamic Batching
- [ ] Not started

### Stretch 3: Cache Staleness / Drift Handling
- [x] Confidence decay implemented (primary mechanism): `effective_similarity = raw_similarity - (CACHE_DECAY_RATE_PER_DAY * age_in_days)`, applied to both the exact-match and cosine-similarity lookup paths
- [x] Hard TTL implemented (backstop): real Redis `EXPIRE` on each entry (pipelined with the write), not a check in application code
- [x] Verified: RediSearch automatically drops an expired key from its index — confirmed directly, not assumed
- [x] Verified: an entry that would've HIT at raw similarity now correctly MISSes once an artificially-aged timestamp pushes it below the decayed threshold
- [x] Verified: an entry past the hard TTL is genuinely gone from both entry storage (`EXISTS`) and the RediSearch index (KNN search), not just skipped in application logic
- [x] Verified: fresh (non-aged) exact-match/cosine-hit/dissimilar-miss behavior from Phase 3/5/7 unchanged
- [x] Full pytest suite (9 tests) re-verified passing

**Status:** Done, verified 2026-08-03 — see `phases/phase-8.md`. **Key numbers:** `CACHE_DECAY_RATE_PER_DAY=0.05` (5%/day, openly acknowledged as arbitrary), `CACHE_TTL_SECONDS=86400` (24h). Live test: the verified 0.9277-similarity password paraphrase pair HIT fresh, then MISSed once backdated 12 hours (`effective_score=0.9027 < 0.92`). Live test: a near-perfect (0.999 similarity) entry aged to 23.9 hours still passed the decayed threshold (`effective=0.9492`) — decay math alone would not have blocked it — while a real Redis TTL expiry (tested with a short substitute value) made an entry genuinely unreachable from both storage and the RediSearch index, confirming the hard TTL is the only unconditional guarantee.

### Stretch 4: Async Result Delivery
- [ ] Not started

### Stretch 5: RediSearch (Redis Stack) Vector Search
- [x] `docker-compose.yml`'s `redis` service swapped for `redis/redis-stack-server`
- [x] HNSW index (cosine distance, dim 384) created over cached embeddings, replacing the brute-force NumPy scan in `app/cache.py`
- [x] Exact-string match check kept as the first, cheap check (unchanged from Phase 3)
- [x] 0.92 threshold behavior verified equivalent (RediSearch's cosine distance confirmed empirically to equal `1 - cosine_similarity`, not assumed)
- [x] Phase 3/5 test cases (exact hit, cosine hit, dissimilar miss) re-verified passing
- [x] Full pytest suite (9 tests) re-verified passing
- [x] Phase 6 Cold-scenario load test re-run at the same 500-concurrent configuration, numbers compared side by side with the old brute-force run

**Status:** Done, verified 2026-08-03 — see `phases/phase-7.md`. **Key result:** Cold-scenario 500-concurrent load test went from 3.87 req/s completed / 67.7% requests timing out (brute-force) to 22.81 req/s completed / 0% failures (RediSearch KNN) — a ~6x throughput increase and elimination of the SLA-timeout failure mode Phase 6 documented. **Also found and fixed:** RediSearch's default `EF_RUNTIME` (search-time recall knob, default 10) missed an obvious 0.975-similarity near-duplicate once the cache reached realistic scale (~1700 entries) — caught by re-running Phase 5's own test suite at that scale, not by the isolated small-scale threshold check. Fixed via a new `CACHE_EF_RUNTIME=300` setting, confirmed to cost no measurable latency at this scale.

---

## Overall Progress Snapshot
_(Update this line manually or ask Claude Code to update it after each session)_

**Last updated:** 2026-08-03
**Phases complete:** 6 / 6
**Currently on:** Base project complete — stretch goals available, none started
