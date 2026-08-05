"""Review job endpoints: lease, report, inspect.

The lease/report pair is the product's spine. A checked-in client leases the
oldest queued job whose policy floor it clears, runs its round however it
likes, and reports back; the hub relays the result to the PR.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse

from review_bingo_hub.api.clients import ClientDep
from review_bingo_hub.db.session import SessionDep
from review_bingo_hub.models.review_client import ClientStatus, tiers_at_or_below
from review_bingo_hub.models.review_job import (
    JobState,
    ReviewJobLease,
    ReviewJobRead,
    ReviewJobReport,
)
from review_bingo_hub.services.client_service import touch_client
from review_bingo_hub.services.job_service import (
    get_job,
    lease_next_job,
    lease_specific_job,
    list_jobs,
    report_job,
)
from review_bingo_hub.services.relay_service import relay_result, relay_target, render_comment

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/lease", response_model=ReviewJobLease | None)
async def lease_job_endpoint(session: SessionDep, client: ClientDep) -> ReviewJobLease | None:
    """Lease the next eligible job; null body when the queue is dry.

    Requires the client to be checked in — checking in is the grid's
    availability signal, and leasing while checked out would defeat it.
    """
    if client.status != ClientStatus.CHECKED_IN:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Check in before leasing")

    await touch_client(session, client)
    job = await lease_next_job(session, client)
    await session.commit()

    if job is None:
        return None
    if job.lease_expires_at is None:  # pragma: no cover - lease always sets expiry
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Lease missing expiry")
    return ReviewJobLease(job=ReviewJobRead.model_validate(job), lease_expires_at=job.lease_expires_at)


@router.post("/{job_id}/lease", response_model=ReviewJobLease)
async def lease_specific_job_endpoint(job_id: UUID, session: SessionDep, client: ClientDep) -> ReviewJobLease:
    """Lease one named job — the "I picked this one" path.

    Unlike `/jobs/lease`, an unavailable job is an error rather than an empty
    body: the caller asked for something specific, so silence would be a worse
    answer than a reason.
    """
    if client.status != ClientStatus.CHECKED_IN:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Check in before leasing")

    job = await get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    # min_tier/tier are ModelTier at the type level but plain strings at runtime —
    # the columns are sa.String(), so SQLModel hands back what Postgres stored.
    # Membership still works (ModelTier is a StrEnum), but .value does not exist.
    if job.min_tier not in tiers_at_or_below(client.tier):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Job requires tier {str(job.min_tier)!r} or better; this client declares {str(client.tier)!r}",
        )

    await touch_client(session, client)
    leased = await lease_specific_job(session, client, job_id)
    await session.commit()

    if leased is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job is not available to lease (already leased, or no longer queued)",
        )
    if leased.lease_expires_at is None:  # pragma: no cover - lease always sets expiry
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Lease missing expiry")
    return ReviewJobLease(job=ReviewJobRead.model_validate(leased), lease_expires_at=leased.lease_expires_at)


@router.post("/{job_id}/report", response_model=ReviewJobRead)
async def report_job_endpoint(
    job_id: UUID,
    payload: ReviewJobReport,
    session: SessionDep,
    client: ClientDep,
) -> ReviewJobRead:
    """Submit a completed round for a job this client holds the lease on.

    The report is committed first, then relayed best-effort — a GitHub
    hiccup never loses a client's finished work.
    """
    job = await get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.state != JobState.LEASED or job.leased_by != client.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job is not leased to this client (lease may have expired and been reclaimed)",
        )

    await touch_client(session, client)
    job = await report_job(session, job, client, payload)
    await session.commit()

    relay_error = await relay_result(job)
    job.relay_error = relay_error
    if relay_error is None:
        job.state = JobState.RELAYED
    session.add(job)
    await session.commit()
    await session.refresh(job)

    return ReviewJobRead.model_validate(job)


@router.get("", response_model=list[ReviewJobRead])
async def list_jobs_endpoint(
    session: SessionDep,
    state: JobState | None = None,
    repo_full_name: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> list[ReviewJobRead]:
    """Job feed for the dashboard, newest first."""
    jobs = await list_jobs(session, state=state, repo_full_name=repo_full_name, offset=offset, limit=limit)
    return [ReviewJobRead.model_validate(j) for j in jobs]


@router.get("/{job_id}", response_model=ReviewJobRead)
async def get_job_endpoint(job_id: UUID, session: SessionDep) -> ReviewJobRead:
    job = await get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return ReviewJobRead.model_validate(job)


@router.get("/{job_id}/comment", response_class=PlainTextResponse)
async def job_comment_endpoint(job_id: UUID, session: SessionDep) -> str:
    """The PR comment for a reported job — what the relay posts (or posted)."""
    job = await get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.verdict is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job has no report yet")
    return render_comment(job)


@router.get("/{job_id}/relay-target")
async def relay_target_endpoint(job_id: UUID, session: SessionDep) -> dict[str, Any]:
    """Where this job's result goes (or went): github vs log mode."""
    job = await get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return relay_target(job)
