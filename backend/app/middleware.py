"""HTTP middleware for the FastAPI app.

Currently provides:
  - RequestIdMiddleware — reads/generates the X-Request-ID header, binds it
    to structlog contextvars for the duration of the request, exposes it via
    ``request.state.request_id``, and echoes it back on the response so the
    frontend can log/correlate.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from structlog.contextvars import bind_contextvars, reset_contextvars

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Propagate X-Request-ID through logs and the HTTP response.

    The frontend sets the header on every outgoing axios request (see
    ``frontend/src/services/api.ts``). If it's missing — direct curl, health
    probe, etc. — we mint a uuid4 so logs are still stitchable.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id

        tokens = bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            # reset_contextvars restores the previous value (or unsets if there
            # was none), which is concurrency-safe across overlapping requests.
            reset_contextvars(**tokens)

        response.headers[REQUEST_ID_HEADER] = request_id
        return response
