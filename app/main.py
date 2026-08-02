import asyncio
import contextlib
import json
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from redis.asyncio import Redis
from sentence_transformers import SentenceTransformer

from app.cache import SemanticCache
from app.rate_limiter import TokenBucketRateLimiter
from config.settings import (
    CACHE_SIMILARITY_THRESHOLD,
    EMBEDDING_MODEL_NAME,
    RATE_LIMIT_CAPACITY,
    RATE_LIMIT_WINDOW_SECONDS,
    REDIS_URL,
    RESULT_QUEUE_KEY,
    TASK_QUEUE_KEY,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# Quiet the HTTP request logs from the one-time model download at startup
# so they don't drown out the CACHE_HIT/CACHE_MISS lines this phase cares about.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("semantic_queue.api")

# task_id -> Future that gets resolved once a result is ready. In-process
# only, not Redis-backed — see phases/phase-2.md for why (single API
# replica, no pub/sub overhead). The real worker (Phase 4) runs as a
# separate OS process and can't touch this dict or these Futures directly
# — see _result_listener below for how a cross-process result actually
# gets here.
pending_results: dict[str, asyncio.Future] = {}


async def _result_listener(blocking_redis: Redis) -> None:
    """Long-lived background task, started at app startup: continuously
    BRPOPs the results list the worker LPUSHes onto, and resolves the
    matching in-process Future. This — not Redis pub/sub, not polling —
    is the settled Phase 4 design for getting a result computed in a
    separate worker process back to the specific /v1/predict call that's
    awaiting it in this process.

    Takes a dedicated Redis connection (not the shared `app.state.redis`
    pool) with no client-side socket timeout — see the note in `lifespan`
    for why an indefinitely-blocking BRPOP needs one.
    """
    while True:
        try:
            item = await blocking_redis.brpop(RESULT_QUEUE_KEY, timeout=0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("result listener BRPOP failed, retrying in 1s")
            await asyncio.sleep(1)
            continue

        _, raw = item
        payload = json.loads(raw)
        task_id = payload["task_id"]
        future = pending_results.get(task_id)
        if future and not future.done():
            future.set_result(payload["result"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = Redis.from_url(REDIS_URL, decode_responses=True)
    app.state.rate_limiter = TokenBucketRateLimiter(
        app.state.redis,
        capacity=RATE_LIMIT_CAPACITY,
        window_seconds=RATE_LIMIT_WINDOW_SECONDS,
    )
    # Loaded once at startup (not per-request) since this is a multi-second
    # blocking call. Startup itself blocks the event loop here too, but
    # that's fine — uvicorn doesn't accept connections until this finishes.
    app.state.embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    app.state.cache = SemanticCache(
        app.state.redis, app.state.embedding_model, threshold=CACHE_SIMILARITY_THRESHOLD
    )

    # A separate connection from app.state.redis, on purpose: BRPOP with
    # timeout=0 blocks indefinitely at the Redis protocol level, but
    # redis-py 8.x's client applies its own socket_timeout (default 5s)
    # to every read regardless of the command's own timeout — so an
    # indefinite BRPOP on the shared client gets killed client-side after
    # 5s of no results. socket_timeout=None disables that for this
    # connection. This also happens to be standard Redis practice anyway:
    # a blocking command occupies a connection until it returns, so it
    # shouldn't share a pool with the quick rate-limit/cache calls.
    app.state.blocking_redis = Redis.from_url(
        REDIS_URL, decode_responses=True, socket_timeout=None
    )

    listener_task = asyncio.create_task(_result_listener(app.state.blocking_redis))

    yield

    listener_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await listener_task
    await app.state.blocking_redis.aclose()
    await app.state.redis.aclose()


app = FastAPI(lifespan=lifespan)


def get_client_id(x_client_id: str | None = Header(default=None)) -> str:
    if not x_client_id:
        raise HTTPException(
            status_code=400, detail="X-Client-ID header is required"
        )
    return x_client_id


async def enforce_rate_limit(
    request: Request, client_id: str = Depends(get_client_id)
) -> str:
    allowed = await request.app.state.rate_limiter.allow(client_id)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    return client_id


class PredictRequest(BaseModel):
    text: str


class PredictResponse(BaseModel):
    task_id: str | None = None
    result: dict
    cache_status: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/predict", response_model=PredictResponse)
async def predict(
    payload: PredictRequest,
    request: Request,
    client_id: str = Depends(enforce_rate_limit),
):
    cache: SemanticCache = request.app.state.cache
    lookup = await cache.lookup(payload.text)

    if lookup.hit:
        return PredictResponse(result=lookup.result, cache_status="HIT")

    task_id = str(uuid.uuid4())
    future = asyncio.get_running_loop().create_future()
    pending_results[task_id] = future

    # Real path: hand off to workers/inference_worker.py via the Redis
    # task queue. entry_id travels with the task so the worker can write
    # the cache entry under the same key a future lookup() will derive
    # for this text, without re-deriving it itself.
    await request.app.state.redis.lpush(
        TASK_QUEUE_KEY,
        json.dumps(
            {"task_id": task_id, "entry_id": lookup.entry_id, "text": payload.text}
        ),
    )

    try:
        result = await asyncio.wait_for(future, timeout=10.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for result")
    finally:
        pending_results.pop(task_id, None)

    return PredictResponse(task_id=task_id, result=result, cache_status="MISS")
