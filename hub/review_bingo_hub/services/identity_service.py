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
from review_bingo_hub.models.github_identity import GithubIdentity, IdentityRepoAccess, PermissionLevel
from review_bingo_hub.models.review_client import ReviewClient
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


class PolicyAuthorizationError(Exception):
    """A policy read or write could not be authorized for this caller.

    A domain exception rather than an HTTPException, same reasoning as
    EnrolmentError: this module has no HTTP layer of its own, so api/policies.py
    maps these at the boundary.
    """

    def __init__(self, *, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


class PolicyCallerUnauthenticatedError(PolicyAuthorizationError):
    """The bearer credential does not resolve to any known caller (maps to 401)."""


class PolicyWriteForbiddenError(PolicyAuthorizationError):
    """The caller is known but does not administer this repo (maps to 403)."""


# Named ..._UNKNOWN_CALLER rather than ..._UNKNOWN_TOKEN because flake8-bandit
# reads any constant whose *name* contains "token" as a hardcoded credential.
# The values are what they say: the caller could not be resolved.
REASON_UNKNOWN_CALLER = "unknown_token"
REASON_NOT_ADMIN = "not_admin"

# The dev-mode bypass has no ReviewClient to name — no client is registered at
# all for a raw shared-secret write. This sentinel makes that absence explicit
# in the log record rather than leaving the caller-identity fields silently
# missing, which would read as an omission bug rather than a deliberate one.
CALLER_IDENTITY_UNAVAILABLE_DEV_MODE = "unavailable_dev_mode_shared_secret"

# api/clients.py's get_current_client imports this rather than repeating the
# literal, so the two entry points cannot be told apart by their refusal text.
DETAIL_UNKNOWN_CALLER = "Unknown client token"
DETAIL_NOT_ADMIN = "Repo admin access required to set this repo's policy"


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


async def accessible_repo_names(session: AsyncSession, client: ReviewClient) -> frozenset[str] | None:
    """The repos this client's GitHub account may see, or None when scoping is inert.

    The single place "what can this caller reach" is decided, so dispatch,
    reads, and the roster cannot drift into three different answers.

    Returns None to mean *unrestricted, filter by tier only* — the dev-mode
    carve-out. Under CLIENT_ENROLMENT_MODE=dev there is no GitHub account
    behind a client at all, so there is nothing to derive a scope from and
    pretending otherwise would just be a scope we invented.

    Under github mode a client with no identity_id — a stale row from before
    GitHub-derived admission landed — gets an empty frozenset, not None: no
    account means nothing to match against, so nothing leases and nothing
    reads. Fails closed, and the distinction from None is load-bearing.
    """
    if settings.client_enrolment_mode != "github":
        return None
    if client.identity_id is None:
        return frozenset()
    result = await session.execute(
        select(IdentityRepoAccess.repo_full_name).where(col(IdentityRepoAccess.identity_id) == client.identity_id)
    )
    return frozenset(result.scalars().all())


async def _resolve_caller_client(session: AsyncSession, credential: str) -> ReviewClient:
    """The grid client behind a bearer credential, or a 401-shaped refusal."""
    # Deferred, not top-level: client_service.py already imports
    # accessible_repo_names from this module (#22, job/roster access scoping),
    # so a top-level import here in the reverse direction is circular whichever
    # module loads first. Importing at call time breaks the cycle at no runtime
    # cost — both modules have finished loading long before this runs.
    from review_bingo_hub.services.client_service import get_client_by_token  # noqa: PLC0415

    client = await get_client_by_token(session, credential)
    if client is None:
        raise PolicyCallerUnauthenticatedError(reason=REASON_UNKNOWN_CALLER, detail=DETAIL_UNKNOWN_CALLER)
    return client


async def _repo_permission(session: AsyncSession, identity_id: UUID, repo_full_name: str) -> PermissionLevel | None:
    """What GitHub last said this identity could do on one repo, if anything.

    A different question from accessible_repo_names, which answers "which
    repos" rather than "at what level" — hence a second, narrower query rather
    than a filter over that function's result.
    """
    result = await session.execute(
        select(IdentityRepoAccess.permission).where(
            col(IdentityRepoAccess.identity_id) == identity_id,
            col(IdentityRepoAccess.repo_full_name) == repo_full_name,
        )
    )
    return result.scalar_one_or_none()


async def _github_identity_fields(session: AsyncSession, identity_id: UUID | None) -> dict[str, str | int | None]:
    """github_login/github_user_id for an audit record, or both None.

    Required fields rather than optional extras, following identity_resolved's
    precedent: client_id and identity_id are opaque UUIDs, so "which human
    tried this" has to be answerable from the log stream on its own.
    """
    if identity_id is None:
        return {"github_login": None, "github_user_id": None}
    result = await session.execute(
        select(GithubIdentity.github_login, GithubIdentity.github_user_id).where(col(GithubIdentity.id) == identity_id)
    )
    row = result.one_or_none()
    if row is None:
        return {"github_login": None, "github_user_id": None}
    return {"github_login": row[0], "github_user_id": row[1]}


async def authorize_policy_write(session: AsyncSession, credential: str, repo_full_name: str) -> None:
    """Admit or refuse a PUT /policies/{owner}/{repo} call.

    dev mode: the shared enrolment secret substitutes for the repo-admin check
    — the same comparison POST /clients makes (_resolve_dev_credential, in this
    module), not a parallel one, so there is one named bypass rather than two.

    github mode: the caller's hub-minted token must resolve to a client whose
    cached GitHub identity is recorded as `admin` on this repo. A client with
    no identity at all fails closed, exactly as it does for dispatch.

    Raises:
        PolicyCallerUnauthenticatedError: credential resolves to nobody (401).
        PolicyWriteForbiddenError: caller is known but not a repo admin (403).
        EnrolmentDeniedError: dev-mode secret rejected (401).
    """
    if settings.client_enrolment_mode == "dev":
        _resolve_dev_credential(credential)
        LOGGER.warning(
            "policy_write_dev_mode_bypass",
            extra={
                **get_logging_context(),
                "repo_full_name": repo_full_name,
                "caller_identity": CALLER_IDENTITY_UNAVAILABLE_DEV_MODE,
            },
        )
        return

    client = await _resolve_caller_client(session, credential)
    permission = (
        await _repo_permission(session, client.identity_id, repo_full_name) if client.identity_id is not None else None
    )
    log_extra = {
        **get_logging_context(),
        "repo_full_name": repo_full_name,
        "client_id": str(client.id),
        "identity_id": str(client.identity_id) if client.identity_id else None,
        **(await _github_identity_fields(session, client.identity_id)),
    }
    if permission != PermissionLevel.ADMIN:
        LOGGER.warning("policy_write_denied", extra={**log_extra, "permission": permission})
        raise PolicyWriteForbiddenError(reason=REASON_NOT_ADMIN, detail=DETAIL_NOT_ADMIN)
    LOGGER.info("policy_write_authorized", extra=log_extra)


async def caller_accessible_repo_names(session: AsyncSession, credential: str) -> frozenset[str] | None:
    """Repos this credential's caller may see, or None for "no filtering" (dev mode).

    Not a second accessible_repo_names: that function already owns the "what
    can this caller reach" answer for dispatch, job reads, and the roster. This
    one only adds the credential half in front of it — resolve the bearer token
    to a ReviewClient, then delegate — so policy reads cannot drift into a
    fourth answer.

    An unknown token still raises. Read scoping is permissive by design, but
    never open to a credential that resolves to nobody.
    """
    if settings.client_enrolment_mode == "dev":
        _resolve_dev_credential(credential)
        return None
    client = await _resolve_caller_client(session, credential)
    return await accessible_repo_names(session, client)


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
