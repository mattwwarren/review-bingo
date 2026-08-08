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

These are plain module-level helpers, not pytest fixtures — callers import
and call them directly. They sit in `conftest.py` only because that is the
canonical home for test-support code shared across a directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.core.config import settings
from review_bingo_hub.main import app
from review_bingo_hub.models.github_identity import GithubIdentity, PermissionLevel
from review_bingo_hub.services.github_identity_service import (
    GithubRepoAccess,
    GithubUserIdentity,
    get_github_identity_service,
)

GITHUB_TOKEN = "gho_16C7e42F292c6912E7710c838347Ae178B4a"


@dataclass
class FakeGithubIdentityService:
    """Stands in for GitHub. Raises if called when it should not be."""

    identity: GithubUserIdentity | None = None
    repo_access: list[GithubRepoAccess] | None = None
    error: Exception | None = None
    forbidden: bool = False
    calls: int = 0

    async def get_identity(self, token: str) -> GithubUserIdentity:
        self._record(token)
        if self.error is not None:
            raise self.error
        assert self.identity is not None
        return self.identity

    async def get_repo_access(self, token: str) -> list[GithubRepoAccess]:
        self._record(token)
        if self.error is not None:
            raise self.error
        return list(self.repo_access or [])

    def _record(self, token: str) -> None:
        if self.forbidden:
            error_msg = "github_identity_service must not be consulted in dev mode"
            raise AssertionError(error_msg)
        assert token == GITHUB_TOKEN
        self.calls += 1


def use_github_mode(monkeypatch: pytest.MonkeyPatch, fake: FakeGithubIdentityService) -> None:
    monkeypatch.setattr(settings, "client_enrolment_mode", "github")
    app.dependency_overrides[get_github_identity_service] = lambda: fake


def enrolment_headers(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


def marge(login: str = "marge-bouvier", user_id: int = 20482231) -> GithubUserIdentity:
    return GithubUserIdentity(github_user_id=user_id, github_login=login)


def readable(repo: str) -> GithubRepoAccess:
    """One entry in the access snapshot GitHub reports for an account.

    Read is the uninteresting permission on purpose: tests that care about the
    *level* say so explicitly, and everything else only cares that the repo is
    in the set at all.
    """
    return GithubRepoAccess(repo_full_name=repo, permission=PermissionLevel.READ)


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


def records_named(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == event]


def dump(record: logging.LogRecord) -> str:
    return record.getMessage() + repr(record.__dict__)
