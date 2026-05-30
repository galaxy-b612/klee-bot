"""Health check router."""

import datetime

from fastapi import APIRouter

from app import __version__

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    """Return service health status."""
    return {
        "status": "ok",
        "service": "klee-core",
        "version": __version__,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }
