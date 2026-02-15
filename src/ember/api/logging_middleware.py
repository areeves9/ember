"""Request logging middleware for Ember."""

import time
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from ember.logging import get_logger

logger = get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log all incoming requests and responses."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.perf_counter()

        # Extract request info
        method = request.method
        path = request.url.path
        query = str(request.url.query) if request.url.query else ""

        # Process request
        response = await call_next(request)

        # Calculate duration
        duration_ms = (time.perf_counter() - start_time) * 1000

        # Log based on status code
        status = response.status_code
        query_str = f"?{query}" if query else ""

        if status >= 500:
            logger.error(f"{method} {path}{query_str} → {status} ({duration_ms:.1f}ms)")
        elif status >= 400:
            logger.warning(
                f"{method} {path}{query_str} → {status} ({duration_ms:.1f}ms)"
            )
        else:
            logger.info(f"{method} {path}{query_str} → {status} ({duration_ms:.1f}ms)")

        return response
