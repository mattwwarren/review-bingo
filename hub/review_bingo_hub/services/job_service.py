"""Review job dispatch: enqueue from webhooks, lease to clients, accept reports.

Concurrency: leasing uses SELECT ... FOR UPDATE SKIP LOCKED so concurrent
clients never race for the same job. Expired leases are reclaimed lazily at
the top of every lease request — no background worker in v1.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.core.config import settings
from review_bingo_hub.models.repo_policy import RepoPolicy
from review_bingo_hub.models.review_client import ModelTier, ReviewClient, tiers_at_or_below
from review_bingo_hub.models.review_job import JobState, ReviewJob, ReviewJobBase, ReviewJobReport

ACTIVE_STATES = (JobState.QUEUED, JobState.LEASED)


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


async def reclaim_expired_leases(session: AsyncSession) -> int:
    """Requeue leased jobs whose lease has lapsed; exhaust ones out of attempts."""
    now = datetime.now(UTC)
    requeued = await session.execute(
        update(ReviewJob)
        .where(col(ReviewJob.state) == JobState.LEASED.value)
        .where(col(ReviewJob.lease_expires_at) < now)
        .where(col(ReviewJob.attempts) < col(ReviewJob.max_attempts))
        .values(state=JobState.QUEUED.value, leased_by=None, lease_expires_at=None)
    )
    await session.execute(
        update(ReviewJob)
        .where(col(ReviewJob.state) == JobState.LEASED.value)
        .where(col(ReviewJob.lease_expires_at) < now)
        .values(state=JobState.EXHAUSTED.value, leased_by=None, lease_expires_at=None)
    )
    return requeued.rowcount or 0


async def lease_next_job(session: AsyncSession, client: ReviewClient) -> ReviewJob | None:
    """Hand the oldest eligible queued job to a client.

    Eligibility is the policy floor: the job's min_tier must be at or below
    the client's declared tier. FOR UPDATE SKIP LOCKED keeps concurrent
    leases from colliding.
    """
    await reclaim_expired_leases(session)

    eligible_tiers = [t.value for t in tiers_at_or_below(client.tier)]
    result = await session.execute(
        select(ReviewJob)
        .where(col(ReviewJob.state) == JobState.QUEUED.value)
        .where(col(ReviewJob.min_tier).in_(eligible_tiers))
        .order_by(col(ReviewJob.created_at))
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = result.scalar_one_or_none()
    if job is None:
        return None

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
    result = await session.execute(select(ReviewJob).where(col(ReviewJob.id) == job_id))
    return result.scalar_one_or_none()


async def list_jobs(
    session: AsyncSession,
    *,
    state: JobState | None = None,
    repo_full_name: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> list[ReviewJob]:
    query = select(ReviewJob).order_by(col(ReviewJob.created_at).desc()).offset(offset).limit(limit)
    if state is not None:
        query = query.where(col(ReviewJob.state) == state.value)
    if repo_full_name is not None:
        query = query.where(col(ReviewJob.repo_full_name) == repo_full_name)
    result = await session.execute(query)
    return list(result.scalars().all())
