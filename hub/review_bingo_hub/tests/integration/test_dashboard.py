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
async def test_dashboard_serves_policy_panel_markup(client: AsyncClient) -> None:
    """The page a browser receives carries the B2 policy editor, not just its CSS.

    Why RFC 0002 B2 (#47): served-HTML assertions are the whole test surface
    this repo has for `dashboard/index.html` — there is no JS harness, by the
    ticket's own resolved scope decision. So the things asserted here are the
    ones whose absence would silently break the panel end to end: the container
    the rows render into, and the two endpoints the panel cannot work without.
    Runtime behaviour (keyed row reuse, dirty-edit preservation) is not
    assertable from here and is not claimed to be.
    """
    response = await client.get("/dashboard")

    assert response.status_code == HTTPStatus.OK
    assert 'id="policy"' in response.text
    assert "Policy floors" in response.text
    # The panel is read from /auth/me (who am I, and where am I admin) joined
    # against /policies (what is set today); neither fetch is optional.
    assert "/auth/me" in response.text
    assert "/policies" in response.text
    # /policies pages at 100 server-side by default; the panel must ask for
    # more than that in one round trip so an admin's own repo never silently
    # falls off the page and reads as "no policy" (see POLICIES_FETCH_LIMIT).
    assert "/policies?limit=${POLICIES_FETCH_LIMIT}" in response.text


@pytest.mark.asyncio
async def test_dashboard_serves_client_management_markup(client: AsyncClient) -> None:
    """The page a browser receives carries the B3 roster controls, not just their CSS.

    Same surface and same limit as the policy-panel test above: served-HTML
    assertions are the whole test surface `dashboard/index.html` has, so what is
    pinned here is the set of literals whose absence would break client
    management end to end — the container the rows render into, the revoke
    affordance itself, the DELETE call it makes, and the confirmation in front
    of it. A revoke that fired without asking is the one failure mode of this
    panel that cannot be undone, so the confirm is asserted rather than trusted.

    Runtime behaviour (keyed row reuse, the mid-revoke render skip, near-expiry
    flagging) is not assertable from here and is not claimed to be.
    """
    response = await client.get("/dashboard")

    assert response.status_code == HTTPStatus.OK
    assert 'id="roster"' in response.text
    assert "Revoke" in response.text
    # The endpoint A2 (#44) already built — the dashboard adds no route of its
    # own, so this template literal is the whole integration point.
    assert "`${HUB}/clients/${" in response.text
    assert 'method: "DELETE"' in response.text
    assert "confirm(" in response.text


@pytest.mark.asyncio
async def test_dashboard_says_so_when_it_was_not_deployed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hub image ships without dashboard/; that should read as a deploy fact."""
    monkeypatch.setattr(dashboard, "DASHBOARD_INDEX", Path("/nonexistent/index.html"))

    response = await client.get("/dashboard")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert "not deployed" in response.json()["detail"].lower()
