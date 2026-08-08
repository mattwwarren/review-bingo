"""Tests for RequireTokenMiddleware, the deny-by-default request gate.

Tests cover:
- PUBLIC_PATHS is an exact, closed allowlist
- Allowlisted paths reach the endpoint without an Authorization header
- Non-allowlisted paths are rejected with 401 when no header is present
- Any non-empty Authorization header is enough to pass the gate (the
  middleware never validates the token, only its presence)
- CORS preflight (OPTIONS) is never blocked
- Denials are logged at warning level

These are unit tests against a synthetic app; they don't require a database.
"""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from review_bingo_hub.core.middleware import PUBLIC_PATHS, RequireTokenMiddleware

PLACEHOLDER_TOKEN = "Bearer not-a-real-token"


@pytest.fixture
def app_with_require_token_middleware() -> FastAPI:
    """Create a FastAPI app gated by RequireTokenMiddleware.

    Registers a stand-in route for every allowlisted path plus one
    non-allowlisted route, so the gate can be exercised from both sides.

    Returns:
        FastAPI app with RequireTokenMiddleware installed.
    """
    app = FastAPI()
    app.add_middleware(RequireTokenMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"message": "pong"}

    @app.post("/webhooks/github")
    async def webhook() -> dict[str, str]:
        return {"status": "accepted"}

    @app.get("/dashboard")
    async def dashboard() -> dict[str, str]:
        return {"page": "dashboard"}

    @app.post("/auth/device/start")
    async def device_start() -> dict[str, str]:
        return {"status": "started"}

    @app.post("/auth/device/poll")
    async def device_poll() -> dict[str, str]:
        return {"status": "authorization_pending"}

    @app.api_route("/jobs", methods=["GET", "OPTIONS"])
    async def jobs() -> dict[str, str]:
        return {"status": "ok"}

    return app


class TestPublicPaths:
    """Tests pinning the contents of the allowlist itself."""

    def test_public_paths_contents_is_exact(self) -> None:
        """The allowlist is exactly these six paths - nothing else is public.

        The device-flow pair joined it with B1 (#24): the whole point of those
        two endpoints is that the caller has no credential yet, so gating them
        on one would make logging in impossible for anyone not already logged
        in.
        """
        assert (
            frozenset(
                {
                    "/health",
                    "/ping",
                    "/webhooks/github",
                    "/dashboard",
                    "/auth/device/start",
                    "/auth/device/poll",
                }
            )
            == PUBLIC_PATHS
        )

    def test_public_paths_is_immutable(self) -> None:
        """The allowlist is a frozenset so no caller can widen it at runtime."""
        assert isinstance(PUBLIC_PATHS, frozenset)


class TestRequireTokenMiddleware:
    """Tests for the deny-by-default gate."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("path", ["/health", "/ping", "/dashboard"])
    async def test_allowlisted_get_paths_pass_without_token(
        self,
        app_with_require_token_middleware: FastAPI,
        path: str,
    ) -> None:
        """Allowlisted GET paths reach the endpoint with no Authorization header."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_require_token_middleware),
            base_url="http://test",
        ) as client:
            response = await client.get(path)

        assert response.status_code == HTTPStatus.OK

    @pytest.mark.anyio
    async def test_webhook_path_passes_without_token(
        self,
        app_with_require_token_middleware: FastAPI,
    ) -> None:
        """The webhook path is allowlisted - it authenticates via HMAC in the handler."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_require_token_middleware),
            base_url="http://test",
        ) as client:
            response = await client.post("/webhooks/github", json={})

        assert response.status_code == HTTPStatus.OK

    @pytest.mark.anyio
    @pytest.mark.parametrize("path", ["/auth/device/start", "/auth/device/poll"])
    async def test_device_flow_paths_pass_without_token(
        self,
        app_with_require_token_middleware: FastAPI,
        path: str,
    ) -> None:
        """Logging in cannot require being logged in - both device paths are open."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_require_token_middleware),
            base_url="http://test",
        ) as client:
            response = await client.post(path, json={})

        assert response.status_code == HTTPStatus.OK

    @pytest.mark.anyio
    async def test_non_allowlisted_path_rejected_without_token(
        self,
        app_with_require_token_middleware: FastAPI,
    ) -> None:
        """A non-allowlisted path with no Authorization header gets 401."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_require_token_middleware),
            base_url="http://test",
        ) as client:
            response = await client.get("/jobs")

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert response.json() == {"detail": "Authentication required"}

    @pytest.mark.anyio
    async def test_non_allowlisted_path_passes_with_any_token(
        self,
        app_with_require_token_middleware: FastAPI,
    ) -> None:
        """The gate checks presence only - an unvalidated token is enough to pass."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_require_token_middleware),
            base_url="http://test",
        ) as client:
            response = await client.get("/jobs", headers={"Authorization": PLACEHOLDER_TOKEN})

        assert response.status_code == HTTPStatus.OK

    @pytest.mark.anyio
    async def test_non_bearer_scheme_passes(
        self,
        app_with_require_token_middleware: FastAPI,
    ) -> None:
        """Any scheme passes - the middleware never parses the header."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_require_token_middleware),
            base_url="http://test",
        ) as client:
            response = await client.get("/jobs", headers={"Authorization": "Basic anything"})

        assert response.status_code == HTTPStatus.OK

    @pytest.mark.anyio
    async def test_empty_authorization_header_rejected(
        self,
        app_with_require_token_middleware: FastAPI,
    ) -> None:
        """An empty Authorization header is not a token - it gets 401."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_require_token_middleware),
            base_url="http://test",
        ) as client:
            response = await client.get("/jobs", headers={"Authorization": ""})

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    @pytest.mark.anyio
    @pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
    async def test_docs_paths_are_not_public(
        self,
        app_with_require_token_middleware: FastAPI,
        path: str,
    ) -> None:
        """The API docs are deliberately absent from the allowlist."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_require_token_middleware),
            base_url="http://test",
        ) as client:
            response = await client.get(path)

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    @pytest.mark.anyio
    @pytest.mark.parametrize("path", ["/health/detail", "/dashboard/secrets", "/pingpong"])
    async def test_allowlist_is_exact_match_not_prefix(
        self,
        app_with_require_token_middleware: FastAPI,
        path: str,
    ) -> None:
        """A path that merely starts with an allowlisted path is still gated.

        The allowlist is matched exactly, so no sub-path or lookalike path
        inherits an allowlisted path's exemption.
        """
        async with AsyncClient(
            transport=ASGITransport(app=app_with_require_token_middleware),
            base_url="http://test",
        ) as client:
            response = await client.get(path)

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    @pytest.mark.anyio
    async def test_options_preflight_not_blocked(
        self,
        app_with_require_token_middleware: FastAPI,
    ) -> None:
        """CORS preflight carries no Authorization header and must not be gated."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_require_token_middleware),
            base_url="http://test",
        ) as client:
            response = await client.request("OPTIONS", "/jobs")

        assert response.status_code == HTTPStatus.OK

    @pytest.mark.anyio
    async def test_logs_warning_on_denial(
        self,
        app_with_require_token_middleware: FastAPI,
    ) -> None:
        """A denial is logged at warning level with the path and method."""
        with patch("review_bingo_hub.core.middleware.LOGGER") as mock_logger:
            async with AsyncClient(
                transport=ASGITransport(app=app_with_require_token_middleware),
                base_url="http://test",
            ) as client:
                await client.get("/jobs")

            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args
            assert "request_denied_no_token" in str(call_args)
            extra = call_args.kwargs.get("extra", {})
            assert extra.get("path") == "/jobs"
            assert extra.get("method") == "GET"

    @pytest.mark.anyio
    async def test_no_warning_logged_when_token_present(
        self,
        app_with_require_token_middleware: FastAPI,
    ) -> None:
        """A request that passes the gate logs nothing from this middleware."""
        with patch("review_bingo_hub.core.middleware.LOGGER") as mock_logger:
            async with AsyncClient(
                transport=ASGITransport(app=app_with_require_token_middleware),
                base_url="http://test",
            ) as client:
                await client.get("/jobs", headers={"Authorization": PLACEHOLDER_TOKEN})

            mock_logger.warning.assert_not_called()
