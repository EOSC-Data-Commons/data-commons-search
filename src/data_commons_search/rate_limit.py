import math

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import text

from data_commons_search.auth import UserInfo
from data_commons_search.config import settings
from data_commons_search.db import engine
from data_commons_search.utils import logger

INTERVAL_SEC_AUTH = 2.0
INTERVAL_SEC_ANON = 10.0

# Atomic upsert: reset the counter when the previous window has expired,
# otherwise increment in place. Returns the post-update count and the
# remaining seconds until the window ends.
_UPSERT_SQL = """
INSERT INTO rate_limits (key, count, window_end)
VALUES (:key, 1, now() + make_interval(secs => :interval))
ON CONFLICT (key) DO UPDATE
SET count = CASE
        WHEN rate_limits.window_end < now() THEN 1
        ELSE rate_limits.count + 1
    END,
    window_end = CASE
        WHEN rate_limits.window_end < now() THEN now() + make_interval(secs => :interval)
        ELSE rate_limits.window_end
    END
RETURNING count, EXTRACT(EPOCH FROM (window_end - now())) AS retry_after
"""


class RateLimiter:
    """Postgres-backed rate limiter using an atomic UPSERT per key."""

    async def check(self, request: Request, user: UserInfo | None) -> None:
        """Check if the request is allowed under the rate limit. Raises `HTTPException` if not."""
        if not settings.rate_limiting_enabled:
            return
        if user:
            key = f"auth:{user.sub}"
            interval_seconds = INTERVAL_SEC_AUTH
        else:
            client_host = request.client.host if request.client else "unknown"
            key = f"anon:{client_host}"
            interval_seconds = INTERVAL_SEC_ANON

        fullkey = f"{key}:{int(interval_seconds)}"
        try:
            count, retry_after = await run_in_threadpool(self._incr, fullkey, interval_seconds)
        except Exception as e:
            logger.error(f"Error in Postgres rate limiter: {e}")
            return

        if count > 1:
            wait = float(retry_after) if retry_after and retry_after > 0 else float(interval_seconds)
            raise HTTPException(
                status_code=429,
                detail="Too many requests",
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )

    def _incr(self, key: str, interval_seconds: float) -> tuple[int, float]:
        with engine.begin() as conn:
            row = conn.execute(text(_UPSERT_SQL), {"key": key, "interval": interval_seconds}).one()
            count, retry_after = row[0], row[1]
            return int(count), float(retry_after or 0.0)
