"""Integration tests for GitHub-derived admission to the grid.

`POST /clients` used to be open: anyone who could reach the hub could declare
themselves a reviewer. Now the Authorization header on that one call carries
either a GitHub user access token (github mode) or the shared dev secret (dev
mode), and the hub decides admission from it.

What these tests are really guarding is the audit trail. A bypass that leaves
no per-event record is a bypass nobody can review after the fact, so every
decision — resolved, denied, dev-secret-used — is asserted as a log record,
not merely as a status code.

The second half of the file covers check-in re-attestation, which is the same
admission machinery spent a second time: `POST /clients/check-in` may carry a
fresh GitHub token, and when it does the hub re-reads the account's repo access
and replaces the snapshot. Three properties there are worth more than the rest:
a check-in with no token must remain a *pure* heartbeat (no GitHub call, no
write); a token belonging to a different account must be refused rather than
quietly relinking the client; and a GitHub outage must not turn an availability
signal into a lockout.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
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
from review_bingo_hub.models.review_client import ClientStatus, ReviewClient, ReviewClientCreate
from review_bingo_hub.services.client_service import register_client
from review_bingo_hub.services.github_identity_service import (
    GithubIdentityError,
    GithubRepoAccess,
    GithubUnavailableError,
    get_github_identity_service,
)
from review_bingo_hub.tests.integration.conftest import (
    GITHUB_TOKEN,
    FakeGithubIdentityService,
    backdate_access_refreshed_at,
    dump,
    enrolment_headers,
    marge,
    readable,
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

PAYMENTS = "acme/payments"
LEDGER = "acme/ledger"

# Comfortably past the 8h default TTL, so the staleness-adjacent assertions here
# don't depend on a monkeypatched TTL. The boundary itself is pinned in
# test_repo_scoped_access.py, which is where the gate lives.
LONG_AGO_SECONDS = 24 * 60 * 60

# How much clock skew a "this was just refreshed" assertion tolerates.
JUST_NOW = timedelta(minutes=5)


async def sole_identity(session: AsyncSession) -> GithubIdentity:
    """The one GithubIdentity row, re-read past this session's identity map.

    The test session maker sets `expire_on_commit=False`, so an object loaded
    before an HTTP request keeps its stale column values afterwards — the app
    commits on a *different* session, which this one never hears about.
    `expire_all()` forces the reload that makes before/after comparisons real.
    """
    session.expire_all()
    return (await session.execute(select(GithubIdentity))).scalar_one()


async def access_snapshot(session: AsyncSession) -> set[tuple[str, PermissionLevel]]:
    """The repo access set as (repo, permission) pairs, ignoring row identity."""
    session.expire_all()
    rows = (await session.execute(select(IdentityRepoAccess))).scalars().all()
    return {(row.repo_full_name, row.permission) for row in rows}


async def enrol(client: AsyncClient, fake: FakeGithubIdentityService, repos: list[str]) -> dict[str, str]:
    """Register one github-mode client under `marge()` with the given access set.

    Returns its bearer headers. The check-in tests below all start from a
    successfully enrolled client, so the enrolment half is not re-asserted here
    beyond the 201 — the tests above own that.
    """
    fake.identity = marge()
    fake.repo_access = [readable(repo) for repo in repos]
    response = await client.post("/clients", json=REGISTRATION_PAYLOAD, headers=enrolment_headers(GITHUB_TOKEN))
    assert response.status_code == HTTPStatus.CREATED
    body: dict[str, Any] = response.json()
    return enrolment_headers(body["token"])


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


# ---------------------------------------------------------------------------
# Check-in re-attestation — spending a fresh GitHub token a second time
# ---------------------------------------------------------------------------


async def test_check_in_with_fresh_token_adds_a_newly_granted_repo(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo the account gained since enrolment shows up after the next check-in."""
    fake = FakeGithubIdentityService()
    use_github_mode(monkeypatch, fake)
    headers = await enrol(client, fake, [PAYMENTS])
    assert await access_snapshot(session) == {(PAYMENTS, PermissionLevel.READ)}

    fake.repo_access = [readable(PAYMENTS), readable(LEDGER)]
    response = await client.post("/clients/check-in", json={"github_token": GITHUB_TOKEN}, headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert await access_snapshot(session) == {
        (PAYMENTS, PermissionLevel.READ),
        (LEDGER, PermissionLevel.READ),
    }


async def test_check_in_with_fresh_token_removes_a_revoked_repo(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one that matters: revoked access has to *disappear*, not linger.

    A snapshot that only ever grows would keep dispatching a repo GitHub has
    already taken away — the failure nobody would notice, because nothing about
    it looks like an error.
    """
    fake = FakeGithubIdentityService()
    use_github_mode(monkeypatch, fake)
    headers = await enrol(client, fake, [PAYMENTS, LEDGER])

    fake.repo_access = [readable(PAYMENTS)]
    response = await client.post("/clients/check-in", json={"github_token": GITHUB_TOKEN}, headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert await access_snapshot(session) == {(PAYMENTS, PermissionLevel.READ)}


async def test_check_in_with_fresh_token_bumps_access_refreshed_at(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-attestation restarts the staleness clock, which is what unblocks leasing."""
    fake = FakeGithubIdentityService()
    use_github_mode(monkeypatch, fake)
    headers = await enrol(client, fake, [PAYMENTS])
    identity = await sole_identity(session)
    await backdate_access_refreshed_at(session, identity.id, seconds_ago=LONG_AGO_SECONDS)
    assert (await sole_identity(session)).access_refreshed_at < datetime.now(UTC) - JUST_NOW

    response = await client.post("/clients/check-in", json={"github_token": GITHUB_TOKEN}, headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert (await sole_identity(session)).access_refreshed_at > datetime.now(UTC) - JUST_NOW


async def test_check_in_without_token_leaves_access_refreshed_at_untouched(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare heartbeat stays a bare heartbeat — no GitHub call, no write.

    The companion to the test above, and the reason re-attestation is opt-in: if
    a token-less check-in refreshed the clock it would extend the TTL without
    ever re-reading GitHub, which is exactly the stale authorization the TTL
    exists to prevent.
    """
    fake = FakeGithubIdentityService()
    use_github_mode(monkeypatch, fake)
    headers = await enrol(client, fake, [PAYMENTS])
    identity = await sole_identity(session)
    await backdate_access_refreshed_at(session, identity.id, seconds_ago=LONG_AGO_SECONDS)
    before = (await sole_identity(session)).access_refreshed_at
    calls_before = fake.calls

    response = await client.post("/clients/check-in", headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert (await sole_identity(session)).access_refreshed_at == before
    assert fake.calls == calls_before


async def test_check_in_with_invalid_token_is_rejected_and_status_unchanged(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected re-attestation must not quietly succeed as a heartbeat.

    An operator who explicitly attached a token wants to know the token was
    refused. Flipping to CHECKED_IN anyway would leave them believing they are
    plugged in on a refreshed access set that was never refreshed.
    """
    fake = FakeGithubIdentityService()
    use_github_mode(monkeypatch, fake)
    headers = await enrol(client, fake, [PAYMENTS])

    fake.error = GithubIdentityError("Bad credentials")
    response = await client.post("/clients/check-in", json={"github_token": GITHUB_TOKEN}, headers=headers)

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    session.expire_all()
    review_client = (await session.execute(select(ReviewClient))).scalar_one()
    assert review_client.status == ClientStatus.CHECKED_OUT


async def test_check_in_with_token_for_a_different_account_is_rejected(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A token for another GitHub account is refused, not silently relinked.

    Re-attestation is "prove you are still who you enrolled as", so a valid
    token for a *different* account is not a refresh at all — accepting it would
    turn check-in into an undocumented account-transfer endpoint, and would hand
    the client whatever repos that other account can reach.
    """
    fake = FakeGithubIdentityService()
    use_github_mode(monkeypatch, fake)
    headers = await enrol(client, fake, [PAYMENTS])
    identity_before = await sole_identity(session)

    fake.identity = marge(login="homer-simpson", user_id=99991111)
    fake.repo_access = [readable(LEDGER)]
    with caplog.at_level(logging.DEBUG):
        response = await client.post("/clients/check-in", json={"github_token": GITHUB_TOKEN}, headers=headers)

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    denied = records_named(caplog, "enrolment_denied")
    assert [record.reason for record in denied] == ["identity_mismatch"]  # type: ignore[attr-defined]

    # No second identity minted, no relink, and the other account's repo never
    # reaches the snapshot.
    identities = (await session.execute(select(GithubIdentity))).scalars().all()
    assert [row.id for row in identities] == [identity_before.id]
    assert await access_snapshot(session) == {(PAYMENTS, PermissionLevel.READ)}
    session.expire_all()
    review_client = (await session.execute(select(ReviewClient))).scalar_one()
    assert review_client.identity_id == identity_before.id


async def test_check_in_survives_a_github_outage(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable GitHub keeps the existing snapshot; the TTL still decides.

    The deliberate asymmetry with enrolment: refusing a *first* enrolment over
    an outage costs nobody anything, but refusing every check-in would take the
    whole grid offline for the length of a GitHub incident. So the outage is
    swallowed and the snapshot is left exactly as it was — including its clock,
    so a revocation the hub never got to see still expires on schedule.
    """
    fake = FakeGithubIdentityService()
    use_github_mode(monkeypatch, fake)
    headers = await enrol(client, fake, [PAYMENTS])
    refreshed_before = (await sole_identity(session)).access_refreshed_at
    snapshot_before = await access_snapshot(session)

    fake.error = GithubUnavailableError("connection refused")
    response = await client.post("/clients/check-in", json={"github_token": GITHUB_TOKEN}, headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == ClientStatus.CHECKED_IN
    assert (await sole_identity(session)).access_refreshed_at == refreshed_before
    assert await access_snapshot(session) == snapshot_before


async def test_check_in_reattestation_logs_identity_reattested(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every re-attestation leaves a record naming the account, same as enrolment."""
    fake = FakeGithubIdentityService()
    use_github_mode(monkeypatch, fake)
    headers = await enrol(client, fake, [PAYMENTS])

    fake.repo_access = [readable(PAYMENTS), readable(LEDGER)]
    with caplog.at_level(logging.DEBUG):
        response = await client.post("/clients/check-in", json={"github_token": GITHUB_TOKEN}, headers=headers)

    assert response.status_code == HTTPStatus.OK
    reattested = records_named(caplog, "identity_reattested")
    assert len(reattested) == 1
    assert reattested[0].levelno == logging.INFO
    assert reattested[0].github_login == "marge-bouvier"  # type: ignore[attr-defined]
    assert reattested[0].github_user_id == 20482231  # type: ignore[attr-defined]
    assert reattested[0].accessible_repo_count == 2  # type: ignore[attr-defined]


async def test_check_in_reattestation_never_persists_or_logs_the_fresh_token(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The re-attestation token is spent and dropped, exactly like the enrolment one.

    Asserted separately from enrolment's version because this is a second code
    path carrying the same secret, and a redaction that only holds on one path
    is not a redaction.
    """
    fake = FakeGithubIdentityService()
    use_github_mode(monkeypatch, fake)
    headers = await enrol(client, fake, [PAYMENTS])

    with caplog.at_level(logging.DEBUG):
        response = await client.post("/clients/check-in", json={"github_token": GITHUB_TOKEN}, headers=headers)

    assert response.status_code == HTTPStatus.OK
    for record in caplog.records:
        assert GITHUB_TOKEN not in dump(record)
        assert GITHUB_TOKEN[:8] not in dump(record)
        assert GITHUB_TOKEN[-8:] not in dump(record)

    session.expire_all()
    identity = await sole_identity(session)
    access = (await session.execute(select(IdentityRepoAccess))).scalars().all()
    review_client = (await session.execute(select(ReviewClient))).scalar_one()
    persisted = repr([identity.__dict__, [row.__dict__ for row in access], review_client.__dict__])
    assert GITHUB_TOKEN not in persisted


async def test_dev_mode_check_in_ignores_a_github_token_field(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev mode never consults GitHub, even when handed something that looks like a token.

    `forbidden=True` makes this a hard guarantee rather than a soft one: the
    fake raises AssertionError if it is called at all, so a re-attestation path
    that forgot the mode carve-out fails here instead of quietly reaching out.
    """
    fake = FakeGithubIdentityService(forbidden=True)
    monkeypatch.setattr(settings, "client_enrolment_mode", "dev")
    monkeypatch.setattr(settings, "client_enrolment_secret", DEV_SECRET)
    app.dependency_overrides[get_github_identity_service] = lambda: fake

    registered = await client.post("/clients", json=REGISTRATION_PAYLOAD, headers=enrolment_headers(DEV_SECRET))
    assert registered.status_code == HTTPStatus.CREATED
    headers = enrolment_headers(registered.json()["token"])

    response = await client.post("/clients/check-in", json={"github_token": GITHUB_TOKEN}, headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == ClientStatus.CHECKED_IN
    assert fake.calls == 0


async def test_check_in_links_identity_for_a_github_mode_client_with_no_identity_yet(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A github-mode client with no identity gets linked rather than refused.

    The stale pre-A1 row: registered before admission derived from GitHub, so it
    has a valid hub token and no account behind it — and therefore an empty
    access set, meaning it can lease nothing. Refusing its first re-attestation
    as an "account mismatch" would leave it permanently unable to self-heal, so
    the mismatch rule deliberately only applies once an account is already
    linked.
    """
    grid_client, token = await register_client(
        session,
        ReviewClientCreate(name="stale-pre-a1-row", model_name="test-model", provider="test"),
        identity_id=None,
    )
    await session.commit()
    assert grid_client.identity_id is None

    fake = FakeGithubIdentityService(identity=marge(), repo_access=[readable(PAYMENTS)])
    use_github_mode(monkeypatch, fake)

    response = await client.post(
        "/clients/check-in",
        json={"github_token": GITHUB_TOKEN},
        headers=enrolment_headers(token),
    )

    assert response.status_code == HTTPStatus.OK
    identity = await sole_identity(session)
    assert identity.github_user_id == 20482231
    assert response.json()["identity_id"] == str(identity.id)
    assert await access_snapshot(session) == {(PAYMENTS, PermissionLevel.READ)}
