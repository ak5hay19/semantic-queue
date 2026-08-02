import asyncio
import json
import time
import uuid

from redis.asyncio import Redis

from config.settings import BATCH_MAX_DELAY_MS, BATCH_SIZE, REDIS_URL, TASK_QUEUE_KEY
from workers.inference_worker import _collect_batch

# Isolated Redis DB, separate from the live api/worker's db 0 — otherwise
# this test's injected tasks would race against the actual running
# `worker` container, which is continuously BRPOPing the same
# ml_task_queue in the real docker-compose stack. Same key name, no
# collision, because it's a different logical DB.
_TEST_REDIS_URL = REDIS_URL.rsplit("/", 1)[0] + "/15"


def _make_task() -> str:
    task_id = str(uuid.uuid4())
    return json.dumps(
        {"task_id": task_id, "entry_id": task_id, "text": "batching test payload"}
    )


async def _push(redis: Redis, n: int) -> None:
    await redis.delete(TASK_QUEUE_KEY)  # clean slate from any prior test run
    pipe = redis.pipeline()
    for _ in range(n):
        pipe.lpush(TASK_QUEUE_KEY, _make_task())
    await pipe.execute()


def test_batch_size_cap_triggers():
    """More tasks than batch_size land in the queue in one Redis
    pipeline round trip (all present before _collect_batch starts) — the
    real _collect_batch() (imported, not mocked) should cap the batch at
    exactly BATCH_SIZE and return quickly, since it filled up on count,
    not on time.
    """

    async def run():
        redis = Redis.from_url(_TEST_REDIS_URL, decode_responses=True, socket_timeout=None)
        await _push(redis, BATCH_SIZE + 9)

        start = time.monotonic()
        batch = await _collect_batch(redis)
        elapsed_ms = (time.monotonic() - start) * 1000

        await redis.aclose()
        return batch, elapsed_ms

    batch, elapsed_ms = asyncio.run(run())

    assert len(batch) == BATCH_SIZE
    assert elapsed_ms < BATCH_MAX_DELAY_MS, (
        f"batch of size {BATCH_SIZE} took {elapsed_ms:.1f}ms to collect — "
        "expected it to return as soon as it filled up, not wait out the delay"
    )


def test_batch_splits_across_size_and_remainder():
    """Mirrors the manual Phase 4 verification (25 pushed -> 16 then 9):
    a burst larger than batch_size should split into a capped first
    batch and a smaller second batch for the remainder.
    """

    async def run():
        redis = Redis.from_url(_TEST_REDIS_URL, decode_responses=True, socket_timeout=None)
        await _push(redis, 25)

        first_batch = await _collect_batch(redis)
        second_batch = await _collect_batch(redis)

        await redis.aclose()
        return first_batch, second_batch

    first_batch, second_batch = asyncio.run(run())

    assert len(first_batch) == BATCH_SIZE
    assert len(second_batch) == 25 - BATCH_SIZE


def test_max_delay_triggers():
    """Fewer than batch_size tasks are available — _collect_batch should
    wait out the max_delay window (not return instantly, not hang
    forever) before returning whatever it has.
    """
    pushed = 3
    assert pushed < BATCH_SIZE

    async def run():
        redis = Redis.from_url(_TEST_REDIS_URL, decode_responses=True, socket_timeout=None)
        await _push(redis, pushed)

        start = time.monotonic()
        batch = await _collect_batch(redis)
        elapsed_ms = (time.monotonic() - start) * 1000

        await redis.aclose()
        return batch, elapsed_ms

    batch, elapsed_ms = asyncio.run(run())

    assert len(batch) == pushed
    # Generous tolerance either side of the configured max_delay.
    assert elapsed_ms >= BATCH_MAX_DELAY_MS * 0.5, (
        f"returned after {elapsed_ms:.1f}ms with only {pushed} items — "
        f"expected it to wait close to max_delay ({BATCH_MAX_DELAY_MS}ms)"
    )
    assert elapsed_ms < BATCH_MAX_DELAY_MS * 5, (
        f"took {elapsed_ms:.1f}ms, far longer than max_delay "
        f"({BATCH_MAX_DELAY_MS}ms) — may be hanging rather than timing out"
    )
