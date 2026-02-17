import math
from inspect import isawaitable
from typing import Any

import redis.asyncio as aioredis
from fastapi import HTTPException, Request

from data_commons_search.utils import logger

INTERVAL_SEC_AUTH = 1.0
INTERVAL_SEC_ANON = 5.0


class RateLimiter:
    """Redis-backed rate limiter using INCR+EXPIRE per key."""

    def __init__(self, redis_url: str, *, prefix: str = "rl:") -> None:
        self._prefix = prefix
        self._redis_url = redis_url
        self._redis: aioredis.Redis | None = aioredis.from_url(redis_url)

    async def init(self) -> None:
        """Check Redis availability once at app startup.

        Returns:
            True when Redis is reachable and rate limiting is enabled.
        """
        if self._redis is None:
            return
        try:
            ping_result = self._redis.ping()
            if await ping_result if isawaitable(ping_result) else ping_result:
                logger.info(f"Using Redis server at {self._redis_url} for rate limiting.")
                return
        except Exception:
            logger.warning(f"Redis server not available at {self._redis_url}. Rate limiting disabled.")
        await self.aclose()
        self._redis = None

    async def aclose(self) -> None:
        """Close Redis connections cleanly on app shutdown."""
        if self._redis is None:
            return
        await self._redis.aclose()

    async def check(self, request: Request, user: dict[str, Any] | None) -> None:
        """Check if the request is allowed under the rate limit. Raises `HTTPException` if not."""
        if self._redis is None:
            return

        if user:
            # TODO: check the right field
            key = f"auth:{user.get('sub', 'unknown')!s}"
            interval_seconds = INTERVAL_SEC_AUTH
        else:
            client_host = request.client.host if request.client else "unknown"
            key = f"anon:{client_host}"
            interval_seconds = INTERVAL_SEC_ANON

        retry_after = None
        fullkey = f"{self._prefix}{key}:{int(interval_seconds)}"
        try:
            # atomically increment the counter for the current window
            cur = await self._redis.incr(fullkey)
            if cur == 1:
                # first seen in this window: set expiry
                await self._redis.expire(fullkey, int(interval_seconds))
                return
            ttl = await self._redis.ttl(fullkey)
            # ttl may be -1 if no expire set, fall back to interval_seconds
            retry_after = float(interval_seconds) if ttl is None or ttl < 0 else float(ttl)
        except Exception as e:
            logger.error(f"Error in Redis rate limiter: {e}")
            return
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many requests",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
