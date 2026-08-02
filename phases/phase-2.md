# Phase 2: Async API Gateway & Token Bucket Rate Limiter

Status: done and verified, 2026-08-02.

## What got built

```
semantic-queue/
├── app/
│   ├── main.py            # FastAPI app: /health, /v1/predict, rate-limit wiring
│   └── rate_limiter.py     # TokenBucketRateLimiter (redis.asyncio + Lua script)
├── config/
│   └── settings.py         # REDIS_URL, rate limit capacity/window (env-overridable)
```

`app/main.py` replaces the Phase 1 placeholder entirely — the single `GET /`
stub is gone.

## What each piece is and why it exists

**`config/settings.py`** — the values that tune the rate limiter (capacity,
window) and where Redis lives, read from env vars with the `plan.md`
defaults (100 req / 60s) as fallback. Pulled out of `main.py` so Phase 3+
config (similarity threshold, batch size) has an obvious place to live
instead of accumulating as scattered constants across files.

**`app/rate_limiter.py`** — `TokenBucketRateLimiter`, one Redis-backed
bucket per `client_id` (key: `rate_limit:{client_id}`). The actual
check-and-decrement runs as a Redis Lua script, not a plain GET-then-SET,
for atomicity — see "Why atomicity needed a Lua script" below.

**`app/main.py`** — the FastAPI app itself:
- `get_client_id` — a dependency that reads `X-Client-ID` and raises
  `400` if it's missing or empty.
- `enforce_rate_limit` — depends on `get_client_id`, then calls the
  limiter; raises `429` if the bucket's empty.
- `GET /health` — no client ID, no rate limit. A liveness check
  shouldn't be gated behind tenant identification.
- `POST /v1/predict` — the real endpoint: validate client → rate-limit
  → generate a `task_id` → create an `asyncio.Future` → hand the request
  to a (currently fake) worker → await the Future → return the result.

## Plain-language analogies

**Token bucket, the actual mechanism.** Picture a bucket that holds at
most 100 marbles. Water drips into it constantly — not "refills to full
once a minute," but a slow, continuous drip, worth about 1.67 marbles
every second (100 marbles / 60 seconds). Every request that comes in
takes one marble out. If the bucket's empty, the request is turned away
(`429`) — but marbles keep dripping in the whole time, so a request
30 seconds later might find 50 marbles waiting, not zero. This is
different from a "fixed window" limiter (dump 100 marbles in at the
start of every 60-second block, allow requests until they run out, dump
again at the boundary) — fixed windows create bursty behavior right at
the boundary (100 requests at 0:59, another 100 at 1:00 — 200 in two
seconds). `plan.md`'s "100 req/min" phrasing works for either design; I
picked continuous drip because it's what "token bucket" specifically
means as an algorithm, and it's the one without the boundary-burst
problem.

**Why atomicity needed a Lua script.** If "check how many marbles are
left" and "take one out" are two separate steps, two requests arriving
at the exact same instant can both see "1 marble left," both decide
they're allowed, and both take it — the bucket goes negative and the
limiter stops limiting. A Lua script sent to Redis runs as a single,
uninterruptible unit on the server — like a single cashier at a toll
booth who checks your fare and drops the barrier in one motion, instead
of a self-service booth where two cars could both see the barrier open
and both drive through. `TokenBucketRateLimiter.allow()` sends the
entire "read tokens, compute refill, check, decrement, write back" logic
as one `EVAL` call, so Redis itself guarantees no two requests for the
same `client_id` can interleave.

**The `asyncio.Future` mechanism (why `/v1/predict` can "hold the
connection open").** Every incoming request gets a `task_id` and a
matching `asyncio.Future` — think of it as a claim ticket with a locked
mailbox attached, both created the moment the request arrives. The
request handler doesn't return a response yet; it just waits at the
mailbox (`await future`). Whatever eventually produces the real answer
(today: a fake worker that sleeps half a second; from Phase 4 on: the
real inference worker) drops the result in that specific mailbox
(`future.set_result(...)`), which is what lets the waiting request wake
up and finally respond. This only works because it's all one process —
the Future lives in the API process's memory, so only code running in
that same process can resolve it directly.

## Deviations from `plan.md`, and why

1. **Continuous-refill algorithm, not a fixed-window counter.** Covered
   above — `plan.md`'s wording didn't fully disambiguate the two, and
   continuous refill is the more defensible reading of "token bucket."

2. **Atomicity via a Redis Lua script.** `plan.md` didn't specify the
   mechanism, only the requirement ("make sure the check-and-decrement
   is atomic"). A Lua `EVAL` was the most direct way to guarantee it
   without adding a second moving part (e.g. Redis `WATCH`/`MULTI`
   optimistic-locking retries, which is more code for the same
   guarantee here).

3. **Rate-limit keys auto-expire.** Each `rate_limit:{client_id}` key
   gets `EXPIRE`d to `2x` the window (120s at current settings) every
   time it's touched. Not in `plan.md`, but without it, a bucket for
   every `client_id` that ever made one request would live in Redis
   forever — a portfolio-scale annoyance today, an unbounded-memory
   problem at any real scale.

4. **`config/settings.py` introduced.** `plan.md` didn't call out a
   settings module, but the `config/` folder was already reserved for
   exactly this in Phase 1's writeup.

5. **10s timeout on `/v1/predict`'s wait, returning `504`.** Not in
   `plan.md`. A request that awaits a Future with no timeout hangs
   forever if whatever's supposed to resolve it (today: the stub;
   later: the real worker) crashes or never runs. This is a bound on a
   failure mode, not new functionality.

6. **Phase 4 stub (`_fake_worker`), exactly as instructed.** Sleeps
   0.5s then resolves the Future with `{"stub": true, "echo": <input
   text>}`. Marked clearly in code with a comment block and a "delete
   this" note — it exists solely so the full `/v1/predict` request path
   is exercisable before Phase 4's real queue/worker exist.

7. **Open question for Phase 4, not resolved here:** the plan's
   phrasing ("resolved by the worker") is easy to misread as the worker
   process directly calling `future.set_result()`. It can't — Phase 4's
   worker is a separate OS process (`workers/inference_worker.py`) and
   can't touch an `asyncio.Future` living in the API process's memory.
   The real design will need something *inside* the API process that
   notices the worker's result (most likely by watching a Redis key or
   list the worker writes to) and resolves the local Future itself.
   Today's stub sidesteps this because the fake worker happens to run
   as a coroutine in the same process — that convenience goes away in
   Phase 4.

## Verification

All commands run against the live containers (`docker compose up -d`
already applied Phase 2's code).

**Health check:**
```
$ curl -s -w "\nHTTP %{http_code}\n" http://localhost:8001/health
{"status":"ok"}
HTTP 200
```

**Missing `X-Client-ID` → 400:**
```
$ curl -s -w "\nHTTP %{http_code}\n" -X POST http://localhost:8001/v1/predict \
    -H "Content-Type: application/json" -d '{"text": "hello"}'
{"detail":"X-Client-ID header is required"}
HTTP 400
```

**Single valid request → 200, stub result, full round trip through the
Future:**
```
$ curl -s -w "\nHTTP %{http_code}\n" -X POST http://localhost:8001/v1/predict \
    -H "Content-Type: application/json" -H "X-Client-ID: smoke-test" -d '{"text": "hello"}'
{"task_id":"4a31b2eb-adff-4bc8-bb00-60c9c78ec33d","result":{"stub":true,"echo":"hello"}}
HTTP 200
```

**Rate limit enforcement — 110 concurrent requests from one `client_id`,
capacity 100:**
```
$ docker compose exec api python -c "
import asyncio, httpx, collections
async def main():
    async with httpx.AsyncClient(base_url='http://localhost:8000', timeout=15.0) as client:
        async def hit():
            r = await client.post('/v1/predict', json={'text': 'hello'}, headers={'X-Client-ID': 'burst-test'})
            return r.status_code
        results = await asyncio.gather(*[hit() for _ in range(110)])
        print(collections.Counter(results))
asyncio.run(main())
"
Counter({200: 103, 429: 7})
```
103, not exactly 100 — expected, not a bug. The 110 requests aren't
dispatched in true zero time; while they're all in flight, the bucket
keeps refilling at ~1.67 tokens/sec, so a few extra requests squeeze
through during that window. This is the continuous-refill design working
as intended (see analogy above), not the fixed-100-then-hard-stop
behavior a naive counter would give.

**Refill after waiting — same `client_id`, bucket was just emptied:**
```
$ sleep 3
$ curl -s -w "\nHTTP %{http_code}\n" -X POST http://localhost:8001/v1/predict \
    -H "Content-Type: application/json" -H "X-Client-ID: burst-test" -d '{"text": "post-refill check"}'
{"task_id":"23981ac6-e501-41fb-ad5c-636fa63b0333","result":{"stub":true,"echo":"post-refill check"}}
HTTP 200
```
3 seconds at ~1.67 tokens/sec refills ~5 tokens — comfortably enough for
one more request to succeed, confirming the bucket recovers over time
rather than staying locked out until some fixed reset instant.

**Direct look at what's actually stored in Redis** (not required for
the plan.md "done" bar, but useful for debugging later):
```
$ docker compose exec redis redis-cli HGETALL rate_limit:burst-test
tokens
20.94041363398236
last_refill
1.7856951206146207e+9
$ docker compose exec redis redis-cli TTL rate_limit:burst-test
115
```

## How to re-verify this later

```bash
cd ~/Projects/semantic-queue
docker compose up -d --build     # picks up any code changes
docker compose ps                # both should be Up, redis healthy

# health
curl -s -w "\nHTTP %{http_code}\n" http://localhost:8001/health

# missing header -> 400
curl -s -w "\nHTTP %{http_code}\n" -X POST http://localhost:8001/v1/predict \
    -H "Content-Type: application/json" -d '{"text": "hello"}'

# valid request -> 200
curl -s -w "\nHTTP %{http_code}\n" -X POST http://localhost:8001/v1/predict \
    -H "Content-Type: application/json" -H "X-Client-ID: smoke-test" -d '{"text": "hello"}'

# burst past the limit -> mix of 200s and 429s (use a fresh client_id if
# the previous test's bucket hasn't refilled)
docker compose exec api python -c "
import asyncio, httpx, collections
async def main():
    async with httpx.AsyncClient(base_url='http://localhost:8000', timeout=15.0) as client:
        async def hit():
            r = await client.post('/v1/predict', json={'text': 'hello'}, headers={'X-Client-ID': 'burst-test-2'})
            return r.status_code
        print(collections.Counter(await asyncio.gather(*[hit() for _ in range(110)])))
asyncio.run(main())
"

# wait, then confirm the same client_id succeeds again
sleep 3
curl -s -w "\nHTTP %{http_code}\n" -X POST http://localhost:8001/v1/predict \
    -H "Content-Type: application/json" -H "X-Client-ID: burst-test-2" -d '{"text": "post-refill"}'
```
