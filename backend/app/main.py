"""FastAPI entrypoint."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.api.routes.auth import router as auth_router
from app.api.routes.backtests import router as backtests_router
from app.api.routes.leaderboard import router as leaderboard_router
from app.api.routes.market import router as market_router
from app.api.routes.orders import router as orders_router
from app.api.routes.portfolios import router as portfolios_router
from app.api.routes.ws import router as ws_router
from app.core.config import settings
from app.core.middleware import RateLimitMiddleware, RequestContextMiddleware
from app.db.session import engine
from app.streaming.hub import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the WS hub's Redis subscription bridge on boot, tear down on exit.
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

# Middleware order: added last runs first (outermost). We want request-context/
# logging on the very outside, then the rate limiter, then CORS closest to routes.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials="*" not in settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)
if settings.RATE_LIMIT_ENABLED:
    app.add_middleware(
        RateLimitMiddleware,
        redis_url=settings.REDIS_URL,
        limit=settings.RATE_LIMIT_PER_MINUTE,
        window_seconds=60,
    )
app.add_middleware(RequestContextMiddleware)

app.include_router(auth_router)
app.include_router(portfolios_router)
app.include_router(orders_router)
app.include_router(backtests_router)
app.include_router(leaderboard_router)
app.include_router(market_router)
app.include_router(ws_router)


@app.get("/health")
def health() -> dict:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "app": settings.APP_NAME}


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Prometheus exposition for the WEB process (request counts/latency).
    Worker metrics are served by the worker itself — see tasks.py."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
