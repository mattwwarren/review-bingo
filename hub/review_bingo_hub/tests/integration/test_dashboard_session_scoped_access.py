"""Integration tests for what a dashboard session may read, and what it may not do.

`test_repo_scoped_access.py` pins the same invariant for a registered client.
This file exists because B1 (#24) introduced a *second* kind of caller — a
person's browser session, resolved through `ScopedCallerDep` — and the risk of
adding one is that it arrives with a different answer to "what can this caller
see" than the machine path already had.

Two properties matter more than the rest:

* **A session reads exactly what its identity reads.** Same access set, same
  404-not-403 oracle rule on a job it cannot reach. Anything narrower would
  blank the dashboard; anything wider would make the login a privilege
  escalation.
* **Reading is all it may do.** Leasing, checking in, reporting, and writing a
  repo policy stay on the machine credential path. The policy-write case is
  asserted as 401 rather than 403 on purpose: 403 would mean `ScopedCallerDep`
  had been wired into `RepoAdminDep` and the session merely lacked admin, which
  is a far shorter step away from actually granting it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.models.dashboard_session import DashboardSession
from review_bingo_hub.models.repo_policy import RepoPolicyUpsert
from review_bingo_hub.services.client_service import hash_token
from review_bingo_hub.services.policy_service import upsert_policy
from review_bingo_hub.tests.integration.conftest import (
    ALLOWED,
    FORBIDDEN,
    UNRELATED,
    Enrolee,
    FakeGithubIdentityService,
    enqueue,
    enrol_github_client,
    readable,
    start_dashboard_session,
)

VIEWER = Enrolee(login="marge-viewer", user_id=91, repo_access=[readable(ALLOWED)])


async def expire_session_for(session: AsyncSession, token: str) -> None:
    """Age one dashboard session past its expiry, then commit so the app sees it.

    A direct column write rather than a frozen clock, matching
    `backdate_access_refreshed_at`: the suite has no freezegun, and the expiry
    rule reads exactly this one column.
    """
    await session.execute(
        update(DashboardSession)
        .where(col(DashboardSession.token_hash) == hash_token(token))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Reads: a session sees exactly what its identity sees
# ---------------------------------------------------------------------------


async def test_dashboard_session_job_feed_is_scoped_to_its_identity(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inside = await enqueue(client, ALLOWED, "session-inside")
    await enqueue(client, FORBIDDEN, "session-outside")

    headers = await start_dashboard_session(client, monkeypatch, FakeGithubIdentityService(), VIEWER)

    response = await client.get("/jobs", headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert [job["id"] for job in response.json()] == [inside]


async def test_dashboard_session_get_job_out_of_access_is_404(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The oracle-safety rule holds for the browser too, not just the machine.

    A real-but-invisible job and a job id that never existed must be
    indistinguishable, or the dashboard becomes a way to enumerate which job ids
    are real across every org on the hub.
    """
    forbidden_id = await enqueue(client, FORBIDDEN, "session-oracle")

    headers = await start_dashboard_session(client, monkeypatch, FakeGithubIdentityService(), VIEWER)

    real_but_forbidden = await client.get(f"/jobs/{forbidden_id}", headers=headers)
    never_existed = await client.get(f"/jobs/{uuid4()}", headers=headers)

    assert real_but_forbidden.status_code == HTTPStatus.NOT_FOUND
    assert real_but_forbidden.json() == never_existed.json()


async def test_dashboard_session_sees_the_roster_without_a_client_row_of_its_own(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session has no machine on the grid, and the roster must not trip over that.

    `list_clients` used to add "plus the requester's own row" unconditionally.
    A caller with no row at all is the case that would have crashed it.
    """
    fake = FakeGithubIdentityService()
    overlapping_id, _ = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="shares", user_id=1, repo_access=[readable(ALLOWED)])
    )
    unrelated_id, _ = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="apart", user_id=2, repo_access=[readable(UNRELATED)])
    )

    headers = await start_dashboard_session(client, monkeypatch, fake, VIEWER)

    response = await client.get("/clients", headers=headers)

    assert response.status_code == HTTPStatus.OK
    ids = {row["id"] for row in response.json()}
    assert ids == {overlapping_id}
    assert unrelated_id not in ids


async def test_dashboard_session_policy_reads_are_scoped_to_its_identity(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy reads narrow through the same access set, via `caller_accessible_repo_names`."""
    await upsert_policy(session, ALLOWED, RepoPolicyUpsert())
    await upsert_policy(session, FORBIDDEN, RepoPolicyUpsert())
    await session.commit()

    headers = await start_dashboard_session(client, monkeypatch, FakeGithubIdentityService(), VIEWER)

    listed = await client.get("/policies", headers=headers)
    hidden = await client.get(f"/policies/{FORBIDDEN}", headers=headers)

    assert listed.status_code == HTTPStatus.OK
    assert [row["repo_full_name"] for row in listed.json()] == [ALLOWED]
    assert hidden.status_code == HTTPStatus.NOT_FOUND


async def test_client_token_and_dashboard_session_read_the_same_feed(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the `ScopedCallerDep` unification.

    Two credentials, one access set: if the two paths ever answer differently
    for the same identity, one of them has grown a filter the other has not.
    """
    job_id = await enqueue(client, ALLOWED, "both-credentials")

    fake = FakeGithubIdentityService()
    _, client_headers = await enrol_github_client(
        client, monkeypatch, fake, Enrolee(login="machine", user_id=91, repo_access=[readable(ALLOWED)])
    )
    session_headers = await start_dashboard_session(client, monkeypatch, fake, VIEWER)

    via_client = await client.get("/jobs", headers=client_headers)
    via_session = await client.get("/jobs", headers=session_headers)

    assert via_client.status_code == via_session.status_code == HTTPStatus.OK
    assert [j["id"] for j in via_client.json()] == [job_id]
    assert [j["id"] for j in via_session.json()] == [job_id]


# ---------------------------------------------------------------------------
# Writes: a session may not do any of them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/jobs/lease", "/clients/check-in", "/clients/check-out"])
async def test_dashboard_session_cannot_act_as_a_grid_client(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """Leasing and availability signals belong to machines, not to a browser tab."""
    headers = await start_dashboard_session(client, monkeypatch, FakeGithubIdentityService(), VIEWER)

    response = await client.post(path, headers=headers)

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_dashboard_session_cannot_write_a_repo_policy(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401, not 403 — the write path never learns this credential exists.

    403 would mean `ScopedCallerDep` had been wired into `RepoAdminDep` and the
    session simply was not an admin. That is a materially different posture, and
    a much shorter step from here to a session that *can* turn the model floor
    down.
    """
    headers = await start_dashboard_session(client, monkeypatch, FakeGithubIdentityService(), VIEWER)

    response = await client.put(f"/policies/{ALLOWED}", json={"min_tier": "standard"}, headers=headers)

    assert response.status_code == HTTPStatus.UNAUTHORIZED


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


async def test_expired_dashboard_session_is_rejected(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session past `expires_at` resolves to nobody, exactly like a bogus token."""
    await enqueue(client, ALLOWED, "expired-session")
    headers = await start_dashboard_session(client, monkeypatch, FakeGithubIdentityService(), VIEWER)

    before = await client.get("/jobs", headers=headers)
    assert before.status_code == HTTPStatus.OK

    await expire_session_for(session, headers["Authorization"].removeprefix("Bearer "))

    after = await client.get("/jobs", headers=headers)

    assert after.status_code == HTTPStatus.UNAUTHORIZED
