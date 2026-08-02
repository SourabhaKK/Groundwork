"""The FastAPI service, gated from day one.

The gate came first on purpose: `/report` was a token-protected, rate-limited
501 from Phase 0.5, so every later phase was developed against real auth and
rate-limit constraints rather than being exempted from them until Phase 6.
Phase 6.1 replaced the stub's body with the real graph. The gate around it did
not change, which was the point — see `src/api/report.py`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from src.api.rate_limit import RateLimiter, get_limiter
from src.api.report import router as report_router
from src.config import get_settings
from src.data_sources.breakers import breaker_snapshots
from src.data_sources.search_quota import tracker as search_quota
from src.llm.router import get_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Fail fast and loudly if a required key is missing, rather than letting it
    # surface mid-run as an empty report that looks like thin evidence.
    get_settings()
    logger.info("configuration validated; all required keys present")
    yield


app = FastAPI(
    title="Groundwork — UK Company Underwriting Intelligence",
    description="First-pass intelligence report on a UK company, with per-section confidence.",
    version="0.1.0",
    lifespan=lifespan,
)


class HealthResponse(BaseModel):
    status: str
    llm_breaker: dict[str, object]
    source_breakers: list[dict[str, object]]
    rate_limit: dict[str, int]
    search_quota: dict[str, object]


@app.get("/health", response_model=HealthResponse)
def health(limiter: RateLimiter = Depends(get_limiter)) -> HealthResponse:
    """Unauthenticated and unmetered by design.

    The Phase 6.4a keep-warm cron has to reach this without carrying the demo
    secret in a public GitHub Actions workflow. It exposes operational state
    only — never a credential, never anything about a specific company.
    """
    try:
        breaker = get_router().breaker.snapshot()
    except Exception:  # noqa: BLE001 - diagnostics endpoint: degrade, don't 500
        logger.exception("LLM router unavailable")
        breaker = {"state": "unavailable"}

    quota = search_quota.snapshot()
    return HealthResponse(
        status="ok",
        llm_breaker=breaker,
        # An empty list means no source has been called yet this container
        # lifetime — which is not the same as every source being healthy, and
        # must not be read that way.
        source_breakers=breaker_snapshots(),
        rate_limit=limiter.snapshot(),
        search_quota={
            "status": quota.status.value,
            "searches_this_container": quota.searches_this_container,
            "credits_this_container": quota.credits_this_container,
            "detail": quota.detail,
        },
    )


#: Phase 6.1. The route carries its own auth and rate-limit dependencies, the
#: same two the Phase 0.5 stub carried.
app.include_router(report_router)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "groundwork", "docs": "/docs", "health": "/health"}
