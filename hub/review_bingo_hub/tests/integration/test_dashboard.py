"""The hub serves the dashboard so it shares an origin with the API.

Same-origin is the whole reason this route exists: opened from the filesystem,
the page's polling would be a cross-origin request to :7575 and the browser
would block it.
"""

from http import HTTPStatus
from pathlib import Path

import pytest
from httpx import AsyncClient

from review_bingo_hub.api import dashboard


@pytest.mark.asyncio
async def test_dashboard_is_served_as_html(client: AsyncClient) -> None:
    response = await client.get("/dashboard")

    assert response.status_code == HTTPStatus.OK
    assert response.headers["content-type"].startswith("text/html")
    assert "review-bingo" in response.text


@pytest.mark.asyncio
async def test_dashboard_no_longer_ships_the_placeholder_credential(client: AsyncClient) -> None:
    """The shipped page must not carry `Bearer pending-enrolment` any more.

    Asserted against the served bytes rather than the source file: what matters
    is what a browser receives. B1 (#24) replaced that placeholder with a real
    device-flow login, and a leftover copy would send every visitor down a path
    that authenticates nothing and renders as "not authorized" forever.
    """
    response = await client.get("/dashboard")

    assert response.status_code == HTTPStatus.OK
    assert "pending-enrolment" not in response.text


@pytest.mark.asyncio
async def test_dashboard_says_so_when_it_was_not_deployed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hub image ships without dashboard/; that should read as a deploy fact."""
    monkeypatch.setattr(dashboard, "DASHBOARD_INDEX", Path("/nonexistent/index.html"))

    response = await client.get("/dashboard")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert "not deployed" in response.json()["detail"].lower()
