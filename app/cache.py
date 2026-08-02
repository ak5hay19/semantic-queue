import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass

import numpy as np
from redis.asyncio import Redis
from sentence_transformers import SentenceTransformer

logger = logging.getLogger("semantic_queue.cache")

# One hash per entry: {"text": ..., "result": <json>} — keyed directly by
# sha256(text), so an exact-match lookup is a single O(1) HGETALL on a key
# derived from the incoming text, no scan or index needed.
_ENTRY_KEY_PREFIX = "cache:entry:"

# All cached embeddings in one Redis hash (field=entry_id, value=JSON
# float list) so a cosine-similarity lookup pulls every candidate back in
# a single HGETALL round trip, not one round trip per candidate.
_EMBEDDINGS_KEY = "cache:embeddings"


def _entry_id(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _best_similarity_match(
    raw_embeddings: dict[str, str], query_vector: np.ndarray
) -> tuple[str, float]:
    """Decode every cached embedding, build the matrix, and run the
    vectorized cosine-similarity comparison. Pure CPU-bound work with no
    I/O — deliberately synchronous so it can be run via asyncio.to_thread,
    the same pattern used for the embedding model call. Cost is O(cache
    size); left inline on the event loop it blocked every request for
    ~58ms at a 266-entry cache (measured directly, see phases/phase-4.md).
    """
    ids = list(raw_embeddings.keys())
    matrix = np.array(
        [json.loads(raw_embeddings[i]) for i in ids], dtype=np.float32
    )
    similarities = (matrix @ query_vector) / (
        np.linalg.norm(matrix, axis=1) * np.linalg.norm(query_vector) + 1e-10
    )
    best_idx = int(np.argmax(similarities))
    return ids[best_idx], float(similarities[best_idx])


@dataclass
class CacheLookupResult:
    hit: bool
    method: str | None  # "exact" | "cosine_similarity" | None
    result: dict | None
    entry_id: str
    embedding: np.ndarray | None
    score: float | None = None


class SemanticCache:
    """Redis-backed cache: exact-string match first, then brute-force
    cosine similarity over every cached embedding. No RediSearch/Redis
    Stack — plain Redis 7.x, similarity computed in Python/NumPy.
    """

    def __init__(self, redis: Redis, model: SentenceTransformer, threshold: float):
        self.redis = redis
        self.model = model
        self.threshold = threshold

    async def _embed(self, text: str) -> np.ndarray:
        # model.encode() is a blocking CPU call — offloaded to a thread so
        # it doesn't block the event loop while it runs.
        vector = await asyncio.to_thread(self.model.encode, text)
        return np.asarray(vector, dtype=np.float32)

    async def lookup(self, text: str) -> CacheLookupResult:
        entry_id = _entry_id(text)

        exact = await self.redis.hgetall(f"{_ENTRY_KEY_PREFIX}{entry_id}")
        if exact:
            logger.info(
                "cache hit (exact)",
                extra={
                    "event": "cache_lookup",
                    "cache_status": "HIT",
                    "method": "exact",
                    "entry_id": entry_id,
                },
            )
            return CacheLookupResult(
                hit=True,
                method="exact",
                result=json.loads(exact["result"]),
                entry_id=entry_id,
                embedding=None,
            )

        query_vector = await self._embed(text)

        raw_embeddings = await self.redis.hgetall(_EMBEDDINGS_KEY)
        if raw_embeddings:
            # Decode + matrix build + the vectorized cosine-similarity
            # call are all CPU-bound with no I/O — offloaded to a thread
            # so this doesn't block the event loop while it runs, same
            # reasoning as _embed(). Cost is O(cache size); this used to
            # run inline and cost ~58ms/request at a 266-entry cache.
            best_id, best_score = await asyncio.to_thread(
                _best_similarity_match, raw_embeddings, query_vector
            )

            if best_score > self.threshold:
                cached_entry = await self.redis.hgetall(
                    f"{_ENTRY_KEY_PREFIX}{best_id}"
                )
                logger.info(
                    "cache hit (cosine_similarity)",
                    extra={
                        "event": "cache_lookup",
                        "cache_status": "HIT",
                        "method": "cosine_similarity",
                        "entry_id": entry_id,
                        "matched_entry_id": best_id,
                        "score": round(best_score, 4),
                        "threshold": self.threshold,
                    },
                )
                return CacheLookupResult(
                    hit=True,
                    method="cosine_similarity",
                    result=json.loads(cached_entry["result"]),
                    entry_id=entry_id,
                    embedding=query_vector,
                    score=best_score,
                )

            logger.info(
                "cache miss",
                extra={
                    "event": "cache_lookup",
                    "cache_status": "MISS",
                    "entry_id": entry_id,
                    "best_score": round(best_score, 4),
                    "threshold": self.threshold,
                },
            )
        else:
            logger.info(
                "cache miss (empty cache)",
                extra={
                    "event": "cache_lookup",
                    "cache_status": "MISS",
                    "entry_id": entry_id,
                    "reason": "empty_cache",
                },
            )

        return CacheLookupResult(
            hit=False,
            method=None,
            result=None,
            entry_id=entry_id,
            embedding=query_vector,
        )

    async def store(
        self, entry_id: str, text: str, result: dict, embedding: np.ndarray
    ) -> None:
        await self.redis.hset(
            f"{_ENTRY_KEY_PREFIX}{entry_id}",
            mapping={"text": text, "result": json.dumps(result)},
        )
        await self.redis.hset(_EMBEDDINGS_KEY, entry_id, json.dumps(embedding.tolist()))
