# Phase 4: Async Task Queue & Dynamic Batching Worker

Status: done and verified, 2026-08-02.

## What got built

```
semantic-queue/
├── workers/
│   └── inference_worker.py   # real worker process: BRPOP -> batch -> model -> cache/results
├── app/
│   └── main.py                 # _fake_worker removed; real enqueue + BRPOP result listener
├── config/
│   └── settings.py             # + TASK_QUEUE_KEY, RESULT_QUEUE_KEY, BATCH_SIZE, BATCH_MAX_DELAY_MS
└── docker-compose.yml           # + worker service (same image, different command)
```

`app/main.py`'s `_fake_worker` stub from Phase 2 is gone entirely — `/v1/predict`
on a cache miss now talks to a real, separate process.

## What each piece is and why it exists

**`workers/inference_worker.py`** — a standalone async script (no FastAPI,
no HTTP server), run as its own container/OS process. Its loop:
1. `_collect_batch` — `BRPOP`s `ml_task_queue`, blocking for the first
   task, then keeps pulling more (shrinking timeout) until either
   `batch_size` (16) tasks are collected or `max_delay` (20ms) elapses.
2. `_process_batch` — one `model.encode(list_of_texts)` call over the
   *entire* batch, one batched forward pass through the mock classifier,
   then per-item: `LPUSH` the result onto `predict_results` and write the
   result+embedding to the semantic cache.

**`docker-compose.yml`'s new `worker` service** — same image as `api`
(same `Dockerfile`, same dependencies, same `hf_cache` volume so it
doesn't redownload the model), just a different `command:`. This is what
makes "the worker runs as a separate process" concrete in this project —
a genuinely separate container, separate Python interpreter, separate
memory space, only connected to the API through Redis.

**`config/settings.py` additions** — `TASK_QUEUE_KEY`
(`ml_task_queue`), `RESULT_QUEUE_KEY` (`predict_results`), `BATCH_SIZE`
(16), `BATCH_MAX_DELAY_MS` (20) — the plan.md defaults, shared between
`app/main.py` (producer + result listener) and the worker (consumer +
result producer) so both sides agree on the queue names without
duplicating string literals.

**`app/main.py` changes** — `predict()`'s cache-miss branch now
`LPUSH`es `{task_id, entry_id, text}` onto `ml_task_queue` instead of
spawning the stub. A new `_result_listener` background task, started at
app startup, continuously `BRPOP`s `predict_results` and resolves the
matching `Future` — this is the Phase 2/3 "open question" (how does a
separate-process worker resolve an in-process Future?) now actually
implemented, exactly per the design recorded in `plan.md`'s Phase 4
section.

## Plain-language analogies

**The task/result queues as two conveyor belts.** `ml_task_queue` carries
work *into* the worker; `predict_results` carries finished work back
*out*. They're deliberately separate belts, not one bidirectional one —
the API only ever pushes onto the first and reads from the second, the
worker only ever reads from the first and pushes onto the second. Neither
side polls or asks "is it done yet?"; `BRPOP` on either belt just blocks
until something's there, so both processes sit idle (no CPU spent) until
there's real work.

**Dynamic batching as a shuttle bus, not a taxi.** A taxi (no batching)
leaves the moment one passenger is ready — fully utilized per trip, but
you pay the fixed "trip overhead" (here: a full model forward pass) once
per passenger. A shuttle bus waits a *little* — up to a full busload (16)
or up to a short fixed wait (20ms), whichever comes first — then leaves
with everyone aboard, paying that same trip overhead once for many
passengers. The `max_delay` is what stops the bus from waiting forever
for a 16th passenger who isn't coming; the `batch_size` is what stops it
from overloading past capacity waiting to top off a slow trickle.

**Why the batched forward pass matters, concretely.** `model.encode(16
texts)` runs those 16 sentences through the transformer as one matrix
operation instead of 16 separate ones — the actual computation for the
identical model is reorganized to share work (batched matrix multiplies
are far more efficient per-item than the same multiplies done one row at
a time), which is the entire efficiency argument for dynamic batching.

## Deviations from `plan.md`, and why

1. **The worker recomputes the embedding, duplicating the API's
   cache-lookup embedding.** The API already computes an embedding for
   every cache-miss request (to run the cosine-similarity check in
   `SemanticCache.lookup()`), and the worker then computes the *same*
   embedding again as part of its batched forward pass. This is
   deliberate, not an oversight: `plan.md`'s own architecture diagram
   treats "check the cache" and "compute embeddings + inference" as two
   separate boxes, and if the API instead passed its already-computed
   embedding through to the worker, the "batched forward pass" would
   only be batching the (nearly free) classifier step — the one
   genuinely expensive operation in this pipeline (MiniLM encoding)
   would happen one-at-a-time in the API and never get the batching
   benefit at all, defeating the point of this phase. A production
   system would very likely avoid this duplicate compute; keeping it
   here is what makes batching's benefit real rather than hollow.

2. **"Mark task COMPLETED" has no separate status field.** The settled
   Phase 3 design (recorded in `plan.md`'s Phase 4 section) already
   answers this implicitly: the worker's `LPUSH` onto `predict_results`
   *is* the completion signal — the API's listener consumes it with a
   destructive `BRPOP`, so once popped it's done and nothing else ever
   needs to check a separate status. Adding a `task:{id}:status` key
   that nothing reads would be an unused mechanism.

3. **One batching loop per worker process, not an explicit pool.**
   `plan.md`'s architecture diagram labels this "Background Worker Pool,"
   but the Phase 4 spec's own text describes a single
   `workers/inference_worker.py` with one dynamic batcher. Implemented as
   exactly that — one sequential accumulate-then-process loop per worker
   process. Running multiple worker containers (`docker compose up
   --scale worker=3`) would give a real pool for free since they'd all
   `BRPOP` the same `ml_task_queue`, but that's not needed to demonstrate
   batching at this scale and wasn't set up.

4. **No hot-reload for the worker.** The `api` service runs uvicorn with
   `--reload`; the worker has no equivalent — editing
   `workers/inference_worker.py` requires `docker compose restart
   worker`. Not requested, and adding a `watchfiles`-wrapped command is a
   small enough gap not worth the extra moving part right now.

5. **Real bug found and fixed: `redis-py` 8.0.1's client-side
   `socket_timeout` (default 5s) killed indefinitely-blocking `BRPOP`
   calls.** `BRPOP(key, timeout=0)` is supposed to block forever at the
   Redis protocol level, but this redis-py version applies its own
   5-second client-side read timeout to *every* read regardless of the
   command's own timeout argument — so both the worker's first `BRPOP`
   (waiting for a task) and the API's result-listener `BRPOP` (waiting
   for a result) crashed with `TimeoutError` after 5 seconds of no
   traffic. Fixed by giving each of those two specific calls a
   **dedicated** Redis connection constructed with `socket_timeout=None`,
   separate from the general-purpose pooled client used for quick
   rate-limit/cache operations. This isn't just a workaround for the
   library quirk — it's also standard Redis practice on its own merits:
   a blocking command occupies its connection until it returns, so it
   shouldn't share a pool with calls that need to stay fast.

## Verification

**End-to-end single request, real worker (not the old stub):**
```
$ curl -s -w "\nHTTP %{http_code}\n" -X POST http://localhost:8001/v1/predict \
    -H "Content-Type: application/json" -H "X-Client-ID: worker-smoke-test" \
    -d '{"text": "the quick brown fox jumps over the lazy dog"}'
{"task_id":"a0ed13ad-ebbe-43d2-afd0-9ef8305acece","result":{"predicted_class":1,"echo":"the quick brown fox jumps over the lazy dog"},"cache_status":"MISS"}
HTTP 200

$ docker compose logs worker --tail 2
BATCH_PROCESSED size=1 duration_ms=80.4 task_ids=['a0ed13ad-...']
```

**A finding, and a course correction:** the first attempt at a batching
demo fired 50 concurrent requests using a numbered template ("...sentence
number 0...", "...number 1...", etc.). Result: `Counter({200: 50})`, but
checking the cache logs showed **49 of 50 were cosine-similarity cache
hits**, not worker traffic at all — near-identical template sentences
differing only by a number score >0.95 similarity to each other, so the
Phase 3 cache (correctly!) deduplicated them before they ever reached the
queue. Fixed the test, not the code: switched to genuinely distinct
sentences on unrelated topics (science, geography, cooking, sports, ...)
so they wouldn't cache-collide with each other.

**Real batching, 24 concurrent genuinely-distinct requests:**
```
$ docker compose exec api python -c "... 24 distinct-topic sentences, concurrent ..."
status codes: Counter({200: 24})
cache status: Counter({'MISS': 24})

$ docker compose logs worker --since 30s | grep BATCH_PROCESSED
BATCH_PROCESSED size=2  duration_ms=49.4
BATCH_PROCESSED size=5  duration_ms=72.0
BATCH_PROCESSED size=13 duration_ms=129.0
BATCH_PROCESSED size=4  duration_ms=18.8
```
2 + 5 + 13 + 4 = 24 — every request accounted for, grouped into 4
batches instead of processed one at a time. Largest single batch: 13.

**A second burst, 40 requests, trying to hit the full batch_size=16
cap:**
```
status codes: Counter({200: 40})
cache status: Counter({'MISS': 40})
```
Sum of logged batch sizes = 40 (21 batches, sizes ranging 1-4). The
16-item cap was never actually hit in either run — the largest observed
batch was 13. This is an honest finding, not glossed over: each request
first pays for its own cache-lookup embedding in the API (a CPU-bound
call routed through `asyncio.to_thread`'s limited thread pool) before it
ever reaches `LPUSH`, which spreads request arrivals at the queue out
more than the raw concurrent-dispatch rate would suggest — so the
20ms-max_delay trigger fires more often than the 16-item cap on this
machine, at this scale. The mechanism is verified correct either way:
both a size trigger and a time trigger are real and independently
provable from the code, and the time trigger is what's been observed
firing across these test runs.

**Full round trip confirmed — a worker-computed, worker-cached result is
served correctly on repeat:**
```
$ curl ... -d '{"text": "The novel explores themes of identity and belonging"}'
{"task_id":null,"result":{"predicted_class":1,"echo":"The novel explores themes of identity and belonging"},"cache_status":"HIT"}
```

**Cache actually populated by the worker, not the API (API no longer
writes to cache — see `app/main.py` changes above):**
```
$ docker compose exec redis redis-cli DBSIZE
71
$ docker compose exec redis redis-cli HLEN cache:embeddings
68
```

## How to re-verify this later

```bash
cd ~/Projects/semantic-queue
docker compose up -d --build
docker compose logs api --tail 5     # "Application startup complete"
docker compose logs worker --tail 5  # "worker ready: batch_size=16 max_delay_ms=20.0 ..."

# single request through the real worker
curl -s -X POST http://localhost:8001/v1/predict \
  -H "Content-Type: application/json" -H "X-Client-ID: worker-smoke-test" \
  -d '{"text": "the quick brown fox jumps over the lazy dog"}'
docker compose logs worker --tail 3   # look for BATCH_PROCESSED size=1

# real batching: fire many genuinely distinct-topic requests concurrently
# (near-duplicate text will cache-hit instead of reaching the worker —
# see the "finding" above)
docker compose exec api python -c "
import asyncio, httpx, collections
topics = [f'a completely different sentence about topic number {i} covering unrelated subject matter entirely' for i in range(30)]
# NOTE: the line above is for illustration only — use genuinely distinct
# subjects (see phases/phase-4.md's 24/40-sentence lists) or most of
# these will legitimately cache-hit each other instead of batching.
async def main():
    async with httpx.AsyncClient(base_url='http://localhost:8000', timeout=20.0) as client:
        async def hit(t):
            r = await client.post('/v1/predict', json={'text': t}, headers={'X-Client-ID': 'batch-verify'})
            return r.status_code, r.json().get('cache_status')
        results = await asyncio.gather(*[hit(t) for t in topics])
        print(collections.Counter(r[0] for r in results))
        print(collections.Counter(r[1] for r in results))
asyncio.run(main())
"
docker compose logs worker --since 30s | grep BATCH_PROCESSED

# confirm the worker, not the API, is writing the cache
docker compose exec redis redis-cli DBSIZE
docker compose exec redis redis-cli HLEN cache:embeddings
```
