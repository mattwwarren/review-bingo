"""Unit tests for `identity_id_access_is_stale`.

The sibling `identity_access_is_stale` is pinned against a real database in
`test_repo_scoped_access.py`, and these mirror those cases one for one — same
carve-outs, same boundary, same "missing row reads as stale" direction. They
live here rather than beside them because this helper is keyed on an
`identity_id` alone, which is precisely the caller that has no `ReviewClient`
row to build a fixture from: a dashboard session.

The session is mocked rather than taken from the fixture: `tests/unit/conftest.py`
raises `NotImplementedError` from its `session` fixture by design, and the two
carve-out cases below are *about* never reaching the database at all — which a
mock can assert and a real session cannot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from review_bingo_hub.core.config import settings
from review_bingo_hub.services.identity_service import identity_id_access_is_stale

TTL_SECONDS = 3600


def session_returning(refreshed_at: datetime | None) -> AsyncSession:
    """A session whose one query answers with this `access_refreshed_at`."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = refreshed_at
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=result)
    return cast(AsyncSession, session)


def forbidding_session() -> AsyncSession:
    """A session that fails the test if it is queried at all.

    The carve-outs below are not "returns False" facts, they are "never asks"
    facts — a helper that read the clock and then discarded the answer would
    satisfy a return-value assertion while still spending a query per wake on
    every stream in dev mode. Mirrors `FakeGithubIdentityService.forbidden`,
    which pins the same shape of claim for GitHub calls.
    """
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=AssertionError("the database must not be consulted"))
    return cast(AsyncSession, session)


def github_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "client_enrolment_mode", "github")
    monkeypatch.setattr(settings, "identity_access_ttl_seconds", TTL_SECONDS)


async def test_stale_past_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    github_mode(monkeypatch)
    session = session_returning(datetime.now(UTC) - timedelta(seconds=TTL_SECONDS * 2))

    assert await identity_id_access_is_stale(session, uuid4()) is True


async def test_fresh_within_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive control: a helper that always said True would pass the rest."""
    github_mode(monkeypatch)
    session = session_returning(datetime.now(UTC) - timedelta(seconds=TTL_SECONDS // 2))

    assert await identity_id_access_is_stale(session, uuid4()) is False


async def test_missing_identity_row_reads_as_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """An identity that resolves to nothing is refused, not admitted.

    Same direction `identity_access_is_stale` already guesses in: "the
    authorization snapshot cannot be found" must not resolve to "not expired".
    """
    github_mode(monkeypatch)
    session = session_returning(None)

    assert await identity_id_access_is_stale(session, uuid4()) is True


async def test_dev_mode_never_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dev mode has no GitHub snapshot to age, so the clock is not even consulted."""
    monkeypatch.setattr(settings, "client_enrolment_mode", "dev")

    assert await identity_id_access_is_stale(forbidding_session(), uuid4()) is False


async def test_identity_less_caller_never_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """No account means no snapshot to age — and an empty access set already.

    Calling it stale would answer "reconnect" to a caller whose real problem is
    that it can see nothing to be sent, reconnection included.
    """
    github_mode(monkeypatch)

    assert await identity_id_access_is_stale(forbidding_session(), None) is False
