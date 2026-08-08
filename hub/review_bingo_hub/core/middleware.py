"""Middleware for request validation and processing.

Provides cross-cutting middleware for:
- Request payload size validation
- Request/response logging
- Deny-by-default token presence gate
- Error handling
- Performance monitoring
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

LOGGER = logging.getLogger(__name__)

# Constants
MAX_REQUEST_SIZE_BYTES_DEFAULT = 50 * 1024 * 1024
ERROR_STATUS_CODE_THRESHOLD = 400

# The complete set of paths reachable without an Authorization header.
# Deliberately a fixed constant rather than configuration: an allowlist that
# can be widened from a .env file is an allowlist an operator can widen by
# accident, and "which paths are open" is a property of the service's design,
# not of a deployment.
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/ping",
        "/webhooks/github",
        "/dashboard",
        "/auth/device/start",
        "/auth/device/poll",
    }
)


class RequestSizeValidationMiddleware(BaseHTTPMiddleware):
    """Middleware to validate request payload size.

    Enforces maximum request body size to prevent:
    - Memory exhaustion from huge uploads
    - DoS attacks with large payloads
    - Accidental client errors (like uploading wrong file)

    Returns 413 Payload Too Large if content exceeds limit.

    Configuration:
        MAX_REQUEST_SIZE_BYTES: Maximum allowed request size
            - Default: 50MB (50 * 1024 * 1024)
            - Set via environment: MAX_REQUEST_SIZE_BYTES

    Examples:
        # In main.py
        from review_bingo_hub.core.middleware import RequestSizeValidationMiddleware
        app.add_middleware(RequestSizeValidationMiddleware, max_size_bytes=100*1024*1024)

    Security Notes:
        - Always enforce reasonable limits based on use case
        - Set limits lower than server memory available
        - Document limits in API documentation
        - Monitor for clients repeatedly hitting limit
    """

    def __init__(self, app: ASGIApp, max_size_bytes: int = MAX_REQUEST_SIZE_BYTES_DEFAULT) -> None:
        """Initialize middleware with max request size.

        Args:
            app: FastAPI application
            max_size_bytes: Maximum request body size (default: 50MB)
        """
        super().__init__(app)
        self.max_size_bytes = max_size_bytes
        self.max_size_mb = max_size_bytes / (1024 * 1024)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Validate request size before passing to endpoint.

        Args:
            request: Incoming HTTP request
            call_next: Next middleware in chain

        Returns:
            Response from endpoint or 413 error if size exceeded
        """
        # Check Content-Length header if present
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size_bytes = int(content_length)
                if size_bytes > self.max_size_bytes:
                    LOGGER.warning(
                        "request_size_exceeded",
                        extra={
                            "size_bytes": size_bytes,
                            "max_bytes": self.max_size_bytes,
                            "path": request.url.path,
                            "method": request.method,
                        },
                    )
                    return JSONResponse(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        content={
                            "detail": (f"Request payload exceeds maximum allowed size ({self.max_size_mb:.1f} MB)")
                        },
                    )
            except ValueError:
                # Invalid content-length header, let endpoint handle it
                pass

        # Pass to next middleware
        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log all HTTP requests and responses.

    Captures:
    - Method and path
    - Status code
    - Response time
    - Request size
    - Response size

    Useful for:
    - Performance monitoring
    - Request tracing
    - Debugging
    - Access logs

    Configuration:
        - Set log level to DEBUG to see request/response bodies
        - Use request_id from context for correlation

    Examples:
        # In main.py
        from review_bingo_hub.core.middleware import RequestLoggingMiddleware
        app.add_middleware(RequestLoggingMiddleware)
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Log request and response details.

        Args:
            request: Incoming HTTP request
            call_next: Next middleware in chain

        Returns:
            Response from endpoint
        """
        # Start timer
        start_time = time.perf_counter()

        # Get request info
        method = request.method
        path = request.url.path
        request_size = int(request.headers.get("content-length", 0))

        # Call next middleware
        try:
            response = await call_next(request)
        except Exception as exc:
            # Log exceptions but don't suppress them
            duration = time.perf_counter() - start_time
            LOGGER.exception(
                "request_failed",
                extra={
                    "method": method,
                    "path": path,
                    "duration_seconds": duration,
                    "error": str(exc),
                },
            )
            raise

        # Calculate response time
        duration = time.perf_counter() - start_time
        response_size = int(response.headers.get("content-length", 0))

        # Log based on status code
        log_func = LOGGER.warning if response.status_code >= ERROR_STATUS_CODE_THRESHOLD else LOGGER.info

        log_func(
            "http_request",
            extra={
                "method": method,
                "path": path,
                "status_code": response.status_code,
                "duration_seconds": round(duration, 3),
                "request_size_bytes": request_size,
                "response_size_bytes": response_size,
            },
        )

        return response


class RequireTokenMiddleware(BaseHTTPMiddleware):
    """Deny-by-default gate: unauthenticated requests to non-public paths get 401.

    Every path is closed unless it appears in PUBLIC_PATHS. This inverts the
    hub's previous posture, where a route was open unless someone remembered
    to guard it - a default that fails silently in exactly the direction you
    do not want.

    What "has a token" means here:
        The Authorization header is present and non-empty. That is the whole
        check. Any scheme, any value.

    What it does NOT mean:
        This middleware does not validate anything. It never answers "is this
        token real" - only "is there something to check". Per-route validity
        stays exactly where it already lives: ClientDep/get_current_client for
        client tokens, ScopedCallerDep for reads a dashboard session may also
        make, verify_signature for webhook HMAC. B1 (#24) slotted the device
        flow's real validation into that layer, not this one.

    Configuration:
        None. PUBLIC_PATHS is a fixed constant, not a setting.

    Examples:
        # In main.py
        from review_bingo_hub.core.middleware import RequireTokenMiddleware
        app.add_middleware(RequireTokenMiddleware)

    Security Notes:
        - PUBLIC_PATHS is matched EXACTLY, never by prefix. A prefix match
          would make "/health" open up "/health/../admin" and every future
          sub-path nobody re-reviewed; an exact set can only be widened on
          purpose, in a diff.
        - "/docs" and "/openapi.json" are deliberately absent: the schema
          enumerates every route and its payload shape, which is the first
          thing worth withholding from an unauthenticated caller.
        - "/metrics" is deliberately absent for the same reason.
        - "/webhooks/github" is public here because it authenticates by HMAC
          signature inside the handler, not by a bearer token. Removing it
          from the allowlist would break GitHub delivery, not harden it.
        - "/auth/device/start" and "/auth/device/poll" are public because they
          are how a caller *obtains* a credential: gating them on one would make
          signing in possible only for someone already signed in. They mint
          nothing on their own - authorization comes from GitHub, and a poll
          writes a session only once GitHub says the person authorized it.
        - OPTIONS is always allowed through: CORS preflight carries no
          Authorization header by specification, so gating it would break
          every cross-origin call before the real request was ever made.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Reject requests to non-public paths that carry no Authorization header.

        Args:
            request: Incoming HTTP request
            call_next: Next middleware in chain

        Returns:
            Response from endpoint, or 401 if the gate denies the request
        """
        if request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        if not request.headers.get("authorization"):
            LOGGER.warning(
                "request_denied_no_token",
                extra={
                    "path": request.url.path,
                    "method": request.method,
                },
            )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Authentication required"},
            )

        return await call_next(request)
