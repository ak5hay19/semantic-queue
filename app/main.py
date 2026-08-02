import asyncio
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
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# Quiet the HTTP request logs from the one-time model download at startup
# so they don't drown out the CACHE_HIT/CACHE_MISS lines this phase cares about.
logging.getLogger("httpx").setLevel(logging.WARNING)

# task_id -> Future that gets resolved once a result is ready. In-process
# only, not Redis-backed — see phases/phase-2.md for why (single API
# replica, no pub/sub overhead). Phase 4's real worker runs out-of-process,
# so something inside the API process will still need to notice the
# worker's result and resolve the matching Future; that mechanism isn't
# built yet — Phase 2 only needs the Future/await machinery to exist.
pending_results: dict[str, asyncio.Future] = {}


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
    yield
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


# --- Phase 4 stub, delete when the real worker exists --------------------
# The real path: enqueue {task_id, payload} to the Redis List
# (workers/inference_worker.py), a worker pops it via BRPOP, batches it,
# runs the model, and eventually the result reaches this Future. None of
# that exists yet, so this fakes the round trip with a short sleep and a
# canned response purely so /v1/predict's request/response path (rate
# limit -> cache -> enqueue -> await result) is testable now.
async def _fake_worker(task_id: str, payload: PredictRequest) -> None:
    await asyncio.sleep(0.5)
    future = pending_results.get(task_id)
    if future and not future.done():
        future.set_result({"stub": True, "echo": payload.text})


# ---------------------------------------------------------------------------


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

    asyncio.create_task(_fake_worker(task_id, payload))

    try:
        result = await asyncio.wait_for(future, timeout=10.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for result")
    finally:
        pending_results.pop(task_id, None)

    await cache.store(lookup.entry_id, payload.text, result, lookup.embedding)

    return PredictResponse(task_id=task_id, result=result, cache_status="MISS")
