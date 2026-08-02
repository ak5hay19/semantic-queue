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
- [ ] `app/cache.py` created
- [ ] Exact-string match check implemented
- [ ] Embedding generation via sentence-transformers implemented
- [ ] Cosine similarity check implemented (NumPy)
- [ ] Threshold logic (0.92) implemented
- [ ] Verified: two differently-worded similar prompts return same cached result
- [ ] Verified: logs clearly show CACHE_HIT vs CACHE_MISS

**Status:** Not started

---

## Phase 4: Async Task Queue & Dynamic Batching Worker
- [ ] Producer pushes to Redis List (`LPUSH ml_task_queue`) implemented
- [ ] `workers/inference_worker.py` created
- [ ] Worker pops tasks via `BRPOP`
- [ ] Dynamic batcher implemented (batch_size=16 OR max_delay=20ms)
- [ ] Batched PyTorch forward pass implemented
- [ ] Results + embeddings written back to semantic cache
- [ ] Task state updated to COMPLETED
- [ ] Verified: concurrent requests visibly get grouped into batches in logs

**Status:** Not started

---

## Phase 5: Testing & Telemetry
- [ ] Structured JSON logging implemented (latency_ms, cache_status, batch_size, qps)
- [ ] pytest + httpx tests written for rate limiting
- [ ] pytest + httpx tests written for cache hit/miss correctness
- [ ] pytest + httpx tests written for batching accuracy
- [ ] Full test suite passes

**Status:** Not started

---

## Phase 6: Load Benchmarking
- [ ] locustfile.py written, simulates 500+ concurrent clients
- [ ] Cold run completed (0% cache hits) — numbers recorded
- [ ] Warm run completed (high cache hits) — numbers recorded
- [ ] Overload run completed (429 enforcement verified) — numbers recorded
- [ ] Peak QPS recorded
- [ ] p50/p95/p99 latency recorded
- [ ] % latency reduction (cache hit vs miss) calculated

**Status:** Not started

---

## BASE PROJECT COMPLETE
- [ ] All phases above checked off
- [ ] README written explaining architecture, how to run it, and key numbers
- [ ] Can explain every design decision out loud without notes

---

## Stretch Goals (only attempt after base project complete)

### Stretch 1: Adaptive Similarity Threshold
- [ ] Not started

### Stretch 2: Cost-Aware Dynamic Batching
- [ ] Not started

### Stretch 3: Cache Staleness / Drift Handling
- [ ] Not started

### Stretch 4: Async Result Delivery
- [ ] Not started

---

## Overall Progress Snapshot
_(Update this line manually or ask Claude Code to update it after each session)_

**Last updated:** 2026-08-02
**Phases complete:** 2 / 6
**Currently on:** Phase 3
