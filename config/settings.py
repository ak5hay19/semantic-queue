import os

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# plan.md's example: 100 requests per 60s window per client_id.
RATE_LIMIT_CAPACITY = int(os.environ.get("RATE_LIMIT_CAPACITY", "100"))
RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))

EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")
CACHE_SIMILARITY_THRESHOLD = float(os.environ.get("CACHE_SIMILARITY_THRESHOLD", "0.92"))
