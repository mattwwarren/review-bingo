"""Grid client registry: registration, token auth, check-in/check-out."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.models.review_client import (
    ClientStatus,
    ReviewClient,
    ReviewClientCreate,
)


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


async def list_clients(session: AsyncSession, offset: int = 0, limit: int = 100) -> list[ReviewClient]:
    result = await session.execute(
        select(ReviewClient).order_by(col(ReviewClient.created_at)).offset(offset).limit(limit)
    )
    return list(result.scalars().all())
