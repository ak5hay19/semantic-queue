import asyncio
import json
import logging
import time

import numpy as np
import torch
import torch.nn as nn
from redis.asyncio import Redis
from sentence_transformers import SentenceTransformer

from app.cache import SemanticCache
from config.logging_config import configure_logging
from config.settings import (
    BATCH_MAX_DELAY_MS,
    BATCH_SIZE,
    CACHE_SIMILARITY_THRESHOLD,
    EMBEDDING_MODEL_NAME,
    REDIS_URL,
    RESULT_QUEUE_KEY,
    TASK_QUEUE_KEY,
)

configure_logging()
logger = logging.getLogger("semantic_queue.worker")

NUM_MOCK_CLASSES = 3


class MockClassifierHead(nn.Module):
    """Toy downstream task fed the MiniLM embedding — a stand-in for "some
    real model" per plan.md's Phase 4 spec. Random-initialized, untrained;
    its output doesn't need to be meaningful, it only needs to exercise a
    real batched forward pass after the embedding step. Seeded so repeated
    demo runs produce the same output for the same input.
    """

    def __init__(self, input_dim: int = 384, num_classes: int = NUM_MOCK_CLASSES):
        super().__init__()
        self.linear = nn.Linear(input_dim, num_classes)

    def forward(self, embeddings: torch.Tensor) -> torch.Tensor:
        return self.linear(embeddings)


async def _collect_batch(blocking_redis: Redis) -> list[dict]:
    """Blocks for the first task (nothing to batch until one exists), then
    keeps pulling more with a shrinking timeout until batch_size is hit or
    max_delay runs out — whichever comes first.

    Takes a Redis connection with no client-side socket timeout — see the
    note in main() for why the indefinite first BRPOP needs one.
    """
    _, raw = await blocking_redis.brpop(TASK_QUEUE_KEY, timeout=0)
    batch = [json.loads(raw)]

    deadline = time.monotonic() + BATCH_MAX_DELAY_MS / 1000
    while len(batch) < BATCH_SIZE:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        item = await blocking_redis.brpop(TASK_QUEUE_KEY, timeout=remaining)
        if item is None:
            break
        batch.append(json.loads(item[1]))

    return batch


async def _process_batch(
    batch: list[dict],
    redis: Redis,
    model: SentenceTransformer,
    classifier: MockClassifierHead,
    cache: SemanticCache,
) -> None:
    start = time.monotonic()
    texts = [item["text"] for item in batch]

    # One batched forward pass over all N texts, not N separate calls —
    # this is the actual point of dynamic batching, so both the embedding
    # step and the classifier step run once over the whole batch.
    embeddings = await asyncio.to_thread(model.encode, texts)
    embeddings = np.asarray(embeddings, dtype=np.float32)

    logits = await asyncio.to_thread(
        lambda: classifier(torch.from_numpy(embeddings)).detach().numpy()
    )
    predicted_classes = logits.argmax(axis=1)

    for item, embedding, predicted_class in zip(batch, embeddings, predicted_classes):
        result = {"predicted_class": int(predicted_class), "echo": item["text"]}

        # This LPUSH is what "marks the task completed" in this design —
        # there's no separate status field, since the API's listener
        # consumes results with a destructive BRPOP: once popped, it's
        # done, and nothing else ever needs to check a status for it.
        await redis.lpush(
            RESULT_QUEUE_KEY,
            json.dumps({"task_id": item["task_id"], "result": result}),
        )
        await cache.store(item["entry_id"], item["text"], result, embedding)

    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info(
        "batch processed",
        extra={
            "event": "batch_processed",
            "batch_size": len(batch),
            "duration_ms": round(elapsed_ms, 1),
            "task_ids": [item["task_id"] for item in batch],
        },
    )


async def main() -> None:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    # Dedicated connection for the indefinitely-blocking BRPOP in
    # _collect_batch — see that function's docstring. redis-py 8.x's
    # client-side socket_timeout (default 5s) would otherwise kill a
    # BRPOP(timeout=0) call after 5s of no tasks, even though Redis
    # itself was asked to block forever.
    blocking_redis = Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=None)

    logger.info("loading embedding model %s", EMBEDDING_MODEL_NAME)
    model = SentenceTransformer(EMBEDDING_MODEL_NAME)

    torch.manual_seed(42)
    classifier = MockClassifierHead()

    cache = SemanticCache(redis, model, threshold=CACHE_SIMILARITY_THRESHOLD)

    logger.info(
        "worker ready: batch_size=%d max_delay_ms=%s queue=%s",
        BATCH_SIZE,
        BATCH_MAX_DELAY_MS,
        TASK_QUEUE_KEY,
    )

    while True:
        batch = await _collect_batch(blocking_redis)
        await _process_batch(batch, redis, model, classifier, cache)


if __name__ == "__main__":
    asyncio.run(main())
