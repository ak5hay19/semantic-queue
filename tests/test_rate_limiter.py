import asyncio
import collections
import time

import httpx

from config.settings import RATE_LIMIT_CAPACITY
from tests.conftest import API_BASE_URL, unique_client_id


def test_missing_client_id_returns_400(client: httpx.Client):
    response = client.post("/v1/predict", json={"text": "no client id header"})
    assert response.status_code == 400


def test_exceeding_limit_returns_429_then_refills():
    """Fires a burst well past RATE_LIMIT_CAPACITY under one fresh
    client_id — all concurrently, matching how the limiter was manually
    verified in Phase 2 — then confirms a request succeeds again after a
    short wait, proving the bucket actually refills rather than staying
    locked out.

    Uses identical text for every request in the burst: after the first
    request populates the cache, the rest short-circuit through the
    exact-match path (fast, no embedding compute), keeping this test
    fast and not spamming the semantic cache with throwaway entries —
    this test is about rate limiting, not caching.
    """
    client_id = unique_client_id("ratelimit")
    text = "rate limit burst test — identical text for every request"
    burst_size = RATE_LIMIT_CAPACITY + 20

    async def fire_burst() -> collections.Counter:
        async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=15.0) as c:
            async def hit():
                r = await c.post(
                    "/v1/predict",
                    json={"text": text},
                    headers={"X-Client-ID": client_id},
                )
                return r.status_code

            results = await asyncio.gather(*[hit() for _ in range(burst_size)])
            return collections.Counter(results)

    counts = asyncio.run(fire_burst())
    assert counts[429] > 0, f"expected some 429s from a burst of {burst_size}, got {counts}"
    assert counts[200] > 0, f"expected some successes too, got {counts}"

    time.sleep(3)  # ~1.67 tokens/sec refill at default settings — a few seconds is enough for one more

    with httpx.Client(base_url=API_BASE_URL, timeout=15.0) as c:
        response = c.post(
            "/v1/predict", json={"text": text}, headers={"X-Client-ID": client_id}
        )
    assert response.status_code == 200, "expected the bucket to have refilled after waiting"
