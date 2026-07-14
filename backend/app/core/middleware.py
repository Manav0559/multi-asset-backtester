"""Cross-cutting HTTP middleware: request IDs, structured access logging, and a
Redis-backed fixed-window rate limiter.

Why here (not per-route): these are infra concerns that must wrap *every* request
uniformly. The rate limiter degrades open — if Redis is unreachable it lets the
request through rather than taking the API down with the cache.
"""
from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS

logger = logging.getLogger("backtester.access")


def _route_template(request: Request) -> str:
    """The matched route's path TEMPLATE (e.g. /backtests/{backtest_id}) —
    set on the scope by the router during call_next. Falls back to a single
    bucket for unmatched paths so 404 scans can't explode label cardinality."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns each request an ID, emits one structured access log line, and
    records Prometheus count/latency with method, route template, and status.
    The ID is echoed in `X-Request-ID` so a client error can be traced to a
    single server log line."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = time.perf_counter() - start
            HTTP_REQUESTS.labels(request.method, _route_template(request), "500").inc()
            HTTP_LATENCY.labels(request.method, _route_template(request)).observe(elapsed)
            logger.exception(
                "request_failed",
                extra={"request_id": request_id, "method": request.method,
                       "path": request.url.path, "elapsed_ms": round(elapsed * 1000, 1)},
            )
            raise
        elapsed = time.perf_counter() - start
        route = _route_template(request)
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, route).observe(elapsed)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "%s %s %s %.1fms",
            request.method, request.url.path, response.status_code, elapsed * 1000,
            extra={"request_id": request_id, "status": response.status_code},
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-client rate limit (default 120 req / 60s), keyed on the
    JWT subject when present, else the client IP. In-process counters (single
    web process) — exact here, and an abuse backstop, not a smoother.

    A fixed window is deliberately chosen over a token bucket: a single counter
    per (client, window), cheap, and good enough. Stale windows are pruned
    lazily so the map can't grow without bound.
    """

    # Paths that must never be rate limited (liveness probes, Prometheus
    # scrapes, WS upgrade).
    EXEMPT_PREFIXES = ("/health", "/metrics", "/ws", "/docs", "/openapi.json", "/redoc")

    def __init__(self, app, limit: int = 120, window_seconds: int = 60):
        super().__init__(app)
        self._limit = limit
        self._window = window_seconds
        self._counts: dict[str, int] = {}
        self._window_id = 0

    def _identity(self, request: Request) -> str:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            # Cheap, non-verifying subject extraction — enough for a rate key.
            return f"tok:{auth[7:][:24]}"
        client = request.client
        return f"ip:{client.host if client else 'unknown'}"

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path.startswith(self.EXEMPT_PREFIXES):
            return await call_next(request)

        window = int(time.time()) // self._window
        if window != self._window_id:      # new window: reset all counters
            self._counts = {}
            self._window_id = window
        key = self._identity(request)
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count

        if count > self._limit:
            retry_after = self._window - (int(time.time()) % self._window)
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": str(retry_after),
                         "X-RateLimit-Limit": str(self._limit)},
            )
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self._limit)
        response.headers["X-RateLimit-Remaining"] = str(max(self._limit - count, 0))
        return response
