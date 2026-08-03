# SemanticQueue

An asynchronous, multi-tenant ML inference gateway built in Python. It sits in front of a machine learning model and makes it cheaper and faster to serve at scale — without needing more GPUs.

## What it does

Most naive ML-serving setups run one inference call per request, with no protection against traffic spikes and no reuse of prior work. SemanticQueue adds three layers in front of the model, each solving a different part of that problem:

1. **Rate limiting (token bucket, Redis-backed)** — protects the system from being overwhelmed before any expensive work starts.
2. **Semantic caching** — before running the model, checks whether a *semantically similar* request has already been answered (via brute-force cosine similarity over embeddings stored in Redis — not just exact string matching), and returns the cached result in milliseconds if so.
3. **Dynamic batching** — when a request does need the model, it's grouped with other concurrent requests into a batch (up to a size or time limit, whichever comes first) so the model runs more efficiently per request instead of one at a time.

The result is a system that avoids redundant computation when it can, and makes the computation it does run as efficient as possible when it can't.

## Why it's built this way

- **FastAPI + asyncio**: the gateway itself is fully non-blocking — I/O (Redis calls, queueing) never blocks the event loop, so the API stays responsive under load.
- **Redis**: used for three different jobs at once — rate-limit counters, the semantic cache (storing embeddings + results), and a lightweight task queue (via Redis Lists) connecting the API to background workers.
- **PyTorch + sentence-transformers**: a small embedding model (`all-MiniLM-L6-v2`) generates vectors used both for semantic-cache comparisons and as input to a lightweight mock downstream task that produces the actual prediction; this runs off the main event loop so it doesn't block other requests while computing.
- **Client identification**: every request carries an `X-Client-ID` header, used both as the rate-limit key and as the general tenant identifier; requests without it are rejected with HTTP 400 (no anonymous/shared fallback bucket).
- **Synchronous response model**: `/v1/predict` holds the connection open and awaits the worker's result via an in-process `asyncio.Future` keyed by `task_id` (not Redis pub/sub — unnecessary overhead for a single-node setup) rather than returning a task ID to poll — simpler to reason about and demo for a portfolio-scale system.

## Architecture

```mermaid
flowchart TD
    A[Client Request] --> B[FastAPI Gateway Router]
    B --> C{Token Bucket Rate Limiter - Redis}
    C -->|Exceeded| D[HTTP 429]
    C -->|Allowed| E{Semantic Vector Cache - Redis}
    E -->|Cache Hit, cosine sim > 0.92| F[Return Cached Result ~3ms]
    E -->|Cache Miss| G[Enqueue Task - Redis List LPUSH]
    G --> H[Background Worker Pool - asyncio, BRPOP]
    H --> I{Dynamic Batch Accumulator - 16 requests OR 20ms}
    I --> J[PyTorch Inference Engine]
    J --> K[Compute Embedding + Mock Downstream Task]
    K --> L[Write Result to Semantic Cache]
    K --> M[Return Output to Client]
```

## Tech stack

- **Language**: Python 3.12+
- **API layer**: FastAPI, Uvicorn
- **Store / cache / queue**: Redis 7.x (`redis.asyncio`)
- **ML**: PyTorch, sentence-transformers (`all-MiniLM-L6-v2`), NumPy
- **Infra**: Docker, Docker Compose
- **Load testing**: Locust
- **Testing**: pytest, httpx

## Status

Base project (all 6 phases) complete — see `plan.md` for the phased roadmap and `progress.md` for current status. Stretch goals not yet started.

## Load test results (Phase 6)

Measured with Locust, 500 concurrent simulated clients against the real running stack (not estimates) — full methodology, raw output, and findings in `phases/phase-6.md`.

| Scenario | What it forces | Throughput | p50 / p95 / p99 latency | Result |
|---|---|---|---|---|
| **Cold** (0% cache hits) | Every request: rate limit → embed → cache scan → queue → worker → batch | 3.87 req/s completed | 13.0s / 25.0s / 35.0s | 67.7% of requests hit the API's own 10s SLA timeout (504) — a real, GIL-bound compute ceiling, not a crash |
| **Warm** (100% cache hits) | Every request short-circuits at the cache, no embed/worker | 70.23 req/s | 4.0s / 15.0s / 42.0s | 0 failures |
| **Overload** (5 shared client_ids) | Rate limiter specifically, cheap cached request body | 604 req/s aggregate | 0.27s / 0.95s / 1.5s (accepted requests) | 95.3% of requests correctly rejected with HTTP 429 |

**Cache-hit vs cache-miss latency reduction:** ~69% at p50 under full 500-client load; ~96% comparing best-case single requests (44ms hit vs 1033ms miss) at light load.

**Why Cold's numbers look low for "500 concurrent clients":** the semantic cache's brute-force similarity scan (`json.loads` + NumPy cosine similarity per request) is CPU-bound, GIL-bound Python work — Phase 4 measured that offloading it to a thread frees the event loop but doesn't grant real parallelism, since concurrent GIL-bound threads contend rather than parallelize. At 500 truly concurrent clients that ceiling stops being "slower than hoped" and starts producing outright SLA timeouts. This is a known, deliberately-not-"fixed" limitation — the honest number, not a bug. Full root-cause writeup in `phases/phase-4.md`.

## What this isn't

This is a portfolio-scale project, not a production system. Notably:
- No auth on the API
- Redis List as a queue instead of a real message broker (no delivery guarantees, no dead-letter handling)
- Single-node design — the rate limiter and cache work correctly across multiple API instances since state lives in Redis, but there's no load balancer or multi-node deployment here
- `/v1/predict` is synchronous only — no task-id/polling delivery model in the base build (that's a stretch goal)
- No vector-search module (RediSearch/Redis Stack) — similarity search is brute-force cosine comparison in Python over plain Redis 7.x

These are known, deliberate scope cuts — see `plan.md` for what I'd add given more time.
