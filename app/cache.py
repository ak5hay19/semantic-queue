import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass

import numpy as np
from redis.asyncio import Redis
from redis.commands.search.field import VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query
from redis.exceptions import ResponseError
from sentence_transformers import SentenceTransformer

from config.settings import CACHE_EF_RUNTIME, EMBEDDING_DIM

logger = logging.getLogger("semantic_queue.cache")

# One hash per entry, keyed directly by sha256(text) — an exact-match
# lookup is a single O(1) HMGET on a key derived from the incoming text,
# no scan or index needed. Also the prefix RediSearch watches: every hash
# written under this prefix with an "embedding" field is automatically
# picked up by the HNSW index below, no separate embeddings structure to
# keep in sync (Phase 3/Phase 6 had a second bulk hash, `cache:embeddings`,
# for exactly that bookkeeping — gone now, see phases/phase-7.md).
_ENTRY_KEY_PREFIX = "cache:entry:"

_INDEX_NAME = "cache_idx"


def _entry_id(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


@dataclass
class CacheLookupResult:
    hit: bool
    method: str | None  # "exact" | "cosine_similarity" | None
    result: dict | None
    entry_id: str
    embedding: np.ndarray | None
    score: float | None = None


class SemanticCache:
    """Redis-backed cache: exact-string match first, then a real k-NN
    search over a RediSearch HNSW vector index — not a brute-force scan
    that pulls every cached embedding into the API process (Phase 3's
    original approach, replaced here; see phases/phase-7.md for why and
    the measured before/after).

    Takes a dedicated Redis connection constructed with
    decode_responses=False, not the general-purpose shared client: each
    entry's "embedding" hash field is a raw packed float32 buffer, and a
    decode_responses=True client would try (and fail, or silently
    corrupt) to UTF-8-decode that binary buffer along with every other
    reply on the connection.
    """

    def __init__(self, redis: Redis, model: SentenceTransformer, threshold: float):
        self.redis = redis
        self.model = model
        self.threshold = threshold
        self._index = redis.ft(_INDEX_NAME)

    async def ensure_index(self) -> None:
        """Creates the HNSW index if it doesn't already exist. Idempotent
        on purpose: both the api and worker processes call this at their
        own startup, since either one might come up first, and the index
        only needs to exist before the first vector search/write touches
        it — not before any single specific process starts.
        """
        schema = (
            VectorField(
                "embedding",
                "HNSW",
                {
                    "TYPE": "FLOAT32",
                    "DIM": EMBEDDING_DIM,
                    "DISTANCE_METRIC": "COSINE",
                },
            ),
        )
        try:
            await self._index.create_index(
                schema,
                definition=IndexDefinition(
                    prefix=[_ENTRY_KEY_PREFIX], index_type=IndexType.HASH
                ),
            )
            logger.info(
                "RediSearch index created",
                extra={"event": "index_created", "index": _INDEX_NAME},
            )
        except ResponseError as e:
            if "already exists" not in str(e):
                raise

    async def _embed(self, text: str) -> np.ndarray:
        # model.encode() is a blocking CPU call — offloaded to a thread so
        # it doesn't block the event loop while it runs.
        vector = await asyncio.to_thread(self.model.encode, text)
        return np.asarray(vector, dtype=np.float32)

    async def lookup(self, text: str) -> CacheLookupResult:
        entry_id = _entry_id(text)

        exact = await self.redis.hmget(f"{_ENTRY_KEY_PREFIX}{entry_id}", "result")
        if exact[0] is not None:
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
                result=json.loads(exact[0]),
                entry_id=entry_id,
                embedding=None,
            )

        query_vector = await self._embed(text)

        # RediSearch's own real KNN search, run as a single round trip —
        # no bulk fetch of candidate embeddings into this process, no
        # Python-side decode/matrix-build/compute loop. "*=>[KNN 1 ...]"
        # asks for the single nearest neighbor across every indexed entry;
        # COSINE distance_metric returns a *distance* (0 = identical),
        # verified empirically (not assumed) against plain NumPy cosine
        # similarity to equal `1 - distance` — see phases/phase-7.md.
        #
        # EF_RUNTIME explicitly raised above RediSearch's own default (10):
        # measured directly (not assumed) that the default missed an
        # actual 0.975-similarity near-duplicate entirely once the cache
        # held ~1700 entries, returning three unrelated ~0.21-similarity
        # candidates instead — a real recall failure, not a rounding
        # difference. CACHE_EF_RUNTIME=300 was confirmed to find the true
        # match at that same scale, at <1ms extra query cost. See
        # phases/phase-7.md for the measurements behind this value.
        query = (
            Query(f"*=>[KNN 1 @embedding $vec EF_RUNTIME {CACHE_EF_RUNTIME} AS score]")
            .sort_by("score")
            .return_fields("score", "result")
            .dialect(2)
        )
        search_result = await self._index.search(
            query, query_params={"vec": query_vector.tobytes()}
        )

        if search_result.docs:
            doc = search_result.docs[0]
            best_score = 1.0 - float(doc.score)

            if best_score > self.threshold:
                matched_entry_id = doc.id[len(_ENTRY_KEY_PREFIX):]
                logger.info(
                    "cache hit (cosine_similarity)",
                    extra={
                        "event": "cache_lookup",
                        "cache_status": "HIT",
                        "method": "cosine_similarity",
                        "entry_id": entry_id,
                        "matched_entry_id": matched_entry_id,
                        "score": round(best_score, 4),
                        "threshold": self.threshold,
                    },
                )
                return CacheLookupResult(
                    hit=True,
                    method="cosine_similarity",
                    result=json.loads(doc.result),
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
            mapping={
                "text": text.encode("utf-8"),
                "result": json.dumps(result).encode("utf-8"),
                "embedding": np.asarray(embedding, dtype=np.float32).tobytes(),
            },
        )
