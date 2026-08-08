"""Integration tests for GitHub-derived admission to the grid.

`POST /clients` used to be open: anyone who could reach the hub could declare
themselves a reviewer. Now the Authorization header on that one call carries
either a GitHub user access token (github mode) or the shared dev secret (dev
mode), and the hub decides admission from it.

What these tests are really guarding is the audit trail. A bypass that leaves
no per-event record is a bypass nobody can review after the fact, so every
decision — resolved, denied, dev-secret-used — is asserted as a log record,
not merely as a status code.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from review_bingo_hub.core.config import Settings, settings
from review_bingo_hub.main import app
from review_bingo_hub.models.github_identity import (
    GithubIdentity,
    IdentityRepoAccess,
    PermissionLevel,
)
from review_bingo_hub.models.review_client import ReviewClient
from review_bingo_hub.services.github_identity_service import (
    GithubIdentityError,
    GithubRepoAccess,
    GithubUnavailableError,
    get_github_identity_service,
)
from review_bingo_hub.tests.integration.conftest import (
    GITHUB_TOKEN,
    FakeGithubIdentityService,
    dump,
    enrolment_headers,
    marge,
    records_named,
    use_github_mode,
)

DEV_SECRET = "test-fixture-placeholder"

REGISTRATION_PAYLOAD = {
    "name": "marge-mac-mini",
    "model_name": "qwen2.5-coder-32b",
    "provider": "ollama",
    "tier": "standard",
}


async def test_enrolment_accepted_populates_identity_and_access_set(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGithubIdentityService(
        identity=marge(),
        repo_access=[
            GithubRepoAccess(repo_full_name="acme/payments", permission=PermissionLevel.ADMIN),
            GithubRepoAccess(repo_full_name="acme/docs", permission=PermissionLevel.READ),
        ],
    )
    use_github_mode(monkeypatch, fake)

    response = await client.post("/clients", json=REGISTRATION_PAYLOAD, headers=enrolment_headers(GITHUB_TOKEN))

    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    assert body["client"]["identity_id"] is not None

    identity = (await session.execute(select(GithubIdentity))).scalar_one()
    assert identity.github_user_id == 20482231
    assert identity.github_login == "marge-bouvier"

    access = (await session.execute(select(IdentityRepoAccess))).scalars().all()
    assert {(a.repo_full_name, a.permission) for a in access} == {
        ("acme/payments", PermissionLevel.ADMIN),
        ("acme/docs", PermissionLevel.READ),
    }
    assert all(a.identity_id == identity.id for a in access)

    review_client = (await session.execute(select(ReviewClient))).scalar_one()
    assert review_client.identity_id == identity.id

    # The GitHub credential is used and dropped; nothing persists it.
    persisted = repr([identity.__dict__, [a.__dict__ for a in access], review_client.__dict__])
    assert GITHUB_TOKEN not in persisted


async def test_enrolment_accepted_logs_identity_resolved(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeGithubIdentityService(
        identity=marge(),
        repo_access=[
            GithubRepoAccess(repo_full_name="acme/payments", permission=PermissionLevel.ADMIN),
            GithubRepoAccess(repo_full_name="acme/docs", permission=PermissionLevel.READ),
        ],
    )
    use_github_mode(monkeypatch, fake)

    with caplog.at_level(logging.DEBUG):
        response = await client.post("/clients", json=REGISTRATION_PAYLOAD, headers=enrolment_headers(GITHUB_TOKEN))

    assert response.status_code == HTTPStatus.CREATED
    resolved = records_named(caplog, "identity_resolved")
    assert len(resolved) == 1
    assert resolved[0].levelno == logging.INFO
    assert resolved[0].github_login == "marge-bouvier"  # type: ignore[attr-defined]
    assert resolved[0].github_user_id == 20482231  # type: ignore[attr-defined]
    assert resolved[0].accessible_repo_count == 2  # type: ignore[attr-defined]

    for record in caplog.records:
        assert GITHUB_TOKEN not in dump(record)
        assert GITHUB_TOKEN[:8] not in dump(record)
        assert GITHUB_TOKEN[-8:] not in dump(record)


async def test_enrolment_denied_with_invalid_github_token(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeGithubIdentityService(error=GithubIdentityError("Bad credentials"))
    use_github_mode(monkeypatch, fake)

    with caplog.at_level(logging.DEBUG):
        response = await client.post("/clients", json=REGISTRATION_PAYLOAD, headers=enrolment_headers(GITHUB_TOKEN))

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    denied = records_named(caplog, "enrolment_denied")
    assert len(denied) == 1
    assert denied[0].levelno == logging.WARNING
    assert denied[0].reason == "credential_rejected"  # type: ignore[attr-defined]
    assert GITHUB_TOKEN not in dump(denied[0])

    assert (await session.execute(select(ReviewClient))).scalars().all() == []


async def test_enrolment_rejects_expired_or_revoked_token(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub answering 401 is a rejection, not an outage — the hub says 401 too."""
    fake = FakeGithubIdentityService(error=GithubIdentityError("401 Unauthorized from GitHub"))
    use_github_mode(monkeypatch, fake)

    response = await client.post("/clients", json=REGISTRATION_PAYLOAD, headers=enrolment_headers(GITHUB_TOKEN))

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_enrolment_fails_closed_when_github_unavailable(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeGithubIdentityService(error=GithubUnavailableError("connection refused"))
    use_github_mode(monkeypatch, fake)

    with caplog.at_level(logging.DEBUG):
        response = await client.post("/clients", json=REGISTRATION_PAYLOAD, headers=enrolment_headers(GITHUB_TOKEN))

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    denied = records_named(caplog, "enrolment_denied")
    assert len(denied) == 1
    assert denied[0].levelno == logging.WARNING
    assert denied[0].reason == "github_unreachable"  # type: ignore[attr-defined]
    assert GITHUB_TOKEN not in dump(denied[0])

    # Fail closed: an unreachable GitHub must not mint a client.
    assert (await session.execute(select(ReviewClient))).scalars().all() == []


async def test_enrolment_requires_a_credential(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed Authorization header gets past the presence gate but not the endpoint."""
    fake = FakeGithubIdentityService(forbidden=True)
    use_github_mode(monkeypatch, fake)

    response = await client.post("/clients", json=REGISTRATION_PAYLOAD, headers={"Authorization": "not-a-bearer"})

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_second_client_same_github_account_shares_one_identity_and_refreshes_access(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGithubIdentityService(
        identity=marge(),
        repo_access=[GithubRepoAccess(repo_full_name="acme/payments", permission=PermissionLevel.ADMIN)],
    )
    use_github_mode(monkeypatch, fake)

    first = await client.post("/clients", json=REGISTRATION_PAYLOAD, headers=enrolment_headers(GITHUB_TOKEN))
    assert first.status_code == HTTPStatus.CREATED

    # Same GitHub account, different login casing is irrelevant; what changed is
    # the repo set — access is a snapshot, so the newer one wins.
    fake.repo_access = [
        GithubRepoAccess(repo_full_name="acme/ledger", permission=PermissionLevel.WRITE),
        GithubRepoAccess(repo_full_name="beta/tools", permission=PermissionLevel.READ),
    ]
    second = await client.post(
        "/clients",
        json={**REGISTRATION_PAYLOAD, "name": "second-box"},
        headers=enrolment_headers(GITHUB_TOKEN),
    )
    assert second.status_code == HTTPStatus.CREATED

    identities = (await session.execute(select(GithubIdentity))).scalars().all()
    assert len(identities) == 1
    assert first.json()["client"]["identity_id"] == second.json()["client"]["identity_id"]

    access = (await session.execute(select(IdentityRepoAccess))).scalars().all()
    assert {(a.repo_full_name, a.permission) for a in access} == {
        ("acme/ledger", PermissionLevel.WRITE),
        ("beta/tools", PermissionLevel.READ),
    }


async def test_dev_mode_registration_accepts_configured_secret(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGithubIdentityService(forbidden=True)
    monkeypatch.setattr(settings, "client_enrolment_mode", "dev")
    monkeypatch.setattr(settings, "client_enrolment_secret", DEV_SECRET)
    app.dependency_overrides[get_github_identity_service] = lambda: fake

    response = await client.post("/clients", json=REGISTRATION_PAYLOAD, headers=enrolment_headers(DEV_SECRET))

    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["client"]["identity_id"] is None
    assert fake.calls == 0

    review_client = (await session.execute(select(ReviewClient))).scalar_one()
    assert review_client.identity_id is None


async def test_dev_mode_registration_logs_warning_every_time(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(settings, "client_enrolment_mode", "dev")
    monkeypatch.setattr(settings, "client_enrolment_secret", DEV_SECRET)

    with caplog.at_level(logging.DEBUG):
        for name in ("box-one", "box-two"):
            response = await client.post(
                "/clients",
                json={**REGISTRATION_PAYLOAD, "name": name},
                headers=enrolment_headers(DEV_SECRET),
            )
            assert response.status_code == HTTPStatus.CREATED

    used = records_named(caplog, "dev_mode_secret_used")
    # Every enrolment, not just the first: a startup-only warning scrolls away.
    assert len(used) == 2
    assert all(r.levelno == logging.WARNING for r in used)
    assert all(DEV_SECRET not in dump(r) for r in used)


async def test_dev_mode_registration_rejects_wrong_secret(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(settings, "client_enrolment_mode", "dev")
    monkeypatch.setattr(settings, "client_enrolment_secret", DEV_SECRET)

    with caplog.at_level(logging.DEBUG):
        response = await client.post(
            "/clients",
            json=REGISTRATION_PAYLOAD,
            headers=enrolment_headers("not-the-secret"),
        )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    denied = records_named(caplog, "enrolment_denied")
    assert len(denied) == 1
    assert denied[0].reason == "credential_rejected"  # type: ignore[attr-defined]
    assert (await session.execute(select(ReviewClient))).scalars().all() == []


async def test_dev_mode_without_a_configured_secret_denies_everything(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset secret must not degrade into "any header will do"."""
    monkeypatch.setattr(settings, "client_enrolment_mode", "dev")
    monkeypatch.setattr(settings, "client_enrolment_secret", None)

    response = await client.post("/clients", json=REGISTRATION_PAYLOAD, headers=enrolment_headers("anything"))

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_github_mode_is_the_default_without_explicit_config() -> None:
    """The secure branch is the one you get by doing nothing."""
    assert Settings.model_fields["client_enrolment_mode"].default == "github"


async def test_registered_client_can_still_check_in(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrolment changes admission, not the hub-minted token that follows it."""
    fake = FakeGithubIdentityService(identity=marge(), repo_access=[])
    use_github_mode(monkeypatch, fake)

    response = await client.post("/clients", json=REGISTRATION_PAYLOAD, headers=enrolment_headers(GITHUB_TOKEN))
    assert response.status_code == HTTPStatus.CREATED
    body: dict[str, Any] = response.json()

    check_in = await client.post("/clients/check-in", headers=enrolment_headers(body["token"]))
    assert check_in.status_code == HTTPStatus.OK
