"""Assembles the v1 API router.

All feature routers (tracks, sessions, queue, etc.) will be included here
as they are built in subsequent phases. The health router is mounted
separately at the root level in main.py so it has no /api/v1 prefix.
"""

from fastapi import APIRouter

v1_router = APIRouter()

# Feature routers will be added here in later phases, e.g.:
# from app.api.v1 import tracks, sessions, queue, recommendations
# v1_router.include_router(tracks.router, prefix="/tracks", tags=["tracks"])
