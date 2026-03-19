"""Assembles the v1 API router.

Feature routers are included here with their URL prefixes and tags.
The health router is mounted separately at the root level in main.py
so it has no /api/v1 prefix.
"""

from fastapi import APIRouter

from app.api.v1 import playback, queue, recommendations, sessions, sse, tags, tracks

v1_router = APIRouter()

v1_router.include_router(sessions.router, prefix="/sessions", tags=["sessions"])

# Queue routes use two different URL shapes:
#   /sessions/{session_id}/queue  (GET — read queue state)
#   /queue/...                    (POST/DELETE — mutate entries)
# Both live in the same router module; we mount without a prefix so that
# the paths declared in queue.py are used as-is.
v1_router.include_router(queue.router, tags=["queue"])

v1_router.include_router(tracks.router, prefix="/tracks", tags=["tracks"])

# Playback routes declare their own full paths (e.g. /tracks/{id}/stream)
# so they are mounted without a prefix.
v1_router.include_router(playback.router, tags=["playback"])

# Recommendations — no prefix, paths are declared in recommendations.py.
v1_router.include_router(recommendations.router, tags=["recommendations"])

# Mood tags — no prefix, paths are declared in tags.py.
v1_router.include_router(tags.router, tags=["tags"])

# SSE job status stream — no prefix, paths are declared in sse.py.
v1_router.include_router(sse.router, tags=["jobs"])
