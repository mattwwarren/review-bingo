"""Grid client registry: registration, token auth, check-in/check-out."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col

from review_bingo_hub.models.github_identity import IdentityRepoAccess
from review_bingo_hub.models.review_client import (
    ClientStatus,
    ReviewClient,
    ReviewClientCreate,
)
from review_bingo_hub.services.identity_service import accessible_repo_names


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def register_client(
    session: AsyncSession,
    payload: ReviewClientCreate,
    identity_id: UUID | None = None,
) -> tuple[ReviewClient, str]:
    """Create a client and mint its bearer token.

    Returns the client and the plaintext token — the only time it exists
    outside the caller's hands. Only the SHA-256 digest is stored.

    `identity_id` is the GitHub account admission was derived from, already
    resolved by identity_service; None under dev-mode enrolment.
    """
    token = secrets.token_urlsafe(32)
    client = ReviewClient(**payload.model_dump(), token_hash=hash_token(token), identity_id=identity_id)
    session.add(client)
    await session.flush()  # type: ignore[attr-defined]
    await session.refresh(client)
    return client, token


async def get_client_by_token(session: AsyncSession, token: str) -> ReviewClient | None:
    result = await session.execute(select(ReviewClient).where(col(ReviewClient.token_hash) == hash_token(token)))
    return result.scalar_one_or_none()


async def set_client_status(session: AsyncSession, client: ReviewClient, status: ClientStatus) -> ReviewClient:
    client.status = status
    client.last_seen_at = datetime.now(UTC)
    session.add(client)
    await session.flush()  # type: ignore[attr-defined]
    await session.refresh(client)
    return client


async def touch_client(session: AsyncSession, client: ReviewClient) -> None:
    """Record activity without changing status (lease, report)."""
    client.last_seen_at = datetime.now(UTC)
    session.add(client)
    await session.flush()  # type: ignore[attr-defined]


async def list_clients(
    session: AsyncSession,
    identity_id: UUID | None,
    own_client_id: UUID | None,
    offset: int = 0,
    limit: int = 100,
) -> list[ReviewClient]:
    """The roster, scoped to machines this caller could plausibly share work with.

    "Who else is plugged in" is not public: a full roster would tell anyone who
    can enrol how many machines every other org runs, and under what identities.
    So the roster is the overlap — clients enrolled under an identity that shares
    at least one repo with the caller's access set — plus the caller's own row,
    which is always visible even when it overlaps with nobody.

    `own_client_id` is None when the caller is a signed-in dashboard rather than
    a machine: a person has no row on the grid, so there is no "own row" clause
    to add. Passed explicitly rather than derived, so that absence is a value
    this function was handed and not one it has to guess at.

    Unscoped under dev-mode enrolment, where there are no identities to overlap.
    """
    query = select(ReviewClient).order_by(col(ReviewClient.created_at)).offset(offset).limit(limit)

    access = await accessible_repo_names(session, identity_id)
    if access is not None:
        overlapping_identities = (
            select(IdentityRepoAccess.identity_id).where(col(IdentityRepoAccess.repo_full_name).in_(access)).distinct()
        )
        visible: list[ColumnElement[bool]] = [col(ReviewClient.identity_id).in_(overlapping_identities)]
        if own_client_id is not None:
            visible.append(col(ReviewClient.id) == own_client_id)
        query = query.where(or_(*visible))

    result = await session.execute(query)
    return list(result.scalars().all())
