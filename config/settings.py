import os

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
# redis-py's async client defaults this pool to 100 connections. Fine for
# normal traffic, but Phase 6 load testing (500+ concurrent clients, each
# needing a connection for the rate-limit check and cache lookup) hit
# `MaxConnectionsError` at that default — see phases/phase-6.md. Bumped to
# comfortably cover the load-test target; this is a connection-count
# ceiling, not the actual measured throughput bottleneck (that one's
# GIL-bound compute, documented in phases/phase-4.md, and deliberately
# left alone).
REDIS_MAX_CONNECTIONS = int(os.environ.get("REDIS_MAX_CONNECTIONS", "512"))

# plan.md's example: 100 requests per 60s window per client_id.
RATE_LIMIT_CAPACITY = int(os.environ.get("RATE_LIMIT_CAPACITY", "100"))
RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))

EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")
# all-MiniLM-L6-v2's fixed output width — the RediSearch HNSW index (Phase
# 7, app/cache.py) needs this declared up front, not inferred at runtime.
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))
CACHE_SIMILARITY_THRESHOLD = float(os.environ.get("CACHE_SIMILARITY_THRESHOLD", "0.92"))
# RediSearch's HNSW search-time exploration factor. Its own default (10)
# is tuned for speed over recall and was measured (not assumed) to
# outright miss a genuine 0.975-similarity near-duplicate once the cache
# held ~1700 entries — a real correctness problem for a cache, where a
# missed hit means silently falling back to a full re-inference instead
# of an error. Raising this was measured to cost <1ms extra per query at
# that same scale (well within noise), so there's no real throughput
# trade-off at this project's scale — see phases/phase-7.md.
CACHE_EF_RUNTIME = int(os.environ.get("CACHE_EF_RUNTIME", "300"))

# Stretch 3 (Phase 8): confidence decay + hard TTL, on top of Phase 7's
# RediSearch cache. Both values are necessarily somewhat arbitrary — no
# real-world data-freshness study backs these numbers, and phases/phase-8.md
# says so honestly rather than dressing them up as principled.
#
# CACHE_DECAY_RATE_PER_DAY: similarity points subtracted per day of entry
# age before comparing against CACHE_SIMILARITY_THRESHOLD. 0.05/day means
# a borderline match (~0.95 raw similarity) goes stale in under a day,
# while a near-perfect match (~1.0) can still clear threshold right up
# to the hard TTL below — see phases/phase-8.md for why that gap is
# exactly the scenario the hard TTL exists to close.
CACHE_DECAY_RATE_PER_DAY = float(os.environ.get("CACHE_DECAY_RATE_PER_DAY", "0.05"))
# CACHE_TTL_SECONDS: unconditional expiration, enforced via a real Redis
# TTL on the entry's own key (not a check in application code) — 24 hours
# picked as a simple, round, easy-to-reason-about upper bound.
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", str(24 * 60 * 60)))

# Redis List the API LPUSHes {task_id, entry_id, text} onto on a cache miss,
# and the worker BRPOPs from to accumulate a batch.
TASK_QUEUE_KEY = "ml_task_queue"
# Redis List the worker LPUSHes {task_id, result} onto once a batch finishes,
# and the API's background listener BRPOPs from to resolve the matching Future.
RESULT_QUEUE_KEY = "predict_results"

# plan.md's example: batch_size=16 OR max_delay=20ms, whichever comes first.
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "16"))
BATCH_MAX_DELAY_MS = float(os.environ.get("BATCH_MAX_DELAY_MS", "20"))
