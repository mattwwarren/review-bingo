"""Shared seams for integration tests that exercise GitHub-derived identity.

`FakeGithubIdentityService` and its three companions started life inside
`test_client_enrolment.py`. They live here now because a second consumer
arrived (`test_repo_scoped_access.py`): the same fake has to stand in for
GitHub whenever a test needs a client enrolled under a *specific* identity
and access set, and copying it would let the two copies drift.

`records_named` and `dump` made the same trip for the same reason: a second
consumer arrived (`test_policy_authorization.py`), which asserts the policy
authorization audit trail the way `test_client_enrolment.py` asserts the
enrolment one.

`backdate_access_refreshed_at` arrived here already shared: check-in
re-attestation (`test_client_enrolment.py`) and the access-staleness gate
(`test_repo_scoped_access.py`) both have to age the same clock.

`access` arrived from `test_policy_authorization.py` when RFC 0002 B2 (#47)
gave `test_dashboard_session_scoped_access.py` a second need for it. It came
here rather than becoming this suite's first test-to-test import: no test
module in this directory imports from a sibling, and `readable` — which is now
one line of `access` — is the precedent for where a helper of this shape lives.

`Enrolee`/`enrol_github_client`/`enqueue` and the repo-name constants made the
same trip when B1 (#24) arrived: `test_dashboard_session_scoped_access.py`
needs the identical "enrol one client under a specific identity, then call a
round from a webhook" scaffolding `test_repo_scoped_access.py` already had, and
two copies of an access-scoping helper is two places for the boundary under
test to drift.

`enrol_dev_client` made the trip on RFC 0002 B3 (#48), on the same second-consumer
rule: `test_client_roster_attestation.py` has to prove two identity-less clients
never read as each other's own row, and that needs exactly the dev-mode enrolment
`test_repo_scoped_access.py` already had a helper for.

These are plain module-level helpers, not pytest fixtures — callers import
and call them directly. They sit in `conftest.py` only because that is the
canonical home for test-support code shared across a directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.core.config import settings
from review_bingo_hub.main import app
from review_bingo_hub.models.dashboard_session import DashboardSession
from review_bingo_hub.models.github_identity import GithubIdentity, PermissionLevel
from review_bingo_hub.services.client_service import hash_token
from review_bingo_hub.services.github_identity_service import (
    DeviceCodeGrant,
    DevicePollResult,
    DevicePollStatus,
    GithubRepoAccess,
    GithubUserIdentity,
    get_github_identity_service,
)

GITHUB_TOKEN = "gho_16C7e42F292c6912E7710c838347Ae178B4a"

# The App client id the device flow quotes to GitHub. Distinct from the App id,
# and unset by default in tests (isolate_github_app_config), so a test that
# wants the dashboard login has to say so.
DEVICE_CLIENT_ID = "Iv23liTESTCLIENTID00"

PR_WEBHOOK_HEADERS = {"X-GitHub-Event": "pull_request"}

# One repo the caller can reach and one it cannot: every access assertion in the
# suite is "in ALLOWED, not in FORBIDDEN", so naming them once keeps the tests
# about the boundary rather than about string literals.
ALLOWED = "acme/payments"
FORBIDDEN = "acme/other-repo"
UNRELATED = "acme/unrelated"


@dataclass
class FakeGithubIdentityService:
    """Stands in for GitHub. Raises if called when it should not be."""

    identity: GithubUserIdentity | None = None
    repo_access: list[GithubRepoAccess] | None = None
    error: Exception | None = None
    forbidden: bool = False
    calls: int = 0
    device_grant: DeviceCodeGrant | None = None
    # A queue rather than a single value: the device flow's whole shape is a
    # *sequence* of answers (pending, pending, authorized), and a fake that can
    # only hold one of them cannot express the case the real flow spends most
    # of its time in.
    poll_results: list[DevicePollResult] = field(default_factory=list)
    device_codes_seen: list[str] = field(default_factory=list)
    # Separate from `error` so a test can script "GitHub authorized the device
    # code, then failed on the identity read" — a real window, since those are
    # two calls seconds apart, and the one place a login can fail after the
    # person has already said yes.
    identity_error: Exception | None = None

    async def get_identity(self, token: str) -> GithubUserIdentity:
        self._record(token)
        if self.identity_error is not None:
            raise self.identity_error
        if self.error is not None:
            raise self.error
        assert self.identity is not None
        return self.identity

    async def get_repo_access(self, token: str) -> list[GithubRepoAccess]:
        self._record(token)
        if self.identity_error is not None:
            raise self.identity_error
        if self.error is not None:
            raise self.error
        return list(self.repo_access or [])

    async def request_device_code(self) -> DeviceCodeGrant:
        self._record_call()
        if self.error is not None:
            raise self.error
        assert self.device_grant is not None
        return self.device_grant

    async def poll_device_token(self, device_code: str) -> DevicePollResult:
        self._record_call()
        self.device_codes_seen.append(device_code)
        if self.error is not None:
            raise self.error
        assert self.poll_results, "no scripted poll result left for this call"
        return self.poll_results.pop(0)

    def _record(self, token: str) -> None:
        self._record_call()
        assert token == GITHUB_TOKEN

    def _record_call(self) -> None:
        if self.forbidden:
            error_msg = "github_identity_service must not be consulted in dev mode"
            raise AssertionError(error_msg)
        self.calls += 1


def device_grant(**overrides: Any) -> DeviceCodeGrant:  # noqa: ANN401 - dataclass field overrides
    """A device-code grant shaped like GitHub's, with per-test overrides.

    Mirrors `client/test_bingo_client.py`'s `device_code_grant()` field for
    field: the CLI and the hub broker the same flow, and a fake that disagreed
    between them would hide exactly the drift worth catching.
    """
    fields: dict[str, Any] = {
        "device_code": "3584d83530557fdd1f46af8289938c8ef79f9dc5",
        "user_code": "WDJB-MJHT",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 899,
        "interval": 5,
    }
    fields.update(overrides)
    return DeviceCodeGrant(**fields)


def use_github_mode(monkeypatch: pytest.MonkeyPatch, fake: FakeGithubIdentityService) -> None:
    monkeypatch.setattr(settings, "client_enrolment_mode", "github")
    app.dependency_overrides[get_github_identity_service] = lambda: fake


def enrolment_headers(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


def marge(login: str = "marge-bouvier", user_id: int = 20482231) -> GithubUserIdentity:
    return GithubUserIdentity(github_user_id=user_id, github_login=login)


def access(repo: str, permission: PermissionLevel) -> GithubRepoAccess:
    """One entry in the access snapshot GitHub reports for an account."""
    return GithubRepoAccess(repo_full_name=repo, permission=permission)


def readable(repo: str) -> GithubRepoAccess:
    """One entry in the access snapshot GitHub reports for an account.

    Read is the uninteresting permission on purpose: tests that care about the
    *level* say so explicitly, and everything else only cares that the repo is
    in the set at all.
    """
    return access(repo, PermissionLevel.READ)


async def backdate_access_refreshed_at(session: AsyncSession, identity_id: UUID, *, seconds_ago: int) -> None:
    """Age an identity's access-snapshot clock, then commit so the app can see it.

    A direct column write rather than a frozen clock: the suite has no
    freezegun (or any equivalent), and the staleness rule reads exactly this
    one column, so moving the column *is* the whole simulation. Commits because
    every caller's next step is an HTTP request served on a different session.
    """
    await session.execute(
        update(GithubIdentity)
        .where(col(GithubIdentity.id) == identity_id)
        .values(access_refreshed_at=datetime.now(UTC) - timedelta(seconds=seconds_ago))
    )
    await session.commit()


async def expire_session_for(session: AsyncSession, token: str) -> None:
    """Age one dashboard session past its expiry, then commit so the app sees it.

    A direct column write rather than a frozen clock, matching
    `backdate_access_refreshed_at`: the suite has no freezegun, and the expiry
    rule reads exactly this one column.

    Promoted here from `test_dashboard_session_scoped_access.py` when
    `test_auth_me.py` arrived as a second consumer, per this module's own
    documented promotion policy.
    """
    await session.execute(
        update(DashboardSession)
        .where(col(DashboardSession.token_hash) == hash_token(token))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()


def records_named(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == event]


def dump(record: logging.LogRecord) -> str:
    return record.getMessage() + repr(record.__dict__)


def pr_payload(repo: str, sha: str, number: int = 7) -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": repo},
        "pull_request": {"number": number, "head": {"sha": sha}, "title": "Fix rounding"},
    }


async def enqueue(client: AsyncClient, repo: str, sha: str, number: int = 7) -> str:
    """Call a review round from a PR webhook, returning the queued job's id."""
    response = await client.post("/webhooks/github", json=pr_payload(repo, sha, number), headers=PR_WEBHOOK_HEADERS)
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "queued"
    job_id: str = response.json()["job_id"]
    return job_id


@dataclass(frozen=True)
class Enrolee:
    """One GitHub account to enrol under, plus what GitHub says it can reach.

    Bundled into one object rather than spread across parameters of
    `enrol_github_client` so that helper stays inside the repo's argument-count
    limit; the fields are exactly the knobs the tests need to turn.
    """

    login: str
    user_id: int
    repo_access: list[GithubRepoAccess] = field(default_factory=list)
    tier: str = "standard"
    name: str | None = None


async def enrol_github_client(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeGithubIdentityService,
    enrolee: Enrolee,
) -> tuple[str, dict[str, str]]:
    """Register + check in one github-mode client under a specific identity + access set.

    Calls `use_github_mode` (idempotent if a test already called it) then points
    `fake` at this identity/access set immediately before the POST it makes.
    Safe to call repeatedly with distinct enrolees in the same test: registration
    is awaited one request at a time, and once a call returns, that client's row
    and bearer token are independent durable facts — the fake only has to
    represent one identity at the instant of registration, not all of them at
    once. Returns (client_id, bearer headers).
    """
    use_github_mode(monkeypatch, fake)
    fake.identity = marge(login=enrolee.login, user_id=enrolee.user_id)
    fake.repo_access = list(enrolee.repo_access)

    response = await client.post(
        "/clients",
        json={
            "name": enrolee.name or enrolee.login,
            "model_name": "test-model",
            "provider": "test",
            "tier": enrolee.tier,
        },
        headers=enrolment_headers(GITHUB_TOKEN),
    )
    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    headers = enrolment_headers(body["token"])

    check_in = await client.post("/clients/check-in", headers=headers)
    assert check_in.status_code == HTTPStatus.OK
    client_id: str = body["client"]["id"]
    return client_id, headers


async def enrol_dev_client(client: AsyncClient, name: str) -> tuple[str, dict[str, str]]:
    """Register + check in a dev-mode client, riding the fixture's placeholder secret."""
    response = await client.post(
        "/clients",
        json={"name": name, "model_name": "test-model", "provider": "test", "tier": "standard"},
    )
    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    headers = enrolment_headers(body["token"])

    check_in = await client.post("/clients/check-in", headers=headers)
    assert check_in.status_code == HTTPStatus.OK
    client_id: str = body["client"]["id"]
    return client_id, headers


async def start_dashboard_session(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeGithubIdentityService,
    enrolee: Enrolee,
) -> dict[str, str]:
    """Run the hub-side device flow to completion, returning the session's bearer headers.

    The dashboard's counterpart to `enrol_github_client`: same identity/access
    set knobs, but the caller that comes out the other side is a person's
    browser session rather than a registered machine.
    """
    use_github_mode(monkeypatch, fake)
    monkeypatch.setattr(settings, "github_app_client_id", DEVICE_CLIENT_ID)
    fake.identity = marge(login=enrolee.login, user_id=enrolee.user_id)
    fake.repo_access = list(enrolee.repo_access)
    fake.device_grant = device_grant()
    fake.poll_results = [DevicePollResult(status=DevicePollStatus.AUTHORIZED, access_token=GITHUB_TOKEN)]

    start = await client.post("/auth/device/start")
    assert start.status_code == HTTPStatus.OK
    poll = await client.post("/auth/device/poll", json={"device_code": start.json()["device_code"]})
    assert poll.status_code == HTTPStatus.OK
    return enrolment_headers(poll.json()["session_token"])
