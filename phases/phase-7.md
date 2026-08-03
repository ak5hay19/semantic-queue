# Phase 7 (Stretch 5): RediSearch Vector Search

Status: done and verified, 2026-08-03.

This is a stretch goal, not a base-project phase — the base project (Phases
1–6) is unchanged and complete. It's numbered "Phase 7" here purely because
it's the seventh piece of work done on this codebase in sequence, and
because it directly supersedes one specific part of Phase 3's design going
forward. **Phase 3's own history is not edited or erased** — `phases/phase-3.md`
still accurately describes what was built and why brute-force was the right
call *at that point*; this phase explains why it stopped being the right
call once Phase 6 put real numbers behind it.

## What got built

```
semantic-queue/
├── docker-compose.yml     # redis:7-alpine -> redis/redis-stack-server:7.4.0-v3
├── app/cache.py            # brute-force NumPy scan -> RediSearch HNSW + real KNN
├── app/main.py              # cache gets its own dedicated (raw-bytes) Redis connection
├── workers/inference_worker.py  # same dedicated-connection change, worker side
└── config/settings.py       # + EMBEDDING_DIM
```

## Why this, and why now — the actual trigger

`plan.md`'s Phase 3 section always named this as the alternative: "Vector
similarity search implementation" was an explicit decision point, resolved
at the time as *brute-force in Python* specifically because a portfolio-scale
cache didn't need a dedicated vector-search module yet. That reasoning was
sound when it was made — nobody had measured what "not yet" actually cost.

Phase 6 measured it. Running the Cold scenario (every request a genuine
cache miss, 500 concurrent clients) found that **67.7% of requests missed
the API's own 10-second SLA and came back as HTTP 504** — not a crash, a
real, reproducible ceiling. Phase 4 had already root-caused *why*:
`SemanticCache.lookup()`'s brute-force step (`HGETALL` every cached
embedding into the API process, `json.loads` each one, build a NumPy
matrix, compute cosine similarity) is CPU-bound, GIL-bound Python work.
Offloading it to a thread (already done, back in Phase 4) frees the event
loop but doesn't grant real parallelism — concurrent GIL-bound threads
contend rather than parallelize. At 500 concurrent clients, that ceiling
stopped being "slower than hoped" and started being "most requests don't
finish in time." This phase replaces the specific step that ceiling lives
in — nothing else in the request path changes.

## What RediSearch/HNSW actually is, in plain terms

**The old approach was a librarian who reads every single book in the
building, cover to cover, every time you ask a question**, to figure out
which one is most relevant — correct, but the work scales with how many
books are on the shelves, and it's all happening in the librarian's own
head (the API process), one book at a time (a Python loop), with only one
librarian able to think at once (the GIL). Add more books (more cache
entries) and every future question takes longer, no matter how well
organized the shelves are, because "reading every book" was never actually
about the shelving — it's about the reading.

**HNSW (Hierarchical Navigable Small World) is a pre-built index that lets
you skip almost all of the books.** Think of it like this: instead of
reading everything, the librarian pre-organizes the whole collection into
a multi-level map ahead of time — a small set of "highway" landmarks at the
top level that roughly sort the whole collection into neighborhoods, and
progressively finer, "local street" connections at lower levels. A new
question starts at the top level, jumps to the landmark closest to it,
then descends level by level, only ever looking at a handful of
candidates at each step — never the whole collection. It's *approximate*
(there's a small chance the truly-best answer's neighborhood gets missed),
but the accuracy/speed trade-off is exactly what makes it practical at any
real scale, and it's the standard algorithm behind essentially every
production vector database.

**Critically, this work now happens inside Redis itself** — a piece of
purpose-built C code maintaining its own index structure — **not inside
the API's own Python process.** The API sends one query (a single Redis
round trip: "find me the nearest neighbor to this vector") and gets back
one answer. There's no bulk data transfer of every embedding into Python,
no `json.loads` loop, no NumPy matrix build, and critically, none of it
runs on the API's own event loop or competes for its GIL — it's someone
else's CPU (Redis's own process) doing the search, in native code,
not concurrent Python threads contending with each other.

## The actual code change

**Storage**, `app/cache.py`: Phase 3/6's design kept embeddings in a
*separate* bulk hash (`cache:embeddings`, one `HGETALL` pulling every
embedding at once) from each entry's own hash (`cache:entry:{id}`, holding
`text`/`result`). That separate structure existed purely to make the
brute-force bulk-fetch efficient — with RediSearch doing the search
instead, it's not needed. Embeddings now live as a third field, `embedding`
(a raw packed `float32` buffer), directly on each entry's own hash. One
`HSET` writes text, result, and embedding together; RediSearch watches the
`cache:entry:` key prefix and indexes the `embedding` field automatically,
in the background, the moment a matching hash is written — no separate
"add to index" call from application code at all.

**Lookup**, `app/cache.py`: the exact-match branch is unchanged in
behavior (still a single O(1) key lookup on `sha256(text)`), just switched
from `HGETALL` to `HMGET ... result` (only the field actually needed). The
brute-force branch — `HGETALL` the bulk hash, decode every embedding,
build a matrix, compute cosine similarity, all inside `asyncio.to_thread`
— is gone entirely, replaced by:

```python
query = (
    Query("*=>[KNN 1 @embedding $vec AS score]")
    .sort_by("score")
    .return_fields("score", "result")
    .dialect(2)
)
search_result = await self._index.search(query, query_params={"vec": query_vector.tobytes()})
```

One `await`, one Redis round trip, no Python-side loop over cached
entries regardless of how many there are.

**A new `ensure_index()` method**, called once at startup by both the
`api` and `worker` processes (idempotent — whichever runs `FT.CREATE`
first wins, the other catches Redis's own "Index already exists" error
and moves on):

```python
VectorField("embedding", "HNSW", {
    "TYPE": "FLOAT32", "DIM": EMBEDDING_DIM, "DISTANCE_METRIC": "COSINE",
})
```

**A dedicated Redis connection for the cache**, in both `app/main.py`
(`app.state.cache_redis`) and `workers/inference_worker.py`
(`cache_redis`), constructed with `decode_responses=False` — separate
from the general-purpose client used for rate limiting and the task
queue. This is not optional: the `embedding` hash field is raw binary,
and the shared client's `decode_responses=True` setting tries to UTF-8-decode
*every* reply on that connection, which corrupts (or outright crashes on)
binary vector data. `decode_responses` is a connection-level setting in
redis-py, not per-field, so the cleanest fix is what the rest of this
project already does for other special-purpose connections (see Phase
4's dedicated `blocking_redis`): give this one specific need its own
connection instead of contorting the shared one.

## Verifying the threshold behavior is equivalent — measured, not assumed

`plan.md`'s Phase 3 threshold (`cosine similarity > 0.92`) needed to mean
the same thing under RediSearch's `COSINE` distance metric, which returns
a *distance*, not a similarity. Checked directly against the live model
before writing any lookup logic, the same way Phase 3/5/6 already
established as this project's own standard for exactly this kind of claim:

```
doc.score (RediSearch, COSINE distance):  0.025000035762786865
1 - doc.score (implied cosine similarity): 0.9749996423721313
numpy cosine similarity (same two vectors, computed directly): 0.9749999642372131
```

Confirmed to 4+ decimal places: RediSearch's `COSINE` distance is exactly
`1 - cosine_similarity`. `SemanticCache.lookup()`'s threshold check became
`best_score = 1.0 - float(doc.score); if best_score > self.threshold`,
preserving the exact same "> 0.92" semantics against the exact same
underlying quantity — just computed by RediSearch instead of NumPy.

Also checked (not assumed): an empty index (`FT.SEARCH` against zero
indexed documents) returns `total: 0`, `docs: []` with no error — matches
the old "empty cache" branch's behavior exactly.

## A real bug the re-verification step caught: HNSW's default recall was too low

The threshold-semantics check above was run against a small, freshly-created
index (a handful of entries) and passed cleanly — `0.975` similarity,
matched exactly. But after the Cold load test grew the cache to ~1700
entries, re-running the pytest suite (the same explicit instruction to
"re-run the Phase 3/5 test cases... confirm they still pass") caught a real
failure: `test_cosine_similarity_hit_above_threshold` came back `MISS`
instead of `HIT` for the exact same verified 0.9750-similarity pair that had
just passed moments earlier at small scale.

Investigated directly rather than assumed away: queried the ~1700-entry
index for the true best match to `PARAPHRASE_B` and printed the top 3
results back with their distances.

```
total: 3
cache:entry:c5519461... score(distance): 0.777  (cosine sim ≈ 0.22)
cache:entry:9b598202... score(distance): 0.785  (cosine sim ≈ 0.22)
cache:entry:c4c6e3b6... score(distance): 0.789  (cosine sim ≈ 0.21)
true cosine sim, PARAPHRASE_A vs PARAPHRASE_B (plain NumPy): 0.9750
```

**The actual near-duplicate (0.975 similarity) wasn't in the top 3 results
at all** — HNSW returned three essentially-unrelated candidates (~0.21–0.22
similarity) instead. This isn't HNSW's normal, expected approximation error
(missing a marginal, borderline candidate); it's a wholesale miss of an
obvious, extremely close match. The culprit: `EF_RUNTIME`, RediSearch's
search-time "how hard to look" parameter, defaults to **10** — tuned by
RediSearch for speed, not recall, and apparently not nearly wide enough to
reliably surface a true nearest neighbor once the graph holds ~1700 nodes.

Confirmed the specific cause and the fix by sweeping `EF_RUNTIME` directly
against the same real, unmodified 1700-entry index:

```
EF_RUNTIME=10:   found_true_match=False  (RediSearch's own default)
EF_RUNTIME=100:  found_true_match=True
EF_RUNTIME=500:  found_true_match=True
EF_RUNTIME=2000: found_true_match=True
```

And confirmed the fix isn't a hidden throughput trade-off, by timing 20
queries at each value against the same index:

```
EF_RUNTIME=10:   avg 0.69ms  (min 0.38ms, max 4.86ms)
EF_RUNTIME=100:  avg 0.59ms
EF_RUNTIME=200:  avg 0.68ms
EF_RUNTIME=500:  avg 0.76ms
EF_RUNTIME=1000: avg 0.86ms
```

All sub-millisecond, no meaningful difference — at this project's scale,
RediSearch's default was trading away correctness for a speed benefit that
doesn't actually exist here. **Fixed** by adding `CACHE_EF_RUNTIME=300`
(`config/settings.py`) and passing it explicitly in the KNN query:
`*=>[KNN 1 @embedding $vec EF_RUNTIME 300 AS score]`. Comfortable margin
above the `100` that was confirmed sufficient, still with no measurable
latency cost.

**Why this matters, and why it was worth catching now instead of shipping
it:** a semantic cache's entire value proposition is "reliably catch
near-duplicate requests." A cache that silently, intermittently fails to
recognize an obvious near-duplicate once it grows past a few hundred
entries doesn't just lose a small performance optimization — it
quietly degrades the exact property Phase 3 built this feature to
guarantee, and it would have gotten *worse* over time as the cache kept
growing, not better. This is also a direct, concrete illustration of why
this phase's instructions insisted on measuring rather than assuming
threshold-equivalence: a check run once against a small, empty-ish index
would never have caught this, because the failure mode is scale-dependent.
The full pytest suite (re-run twice for repeatability, still against this
same ~1700-entry index, not a freshly flushed one) now passes cleanly with
the fix in place — see "Re-verifying Phase 3/5's own test cases" below.

## Re-verifying Phase 3/5's own test cases

The exact three behaviors Phase 3 defined and Phase 5 turned into
automated tests, re-run live against the new implementation:

```
=== exact match (same text twice) ===
{"cache_status":"MISS", ...}
{"cache_status":"HIT", ...}                    # 2nd request, same text

=== cosine-hit (verified 0.9750 paraphrase pair, from phases/phase-3.md) ===
{"cache_status":"MISS", ...}                    # "Can you tell me what the weather is like today"
{"cache_status":"HIT", ...}                     # "Could you tell me what today weather is like"

=== dissimilar miss (verified 0.0027 pair, from phases/phase-3.md) ===
{"cache_status":"MISS", ...}                    # chef/meal sentence
{"cache_status":"MISS", ...}                    # submarine/ocean sentence
```

Structured logs for the cosine-hit case confirm the score:
```
"message": "cache hit (cosine_similarity)", "cache_status": "HIT",
"method": "cosine_similarity", "score": 0.975, "threshold": 0.92
```
`0.975` — matching Phase 3's own originally-measured `0.9750` for this
exact pair to 3 decimal places, computed via an entirely different code
path (RediSearch's C implementation vs. NumPy). This is the strongest
evidence that the swap is behaviorally equivalent, not just "probably
fine."

**Full pytest suite**, unchanged test files, run twice for repeatability —
**against the same ~1700-entry cache the `EF_RUNTIME` recall bug above was
found and fixed on**, not a freshly flushed small one, since that's the
only way this result actually proves the fix works at the scale that
mattered:
```
$ docker compose exec api python -m pytest tests/ -v
============================== 9 passed in 4.64s ===============================
$ docker compose exec api python -m pytest tests/ -v   # re-run
============================== 9 passed in 4.21s ===============================
```
All 9 tests — rate limiting, exact/cosine/dissimilar cache behavior, the
Phase 4 concurrency regression test, and batching — pass, including the one
that caught the recall bug on the first post-swap run. None of the test
files were modified for this phase; they exercise the public behavior of
`/v1/predict` and `SemanticCache`, which is exactly what's supposed to stay
stable across this swap — and exactly what caught the regression before it
shipped.

## The actual point of this phase: before/after at 500 concurrent clients

Same exact Locust configuration as Phase 6's Cold scenario
(`ColdUser`, `-u 500 -r 50 -t 60s`, guaranteed-novel word-salad request
bodies, fresh `X-Client-ID` per request, freshly flushed cache beforehand):

| | **Before** (brute-force, Phase 6) | **After** (RediSearch KNN, Phase 7, `EF_RUNTIME=300`) |
|---|---|---|
| Completed requests | 203 | **1241** |
| Failed / timed out (504) | 426 (67.7%) | **0 (0%)** |
| Completed throughput | 3.87 req/s | **22.81 req/s (5.9x)** |
| p50 latency (completed) | 13.0s | 21.0s |
| p95 latency (completed) | 25.0s | 23.0s |
| p99 latency (completed) | 35.0s | 25.0s |
| Min latency (completed) | 1.03s | 0.73s |

(This run used the final, `EF_RUNTIME=300`-fixed code — a first pass run
before that fix, with the default `EF_RUNTIME=10`, measured 23.33 req/s /
1239 completions / 0 failures: statistically the same numbers, confirming
the recall fix didn't cost any measurable throughput, exactly as the
direct per-query timing check above already predicted.)

**Read this table carefully — the p50 number went up, and that's not a
regression, it's a change in what's being measured.** In the "before" run,
67.7% of requests never got a real answer at all; they hit the app's own
10-second SLA and gave up. The 203 that *did* complete successfully were,
in effect, a lucky subset — whichever requests happened to clear the
congested system fast enough to beat that 10-second cutoff. That's a
biased sample, weighted toward the fast end, not a representative
latency number. In the "after" run, **every single request stayed in the
system until it got a real answer** — nothing gave up early — so the p50
here describes genuine full-system latency under 500-way contention for
100% of the traffic, not a survivorship-biased subset of it. The far more
meaningful comparison is throughput and failure rate: 6x more requests
actually completed, and the specific failure mode Phase 6 documented
(the majority of traffic timing out) is gone.

**What's the new bottleneck, now that brute-force cache-scan is gone?**
Checked directly, not assumed: batch sizes during this run stayed small
(395 batches of 1, 362 of 2, tapering off — 919 batches for 1260 tasks),
each processed in roughly 15–40ms. Summed, that's well under half of the
60-second window — the worker itself isn't saturated. The remaining
ceiling is most likely plain coordination overhead at 500-way concurrency
on a single event loop: each request still makes several sequential Redis
round trips (rate-limit check, exact-match `HMGET`, KNN search, task
enqueue) plus waits on the cross-process result listener, and with 500
requests all interleaving those steps on one asyncio event loop, pure
scheduling/context-switch overhead becomes non-trivial even with the
expensive compute step removed. This wasn't investigated further —
diagnosing *that* ceiling is future work, not this stretch goal's scope —
but it's flagged honestly rather than implying RediSearch alone makes
throughput unlimited.

## Deviations from the stretch-goal instructions, and why

1. **`redis/redis-stack-server:7.4.0-v3`, not `redis/redis-stack`.** The
   `-server` variant excludes RedisInsight (a bundled GUI) — unnecessary
   for a headless Docker Compose service reached only by the api/worker
   containers. Pinned to a specific tag (matching this project's existing
   pin-everything convention) rather than `latest`.
2. **Embedding storage consolidated onto the entry's own hash, dropping
   the separate `cache:embeddings` bulk hash entirely**, rather than
   adding a third parallel structure. Not explicitly requested, but it's
   a direct, low-risk simplification the new approach enables: the bulk
   hash's only reason to exist was making the brute-force bulk-fetch
   efficient, and RediSearch needs one hash per document to index in the
   first place, so keeping the old structure around would have meant
   maintaining two representations of the same data.
3. **Both `api` and `worker` call `ensure_index()` at startup**, not just
   one. Neither process has a fixed "runs first" guarantee across
   restarts/rebuilds, and the idempotent catch-and-ignore-if-exists
   pattern means there's no real cost to both attempting it.
4. **A dedicated `decode_responses=False` Redis connection for the cache,
   in both processes** — not explicitly called for in the instructions,
   but required the moment binary vector bytes and UTF-8 text share a
   connection's reply-decoding setting; documented in detail above.
5. **Not fixed, flagged instead: `FLUSHALL`/`FLUSHDB` now also destroys
   the RediSearch index**, not just the cached data. Under the old
   brute-force design, flushing Redis just meant "the cache is now empty
   but still fully functional." Under RediSearch, a flush removes the
   index definition along with the keyspace it was built over, and
   `/v1/predict` will 500 with `redis.exceptions.ResponseError: cache_idx:
   no such index` until a process re-runs `ensure_index()` (i.e. an
   `api`/`worker` restart). Ran into this directly while testing — see
   "How to re-verify" below for the correct flush sequence going forward.
   Worth knowing as a real operational difference before assuming a bare
   `FLUSHDB` is always safe.
6. **`CACHE_EF_RUNTIME` setting added (`config/settings.py`, default
   `300`), not in the original instructions.** Not a deviation chosen up
   front — it's the direct result of the "verify, don't assume" step
   the instructions explicitly asked for. RediSearch's own default
   `EF_RUNTIME` (10) measurably failed to find a genuine 0.975-similarity
   near-duplicate once the cache reached realistic scale (~1700 entries,
   reached by this phase's own load testing) — see "A real bug the
   re-verification step caught" above for the full investigation and the
   measurements behind the chosen value. Required to make the "0.92+
   still resolves as a HIT" guarantee actually hold at scale, not just at
   the small scale an initial check happened to run against.

## How to re-verify this later

```bash
cd ~/Projects/semantic-queue
docker compose up -d --build
docker compose logs redis --tail 5   # confirm redis-stack-server, not plain redis
docker compose exec redis redis-cli MODULE LIST | grep -A2 '"search"\|name.*search'
docker compose logs worker --tail 5  # "RediSearch index created" (first boot only)

# spot-check exact / cosine-hit / dissimilar-miss, same pairs Phase 3 verified
curl -s -X POST http://localhost:8001/v1/predict -H "Content-Type: application/json" \
  -H "X-Client-ID: p7-check" -d '{"text": "Can you tell me what the weather is like today"}'
curl -s -X POST http://localhost:8001/v1/predict -H "Content-Type: application/json" \
  -H "X-Client-ID: p7-check" -d '{"text": "Could you tell me what today weather is like"}'
docker compose logs api --since 10s | grep '"event": "cache_lookup"'   # expect score ~0.975

# full suite
docker compose exec api python -m pytest tests/ -v

# IMPORTANT: FLUSHDB/FLUSHALL now also removes the RediSearch index, not
# just the cached data — an api/worker restart is required afterward to
# recreate it (ensure_index() runs at startup), or /v1/predict will 500
# with "cache_idx: no such index" on the very next cache-miss request.
docker compose exec redis redis-cli FLUSHDB
docker compose restart api worker
docker compose logs worker --tail 3   # confirm "RediSearch index created" again

# re-run the same Cold-scenario comparison
./.venv/bin/locust -f locustfile.py ColdUser --host http://localhost:8001 \
  --headless -u 500 -r 50 -t 60s --only-summary
```
