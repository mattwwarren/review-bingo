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

from review_bingo_hub.models.github_identity import GithubIdentity, IdentityRepoAccess
from review_bingo_hub.models.review_client import (
    ClientStatus,
    ReviewClient,
    ReviewClientCreate,
    ReviewClientRead,
    ReviewClientRosterRead,
)
from review_bingo_hub.services.identity_service import ScopedCaller, access_freshness, accessible_repo_names


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


async def get_client_by_id(session: AsyncSession, client_id: UUID) -> ReviewClient | None:
    """Look a client up by its row id rather than by the token it presents.

    A sibling of `get_client_by_token`, not a widening of it: this one answers
    "which client is being *talked about*" (a path parameter someone typed),
    where that one answers "which client is *calling*". Keeping them apart is
    what lets revocation authorize a caller against a target at all.
    """
    result = await session.execute(select(ReviewClient).where(col(ReviewClient.id) == client_id))
    return result.scalar_one_or_none()


async def delete_client(session: AsyncSession, client: ReviewClient) -> None:
    """Remove a client from the grid for good.

    A hard delete, not a status flag: the whole point of revocation is that the
    bearer token stops resolving, and `get_client_by_token` finds a soft-deleted
    row exactly as well as a live one. A revoked machine that could still check
    in would be a revocation in name only.

    Callers must release the client's leases first — see
    `job_service.release_leased_jobs_for_client` for why the order matters.
    """
    await session.delete(client)
    await session.flush()  # type: ignore[attr-defined]


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


async def list_clients_with_attestation(
    session: AsyncSession,
    caller: ScopedCaller,
    offset: int = 0,
    limit: int = 100,
) -> list[ReviewClientRosterRead]:
    """The roster, plus what the dashboard needs to manage it (RFC 0002 B3).

    A wrapper around `list_clients` rather than a widening of it, and the
    distinction is load-bearing: *which* rows a caller may see is a security
    answer with its own tests, and this function must not be able to change it.
    It re-derives no scoping — it decorates whatever came back.

    Two facts get added per row. `is_own` compares identities, never machines,
    because the GitHub account is the unit of admission: one person's second box
    is still theirs to revoke, and `DELETE /clients/{id}` authorizes on exactly
    this comparison. The `caller.identity_id is not None` guard is the whole
    point of writing it out — under dev-mode enrolment every client's
    `identity_id` is NULL, so a bare equality would report every dev-mode
    machine as every other one's own and put a revoke button on all of them.

    The attestation fields come from one `WHERE id IN (...)` over the distinct
    identities on the page, not a lookup per row: the roster is polled twice a
    second by every open dashboard, and a per-row query is the N+1 that shape
    invites. A row whose identity cannot be found reports `(None, False)` — the
    same shape a dev-mode row gets, because in both cases there is no snapshot
    to age, and inventing a deadline for one would render as a countdown to
    nothing.
    """
    clients = await list_clients(
        session, identity_id=caller.identity_id, own_client_id=caller.client_id, offset=offset, limit=limit
    )

    identity_ids = {c.identity_id for c in clients if c.identity_id is not None}
    refreshed_by_identity: dict[UUID, datetime] = {}
    if identity_ids:
        rows = await session.execute(
            select(GithubIdentity.id, GithubIdentity.access_refreshed_at).where(
                col(GithubIdentity.id).in_(identity_ids)
            )
        )
        refreshed_by_identity = {identity_id: refreshed_at for identity_id, refreshed_at in rows.all()}

    roster: list[ReviewClientRosterRead] = []
    for client in clients:
        refreshed_at = refreshed_by_identity.get(client.identity_id) if client.identity_id is not None else None
        expires_at, is_stale = access_freshness(refreshed_at) if refreshed_at is not None else (None, False)
        roster.append(
            ReviewClientRosterRead(
                # Through ReviewClientRead rather than straight off the row, the
                # same way check_in_endpoint composes its own subclass: the
                # public view is where "never the token hash" is decided, and
                # widening the roster must not be a way around that.
                **ReviewClientRead.model_validate(client).model_dump(),
                is_own=caller.identity_id is not None and client.identity_id == caller.identity_id,
                access_refreshed_at=refreshed_at,
                access_expires_at=expires_at,
                access_is_stale=is_stale,
            )
        )
    return roster
