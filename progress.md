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
- [ ] Project structure created (`/app`, `/config`, `/workers`, `/tests`)
- [ ] Dockerfile written
- [ ] docker-compose.yml written (api + redis services)
- [ ] requirements.txt written with pinned versions
- [ ] `docker-compose up` boots both containers cleanly
- [ ] Verified Redis reachable from api container (`redis-cli ping` or equivalent)

**Status:** Not started

---

## Phase 2: Async API Gateway & Token Bucket Rate Limiter
- [ ] `app/main.py` FastAPI entry point created
- [ ] TokenBucketRateLimiter implemented using `redis.asyncio`
- [ ] `client_id` read from `X-Client-ID` header
- [ ] `/health` endpoint working
- [ ] `/v1/predict` endpoint working
- [ ] `/v1/predict` implemented as synchronous hold-open (no polling endpoint in base)
- [ ] Verified: exceeding rate limit returns HTTP 429
- [ ] Verified: limit resets after time window

**Status:** Not started

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

**Last updated:** —
**Phases complete:** 0 / 6
**Currently on:** Phase 1
