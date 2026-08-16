"""Review job dispatch: enqueue from webhooks, lease to clients, accept reports.

Concurrency: leasing uses SELECT ... FOR UPDATE SKIP LOCKED so concurrent
clients never race for the same job. Expired leases are reclaimed lazily at
the top of every lease request — no background worker in v1.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import ColumnElement, Select, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped
from sqlmodel import col

from review_bingo_hub.core.config import settings
from review_bingo_hub.models.repo_policy import RepoPolicy
from review_bingo_hub.models.review_client import ModelTier, ReviewClient, tiers_at_or_below
from review_bingo_hub.models.review_job import (
    JobState,
    ReviewJob,
    ReviewJobBase,
    ReviewJobFilters,
    ReviewJobReport,
)
from review_bingo_hub.services.identity_service import accessible_repo_names, identity_access_is_stale

ACTIVE_STATES = (JobState.QUEUED, JobState.LEASED)

DETAIL_STALE_ACCESS = "Cached GitHub access has expired; check in again"


class StaleIdentityAccessError(Exception):
    """The caller's cached GitHub access is past its TTL; it must re-attest first.

    A domain exception defined next to its raise sites rather than in
    `identity_service`, matching the convention that module's own
    EnrolmentError/PolicyAuthorizationError families follow: the staleness
    *rule* lives there, but refusing a lease over it is this module's decision,
    and `api/jobs.py` maps it to a status code at the boundary.
    """

    def __init__(self, detail: str = DETAIL_STALE_ACCESS) -> None:
        self.detail = detail
        super().__init__(detail)


async def _refuse_if_access_stale(session: AsyncSession, client: ReviewClient) -> None:
    """Guard both lease paths, so naming a job cannot dodge the TTL.

    A single helper for the same reason `_filter_to_access` is one: two copies
    of a two-line authorization check are two places for it to be forgotten.
    """
    if await identity_access_is_stale(session, client):
        raise StaleIdentityAccessError


def _strategy_overlap(client: ReviewClient) -> ColumnElement[bool]:
    """The strategy gate: a job's requested strategies must be empty, or overlap the client's.

    One expression shared by both lease paths, for the reason `_filter_to_access`
    is one function: two copies of a dispatch gate are two places for it to
    drift, and naming a job is not a way around a repo's strategy contract any
    more than it is a way around its model floor.

    Empty on the *job* side is the match-any sentinel — a repo that named no
    strategies did not name an impossible one. Empty on the *client* side is
    not symmetric with that: a client offering nothing overlaps nothing, so it
    is only ever handed jobs that asked for nothing.

    `?|` is Postgres' "does this JSONB array hold any of these strings", which
    keeps the whole gate inside the same locking SELECT instead of filtering in
    Python after the rows come back. #65's model allowlist belongs beside this
    one, as another `.where()` on the same query.

    Same rule, expressed in plain Python for the one caller that already has
    both lists loaded and isn't running a query: `models.review_strategy.
    strategies_overlap`, used by `api.jobs`'s targeted-lease pre-check. Change
    the matching semantics in one, change them in the other.
    """
    return sa.or_(
        sa.func.jsonb_array_length(col(ReviewJob.requested_strategies)) == 0,
        col(ReviewJob.requested_strategies).op("?|")(sa.cast(client.offered_strategies, postgresql.ARRAY(sa.Text))),
    )


def _live_policy_list(column: Mapped[Any]) -> ColumnElement[Any]:
    """One RepoPolicy JSONB list column, read live for the ReviewJob row being considered.

    A correlated scalar subquery rather than a join, for a specific reason: a
    joined RepoPolicy would enter the `FOR UPDATE SKIP LOCKED` scope of both
    lease paths, so leasing a job would take a row lock on its repo's policy and
    a concurrent policy write would start contending with dispatch. Only
    ReviewJob rows are being leased here, and only they should be locked.

    Coalesces to an empty array for a repo with no policy row at all, which
    matches `enqueue_job`'s own reading of a missing policy: no policy means no
    floor, not a floor nobody clears.
    """
    return sa.func.coalesce(
        select(column)
        .where(col(RepoPolicy.repo_full_name) == col(ReviewJob.repo_full_name))
        .correlate(ReviewJob)
        .scalar_subquery(),
        sa.cast(sa.literal("[]"), postgresql.JSONB),
    )


def _model_allowlist_clause(client: ReviewClient) -> ColumnElement[bool]:
    """The model-allowlist gate: the job's repo must accept this client's declared model.

    One expression shared by both lease paths, for the reason `_strategy_overlap`
    is one: naming a job is not a way around a repo's allowlist any more than it
    is a way around its model floor.

    Read live off RepoPolicy rather than snapshotted onto the job the way
    `min_tier` and `requested_strategies` are. The two behave differently on
    purpose: a *tier* floor is a statement about the code as it was when the job
    was enqueued, while a group's membership is a statement about the operator's
    current fleet, and an operator who narrows a group must not have to re-PUT
    every policy that references it before the narrowing takes effect.

    Group membership resolves in Python — `settings.model_groups` is process
    config, not a table, so there is nothing for SQL to join against. What
    reaches Postgres is the already-resolved list of groups this client's model
    belongs to, tested against the policy's `accepted_model_groups` with `?|`
    ("holds any of these"), beside a `?` ("holds this") on `accepted_models`.

    Same rule, expressed in plain Python for the one caller that already has the
    policy loaded and isn't running a query: `models.repo_policy.model_allowed`,
    used by `api.jobs`'s targeted-lease pre-check. Change the matching semantics
    in one, change them in the other.
    """
    matching_groups = [name for name, members in settings.model_groups.items() if client.model_name in members]
    accepted_models = _live_policy_list(col(RepoPolicy.accepted_models))
    accepted_groups = _live_policy_list(col(RepoPolicy.accepted_model_groups))

    # Both empty is the match-any sentinel, mirroring `model_allowed`'s first
    # branch. `sa.false()` for the group half when the client's model is in no
    # group at all: there is nothing to test `?|` against, and an empty array
    # literal would be a cast Postgres has no type for here.
    return sa.or_(
        sa.and_(
            sa.func.jsonb_array_length(accepted_models) == 0,
            sa.func.jsonb_array_length(accepted_groups) == 0,
        ),
        accepted_models.op("?")(client.model_name),
        accepted_groups.op("?|")(sa.cast(matching_groups, postgresql.ARRAY(sa.Text)))
        if matching_groups
        else sa.false(),
    )


async def _filter_to_access(session: AsyncSession, identity_id: UUID | None, query: Select) -> Select:
    """AND the caller's access set onto a ReviewJob query, or leave it untouched.

    The one place `lease_next_job`, `lease_specific_job`, and `list_jobs` apply
    the access-set filter, so the three call sites can't drift into three
    slightly different where-clauses. ANDs with any other repo_full_name filter
    already on `query` — asking for a repo outside the access set is an
    ordinary empty result, not a special case.

    Takes the identity rather than the client because a dashboard session is a
    caller with an identity and no client row; see `identity_service.ScopedCaller`.
    """
    access = await accessible_repo_names(session, identity_id)
    if access is not None:
        query = query.where(col(ReviewJob.repo_full_name).in_(access))
    return query


async def enqueue_job(
    session: AsyncSession,
    spec: ReviewJobBase,
    policy: RepoPolicy | None,
) -> ReviewJob | None:
    """Create a job for a PR head, deduplicating active work.

    Returns None when the repo's policy is disabled or an active job for the
    same repo/PR/sha already exists (webhook redeliveries, force-push echoes).
    """
    if policy is not None and not policy.enabled:
        return None

    result = await session.execute(
        select(ReviewJob)
        .where(col(ReviewJob.repo_full_name) == spec.repo_full_name)
        .where(col(ReviewJob.pr_number) == spec.pr_number)
        .where(col(ReviewJob.head_sha) == spec.head_sha)
        .where(col(ReviewJob.state).in_([s.value for s in ACTIVE_STATES]))
        .limit(1)
    )
    if result.scalar_one_or_none() is not None:
        return None

    job = ReviewJob(
        **spec.model_dump(),
        min_tier=policy.min_tier if policy is not None else ModelTier.EXPERIMENTAL,
        requested_strategies=policy.default_strategies if policy is not None else [],
    )
    session.add(job)
    await session.flush()  # type: ignore[attr-defined]
    await session.refresh(job)
    return job


async def cancel_queued_jobs_for_pr(session: AsyncSession, repo_full_name: str, pr_number: int) -> int:
    """Cancel still-queued work for a PR, returning how many jobs were cancelled.

    Called when a PR closes. Leased and reported jobs are deliberately left
    alone: a round already in flight should finish and report rather than
    vanish from under its client.
    """
    result = await session.execute(
        update(ReviewJob)
        .where(col(ReviewJob.repo_full_name) == repo_full_name)
        .where(col(ReviewJob.pr_number) == pr_number)
        .where(col(ReviewJob.state) == JobState.QUEUED.value)
        .values(state=JobState.CANCELLED.value)
    )
    return result.rowcount or 0


async def _transition_leased_jobs(session: AsyncSession, key_predicate: ColumnElement[bool]) -> int:
    """Requeue (or exhaust) every LEASED job matching `key_predicate`.

    Shared by `reclaim_expired_leases` and `release_leased_jobs_for_client`,
    which differ only in what identifies the leases to release — an expired
    clock vs. a specific client. Keeping the two-step requeue-else-exhaust
    transition in one place means a future change to it (a new terminal
    state, a different attempts comparison, another column to clear) cannot
    be applied to one caller and silently forgotten on the other.
    """
    requeued = await session.execute(
        update(ReviewJob)
        .where(col(ReviewJob.state) == JobState.LEASED.value)
        .where(key_predicate)
        .where(col(ReviewJob.attempts) < col(ReviewJob.max_attempts))
        .values(state=JobState.QUEUED.value, leased_by=None, lease_expires_at=None)
    )
    await session.execute(
        update(ReviewJob)
        .where(col(ReviewJob.state) == JobState.LEASED.value)
        .where(key_predicate)
        .values(state=JobState.EXHAUSTED.value, leased_by=None, lease_expires_at=None)
    )
    return requeued.rowcount or 0


async def reclaim_expired_leases(session: AsyncSession) -> int:
    """Requeue leased jobs whose lease has lapsed; exhaust ones out of attempts."""
    now = datetime.now(UTC)
    return await _transition_leased_jobs(session, col(ReviewJob.lease_expires_at) < now)


async def release_leased_jobs_for_client(session: AsyncSession, client_id: UUID) -> int:
    """Requeue (or exhaust) every job this client currently holds a lease on.

    The revocation counterpart to `reclaim_expired_leases`: the identical
    two-step transition, keyed on `leased_by` instead of an expired clock,
    because a revoked client's lease must not wait out its TTL. "This machine
    is gone" is a present-tense claim, and answering it by letting the queue
    sit on work nobody is doing until `LEASE_TTL_SECONDS` elapses answers it in
    the future tense.

    Still exhausts a job that is out of attempts rather than requeueing
    unconditionally, for the same reason the expiry path does: a client that
    revokes itself mid-round must not reset the attempt budget, or a job that
    can never succeed gets dispatched forever.

    Must run *before* the client row is deleted. `review_job.leased_by` is
    ON DELETE SET NULL, so once the delete lands there is nothing left to
    filter on and the lease is orphaned in the LEASED state instead.
    """
    return await _transition_leased_jobs(session, col(ReviewJob.leased_by) == client_id)


async def reported_jobs_for_client(session: AsyncSession, client_id: UUID) -> list[UUID]:
    """Ids of this client's completed rounds whose `reported_by` a hard delete will null.

    Read-only, and a precursor to deleting the client rather than part of it:
    `authorize_client_revoke` calls this while the ids are still resolvable and
    logs them, because `review_job.reported_by` is ON DELETE SET NULL and once
    the delete runs there is nothing left to find.

    The verdict itself survives — summary, findings, and the posted PR comment
    all live on the job row, independent of which machine produced them. What
    is lost is the attribution. That loss is accepted (blocking revocation on
    review history would contradict self-service revocation), but it is not
    allowed to be silent.
    """
    result = await session.execute(
        select(ReviewJob.id)
        .where(col(ReviewJob.reported_by) == client_id)
        .where(col(ReviewJob.state).in_([JobState.REPORTED.value, JobState.RELAYED.value]))
    )
    return list(result.scalars().all())


async def lease_next_job(session: AsyncSession, client: ReviewClient) -> ReviewJob | None:
    """Hand the oldest eligible queued job to a client.

    Eligibility is five independent gates. The policy floor: the job's min_tier
    must be at or below the client's declared tier. The model allowlist: the
    repo must accept the client's declared model, by name or through a group.
    The strategy contract: the job's requested_strategies must be empty or
    overlap what the client offers. Access: the job's repo must be one the
    client's GitHub account can reach. And freshness: that access snapshot must
    not be older than its TTL. All five are resolved here rather than passed in,
    so no caller can hand this function a wider scope than the client actually
    has.

    FOR UPDATE SKIP LOCKED keeps concurrent leases from colliding.

    Raises:
        StaleIdentityAccessError: the access snapshot is past its TTL. Distinct
            from returning None, which means "nothing to give you": the caller
            has to be able to say "check in again" rather than "queue is dry".
    """
    await reclaim_expired_leases(session)
    await _refuse_if_access_stale(session, client)

    eligible_tiers = [t.value for t in tiers_at_or_below(client.tier)]
    query = (
        select(ReviewJob)
        .where(col(ReviewJob.state) == JobState.QUEUED.value)
        .where(col(ReviewJob.min_tier).in_(eligible_tiers))  # gate 1: tier floor
        .where(_model_allowlist_clause(client))  # gate 2: model allowlist (RFC 0003 A4)
        .where(_strategy_overlap(client))  # gate 3: strategy contract (RFC 0003 A2)
        .order_by(col(ReviewJob.created_at))
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    query = await _filter_to_access(session, client.identity_id, query)

    result = await session.execute(query)
    job = result.scalar_one_or_none()
    if job is None:
        return None

    return await _hand_over(session, job, client)


async def lease_specific_job(session: AsyncSession, client: ReviewClient, job_id: UUID) -> ReviewJob | None:
    """Lease one named job, or None when it is not this client's to take.

    Backs "pick this one" flows (dashboard selection, MCP clients) where the
    caller already knows which job it wants. The eligibility rules are the
    same as `lease_next_job` — naming a job is not a way around a repo's model
    floor, nor its model allowlist, nor its strategy contract, nor around its
    access set, nor around that set's TTL — and the state check lives inside the
    locking SELECT so two callers racing for the same job resolve to exactly one
    winner.

    Raises:
        StaleIdentityAccessError: the access snapshot is past its TTL.
    """
    await reclaim_expired_leases(session)
    await _refuse_if_access_stale(session, client)

    eligible_tiers = [t.value for t in tiers_at_or_below(client.tier)]
    query = (
        select(ReviewJob)
        .where(col(ReviewJob.id) == job_id)
        .where(col(ReviewJob.state) == JobState.QUEUED.value)
        .where(col(ReviewJob.min_tier).in_(eligible_tiers))  # gate 1: tier floor
        .where(_model_allowlist_clause(client))  # gate 2: model allowlist (RFC 0003 A4)
        .where(_strategy_overlap(client))  # gate 3: strategy contract (RFC 0003 A2)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    query = await _filter_to_access(session, client.identity_id, query)

    result = await session.execute(query)
    job = result.scalar_one_or_none()
    if job is None:
        return None

    return await _hand_over(session, job, client)


async def _hand_over(session: AsyncSession, job: ReviewJob, client: ReviewClient) -> ReviewJob:
    """Mark a job leased by a client and start its lease clock."""
    job.state = JobState.LEASED
    job.leased_by = client.id
    job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=settings.lease_ttl_seconds)
    job.attempts += 1
    session.add(job)
    await session.flush()  # type: ignore[attr-defined]
    await session.refresh(job)
    return job


async def report_job(
    session: AsyncSession,
    job: ReviewJob,
    client: ReviewClient,
    payload: ReviewJobReport,
) -> ReviewJob:
    """Record a leaseholder's completed round. Caller must verify the lease."""
    job.state = JobState.REPORTED
    job.verdict = payload.verdict
    job.summary = payload.summary
    job.findings = payload.findings
    job.reported_by = client.id
    job.leased_by = None
    job.lease_expires_at = None
    session.add(job)
    await session.flush()  # type: ignore[attr-defined]
    await session.refresh(job)
    return job


async def get_job(session: AsyncSession, job_id: UUID) -> ReviewJob | None:
    """Fetch a job by id with no authorization applied at all.

    Only for callers whose authorization is something other than repo access —
    `report_job_endpoint` gates on holding the lease. Anything answering a
    *read* wants `get_job_for_identity`.
    """
    result = await session.execute(select(ReviewJob).where(col(ReviewJob.id) == job_id))
    return result.scalar_one_or_none()


async def get_job_for_identity(session: AsyncSession, identity_id: UUID | None, job_id: UUID) -> ReviewJob | None:
    """Fetch a job this identity is allowed to see, or None.

    The 404 boundary: callers must **not** distinguish its two None cases.
    "No such job" and "a real job in a repo you cannot reach" have to answer
    identically, or the endpoint becomes an oracle that confirms which job ids
    exist — and with them, the repo layout of every other org on the hub.

    Renamed from `get_job_for_client` in B1 (#24): the caller may now be a
    dashboard session rather than a machine, and a name that says "client" would
    have invited a second copy of this function for the browser — which is
    exactly how two answers to one 404 boundary get born.
    """
    job = await get_job(session, job_id)
    if job is None:
        return None
    access = await accessible_repo_names(session, identity_id)
    if access is not None and job.repo_full_name not in access:
        return None
    return job


async def list_jobs(
    session: AsyncSession,
    identity_id: UUID | None,
    filters: ReviewJobFilters | None = None,
) -> list[ReviewJob]:
    """The job feed, scoped to what this identity may see.

    `identity_id` is a required positional rather than an optional keyword: an
    access filter you can forget to pass is one an endpoint will eventually
    forget to pass. None is a real value here (a caller with no GitHub account),
    not "unscoped" — `accessible_repo_names` fails it closed.
    """
    filters = filters or ReviewJobFilters()
    query = select(ReviewJob).order_by(col(ReviewJob.created_at).desc()).offset(filters.offset).limit(filters.limit)
    if filters.state is not None:
        query = query.where(col(ReviewJob.state) == filters.state.value)
    if filters.repo_full_name is not None:
        query = query.where(col(ReviewJob.repo_full_name) == filters.repo_full_name)
    query = await _filter_to_access(session, identity_id, query)
    result = await session.execute(query)
    return list(result.scalars().all())
