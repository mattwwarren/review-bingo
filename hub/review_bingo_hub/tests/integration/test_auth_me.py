"""Integration tests for `GET /auth/me` — who the caller is, and what it can reach.

`/jobs` and `/clients` already answer "what can this caller see"; this endpoint
answers a narrower question a signed-in dashboard (or a grid client curious about
its own admission) needs directly: which GitHub account a credential resolves to,
how stale that account's cached repo-access snapshot is, and the repos GitHub
reported for it at last refresh.

Three properties matter more than the rest and are asserted directly:

* **Either credential kind answers identically for the same identity.** A grid
  client's own token and a dashboard session both resolve through
  `ScopedCallerDep`, and `/auth/me` must not grow a filter one path has and the
  other does not — `test_client_token_and_dashboard_session_read_the_same_feed`'s
  reasoning, applied here.
* **Only the caller's own account, never another's.** The endpoint takes no repo
  or identity parameter; leaking a second identity's rows would be a caller
  reading someone else's admission data through a route that never asked which
  account it wanted.
* **Staleness is reported, never repaired.** `/auth/me` must not itself bump
  `access_refreshed_at` — that would let polling the endpoint quietly extend the
  staleness clock past a revocation the hub never re-read from GitHub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.core.config import settings
from review_bingo_hub.models.github_identity import PermissionLevel
from review_bingo_hub.models.review_client import ReviewClient
from review_bingo_hub.services.github_identity_service import GithubRepoAccess
from review_bingo_hub.tests.integration.conftest import (
    ALLOWED,
    Enrolee,
    FakeGithubIdentityService,
    backdate_access_refreshed_at,
    enrol_github_client,
    enrolment_headers,
    expire_session_for,
    readable,
    start_dashboard_session,
    use_github_mode,
)


def github_mode_without_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch to github mode for a test that must never reach GitHub.

    Mirrors `test_policy_authorization.py`'s helper of the same name — a test
    exercising a caller-resolution failure has no reason to ever construct a
    real identity, so a fake that raises if consulted at all is the guard.
    """
    use_github_mode(monkeypatch, FakeGithubIdentityService(forbidden=True))


async def identity_id_of(session: AsyncSession, client_id: str) -> UUID:
    """The GitHub identity a registered client is linked to.

    `expire_all()` first: `expire_on_commit=False` means a row this session
    read before the enrolment request would still carry its pre-request
    values. Mirrors `test_repo_scoped_access.py`'s helper of the same name.
    """
    session.expire_all()
    result = await session.execute(select(ReviewClient).where(col(ReviewClient.id) == UUID(client_id)))
    grid_client = result.scalar_one()
    assert grid_client.identity_id is not None
    return grid_client.identity_id


def admin_on(repo: str) -> GithubRepoAccess:
    """One entry in the access snapshot at admin level, the counterpart to `readable`'s read-only entry."""
    return GithubRepoAccess(repo_full_name=repo, permission=PermissionLevel.ADMIN)


# ---------------------------------------------------------------------------
# Both credential kinds answer, identically, for the caller's own identity
# ---------------------------------------------------------------------------


async def test_me_returns_github_login_and_repo_access_for_grid_client(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed permission levels across two repos come back exactly as enrolled."""
    other_repo = "acme/other-repo"
    enrolee = Enrolee(
        login="marge-bouvier",
        user_id=20482231,
        repo_access=[readable(ALLOWED), admin_on(other_repo)],
    )
    _, headers = await enrol_github_client(client, monkeypatch, FakeGithubIdentityService(), enrolee)

    response = await client.get("/auth/me", headers=headers)

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["github_login"] == "marge-bouvier"
    assert {(r["repo_full_name"], r["permission"]) for r in body["repos"]} == {
        (a.repo_full_name, a.permission.value) for a in enrolee.repo_access
    }
    refreshed_at = datetime.fromisoformat(body["access_refreshed_at"])
    assert (datetime.now(UTC) - refreshed_at).total_seconds() < 60


async def test_me_returns_github_login_and_repo_access_for_dashboard_session(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same identity, same answer, through the other credential kind.

    Mirrors `test_client_token_and_dashboard_session_read_the_same_feed`: if
    the two paths ever disagreed here, one of them would have grown a filter
    the other lacks.
    """
    enrolee = Enrolee(login="marge-viewer", user_id=91, repo_access=[readable(ALLOWED)])
    headers = await start_dashboard_session(client, monkeypatch, FakeGithubIdentityService(), enrolee)

    response = await client.get("/auth/me", headers=headers)

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["github_login"] == "marge-viewer"
    assert {(r["repo_full_name"], r["permission"]) for r in body["repos"]} == {
        (a.repo_full_name, a.permission.value) for a in enrolee.repo_access
    }
    refreshed_at = datetime.fromisoformat(body["access_refreshed_at"])
    assert (datetime.now(UTC) - refreshed_at).total_seconds() < 60


# ---------------------------------------------------------------------------
# Never another identity's rows
# ---------------------------------------------------------------------------


async def test_me_never_leaks_another_identity(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGithubIdentityService()
    alpha = "acme/alpha"
    bravo = "acme/bravo"
    a_enrolee = Enrolee(login="alice", user_id=201, repo_access=[admin_on(alpha)])
    b_enrolee = Enrolee(login="bob", user_id=202, repo_access=[admin_on(bravo)])

    _, a_headers = await enrol_github_client(client, monkeypatch, fake, a_enrolee)
    await enrol_github_client(client, monkeypatch, fake, b_enrolee)

    response = await client.get("/auth/me", headers=a_headers)

    assert response.status_code == HTTPStatus.OK
    repo_names = {r["repo_full_name"] for r in response.json()["repos"]}
    assert repo_names == {alpha}
    assert bravo not in repo_names


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


async def test_me_rejects_missing_credential(client: AsyncClient) -> None:
    """No Authorization header: `RequireTokenMiddleware`'s deny-by-default gate
    covers it, not a custom check in the route.

    An empty header value reproduces "absent" for the gate's own presence
    check (`if not request.headers.get("authorization")`) without needing a
    second client fixture that skips the suite's default header injection.
    """
    response = await client.get("/auth/me", headers={"Authorization": ""})

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_me_rejects_unknown_token(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bearer token that resolves to neither a client nor a session is a 401."""
    github_mode_without_github(monkeypatch)

    response = await client.get("/auth/me", headers=enrolment_headers("never-minted-this"))

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_me_rejects_expired_dashboard_session(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrolee = Enrolee(login="marge-viewer", user_id=91, repo_access=[readable(ALLOWED)])
    headers = await start_dashboard_session(client, monkeypatch, FakeGithubIdentityService(), enrolee)

    before = await client.get("/auth/me", headers=headers)
    assert before.status_code == HTTPStatus.OK

    await expire_session_for(session, headers["Authorization"].removeprefix("Bearer "))

    after = await client.get("/auth/me", headers=headers)
    assert after.status_code == HTTPStatus.UNAUTHORIZED


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


async def test_me_reports_fresh_identity_as_not_stale(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrolee = Enrolee(login="fresh-fred", user_id=301, repo_access=[readable(ALLOWED)])
    _, headers = await enrol_github_client(client, monkeypatch, FakeGithubIdentityService(), enrolee)

    response = await client.get("/auth/me", headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["access_is_stale"] is False


async def test_me_reports_stale_identity_when_ttl_exceeded(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot past TTL reports as stale, and `/auth/me` must not itself refresh it."""
    enrolee = Enrolee(login="stale-sally", user_id=302, repo_access=[readable(ALLOWED)])
    client_id, headers = await enrol_github_client(client, monkeypatch, FakeGithubIdentityService(), enrolee)
    identity_id = await identity_id_of(session, client_id)

    backdated_seconds_ago = settings.identity_access_ttl_seconds + 1
    await backdate_access_refreshed_at(session, identity_id, seconds_ago=backdated_seconds_ago)

    response = await client.get("/auth/me", headers=headers)

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["access_is_stale"] is True
    refreshed_at = datetime.fromisoformat(body["access_refreshed_at"])
    age_seconds = (datetime.now(UTC) - refreshed_at).total_seconds()
    assert age_seconds >= backdated_seconds_ago - 5, "access_refreshed_at must still reflect the backdated value"


# ---------------------------------------------------------------------------
# Empty access, and the dev-mode carve-out
# ---------------------------------------------------------------------------


async def test_me_empty_repo_access_returns_empty_list_not_error(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enrolee = Enrolee(login="nowhere-nick", user_id=303, repo_access=[])
    _, headers = await enrol_github_client(client, monkeypatch, FakeGithubIdentityService(), enrolee)

    response = await client.get("/auth/me", headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["repos"] == []


async def test_me_dev_mode_client_with_no_identity_returns_404(client: AsyncClient) -> None:
    """A dev-mode client has no GitHub account behind it — nothing to report."""
    response = await client.post(
        "/clients",
        json={"name": "dev-box", "model_name": "test-model", "provider": "test", "tier": "standard"},
    )
    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    assert body["client"]["identity_id"] is None
    headers = enrolment_headers(body["token"])

    response = await client.get("/auth/me", headers=headers)

    assert response.status_code == HTTPStatus.NOT_FOUND
