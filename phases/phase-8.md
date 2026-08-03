# Phase 8 (Stretch 3): Cache Staleness / Drift Handling

Status: done and verified, 2026-08-03.

This is a stretch goal, not a base-project phase, built directly on top of
Phase 7's RediSearch cache. Phase 7's own implementation is not modified
beyond what's needed to add the two mechanisms below — the KNN query, the
`EF_RUNTIME` fix, and everything else documented in `phases/phase-7.md`
stays exactly as it was.

## The problem this solves, in plain terms

Phase 3 built a semantic cache to answer "has something *close enough* to
this question already been answered?" — but it never asked "...and is
that old answer still true?" A semantic cache has no idea what's inside
the text it's matching; it only knows two embeddings point in a similar
direction. If someone asks "who is the CEO of Company X" today and the
answer gets cached, and someone asks a near-identical question next
month, the cache will happily return last month's answer — even if the
CEO changed in between. The embedding for "who is the CEO of Company X"
hasn't moved at all; the *world* has. This is the classic, underdiscussed
weakness of semantic caching `plan.md` flagged as Stretch 3's whole point:
a stale-but-still-similar-looking answer looks exactly like a correct
cache hit, because similarity and correctness are different things.

Two mechanisms are added here, solving two different halves of this
problem:

**Confidence decay** answers "how much should I still trust this
specific match, given how old it is?" — it's a *ranking* adjustment among
otherwise-live entries. An old entry isn't wrong by definition, but it's
progressively less trustworthy, so it needs a better raw match to still
clear the bar.

**Hard TTL** answers a completely different question: "is this entry
even allowed to exist anymore, regardless of how good a match it looks
like?" It's not a ranking adjustment — it's an unconditional expiration.

**Why one doesn't replace the other:** decay only ever *lowers a score*.
Lowering a score can never, on its own, guarantee an entry is unreachable
— if the raw match is good enough, no amount of decay (short of literally
infinite time) is guaranteed to push it below threshold. A hard TTL
doesn't care about scores at all; it's a real deletion, independent of
whatever the similarity math says. Decay is squarely the *primary*
mechanism (it's what actually kicks in for the overwhelming majority of
aging entries, per the numbers below); TTL is the backstop that makes the
overall guarantee ("nothing is ever servable forever") true regardless of
what decay_rate or threshold happen to be configured — see "Verification"
below for a live, measured demonstration of exactly the gap this closes.

## Confidence decay: the mechanism

`effective_similarity = raw_similarity - (CACHE_DECAY_RATE_PER_DAY * age_in_days)`

Applied to **both** lookup paths, not just the cosine one — an exact
string match is `raw_similarity = 1.0` by definition, and it goes through
the exact same formula rather than being treated as decay-immune. This
was a deliberate design choice, not something the stretch-goal
instructions spelled out: without it, decay would only ever apply to the
cosine path, and an *identical*, word-for-word repeated query would keep
serving its original cached answer indefinitely, relying entirely on the
24-hour hard TTL to ever invalidate it — which would make TTL the thing
actually doing most of the staleness-detection work, backwards from how
this phase's own instructions frame decay as the *primary* mechanism. An
exact match that's decayed stale now falls through to a live cosine
search (in case some other, fresher entry is still a good semantic
match) rather than returning a flat miss immediately.

**`CACHE_DECAY_RATE_PER_DAY = 0.05`** — 5% of similarity lost per day of
age. **Chosen, and honestly not principled**: there's no data-freshness
study behind this number, and a real production system would tie it to
actual knowledge of how fast the underlying domain changes (stock prices
decay in minutes; "who wrote this book" essentially never decays). What
*is* deliberate is the qualitative shape this produces:
- A borderline match (raw similarity ~0.95, just clearing the 0.92
  threshold) goes stale in well under a day: `(0.95 - 0.92) / 0.05 =
  0.6 days ≈ 14.4 hours`.
- A near-perfect match (raw similarity ~0.999, e.g. the same question
  asked in almost identical words) can still clear the decayed threshold
  right up to the edge of the 24-hour hard TTL: at 23.9 hours,
  `0.999 - 0.05 * 0.9958 = 0.9492`, still comfortably above 0.92.

That second bullet is exactly the gap the hard TTL exists to close — an
entry that's "too good a match" for decay alone to ever meaningfully
touch it within a reasonable window.

## Hard TTL: the mechanism

**`CACHE_TTL_SECONDS = 86400`** (24 hours) — also arbitrary, chosen as a
simple, round, easy-to-reason-about upper bound rather than derived from
anything. Enforced as a **real Redis expiration**, not a check in
application code: `SemanticCache.store()` now pipelines an `HSET`
(writing `text`/`result`/`embedding`/the new `created_at` field) together
with an `EXPIRE` on that same key.

This matters because a check-and-skip in application code (e.g. "if
`created_at` is older than 24h, treat as a miss") would leave the actual
data sitting in Redis and in the RediSearch index forever — still
occupying memory, still a candidate the KNN search has to consider on
every single lookup, and still technically reachable by anything that
talks to Redis directly instead of through `SemanticCache`. A real
`EXPIRE` means Redis itself deletes the key once the TTL fires, and —
confirmed directly, not assumed, since this was exactly the kind of claim
this project has learned not to take on faith — **RediSearch automatically
removes the expired key from its index too**, with no separate cleanup
call needed from application code:

```
TTL right after set: 2
found via KNN before expiry: True
... (after the TTL elapses) ...
EXISTS after 3s: 0
found via KNN after expiry: []
```

No index entry lingers pointing at a deleted key, and no application code
anywhere has to remember to clean anything up.

## Plain-language analogy

**Decay is a librarian getting less and less confident in an old
citation** — a book from this year on a fast-moving topic gets cited
readily; the same book, ten years on, gets cited more reluctantly unless
it's an unusually strong, dead-on match, and eventually the librarian
stops trusting it for *anything* short of a perfect match. **The hard TTL
is the library's actual weeding policy** — regardless of how good a
citation that book still makes, past a fixed age it gets pulled from the
shelves and the catalog entry deleted, because the library needs a real
guarantee that nothing impossibly out of date is still quietly sitting
there waiting to be handed to someone.

## Verification — real evidence, not code review

### 1. Decay causes a correct MISS for an entry that would've HIT fresh

Using the already-verified 0.9277-similarity paraphrase pair from Phase
6's own testing (`"I need help resetting my account password"` /
`"Can you help me reset the password on my account"`):

```
$ curl -X POST /v1/predict -d '{"text": "I need help resetting my account password"}'
{"cache_status":"MISS", ...}                    # populates the cache

$ curl -X POST /v1/predict -d '{"text": "Can you help me reset the password on my account"}'
{"cache_status":"HIT", ...}                      # fresh entry, HIT as expected
```

Then, **without waiting in real time** — directly overwriting the stored
entry's `created_at` field to simulate 12 hours (0.5 days) of age:

```python
backdated = time.time() - 12*3600
await r.hset(key, "created_at", str(backdated).encode())
```

Re-querying the exact same paraphrase:

```
$ curl -X POST /v1/predict -d '{"text": "Can you help me reset the password on my account"}'
{"cache_status":"MISS", ...}

# structured log:
{"message": "cache miss", "cache_status": "MISS", "raw_score": 0.9277,
 "age_days": 0.5, "effective_score": 0.9027, "threshold": 0.92}
```

`0.9277 - 0.05 * 0.5 = 0.9027`, below the 0.92 threshold — exactly the
predicted arithmetic, and the request correctly falls through to a full
re-inference instead of serving the (now-stale-by-decay) cached answer.

### 2. An entry past the hard TTL is genuinely gone — both places

Two complementary checks, since this was run twice for reliability after
one earlier attempt (using a monkeypatched short TTL inside an inline
script) transiently failed to show expiry within the observed window on
the first try. Investigated rather than shrugged off: an isolated,
minimal repro (a plain string key, then a plain hash key, both with a
real 2-second `EXPIRE`, no `SemanticCache` or RediSearch involved) expired
correctly and immediately on the first attempt, both times. Re-running the
exact original script twice more also succeeded both times. The most
likely explanation is a one-off scheduling hiccup in the test harness
itself (e.g. a delayed `asyncio.sleep` while the container was still
finishing unrelated background work from a prior test), not a defect in
the expiration mechanism — Redis's own expiry and RediSearch's hook into
it are infrastructure this project doesn't own or need to debug further,
and every other run (four separate ones, across two different sessions)
showed clean, correct, on-time expiry. Final, clean run:

```
=== Test A: decay math at the boundary just under the 24h hard TTL ===
raw=0.999, age=0.9958 days (23.9h, just under 24h TTL)
effective_similarity=0.9492, threshold=0.92 -> HIT
(decay math alone says this still would have been servable)

=== Test B: real Redis TTL expiry + RediSearch index removal ===
stored entry, TTL set to 2 seconds (test-only short value)
  TTL right after store: 2
  EXISTS right after store: 1
  found via KNN before expiry: True
  after 1s: TTL=1 EXISTS=1
  after 2s: TTL=-2 EXISTS=0
  after 3s: TTL=-2 EXISTS=0
  after 4s: TTL=-2 EXISTS=0
  found via KNN after expiry: False
  cache.lookup() for the exact same text after expiry: hit=False method=None
```

Test A and Test B together are the actual point of having two mechanisms:
Test A shows a concrete case — a near-perfect match, aged to just under
the hard TTL boundary — where confidence decay's own math says "still a
valid hit." Test B shows that regardless of what that math says, the real
Redis expiration (here using a short test-only TTL substituted for the
production 24-hour value, since actually waiting a day isn't practical)
makes the entry genuinely unreachable — gone from `EXISTS`, gone from a
live KNN search, and `SemanticCache.lookup()` itself correctly reports a
plain miss (`hit=False`, `method=None`) rather than any special "expired"
state, because as far as the rest of the system is concerned there's
simply nothing there anymore.

### 3. Fresh entries: no regression in ordinary cache behavior

The exact three Phase 3/5/7 behaviors, re-checked against the current
implementation:
```
exact match (same text twice):        MISS, then HIT
cosine-hit (0.9750 paraphrase pair):  MISS, then HIT
dissimilar miss (0.0027 pair):        MISS, then MISS
```
All three unchanged from Phase 7 — decay applied to a freshly-stored
entry (age ≈ 0 days) subtracts a negligible amount (`0.05 * ~0.0001 days
≈ 0.000005`), so normal same-session cache behavior is untouched.

### 4. Full pytest suite

Unchanged test files, run twice for repeatability:
```
$ docker compose exec api python -m pytest tests/ -v
============================== 9 passed in 5.53s ===============================
$ docker compose exec api python -m pytest tests/ -v   # re-run
============================== 9 passed in 4.27s ===============================
```
All 9 tests — rate limiting, exact/cosine/dissimilar cache behavior, the
Phase 4 concurrency regression test, and batching — pass unchanged.

## Deviations from the stretch-goal instructions, and why

1. **Decay applied to the exact-match path too, not just cosine
   similarity.** Not explicitly specified — the instructions describe
   decay in terms of "the raw similarity score," which an exact match
   doesn't technically have. Reasoning for extending it there anyway is
   above ("Confidence decay: the mechanism"): without this, an identical
   repeated query would never go stale except via the 24-hour hard TTL,
   which would make TTL the actual primary staleness mechanism for that
   case — the opposite of what this phase's instructions call for.
2. **A decayed-stale exact match falls through to a live cosine search**
   rather than returning an immediate miss. Small extra cost (one more
   embed + KNN search) in an already-rare case (an exact repeat old
   enough to have decayed past threshold), in exchange for still finding
   a fresher semantically-equivalent entry if one happens to exist.
3. **`HSET` + `EXPIRE` pipelined together in `store()`**, not two separate
   calls. Not explicitly requested; closes a small window where the key
   would briefly exist without a TTL if the process died between two
   separate round trips.
4. **Missing `created_at` (data written before this phase, or any other
   cause) is treated as maximally stale** (age = infinity), not as
   age-zero. The conservative choice for a cache: an entry whose age is
   unknown shouldn't be silently trusted just because decay math can't be
   computed for it.
5. **No RediSearch schema change for `created_at`.** It's stored as a
   plain hash field and read back via `.return_fields(...)`, not declared
   as an indexed/searchable `NUMERIC` field — nothing in this phase
   filters or sorts *by* age at the RediSearch level, so there's nothing
   to index.

## How to re-verify this later

```bash
cd ~/Projects/semantic-queue
docker compose up -d --build
docker compose exec api python -m pytest tests/ -v   # 9 passed

# Decay: fresh HIT, then artificially aged MISS
curl -s -X POST http://localhost:8001/v1/predict -H "Content-Type: application/json" \
  -H "X-Client-ID: p8-check" -d '{"text": "I need help resetting my account password"}'
curl -s -X POST http://localhost:8001/v1/predict -H "Content-Type: application/json" \
  -H "X-Client-ID: p8-check" -d '{"text": "Can you help me reset the password on my account"}'
# -> expect cache_status: HIT

docker compose exec api python -c "
import asyncio, hashlib, time
from redis.asyncio import Redis
async def main():
    r = Redis.from_url('redis://redis:6379/0', decode_responses=False)
    text = 'I need help resetting my account password'
    key = f'cache:entry:{hashlib.sha256(text.strip().encode()).hexdigest()}'
    await r.hset(key, 'created_at', str(time.time() - 12*3600).encode())
asyncio.run(main())
"
curl -s -X POST http://localhost:8001/v1/predict -H "Content-Type: application/json" \
  -H "X-Client-ID: p8-check" -d '{"text": "Can you help me reset the password on my account"}'
# -> expect cache_status: MISS (effective_score ~0.9027, below 0.92)
docker compose logs api --since 10s | grep '"event": "cache_lookup"'

# Hard TTL: real expiry + RediSearch index removal (short test-only TTL)
docker compose exec api python -c "
import asyncio, numpy as np
import app.cache as cache_mod
from app.cache import SemanticCache, _entry_id
from redis.asyncio import Redis
from redis.commands.search.query import Query
async def main():
    r = Redis.from_url('redis://redis:6379/0', decode_responses=False)
    cache_mod.CACHE_TTL_SECONDS = 2
    class FakeModel:
        def encode(self, text): return np.random.RandomState(42).rand(384).astype(np.float32)
    sc = SemanticCache(r, FakeModel(), threshold=0.92)
    text = 'ttl reverify probe'
    entry_id = _entry_id(text)
    key = f'cache:entry:{entry_id}'
    vec = FakeModel().encode(text)
    await sc.store(entry_id, text, {'echo': text}, vec)
    print('TTL:', await r.ttl(key))
    await asyncio.sleep(3)
    print('EXISTS after 3s:', await r.exists(key))
    idx = r.ft('cache_idx')
    q = Query('*=>[KNN 1 @embedding \$vec AS score]').return_fields('score').dialect(2)
    res = await idx.search(q, query_params={'vec': vec.tobytes()})
    print('still in index:', any(d.id == key for d in res.docs))
asyncio.run(main())
"
# -> expect EXISTS after 3s: 0, still in index: False
```
