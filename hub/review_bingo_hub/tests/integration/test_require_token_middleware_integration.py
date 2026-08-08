"""Integration tests for the deny-by-default request gate on the real app.

The unit tests drive a synthetic app; these drive `review_bingo_hub.main.app`
with its real router table, so they pin which *actually registered* routes are
reachable without an Authorization header.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from http import HTTPStatus

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from review_bingo_hub.db.session import get_session
from review_bingo_hub.main import app

# The placeholder the demo scripts, the client CLI, and the dashboard all send.
# It authenticates nothing; it only satisfies the coarse presence check.
PLACEHOLDER_AUTH = "Bearer pending-enrolment"

GATED_PATHS = ["/jobs", "/clients", "/policies/acme/repo", "/policies", "/users", "/docs", "/openapi.json"]

# The two endpoints dashboard/index.html's poll() calls directly.
DASHBOARD_POLLED_PATHS = ["/jobs", "/clients"]


@pytest.fixture
async def client_no_default_headers(
    engine: AsyncEngine,
    session_maker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient]:
    """Bare client against the real app that injects no headers at all.

    The shared `client` fixture injects a placeholder Authorization header so
    the rest of the suite keeps working behind the gate. These tests need the
    opposite: a client that sends exactly what a stranger would.
    """

    async def get_session_override() -> AsyncGenerator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.state.engine = engine
    app.state.async_session_maker = session_maker
    app.dependency_overrides[get_session] = get_session_override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as bare_client:
        yield bare_client

    app.dependency_overrides.clear()


class TestPublicPathsOnRealApp:
    """Allowlisted paths stay reachable by anyone."""

    @pytest.mark.parametrize("path", ["/ping", "/health", "/dashboard"])
    async def test_allowlisted_path_reachable_without_token(
        self,
        client_no_default_headers: AsyncClient,
        path: str,
    ) -> None:
        """An allowlisted path answers without an Authorization header."""
        response = await client_no_default_headers.get(path)

        assert response.status_code == HTTPStatus.OK


class TestGatedPathsOnRealApp:
    """Everything not on the allowlist is denied without a token."""

    @pytest.mark.parametrize("path", GATED_PATHS)
    async def test_gated_path_returns_401_without_token(
        self,
        client_no_default_headers: AsyncClient,
        path: str,
    ) -> None:
        """A non-allowlisted path returns exactly 401 with no header."""
        response = await client_no_default_headers.get(path)

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert response.json() == {"detail": "Authentication required"}

    async def test_client_registration_is_gated(
        self,
        client_no_default_headers: AsyncClient,
    ) -> None:
        """POST /clients is behind the gate too - registration needs a header."""
        response = await client_no_default_headers.post(
            "/clients",
            json={"name": "stranger", "model_name": "m", "provider": "ollama", "tier": "standard"},
        )

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    async def test_gated_path_passes_with_unvalidated_token(
        self,
        client_no_default_headers: AsyncClient,
    ) -> None:
        """The gate is presence-only: an unvalidated placeholder gets through.

        `/docs`, not `/jobs` or `/policies`: A3 gave `/jobs` endpoint-level auth
        (ClientDep) and A4 (#23) has now done the same for `/policies`, so on
        either of those a placeholder clears the gate and is then rejected at
        the endpoint — a 401 that says nothing about the middleware. `/docs` is
        a framework route with no endpoint-level auth to add, so it isolates the
        property under test and cannot be overtaken by a later auth ticket.
        """
        response = await client_no_default_headers.get(
            "/docs",
            headers={"Authorization": PLACEHOLDER_AUTH},
        )

        assert response.status_code == HTTPStatus.OK


class TestDashboardReliance:
    """Pin what the shipped dashboard's polls actually get today.

    These two endpoints are the dashboard's whole data feed, so whatever they
    answer to its placeholder header is a fact about the shipped product. Both
    directions are asserted — no header and placeholder header — so a change to
    either breaks loudly here rather than as a silently empty board.
    """

    @pytest.mark.parametrize("path", DASHBOARD_POLLED_PATHS)
    async def test_dashboard_polled_path_denied_without_header(
        self,
        client_no_default_headers: AsyncClient,
        path: str,
    ) -> None:
        """Without the dashboard's header, its polls would 401."""
        response = await client_no_default_headers.get(path)

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    @pytest.mark.parametrize("path", DASHBOARD_POLLED_PATHS)
    async def test_dashboard_polled_path_rejected_with_placeholder_header(
        self,
        client_no_default_headers: AsyncClient,
        path: str,
    ) -> None:
        """The header the dashboard actually sends no longer resolves its polls.

        A3 made both polled endpoints resolve a real grid client (ClientDep),
        and the shipped placeholder is not one. This is the intended,
        RFC-documented transitional state — the design names it under
        "Consequence worth naming" — and it holds until B1 (#24) gives the
        dashboard a real login. The dashboard renders it as its own
        "not authorized yet" state rather than as a hub outage.

        Pinned as 401 rather than deleted so the day #24 lands, this test goes
        red and asks to be updated instead of quietly staying true forever.
        """
        response = await client_no_default_headers.get(
            path,
            headers={"Authorization": PLACEHOLDER_AUTH},
        )

        assert response.status_code == HTTPStatus.UNAUTHORIZED
