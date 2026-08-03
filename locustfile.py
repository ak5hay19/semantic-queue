"""Phase 6 load testing: three scenarios selected by picking a User class
on the Locust command line (`locust -f locustfile.py <ClassName> ...`).

Each scenario exists to isolate one variable:
  - ColdUser:      every request is genuinely novel text -> forces the
                   cache-miss path (rate limiter, embed, cache scan,
                   queue, worker, batch) on every single request.
  - WarmUser:      requests are drawn from a small, fixed pool of
                   sentences (mostly exact repeats, a few empirically
                   verified paraphrases) -> forces the cache-hit path
                   after the pool is primed.
  - OverloadUser:  a small, fixed pool of client_ids (not one per
                   request) hammered fast enough to reliably exceed
                   RATE_LIMIT_CAPACITY within RATE_LIMIT_WINDOW_SECONDS.

Cold and Warm intentionally use a *fresh* X-Client-ID per request (not
one persistent id per simulated user) specifically so the rate limiter
never becomes the bottleneck being measured in those two scenarios --
only Overload deliberately reuses a handful of ids to trigger 429s.

Every request is tagged with a name based on the response's own
cache_status field (predict_hit / predict_miss / predict_ratelimited),
so Locust's own per-name stats table directly gives p50/p95/p99 broken
down by cache hit vs miss -- no extra plumbing needed to compute the
"% latency reduction, cache-hit vs cache-miss" number Phase 6 asks for.
"""

import json
import random
import time
import uuid

from locust import FastHttpUser, task, between, events

# -- Cold scenario: guaranteed-novel text -----------------------------------
#
# Two failed attempts before this one, both empirically checked (not
# assumed) by embedding batches of candidate text with the live model and
# measuring, for each new item, its max cosine similarity against every
# item generated before it -- simulating exactly what the cache's
# brute-force scan does as it grows during a real run:
#
#   1. A single 4-slot template (subject/topic/qualifier/context) filled
#      with a uuid suffix per request -- the uuid is near-inert to the
#      embedding, so two draws sharing the same slot values collided.
#      Fixed the "same slot values" issue via a shuffled, no-replacement
#      itertools.product over 15**4 combinations... but the fix didn't
#      hold: even with every slot value guaranteed different, 20% of
#      draws still scored >0.92 against *something* earlier in the batch
#      (n=600). The shared sentence shape and connective words ("at the",
#      "despite the", ...) were themselves enough structure for MiniLM to
#      cluster these fairly close together, and with a cache that grows
#      into the hundreds, "close for one of N reasons" adds up fast (a
#      birthday-paradox effect: many chances for any one comparison to
#      clear the bar, not just one).
#   2. Rotating across 5 differently-shaped templates on top of the same
#      word pools -- better (9% instead of 20%) but still not remotely
#      close to the "0% cache hits" this scenario is supposed to
#      guarantee by construction.
#
# What actually got to 0/599 in the same measurement: dropping natural-
# language structure entirely and sending random word salad -- an
# unordered `random.sample` draw from an 80-word vocabulary spanning
# unrelated concrete nouns. No shared grammar, no shared connective
# words, nothing for the embedding to latch onto across draws. The
# system doesn't care that it's not a "real sentence" -- it just embeds
# whatever string it's given -- and this is the one method of the three
# that actually delivers the guaranteed-cache-miss property the scenario
# needs. See phases/phase-6.md for the actual measured numbers behind
# this decision.
_WORD_POOL = (
    "ocean mountain keyboard velvet triangle whisper garden lantern thunder pepper "
    "bicycle harbor crystal falcon meadow engine compass ribbon shadow ember "
    "glacier tunnel orchard puzzle marble candle rocket blossom anchor sparrow "
    "canyon fabric ladder ripple beacon voyage timber granite echo hazard "
    "quartz vinegar sapling turbine cactus lagoon prairie satchel thistle wharf "
    "cinder gravel plume signal tundra almond copper drizzle hollow lattice "
    "pigment relic zephyr mosaic cobalt driftwood flint hearth kestrel paddock "
    "quill saffron trellis vellum wisteria yonder zeppelin abacus brooch cascade "
    "obelisk parchment silhouette isotope meridian citadel"
).split()


def _cold_text() -> str:
    return " ".join(random.sample(_WORD_POOL, 14))


# -- Warm scenario: a small fixed pool, mostly exact repeats ----------------
#
# Base sentences repeated verbatim drive exact-match (sha256) hits -- the
# fast O(1) path. The four paraphrase pairs below were empirically
# verified against the live all-MiniLM-L6-v2 model before being used here
# (see phases/phase-6.md for the check and scores) -- following the same
# "verify, don't assume" lesson Phase 3 and Phase 5 already learned the
# hard way (a previously assumed weather-phrasing pair actually scored
# 0.89, below threshold). Only pairs that scored clearly above 0.92 are
# included, so the cosine-similarity path is genuinely exercised too, not
# just exact-match.
_WARM_EXACT_POOL = [
    "The quarterly report shows steady growth in the northeast region",
    "Our support team resolved the outage within twenty minutes",
    "The new warehouse will open for operations next spring",
    "Customer satisfaction scores improved after the recent update",
    "The engineering team completed the migration ahead of schedule",
    "Sales in the mobile category exceeded expectations this quarter",
]
_WARM_PARAPHRASE_PAIRS = [
    ("Can you tell me what the weather is like today", "Could you tell me what today weather is like"),
    ("I need help resetting my account password", "Can you help me reset the password on my account"),
    ("The flight was delayed due to bad weather", "Bad weather caused the flight to be delayed"),
    ("How do I cancel my subscription", "What is the process to cancel my subscription"),
]
_WARM_POOL = list(_WARM_EXACT_POOL) + [s for pair in _WARM_PARAPHRASE_PAIRS for s in pair]


def _warm_text() -> str:
    return random.choice(_WARM_POOL)


def _name_for_status(cache_status: str | None) -> str:
    if cache_status == "HIT":
        return "/v1/predict [cache_hit]"
    if cache_status == "MISS":
        return "/v1/predict [cache_miss]"
    return "/v1/predict [other]"


def _post_predict(user: FastHttpUser, text: str, client_id: str, *, rate_limit_scenario: bool = False):
    payload = json.dumps({"text": text})
    headers = {"Content-Type": "application/json", "X-Client-ID": client_id}
    with user.client.post(
        "/v1/predict", data=payload, headers=headers, catch_response=True, name="/v1/predict"
    ) as response:
        if response.status_code == 429:
            # Expected, correct behavior for the overload scenario -- not
            # a system failure, so don't mark it as one. For cold/warm,
            # a 429 would be unexpected (each request uses a fresh
            # client_id), so it's left as a real failure there.
            if rate_limit_scenario:
                response.request_meta["name"] = "/v1/predict [ratelimited]"
                response.success()
            else:
                response.request_meta["name"] = "/v1/predict [unexpected_429]"
                response.failure("unexpected 429 -- fresh client_id should not be rate limited")
            return
        if response.status_code != 200:
            response.request_meta["name"] = "/v1/predict [error]"
            response.failure(f"status {response.status_code}")
            return
        try:
            body = response.json()
        except Exception:
            response.request_meta["name"] = "/v1/predict [bad_json]"
            response.failure("response body was not valid JSON")
            return
        response.request_meta["name"] = _name_for_status(body.get("cache_status"))
        response.success()


class ColdUser(FastHttpUser):
    """0% cache hits by construction -- every request is novel."""

    wait_time = between(0.01, 0.1)

    @task
    def predict_novel(self):
        _post_predict(self, _cold_text(), client_id=str(uuid.uuid4()))


class WarmUser(FastHttpUser):
    """High cache-hit rate -- draws from a small fixed pool."""

    wait_time = between(0.01, 0.1)

    @task
    def predict_repeated(self):
        _post_predict(self, _warm_text(), client_id=str(uuid.uuid4()))


class OverloadUser(FastHttpUser):
    """Reliably trips HTTP 429 -- a handful of client_ids share the load,
    each easily exceeding RATE_LIMIT_CAPACITY within RATE_LIMIT_WINDOW_SECONDS.

    Deliberately reuses one fixed, already-cached sentence (not
    _cold_text()) for every request. A first dry run of this scenario
    using novel cache-miss text only managed ~180 total requests across
    500 users in 30s -- the same cache-miss cost from the Cold scenario
    (embed + worker round trip, seconds per request) meant no single
    client_id could fire anywhere near 100 requests before the window
    closed, so the bucket rarely emptied and 429s barely showed up. The
    point of this scenario is to hammer the *rate limiter* specifically,
    which sits in front of the cache/queue and should reject a request in
    milliseconds -- so the request body needs to be cheap (a cache hit),
    not expensive, or the cache-miss path's own throughput ceiling
    quietly becomes the bottleneck being measured instead of the rate
    limiter. See phases/phase-6.md.
    """

    wait_time = between(0, 0.02)
    _client_id_pool = [f"overload-client-{i}" for i in range(5)]
    _text = "overload scenario probe sentence, reused by every request in this run"

    @task
    def predict_burst(self):
        client_id = random.choice(self._client_id_pool)
        _post_predict(self, self._text, client_id=client_id, rate_limit_scenario=True)
