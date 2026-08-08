"""Turn an enrolment credential into an identity the grid will admit.

Separate from `client_service` on purpose: that module owns the hub-minted
bearer token a client uses *after* it has joined, this one owns the question
of whether it may join at all. A2 (check-in re-attestation), A3 (dispatch
filtering) and A4 (policy) all read from here, and folding admission into the
client registry would blur two different auth concepts into one file.

Every decision this module makes is logged, including the successful ones.
A bypass that leaves only a startup banner is a bypass nobody can audit after
the fact — "was dev mode used, and how often" has to be answerable from the
log stream, not from a config file read months later.

The GitHub credential itself never appears in a log record here, whole or in
part. Identity fields (`github_login`, `github_user_id`) are logged instead:
they say who enrolled without handing anyone the means to do it again.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.core.config import settings
from review_bingo_hub.core.logging import get_logging_context
from review_bingo_hub.models.github_identity import GithubIdentity, IdentityRepoAccess
from review_bingo_hub.services.github_identity_service import (
    GithubIdentityError,
    GithubIdentityService,
    GithubRepoAccess,
    GithubUnavailableError,
)

LOGGER = logging.getLogger(__name__)

REASON_CREDENTIAL_REJECTED = "credential_rejected"
REASON_GITHUB_UNREACHABLE = "github_unreachable"

DETAIL_CREDENTIAL_REJECTED = "Enrolment credential rejected"


class EnrolmentError(Exception):
    """An enrolment attempt could not be admitted.

    A domain exception rather than an HTTPException: this module is called by
    A2 (check-in re-attestation), A3 (dispatch filtering), and A4 (policy)
    too, none of which are guaranteed to want POST /clients' own 401/503
    HTTP mapping. The API layer translates this at the boundary instead.
    Shared base so a caller that only wants "did enrolment fail" can catch
    one type, mirroring GithubIdentityError/GithubUnavailableError.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


class EnrolmentDeniedError(EnrolmentError):
    """The credential was explicitly rejected — not a transient condition."""


class EnrolmentUnavailableError(EnrolmentError):
    """GitHub could not be reached — a transient condition, worth a retry."""


async def get_or_create_identity(
    session: AsyncSession,
    github_user_id: int,
    github_login: str,
    repo_access: list[GithubRepoAccess],
) -> GithubIdentity:
    """Upsert the identity and replace its repo access snapshot wholesale.

    Delete-then-insert rather than merge: a repo the account has *lost* access
    to must disappear from the snapshot. Merging would keep granting access
    GitHub already revoked, which is the failure you would never notice.
    """
    result = await session.execute(select(GithubIdentity).where(col(GithubIdentity.github_user_id) == github_user_id))
    identity = result.scalar_one_or_none()

    if identity is None:
        identity = GithubIdentity(github_user_id=github_user_id, github_login=github_login)
    else:
        identity.github_login = github_login
        identity.access_refreshed_at = datetime.now(UTC)
    session.add(identity)
    await session.flush()  # type: ignore[attr-defined]
    await session.refresh(identity)

    await session.execute(delete(IdentityRepoAccess).where(col(IdentityRepoAccess.identity_id) == identity.id))
    for access in repo_access:
        session.add(
            IdentityRepoAccess(
                identity_id=identity.id,
                repo_full_name=access.repo_full_name,
                permission=access.permission,
            )
        )
    await session.flush()  # type: ignore[attr-defined]
    return identity


def _denied(
    *, reason: str, detail: str, unavailable: bool = False
) -> EnrolmentDeniedError | EnrolmentUnavailableError:
    """Log the refusal, then build the domain error to raise. Never logs the credential."""
    LOGGER.warning("enrolment_denied", extra={**get_logging_context(), "reason": reason})
    if unavailable:
        return EnrolmentUnavailableError(reason, detail)
    return EnrolmentDeniedError(reason, detail)


def _resolve_dev_credential(credential: str) -> None:
    """Compare against the shared dev secret, in constant time.

    An unset secret denies everything rather than accepting anything: the
    absence of a configured comparison value must not read as "no comparison
    required".
    """
    expected = settings.client_enrolment_secret
    if not expected or not secrets.compare_digest(credential, expected):
        raise _denied(reason=REASON_CREDENTIAL_REJECTED, detail=DETAIL_CREDENTIAL_REJECTED)
    LOGGER.warning("dev_mode_secret_used", extra={**get_logging_context()})


async def resolve_enrolment_credential(
    session: AsyncSession,
    credential: str,
    github: GithubIdentityService,
) -> UUID | None:
    """Admit (or refuse) an enrolment, returning the identity to link.

    Returns None in dev mode: there is no GitHub account behind a shared
    secret, and inventing a placeholder identity would put a row in
    `github_identity` that no GitHub account corresponds to.

    Raises:
        EnrolmentDeniedError: The credential was rejected (caller maps to 401).
        EnrolmentUnavailableError: GitHub could not be reached (caller maps to
            503). Fails closed either way — no identity, no client.
    """
    if settings.client_enrolment_mode != "github":
        _resolve_dev_credential(credential)
        return None

    try:
        github_identity = await github.get_identity(credential)
        repo_access = await github.get_repo_access(credential)
    except GithubUnavailableError as exc:
        raise _denied(
            reason=REASON_GITHUB_UNREACHABLE,
            detail="Could not verify enrolment with GitHub; try again shortly",
            unavailable=True,
        ) from exc
    except GithubIdentityError as exc:
        raise _denied(reason=REASON_CREDENTIAL_REJECTED, detail=DETAIL_CREDENTIAL_REJECTED) from exc

    identity = await get_or_create_identity(
        session,
        github_user_id=github_identity.github_user_id,
        github_login=github_identity.github_login,
        repo_access=repo_access,
    )

    LOGGER.info(
        "identity_resolved",
        extra={
            **get_logging_context(),
            "github_login": identity.github_login,
            "github_user_id": identity.github_user_id,
            "accessible_repo_count": len(repo_access),
        },
    )
    return identity.id
