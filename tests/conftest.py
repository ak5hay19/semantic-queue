import uuid

import httpx
import pytest
import redis as sync_redis

from app.cache import _entry_id
from config.settings import REDIS_URL

API_BASE_URL = "http://localhost:8000"


@pytest.fixture
def client():
    with httpx.Client(base_url=API_BASE_URL, timeout=15.0) as c:
        yield c


@pytest.fixture
def redis_client():
    r = sync_redis.Redis.from_url(REDIS_URL, decode_responses=True)
    yield r
    r.close()


def unique_client_id(prefix: str) -> str:
    return f"pytest-{prefix}-{uuid.uuid4().hex[:8]}"


def clear_cache_entry(redis_client: sync_redis.Redis, text: str) -> None:
    """Deletes any pre-existing cache entry for `text`, so cache tests
    start from a known state regardless of whether this exact text was
    used in a previous test run (the cache has no TTL by design).
    """
    entry_id = _entry_id(text)
    redis_client.delete(f"cache:entry:{entry_id}")
    redis_client.hdel("cache:embeddings", entry_id)
