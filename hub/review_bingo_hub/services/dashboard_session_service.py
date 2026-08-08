"""Mint and resolve the bearer token a signed-in dashboard reads with.

Deliberately thin, and deliberately a sibling of `client_service` rather than a
part of it: that module owns the token a *machine* gets at registration, this
one owns the token a *person's browser* gets at login. The hashing is shared
(`client_service.hash_token`) because the threat model is identical; the
lifecycles are not, and one module minting both would be one edit away from
letting a login lease work.

The plaintext token exists exactly once — in `create_session`'s return value,
on its way into the response body. It is never written to a row, never logged,
and cannot be recovered from what is stored.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.core.config import settings
from review_bingo_hub.models.dashboard_session import DashboardSession
from review_bingo_hub.services.client_service import hash_token


async def create_session(session: AsyncSession, identity_id: UUID) -> tuple[DashboardSession, str]:
    """Mint a session for one identity, returning the row and the plaintext token.

    Only `hash_token(token)` reaches the database, mirroring
    `review_client.token_hash` exactly. Do not add a plaintext or reversibly
    encrypted column "for convenience": the whole value of hashing here is that
    a copy of this table is not a set of working logins.
    """
    token = secrets.token_urlsafe(32)
    row = DashboardSession(
        identity_id=identity_id,
        token_hash=hash_token(token),
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.dashboard_session_ttl_seconds),
    )
    session.add(row)
    await session.flush()  # type: ignore[attr-defined]
    await session.refresh(row)
    return row, token


async def get_identity_id_for_token(session: AsyncSession, token: str) -> UUID | None:
    """The identity behind a live session token, or None.

    An expired row answers None rather than raising: to every caller, a session
    past its expiry is indistinguishable from a token that was never issued, and
    that is the honest answer — it authenticates nothing either way.
    """
    result = await session.execute(
        select(DashboardSession.identity_id).where(
            col(DashboardSession.token_hash) == hash_token(token),
            col(DashboardSession.expires_at) > datetime.now(UTC),
        )
    )
    return result.scalar_one_or_none()
