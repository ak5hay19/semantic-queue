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
CACHE_SIMILARITY_THRESHOLD = float(os.environ.get("CACHE_SIMILARITY_THRESHOLD", "0.92"))

# Redis List the API LPUSHes {task_id, entry_id, text} onto on a cache miss,
# and the worker BRPOPs from to accumulate a batch.
TASK_QUEUE_KEY = "ml_task_queue"
# Redis List the worker LPUSHes {task_id, result} onto once a batch finishes,
# and the API's background listener BRPOPs from to resolve the matching Future.
RESULT_QUEUE_KEY = "predict_results"

# plan.md's example: batch_size=16 OR max_delay=20ms, whichever comes first.
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "16"))
BATCH_MAX_DELAY_MS = float(os.environ.get("BATCH_MAX_DELAY_MS", "20"))
