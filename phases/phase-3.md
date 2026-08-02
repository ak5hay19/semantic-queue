# Phase 3: Redis Semantic Vector Cache

Status: done and verified, 2026-08-02.

## What got built

```
semantic-queue/
├── app/
│   ├── cache.py        # SemanticCache: exact-match + brute-force cosine similarity
│   └── main.py          # /v1/predict now checks the cache before doing any "work"
├── config/
│   └── settings.py       # + EMBEDDING_MODEL_NAME, CACHE_SIMILARITY_THRESHOLD
├── Dockerfile            # + HF_HOME, writable model cache dir for appuser
└── docker-compose.yml    # + hf_cache named volume
```

## What each piece is and why it exists

**`app/cache.py` (`SemanticCache`)** — the whole point of this phase: before
paying for a real inference, check whether the answer's already known.
Two-stage lookup, cheapest check first:
1. **Exact match** — is this the literal same text as something already
   answered? A single O(1) Redis lookup, no embedding computation at all.
2. **Cosine similarity** — is this a *differently worded* version of
   something already answered? Only reached if the exact check misses,
   since embedding a sentence is orders of magnitude more expensive than
   a hash lookup.

**`config/settings.py` additions** — `EMBEDDING_MODEL_NAME`
(`all-MiniLM-L6-v2`, per `plan.md`) and `CACHE_SIMILARITY_THRESHOLD`
(`0.92`, per `plan.md`), both env-overridable like Phase 2's settings.

**`Dockerfile` / `docker-compose.yml` additions** — a named volume
(`hf_cache`) mounted at `/hf_cache`, with `HF_HOME` pointed at it. Needed
because Phase 1's non-root `appuser` has no home directory, and
sentence-transformers defaults to caching the downloaded model under
`~/.cache` — without an explicit writable location, model loading would
either fail outright or (if it fell back to somewhere unpersisted)
re-download the ~90MB model on every `--reload` restart during
development.

## Plain-language analogies

**What cosine similarity actually measures.** Every sentence gets turned
into a point in a 384-dimensional space (the embedding) such that
sentences with similar *meaning* land in roughly the same direction from
the origin — regardless of exact wording. Cosine similarity measures the
*angle* between two such points, not the distance between them: it's
"are these two arrows pointing the same way," not "are these two arrows
the same length." A score of 1.0 means identical direction (same
meaning), 0 means unrelated, negative means roughly opposite. Threshold
0.92 is a strict bar — it's not asking "same general topic," it's asking
"pointing in *almost exactly* the same direction." That strictness is
exactly why the example pair proposed for testing this phase didn't
actually clear it — see "A finding worth flagging" below.

**Why brute-force fits `plan.md`'s Redis-7.x-not-Stack choice.** Picture
a small used bookstore versus a research library with a specialized
card-catalog system (that's what RediSearch/Redis Stack's vector index
is — a purpose-built index for fast approximate nearest-neighbor search
over millions of vectors). For a handful to a few thousand cached
prompts, physically pulling every book off the shelf and comparing them
by hand (brute-force) is fast enough and needs no specialized
infrastructure. Building and maintaining a real vector index only pays
for itself once the shelf holds far more books than a portfolio project
ever will — which is exactly why `plan.md` picked plain Redis 7.x over
Redis Stack for this project.

**Why the embeddings live in one bulk hash, not one Redis call per
candidate.** `cache:embeddings` is a single Redis hash holding every
cached vector, fetched with one `HGETALL`. That's the difference between
walking to a filing cabinet once and carrying the whole drawer back to
your desk, versus walking to the cabinet, pulling one file, walking
back, comparing it, and repeating for every file — the second approach
pays a network round trip per candidate. Once all the vectors are on the
desk (in a NumPy array), the similarity comparison against all of them
happens as a single vectorized matrix operation, not a Python `for`
loop — that's the difference between one optimized bulk computation and
computing 384-dimensional dot products one at a time in a Python
interpreter loop.

## A finding worth flagging

The originally proposed test pair — `"what's the weather like today"`
vs. `"how's the weather today"` — was checked directly against the
actual model before writing the verification steps below:

```
0.8917  "what's the weather like today" <-> "how's the weather today"
```

**0.89, below the 0.92 threshold.** With the real model, that pair would
*not* have hit the cache — it would have looked like a bug when it's
actually the threshold doing its job (a fairly strict bar, on purpose,
to avoid false-positive cache hits). Rather than lower the threshold to
make the originally suggested pair pass, I found a paraphrase pair that
genuinely clears 0.92 and used that instead:

```
0.9651  "what's the weather like today" <-> "what is today's weather like"
0.0519  "what's the weather like today" <-> "what's the best pizza topping"
```

## Deviations from `plan.md`, and why

1. **Cache keys are derived from `sha256(text)`, not an arbitrary ID.**
   `cache:entry:{sha256(text)}` means the exact-match check is a direct
   key lookup — no separate index structure needed to answer "have I
   seen this exact text before."

2. **Embeddings stored in their own bulk hash (`cache:embeddings`),
   separate from each entry's text/result hash
   (`cache:entry:{id}`).** `plan.md` says "embeddings stored as plain
   Redis values/hashes alongside the original prompt text and result" —
   read literally that could mean one hash per entry with all three
   fields together. Split them instead so the cosine-similarity step's
   bulk fetch (`HGETALL cache:embeddings`) doesn't also drag along every
   cached result payload it doesn't need for that computation — smaller,
   faster bulk fetch as the cache grows.

3. **Embeddings stored as JSON-encoded float lists, not raw bytes.** The
   Redis client (shared with the Phase 2 rate limiter) is configured
   with `decode_responses=True` for convenience with string data — raw
   embedding bytes would get corrupted trying to UTF-8-decode through
   that same client. JSON is also directly inspectable with `redis-cli`,
   which matters for debugging/demoing.

4. **`cache_status` added to the `/v1/predict` response body**
   (`"HIT"`/`"MISS"`), not just logged. `plan.md` only asked for logging;
   this is a small, low-risk addition that makes verifying cache
   behavior possible from the response alone. `task_id` is `null` on a
   hit, since no `Future`/task machinery is touched at all on that path.

5. **Logging is plain `key=value` text via the standard `logging`
   module, not JSON.** Phase 5 ("Structured JSON logs") explicitly owns
   the JSON logging format — building that here would be doing Phase 5's
   work early. What's here (`CACHE_HIT method=... entry_id=...
   score=...`) satisfies this phase's own bar: clearly readable, greppable,
   and states which check resolved each request.

6. **`httpx`'s logger silenced to `WARNING`.** Loading the model at
   startup logs dozens of HTTP request lines (HuggingFace Hub file
   checks) at `INFO` — unrelated noise that would bury the
   `CACHE_HIT`/`CACHE_MISS` lines this phase cares about.

## Verification

All commands run against the live containers, client_id `cache-test` for
the main sequence (fresh bucket, nowhere near the rate limit).

**1) First submission — cache empty, computed fresh, then cached:**
```
$ curl -s -w "\nHTTP %{http_code}\n" -X POST http://localhost:8001/v1/predict \
    -H "Content-Type: application/json" -H "X-Client-ID: cache-test" \
    -d '{"text": "what'"'"'s the weather like today"}'
{"task_id":"62523d56-c0fd-40b8-b1b9-1c37e2e36f07","result":{"stub":true,"echo":"what's the weather like today"},"cache_status":"MISS"}
HTTP 200
```

**2) Exact repeat — exact-match hit:**
```
$ curl ... -d '{"text": "what'"'"'s the weather like today"}'
{"task_id":null,"result":{"stub":true,"echo":"what's the weather like today"},"cache_status":"HIT"}
HTTP 200
```

**3) Paraphrase (verified 0.9651 similarity above) — cosine-similarity hit:**
```
$ curl ... -d '{"text": "what is today'"'"'s weather like"}'
{"task_id":null,"result":{"stub":true,"echo":"what's the weather like today"},"cache_status":"HIT"}
HTTP 200
```
Note the `echo` field shows the *original* cached prompt's text, not the
new phrasing — correct behavior for a semantic cache hit: you get the
previously computed answer, not a new computation for the new wording.

**4) Clearly dissimilar prompt — miss:**
```
$ curl ... -d '{"text": "what'"'"'s the best pizza topping"}'
{"task_id":"438b5434-a4a5-4a0e-bc97-69927def07fe","result":{"stub":true,"echo":"what's the best pizza topping"},"cache_status":"MISS"}
HTTP 200
```

**Logs for all four requests, showing which check resolved each one:**
```
$ docker compose logs api --tail 60 | grep -E "CACHE_HIT|CACHE_MISS"
CACHE_MISS entry_id=3bf2432a... reason=empty_cache
CACHE_HIT method=exact entry_id=3bf2432a...
CACHE_HIT method=cosine_similarity entry_id=a08025b0... matched_entry_id=3bf2432a... score=0.9651 threshold=0.92
CACHE_MISS entry_id=3d978711... best_score=0.0519 threshold=0.92
```

**What's actually stored in Redis:**
```
$ docker compose exec redis redis-cli KEYS "cache:entry:*"
cache:entry:3d978711468c9f00d550497d57e5861fb3ab40500bc94d3d329830cb40e9f677
cache:entry:3bf2432a52527a9ad0c8187fc0c45573bb5ccefed22ce50b4544da590da3a369

$ docker compose exec redis redis-cli HGETALL "cache:entry:3bf2432a..."
text
what's the weather like today
result
{"stub": true, "echo": "what's the weather like today"}

$ docker compose exec redis redis-cli HKEYS "cache:embeddings"
3bf2432a52527a9ad0c8187fc0c45573bb5ccefed22ce50b4544da590da3a369
3d978711468c9f00d550497d57e5861fb3ab40500bc94d3d329830cb40e9f677
```
Only 2 entries after 4 requests, as expected — the paraphrase (request 3)
matched an existing entry rather than creating a third.

## How to re-verify this later

```bash
cd ~/Projects/semantic-queue
docker compose up -d --build
docker compose logs api --tail 5   # confirm "Application startup complete"

# 1) prime the cache
curl -s -X POST http://localhost:8001/v1/predict \
  -H "Content-Type: application/json" -H "X-Client-ID: cache-test" \
  -d '{"text": "what'"'"'s the weather like today"}'

# 2) exact repeat -> cache_status: HIT (method=exact in logs)
curl -s -X POST http://localhost:8001/v1/predict \
  -H "Content-Type: application/json" -H "X-Client-ID: cache-test" \
  -d '{"text": "what'"'"'s the weather like today"}'

# 3) paraphrase -> cache_status: HIT (method=cosine_similarity in logs)
curl -s -X POST http://localhost:8001/v1/predict \
  -H "Content-Type: application/json" -H "X-Client-ID: cache-test" \
  -d '{"text": "what is today'"'"'s weather like"}'

# 4) dissimilar prompt -> cache_status: MISS
curl -s -X POST http://localhost:8001/v1/predict \
  -H "Content-Type: application/json" -H "X-Client-ID: cache-test" \
  -d '{"text": "what'"'"'s the best pizza topping"}'

# see which check resolved each request
docker compose logs api --tail 60 | grep -E "CACHE_HIT|CACHE_MISS"

# inspect what's actually in Redis
docker compose exec redis redis-cli KEYS "cache:entry:*"
docker compose exec redis redis-cli HKEYS "cache:embeddings"
```

Note: if re-run without clearing Redis state, requests 1-2 will both show
as exact-match hits (the entry already exists from a prior run) — flush
first with `docker compose exec redis redis-cli FLUSHALL` for a clean
demo run.
