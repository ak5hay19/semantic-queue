import asyncio
import time

import httpx

from tests.conftest import API_BASE_URL, unique_client_id


def test_health_stays_responsive_during_cache_lookup():
    """Regression test for the Phase 4 finding: a synchronous CPU-bound
    call left un-offloaded inside an async route blocks the entire event
    loop, serializing every concurrent request behind it. Fires a
    cache-miss /v1/predict (forces a real embedding computation and a
    cosine-similarity scan over the live cache) concurrently with a
    /health request on the real running server, and asserts /health
    isn't held up behind it. A test against mocked-out internals
    couldn't catch this class of bug — it's specifically about whether
    the real event loop stays free to schedule other work while one
    request is mid-flight.
    """
    predict_client_id = unique_client_id("concurrency-predict")
    text = f"concurrency regression test unique sentence {predict_client_id}"

    async def run():
        async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=15.0) as c:
            predict_task = asyncio.create_task(
                c.post(
                    "/v1/predict",
                    json={"text": text},
                    headers={"X-Client-ID": predict_client_id},
                )
            )
            # Give the predict request a head start so it's genuinely
            # mid-flight (past the rate limiter, into the cache lookup's
            # embedding computation) before /health is dispatched.
            await asyncio.sleep(0.01)

            health_start = time.perf_counter()
            health_response = await c.get("/health")
            health_latency_ms = (time.perf_counter() - health_start) * 1000

            predict_response = await predict_task

        return health_response, health_latency_ms, predict_response

    health_response, health_latency_ms, predict_response = asyncio.run(run())

    assert health_response.status_code == 200
    assert predict_response.status_code == 200
    # Generous threshold, not a tight SLA — the point is proving /health
    # doesn't get stuck behind the slow request's full duration (which
    # is typically 100-700ms end to end, per the request_completed logs).
    assert health_latency_ms < 100, (
        f"/health took {health_latency_ms:.1f}ms while a cache lookup was "
        "in flight — the event loop may be blocked by synchronous CPU work"
    )
