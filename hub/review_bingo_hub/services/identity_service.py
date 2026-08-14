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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    GithubUserIdentity,
)

LOGGER = logging.getLogger(__name__)

REASON_CREDENTIAL_REJECTED = "credential_rejected"
REASON_GITHUB_UNREACHABLE = "github_unreachable"
REASON_IDENTITY_MISMATCH = "identity_mismatch"

# The two terminal answers a device poll can give. Public, and living beside the
# other REASON_* constants, so `api/auth.py` can log a device refusal under the
# existing `enrolment_denied` event rather than inventing a second denial event:
# "who was refused admission, and why" has to stay one question with one answer
# in the log stream, not two nobody remembers to grep both of.
REASON_DEVICE_TOKEN_EXPIRED = "device_token_expired"  # noqa: S105 - a reason code, not a credential
REASON_DEVICE_ACCESS_DENIED = "device_access_denied"

DETAIL_CREDENTIAL_REJECTED = "Enrolment credential rejected"
DETAIL_GITHUB_UNREACHABLE = "Could not verify enrolment with GitHub; try again shortly"
DETAIL_IDENTITY_MISMATCH = "GitHub token does not match this client's linked account"


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
REASON_CLIENT_NOT_FOUND = "client_not_found"
REASON_CLIENT_WRONG_IDENTITY = "client_wrong_identity"

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


async def accessible_repo_names(session: AsyncSession, identity_id: UUID | None) -> frozenset[str] | None:
    """The repos this GitHub account may see, or None when scoping is inert.

    The single place "what can this caller reach" is decided, so dispatch,
    reads, and the roster cannot drift into three different answers.

    Keyed on the identity rather than on a `ReviewClient` since B1 (#24): a
    dashboard session is a caller with an identity and no client row at all, and
    a signature that demanded a machine would have forced a second, parallel
    answer to this same question for the browser.

    Returns None to mean *unrestricted, filter by tier only* — the dev-mode
    carve-out. Under CLIENT_ENROLMENT_MODE=dev there is no GitHub account
    behind a client at all, so there is nothing to derive a scope from and
    pretending otherwise would just be a scope we invented.

    Under github mode a caller with no identity_id — a stale client row from
    before GitHub-derived admission landed — gets an empty frozenset, not None:
    no account means nothing to match against, so nothing leases and nothing
    reads. Fails closed, and the distinction from None is load-bearing.
    """
    if settings.client_enrolment_mode != "github":
        return None
    if identity_id is None:
        return frozenset()
    result = await session.execute(
        select(IdentityRepoAccess.repo_full_name).where(col(IdentityRepoAccess.identity_id) == identity_id)
    )
    return frozenset(result.scalars().all())


def _snapshot_is_stale(refreshed_at: datetime) -> bool:
    """The one TTL comparison both staleness answers share.

    Extracted so `identity_access_is_stale` (keyed on a `ReviewClient`, with its
    own mode/identity carve-outs) and `caller_identity_snapshot` (keyed directly
    on an `identity_id` it has already resolved, with no carve-outs of its own
    left to make) cannot drift into two different definitions of "too old".
    """
    return datetime.now(UTC) - refreshed_at > timedelta(seconds=settings.identity_access_ttl_seconds)


def access_freshness(refreshed_at: datetime) -> tuple[datetime, bool]:
    """(access_expires_at, access_is_stale) for a raw `GithubIdentity.access_refreshed_at`.

    Shared with `client_service`'s roster enrichment so the roster's per-row
    staleness answer cannot drift from `/auth/me`'s and `identity_access_is_stale`'s
    — all three now reduce to `_snapshot_is_stale`. Public where that one is
    private because this is the cross-module form: a caller outside this file
    needs the deadline as well as the verdict, and reaching for the private name
    to compute one of them itself is exactly the drift the extraction prevents.

    The deadline is returned rather than the TTL that produced it. The roster
    renders one row per machine, and a constant repeated per row is the shape
    `ReviewClientCheckInRead` already declined; an absolute instant is also the
    only form a "expires in 12m" reading can be computed from without the reader
    knowing the hub's configuration.
    """
    expires_at = refreshed_at + timedelta(seconds=settings.identity_access_ttl_seconds)
    return expires_at, _snapshot_is_stale(refreshed_at)


async def identity_access_is_stale(session: AsyncSession, client: ReviewClient) -> bool:
    """Whether this client's cached GitHub access is too old to lease against.

    `accessible_repo_names` answers *which* repos; this answers how old an
    answer the hub will still act on. Both gates exist because they fail
    differently: a wrong access set is wrong about the repos, a stale one was
    right when it was written and nothing has told the hub otherwise since.
    Nobody notifies a webhook when a person loses repo access, so age is the
    only signal available.

    The same two inert carve-outs as `accessible_repo_names`, for the same
    reasons. Dev mode has no GitHub account behind a client, so there is no
    snapshot to age. And a github-mode client with no identity already fails
    closed on access — its set is empty and it leases nothing — so calling it
    stale would answer "check in again" to something a check-in cannot fix.

    A missing identity row reads as maximally stale rather than fresh: the FK
    makes it unreachable in practice, and the direction to guess in when an
    authorization snapshot cannot be found is "refuse".
    """
    if settings.client_enrolment_mode != "github":
        return False
    if client.identity_id is None:
        return False
    result = await session.execute(
        select(GithubIdentity.access_refreshed_at).where(col(GithubIdentity.id) == client.identity_id)
    )
    refreshed_at = result.scalar_one_or_none()
    if refreshed_at is None:
        return True
    return _snapshot_is_stale(refreshed_at)


@dataclass(frozen=True)
class ScopedCaller:
    """Whoever is behind a bearer credential on a *read* endpoint.

    Two credential classes resolve here — a grid client's registration token and
    a dashboard session token — and what a read needs from either is the same
    pair of facts: which GitHub account to scope by, and which client row (if
    any) is the caller's own. Everything downstream keys on `identity_id`, so
    the two callers cannot end up with two different access answers.

    `client_id` is None for a dashboard session: a person has no machine on the
    grid. That is why the roster's "always show me my own row" clause has to
    tolerate its absence rather than assume it.
    """

    identity_id: UUID | None
    client_id: UUID | None


async def resolve_scoped_caller(session: AsyncSession, credential: str) -> ScopedCaller:
    """Resolve a read credential to the identity it reads on behalf of.

    Tries dashboard sessions first and the client registry second — order is
    arbitrary for correctness (the two token spaces are 256-bit random and
    disjoint in practice) but not for cost: every endpoint behind this
    resolver is dashboard-polled (`/jobs`, `/clients`, and their
    sub-resources), not lease-adjacent — grid clients only ever call
    `POST /jobs/lease` and `POST /jobs/{id}/report`, which stay on `ClientDep`
    and never reach here at all. So the dashboard-session lookup is the hit,
    not the miss.

    Raises:
        PolicyCallerUnauthenticatedError: the credential resolves to neither a
            registered client nor a live dashboard session (maps to 401). An
            expired session lands here too, and must: to a caller it is
            indistinguishable from a token that was never issued.
    """
    # Deferred, not top-level: dashboard_session_service imports client_service,
    # which in turn imports this module, so a top-level import of either here
    # would cycle. Importing at call time breaks the cycle at no runtime cost —
    # both modules have finished loading long before this runs.
    from review_bingo_hub.services.client_service import get_client_by_token  # noqa: PLC0415
    from review_bingo_hub.services.dashboard_session_service import get_identity_id_for_token  # noqa: PLC0415

    identity_id = await get_identity_id_for_token(session, credential)
    if identity_id is not None:
        return ScopedCaller(identity_id=identity_id, client_id=None)

    client = await get_client_by_token(session, credential)
    if client is not None:
        return ScopedCaller(identity_id=client.identity_id, client_id=client.id)

    raise PolicyCallerUnauthenticatedError(reason=REASON_UNKNOWN_CALLER, detail=DETAIL_UNKNOWN_CALLER)


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


@dataclass(frozen=True)
class CallerIdentitySnapshot:
    """What `GET /auth/me` reports for the caller's own account.

    Bundles the identity fields with the repo-access snapshot in one dataclass
    rather than returning them separately, since `/auth/me` always wants both
    together and a caller with no identity row gets `None` from
    `caller_identity_snapshot` rather than a snapshot with empty fields.
    """

    github_login: str
    access_refreshed_at: datetime
    access_is_stale: bool
    repo_access: list[GithubRepoAccess]


async def caller_identity_snapshot(session: AsyncSession, identity_id: UUID) -> CallerIdentitySnapshot | None:
    """The identity fields and repo-access snapshot `/auth/me` reports, or None if the row is gone.

    Two queries rather than a join, mirroring `_github_identity_fields` and
    `accessible_repo_names`'s own query shapes: this is a read-path helper, not
    a hot one, and matching the module's existing query shapes keeps the three
    "what does this identity_id resolve to" answers easy to compare by eye.
    """
    result = await session.execute(
        select(GithubIdentity.github_login, GithubIdentity.access_refreshed_at).where(
            col(GithubIdentity.id) == identity_id
        )
    )
    row = result.one_or_none()
    if row is None:
        return None
    github_login, refreshed_at = row

    access_result = await session.execute(
        select(IdentityRepoAccess.repo_full_name, IdentityRepoAccess.permission).where(
            col(IdentityRepoAccess.identity_id) == identity_id
        )
    )
    repo_access = [
        GithubRepoAccess(repo_full_name=repo_full_name, permission=permission)
        for repo_full_name, permission in access_result.all()
    ]

    return CallerIdentitySnapshot(
        github_login=github_login,
        access_refreshed_at=refreshed_at,
        access_is_stale=_snapshot_is_stale(refreshed_at),
        repo_access=repo_access,
    )


async def authorize_policy_write(session: AsyncSession, credential: str, repo_full_name: str) -> None:
    """Admit or refuse a PUT /policies/{owner}/{repo} call.

    dev mode: the shared enrolment secret substitutes for the repo-admin check
    — the same comparison POST /clients makes (_resolve_dev_credential, in this
    module), not a parallel one, so there is one named bypass rather than two.

    github mode: the caller's credential must resolve to a GitHub identity that
    is recorded as `admin` on this repo. A caller with no identity at all fails
    closed, exactly as it does for dispatch.

    Either credential kind resolves here (`resolve_scoped_caller`), the same
    door reads already come through, since RFC 0002 B2 (#47): the dashboard's
    policy editor saves through this endpoint, and RFC 0001 D-POLICY's invariant
    — whoever GitHub says administers a repo may set that repo's model floor —
    names the *identity*, not the machine. A browser session and a grid client
    belonging to the same account resolve to the same `github_identity` row and
    the same cached per-repo permission, so admitting one and not the other
    would answer the same question two ways.

    What is deliberately *not* widened is the check: `permission == ADMIN`
    below runs identically whichever kind resolved, and `client_id` simply
    becomes optional in the audit record because a person has no machine to
    name. Before B2 this path resolved through a client-only helper; that
    narrowness was RFC 0001's read-only-dashboard posture, not a property of
    the invariant.

    `resolve_scoped_caller` tries the dashboard-session table first, so a
    grid-client token — the only credential kind this path saw before B2 —
    now costs one extra indexed lookup (a miss on sessions, then a hit on
    clients) instead of going straight to the client table. Negligible here:
    unlike the poll-driven reads that share this resolver, a policy write is
    a low-frequency, button-click path.

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

    caller = await resolve_scoped_caller(session, credential)
    permission = (
        await _repo_permission(session, caller.identity_id, repo_full_name) if caller.identity_id is not None else None
    )
    log_extra = {
        **get_logging_context(),
        "repo_full_name": repo_full_name,
        # Both optional, and both still present as keys when absent: a dashboard
        # session has no client row, and a dropped field would read as an
        # omission bug rather than the deliberate "there is no machine here".
        "client_id": str(caller.client_id) if caller.client_id else None,
        "identity_id": str(caller.identity_id) if caller.identity_id else None,
        **(await _github_identity_fields(session, caller.identity_id)),
    }
    if permission != PermissionLevel.ADMIN:
        LOGGER.warning("policy_write_denied", extra={**log_extra, "permission": permission})
        raise PolicyWriteForbiddenError(reason=REASON_NOT_ADMIN, detail=DETAIL_NOT_ADMIN)
    LOGGER.info("policy_write_authorized", extra=log_extra)


async def authorize_client_revoke(session: AsyncSession, credential: str, client_id: UUID) -> ReviewClient | None:
    """Admit or refuse a `DELETE /clients/{client_id}` call, returning the target.

    dev mode: the shared enrolment secret may revoke any client — the same
    comparison `POST /clients` and `PUT /policies/{owner}/{repo}` already make
    (`_resolve_dev_credential`), not a parallel one, so there is still one
    named bypass rather than three. A client's own hub-minted token is not that
    secret and does not open this door.

    github mode: the caller's identity must equal the target's. Either
    credential kind resolves here (`resolve_scoped_caller`), because RFC 0002
    D-SELFREVOKE names the *identity*, not the machine — the client that needs
    revoking is very often the one that cannot make the call. A caller with no
    identity at all fails closed, exactly as it does for dispatch and for
    policy writes: `None == None` would otherwise read as "same identity" and
    hand every identity-less client the power to revoke every other one.

    Returns None for both "no such client" and "someone else's client", and the
    endpoint answers 404 to either (RFC 0001 D-404). Distinguishing them would
    make this an oracle for which client ids are real, and with them how many
    machines every other org runs.

    That collapse governs the caller-visible response and nothing else. Both
    refusals are logged as `client_revoke_denied`, carrying an internal `reason`
    that *does* say which happened, plus the caller identity fields and the
    requested `target_client_id`. `target_client_name` appears only when a row
    was actually found — there is nothing to name otherwise, and an empty name
    would read as a real client rather than an absent one. A hub operator
    reading the log stream is not a caller probing the endpoint.

    On authorization, the ids of the target's REPORTED/RELAYED jobs are read
    *before* the caller deletes it and logged as `reported_jobs_detached` /
    `reported_job_ids`: `review_job.reported_by` is ON DELETE SET NULL, so the
    delete quietly detaches every finished round from the machine that produced
    it. The delete still proceeds — the verdict content survives on the job row
    and in the PR comment, and gating self-service revocation on review history
    would defeat the point — but the loss is recorded rather than silent.

    Raises:
        PolicyCallerUnauthenticatedError: credential resolves to nobody (401).
        EnrolmentDeniedError: dev-mode secret rejected (401).
    """
    # Deferred for the same cycle-breaking reason as `resolve_scoped_caller`'s
    # deferred imports above: both client_service and job_service import this
    # module at top level, so the reverse imports have to happen at call time.
    from review_bingo_hub.services.client_service import get_client_by_id  # noqa: PLC0415
    from review_bingo_hub.services.job_service import reported_jobs_for_client  # noqa: PLC0415

    dev_mode = settings.client_enrolment_mode == "dev"
    caller_identity_id: UUID | None = None
    caller_fields: dict[str, str | int | None]

    if dev_mode:
        _resolve_dev_credential(credential)
        caller_fields = {"caller_identity": CALLER_IDENTITY_UNAVAILABLE_DEV_MODE}
        LOGGER.warning(
            "client_revoke_dev_mode_bypass",
            extra={**get_logging_context(), "target_client_id": str(client_id), **caller_fields},
        )
    else:
        caller = await resolve_scoped_caller(session, credential)
        caller_identity_id = caller.identity_id
        caller_fields = {
            "identity_id": str(caller_identity_id) if caller_identity_id else None,
            **(await _github_identity_fields(session, caller_identity_id)),
        }

    log_extra: dict[str, object] = {
        **get_logging_context(),
        "target_client_id": str(client_id),
        **caller_fields,
    }

    target = await get_client_by_id(session, client_id)
    if target is None:
        LOGGER.warning("client_revoke_denied", extra={**log_extra, "reason": REASON_CLIENT_NOT_FOUND})
        return None

    if not dev_mode and (caller_identity_id is None or target.identity_id != caller_identity_id):
        LOGGER.warning(
            "client_revoke_denied",
            extra={**log_extra, "reason": REASON_CLIENT_WRONG_IDENTITY, "target_client_name": target.name},
        )
        return None

    detached = await reported_jobs_for_client(session, target.id)
    LOGGER.info(
        "client_revoke_authorized",
        extra={
            **log_extra,
            "target_client_name": target.name,
            "reported_jobs_detached": len(detached),
            "reported_job_ids": [str(job_id) for job_id in detached],
        },
    )
    return target


async def caller_accessible_repo_names(session: AsyncSession, credential: str) -> frozenset[str] | None:
    """Repos this credential's caller may see, or None for "no filtering" (dev mode).

    Not a second accessible_repo_names: that function already owns the "what
    can this caller reach" answer for dispatch, job reads, and the roster. This
    one only adds the credential half in front of it — resolve the bearer token
    to a ReviewClient, then delegate — so policy reads cannot drift into a
    fourth answer.

    An unknown token still raises. Read scoping is permissive by design, but
    never open to a credential that resolves to nobody.

    Resolves through `resolve_scoped_caller` since B1 (#24), so a signed-in
    dashboard narrows policy reads by the same access set its job feed already
    uses. The signature is unchanged, which is why `api/policies.py` needed no
    edit — the widening happens in one place or not at all.
    """
    if settings.client_enrolment_mode == "dev":
        _resolve_dev_credential(credential)
        return None
    caller = await resolve_scoped_caller(session, credential)
    return await accessible_repo_names(session, caller.identity_id)


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


async def _read_github_identity(
    github: GithubIdentityService,
    credential: str,
) -> tuple[GithubUserIdentity, list[GithubRepoAccess]]:
    """Spend a GitHub token once: who it belongs to, and what it can reach.

    Shared by enrolment and check-in re-attestation so the two cannot drift into
    different exception mappings for the same pair of calls. That mapping is not
    a detail: whether GitHub's answer means "rejected" or "unreachable" decides
    whether the caller fails closed or is let through on its existing snapshot,
    and one place has to own that distinction.

    Raises:
        EnrolmentDeniedError: GitHub rejected the credential.
        EnrolmentUnavailableError: GitHub could not be reached.
    """
    try:
        github_identity = await github.get_identity(credential)
        repo_access = await github.get_repo_access(credential)
    except GithubUnavailableError as exc:
        raise _denied(reason=REASON_GITHUB_UNREACHABLE, detail=DETAIL_GITHUB_UNREACHABLE, unavailable=True) from exc
    except GithubIdentityError as exc:
        raise _denied(reason=REASON_CREDENTIAL_REJECTED, detail=DETAIL_CREDENTIAL_REJECTED) from exc
    return github_identity, repo_access


async def _linked_github_user_id(session: AsyncSession, identity_id: UUID) -> int | None:
    """The GitHub account number a client is already linked to, if the row is there.

    A narrow query of its own rather than a read through `_github_identity_fields`,
    following `_repo_permission`'s precedent: that one shapes an audit record and
    this one decides an authorization, and a helper serving both would invite
    changing the log format into changing who gets admitted.
    """
    result = await session.execute(select(GithubIdentity.github_user_id).where(col(GithubIdentity.id) == identity_id))
    return result.scalar_one_or_none()


async def reattest_identity(
    session: AsyncSession,
    client: ReviewClient,
    github_token: str,
    github: GithubIdentityService,
) -> None:
    """Re-read a client's GitHub repo access from a fresh token, replacing the snapshot.

    The refresh half of D-TTL. Check-in is already the grid's availability
    signal, which makes it the natural place to ask "and are you still who you
    said, with the same repos" — the alternative, polling GitHub for every
    enrolled account, spends someone's rate limit to learn nothing most of the
    time.

    A token resolving to a *different* account than the client is linked to is
    refused rather than accepted: re-attestation means "prove you are still the
    account you enrolled as", so relinking here would quietly turn check-in into
    an account-transfer endpoint and hand the client the other account's repos.
    The one exception is a client with no identity at all — a row predating
    GitHub-derived admission, which can currently lease nothing — where the same
    call links it instead, because refusing would leave it unable to self-heal.

    No-op outside github mode, the same inert carve-out `accessible_repo_names`
    and `identity_access_is_stale` make: dev mode has no GitHub account behind a
    client, so there is nothing to re-attest and a token here is meaningless.

    Raises:
        EnrolmentDeniedError: the token was rejected, or belongs to another
            account (caller maps to 401).
        EnrolmentUnavailableError: GitHub could not be reached. Transient, and
            the caller is expected to let check-in proceed on the existing
            snapshot — see check_in_endpoint.
    """
    if settings.client_enrolment_mode != "github":
        return

    github_identity, repo_access = await _read_github_identity(github, github_token)

    if client.identity_id is not None:
        linked_user_id = await _linked_github_user_id(session, client.identity_id)
        # A missing row compares unequal and so is refused, which is the safe
        # direction: an identity_id pointing at nothing is a broken invariant,
        # not a client to re-link.
        if linked_user_id != github_identity.github_user_id:
            raise _denied(reason=REASON_IDENTITY_MISMATCH, detail=DETAIL_IDENTITY_MISMATCH)

    identity = await get_or_create_identity(
        session,
        github_user_id=github_identity.github_user_id,
        github_login=github_identity.github_login,
        repo_access=repo_access,
    )
    if client.identity_id is None:
        client.identity_id = identity.id
        session.add(client)
        await session.flush()  # type: ignore[attr-defined]

    LOGGER.info(
        "identity_reattested",
        extra={
            **get_logging_context(),
            "client_id": str(client.id),
            "github_login": identity.github_login,
            "github_user_id": identity.github_user_id,
            "accessible_repo_count": len(repo_access),
        },
    )


async def resolve_identity_from_github_token(
    session: AsyncSession,
    github_token: str,
    github: GithubIdentityService,
) -> GithubIdentity:
    """Spend a GitHub user token and return the identity row it resolves to.

    Shared by client enrolment and dashboard login, which is the point: one
    GitHub account is one `github_identity` row however it arrives, so a person
    who enrolled a machine from the CLI and then opens the dashboard does not
    end up as two accounts with two access snapshots that can disagree.

    Raises:
        EnrolmentDeniedError: The token was rejected (caller maps to 401).
        EnrolmentUnavailableError: GitHub could not be reached (caller maps to
            503). Fails closed either way — no identity is written.
    """
    github_identity, repo_access = await _read_github_identity(github, github_token)

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
    return identity


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

    identity = await resolve_identity_from_github_token(session, credential, github)
    return identity.id
