import time

from redis.asyncio import Redis

# Runs as a single atomic Redis operation (Lua scripts execute
# uninterrupted on the Redis server) so a "read tokens, decide, write
# tokens" cycle for one client_id can't interleave with another request
# for the same client_id — the race a plain GET-then-SET would have under
# concurrent requests.
#
# Refills continuously (tokens accrue every call based on elapsed time)
# rather than resetting to full capacity at fixed window boundaries, which
# is what makes this a token *bucket* rather than a fixed-window counter.
_TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local bucket = redis.call("HMGET", key, "tokens", "last_refill")
local tokens
local last_refill

if bucket[1] == false then
    tokens = capacity
    last_refill = now
else
    tokens = tonumber(bucket[1])
    last_refill = tonumber(bucket[2])
end

local elapsed = now - last_refill
if elapsed < 0 then
    elapsed = 0
end

tokens = tokens + elapsed * refill_rate
if tokens > capacity then
    tokens = capacity
end

local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

redis.call("HSET", key, "tokens", tokens, "last_refill", now)
redis.call("EXPIRE", key, math.ceil((capacity / refill_rate) * 2))

return allowed
"""


class TokenBucketRateLimiter:
    """Async, Redis-backed token bucket. One bucket per client_id, key
    schema `rate_limit:{client_id}`.
    """

    def __init__(self, redis: Redis, capacity: int, window_seconds: float):
        self.redis = redis
        self.capacity = capacity
        self.refill_rate = capacity / window_seconds
        self._script = redis.register_script(_TOKEN_BUCKET_SCRIPT)

    async def allow(self, client_id: str) -> bool:
        key = f"rate_limit:{client_id}"
        allowed = await self._script(
            keys=[key],
            args=[self.capacity, self.refill_rate, time.time()],
        )
        return bool(allowed)
