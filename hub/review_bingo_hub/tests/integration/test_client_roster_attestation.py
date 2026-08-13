"""Integration tests for the roster's ownership and attestation fields (RFC 0002 B3).

`test_repo_scoped_access.py` pins *which* clients appear on `GET /clients`;
this module pins the three facts each surviving row now carries about itself,
and nothing about the scoping those tests already own.

Two properties matter more than the rest and are asserted directly rather than
inferred:

* **`is_own` is an identity comparison, not a truthiness one.** Two dev-mode
  clients both have `identity_id is None`, and a naive `client.identity_id ==
  caller.identity_id` would report each as the other's own machine — and so
  offer a revoke control for somebody else's box. `authorize_client_revoke`
  already refuses that write; this pins the *display* half so the dashboard
  never draws a button the hub would answer 404 to.
* **Staleness on the roster is the same answer the rest of the hub gives.**
  The row's `access_is_stale` reduces to `_snapshot_is_stale` through
  `access_freshness`, exactly as `/auth/me` and `identity_access_is_stale` do,
  so a client that cannot lease cannot simultaneously read as fresh on the
  dashboard that would be used to diagnose it.

`identity_access_ttl_seconds` itself is deliberately still absent from these
rows — `test_client_enrolment.py::test_only_check_in_gained_the_ttl_field`
pins that, and `access_expires_at` is the per-row form the roster gets instead:
an absolute deadline is what a "expires in 12m" reading needs, where the raw
constant would be the same number repeated once per row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.core.config import settings
from review_bingo_hub.models.review_client import ReviewClient
from review_bingo_hub.tests.integration.conftest import (
    ALLOWED,
    Enrolee,
    FakeGithubIdentityService,
    backdate_access_refreshed_at,
    enrol_dev_client,
    enrol_github_client,
    readable,
    start_dashboard_session,
)

RECENT = timedelta(minutes=5)


async def identity_id_of(session: AsyncSession, client_id: str) -> UUID:
    """The GitHub identity a registered client is linked to.

    `expire_all()` first: `expire_on_commit=False` means a row this session read
    before the enrolment request would still carry its pre-request values.
    Mirrors `test_repo_scoped_access.py`'s helper of the same name.
    """
    session.expire_all()
    result = await session.execute(select(ReviewClient).where(col(ReviewClient.id) == UUID(client_id)))
    grid_client = result.scalar_one()
    assert grid_client.identity_id is not None
    return grid_client.identity_id


async def roster(client: AsyncClient, headers: dict[str, str]) -> dict[str, dict[str, Any]]:
    """The roster this caller sees, keyed by client id."""
    response = await client.get("/clients", headers=headers)
    assert response.status_code == HTTPStatus.OK
    return {row["id"]: row for row in response.json()}


def moment(value: str) -> datetime:
    """One ISO-8601 instant off the wire.

    The hub stores both columns timezone-aware, so a naive value here would be
    a serialization regression rather than a test convenience to paper over.
    """
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    return parsed


# ---------------------------------------------------------------------------
# is_own — whose machine is this
# ---------------------------------------------------------------------------


async def test_roster_marks_the_callers_own_client_as_own(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ownership follows the GitHub account, so a second box under it is still "mine".

    The account is the unit of admission (see `github_identity`), which is the
    same reason `DELETE /clients/{id}` lets one machine revoke its sibling: a
    per-machine notion of "own" would mark a laptop as somebody else's the
    moment its owner enrolled a second one.
    """
    fake = FakeGithubIdentityService()
    first_id, first_headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="ay", user_id=1, repo_access=[readable(ALLOWED)])
    )
    second_id, _ = await enrol_github_client(
        client,
        monkeypatch,
        fake,
        Enrolee(login="ay", user_id=1, repo_access=[readable(ALLOWED)], name="ay-second-box"),
    )
    stranger_id, _ = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="bee", user_id=2, repo_access=[readable(ALLOWED)])
    )

    rows = await roster(client, first_headers)

    assert rows[first_id]["is_own"] is True
    assert rows[second_id]["is_own"] is True
    assert rows[stranger_id]["is_own"] is False


async def test_roster_dashboard_session_marks_its_identity_as_own(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser has no row on the grid, and still has machines of its own.

    `ScopedCaller.client_id` is None for a dashboard session, so an `is_own`
    derived from the caller's *client* rather than its identity would mark every
    row as somebody else's and hide the revoke control from the one surface B3
    exists to put it on.
    """
    fake = FakeGithubIdentityService()
    viewer = Enrolee(login="viewer", user_id=9, repo_access=[readable(ALLOWED)])
    own_id, _ = await enrol_github_client(client, monkeypatch, fake, viewer)
    stranger_id, _ = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="stranger", user_id=10, repo_access=[readable(ALLOWED)])
    )

    headers = await start_dashboard_session(client, monkeypatch, fake, viewer)
    rows = await roster(client, headers)

    assert rows[own_id]["is_own"] is True
    assert rows[stranger_id]["is_own"] is False


async def test_roster_dev_mode_clients_are_never_marked_own(client: AsyncClient) -> None:
    """Two identity-less clients must not read as each other's own machine.

    The `None == None` trap `authorize_client_revoke` documents, asserted on the
    display side: dev mode leaves every client's `identity_id` NULL, so an
    equality check with no explicit guard would hand every dev-mode box a revoke
    control over every other one.
    """
    assert settings.client_enrolment_mode == "dev"

    first_id, first_headers = await enrol_dev_client(client, "dev-one")
    second_id, second_headers = await enrol_dev_client(client, "dev-two")

    from_first = await roster(client, first_headers)
    from_second = await roster(client, second_headers)

    assert from_first[first_id]["is_own"] is False
    assert from_first[second_id]["is_own"] is False
    assert from_second[first_id]["is_own"] is False
    assert from_second[second_id]["is_own"] is False


# ---------------------------------------------------------------------------
# Attestation freshness — how long this machine can still lease
# ---------------------------------------------------------------------------


async def test_roster_reports_attestation_freshness_for_github_clients(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh enrolment reports when it attested and when that runs out."""
    fake = FakeGithubIdentityService()
    own_id, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="fresh", user_id=1, repo_access=[readable(ALLOWED)])
    )

    row = (await roster(client, headers))[own_id]

    refreshed_at = moment(row["access_refreshed_at"])
    assert datetime.now(UTC) - refreshed_at < RECENT
    assert row["access_is_stale"] is False
    # The deadline is derived, not stored: the same TTL the hub enforces at
    # lease time, resolved to an absolute instant per row so the dashboard can
    # count down without knowing the constant.
    assert moment(row["access_expires_at"]) == refreshed_at + timedelta(seconds=settings.identity_access_ttl_seconds)


async def test_roster_flags_stale_access(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One second past the TTL is stale on the roster, exactly as it is at lease time.

    Backdated to the boundary rather than a day, because the boundary is the
    assertion: a roster that computed staleness from its own threshold would
    still pass a test that aged the clock past every plausible one.
    """
    fake = FakeGithubIdentityService()
    own_id, headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="aged", user_id=1, repo_access=[readable(ALLOWED)])
    )
    await backdate_access_refreshed_at(
        session,
        await identity_id_of(session, own_id),
        seconds_ago=settings.identity_access_ttl_seconds + 1,
    )

    row = (await roster(client, headers))[own_id]

    assert row["access_is_stale"] is True
    assert moment(row["access_expires_at"]) < datetime.now(UTC)


async def test_roster_omits_attestation_fields_for_dev_mode_clients(client: AsyncClient) -> None:
    """No GitHub account behind a client means no attestation clock to report.

    Null rather than an invented instant: dev mode has nothing that ever
    attested, and a synthesised timestamp would render as a countdown to a
    deadline nobody enforces. `access_is_stale` stays False for the same
    reason `identity_access_is_stale` does — there is no snapshot to age.
    """
    assert settings.client_enrolment_mode == "dev"

    dev_id, headers = await enrol_dev_client(client, "dev-solo")

    row = (await roster(client, headers))[dev_id]

    assert row["identity_id"] is None
    assert row["access_refreshed_at"] is None
    assert row["access_expires_at"] is None
    assert row["access_is_stale"] is False
