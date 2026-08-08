"""Review job endpoints: lease, report, inspect.

The lease/report pair is the product's spine. A checked-in client leases the
oldest queued job whose policy floor it clears, runs its round however it
likes, and reports back; the hub relays the result to the PR.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse

from review_bingo_hub.api.clients import ClientDep, ScopedCallerDep
from review_bingo_hub.db.session import SessionDep
from review_bingo_hub.models.review_client import ClientStatus, tiers_at_or_below
from review_bingo_hub.models.review_job import (
    JobState,
    ReviewJobFilters,
    ReviewJobLease,
    ReviewJobRead,
    ReviewJobReport,
)
from review_bingo_hub.services.client_service import touch_client
from review_bingo_hub.services.job_service import (
    StaleIdentityAccessError,
    get_job,
    get_job_for_identity,
    lease_next_job,
    lease_specific_job,
    list_jobs,
    report_job,
)
from review_bingo_hub.services.relay_service import relay_result, relay_target, render_comment

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Reads take ScopedCallerDep (a grid client *or* a signed-in dashboard); lease
# and report keep ClientDep. The split is the read/write boundary B1 (#24) drew:
# a browser session may see any job its GitHub account can already see, but
# taking work and reporting on it are things a registered machine does, and a
# lease handed to a session would be a lease nothing can ever report against.


@router.post("/lease", response_model=ReviewJobLease | None)
async def lease_job_endpoint(session: SessionDep, client: ClientDep) -> ReviewJobLease | None:
    """Lease the next eligible job; null body when the queue is dry.

    Requires the client to be checked in — checking in is the grid's
    availability signal, and leasing while checked out would defeat it.

    Cached GitHub access past its TTL is refused with the same 409, deliberately:
    "check in again" is the same *kind* of answer as "check in first", and a
    client that already handles one needs no new branch for the other.
    """
    if client.status != ClientStatus.CHECKED_IN:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Check in before leasing")

    await touch_client(session, client)
    try:
        job = await lease_next_job(session, client)
    except StaleIdentityAccessError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc
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

    # A job outside this client's access set answers 404, exactly as a job that
    # never existed does — the access check must not confirm the id is real.
    # The tier check stays *after* it: a floor you could clear with a better
    # model is worth explaining, unlike a repo you cannot see.
    job = await get_job_for_identity(session, identity_id=client.identity_id, job_id=job_id)
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
    try:
        leased = await lease_specific_job(session, client, job_id)
    except StaleIdentityAccessError as exc:
        # 409 rather than the 404 an out-of-access job gets: the caller can
        # already see this repo, so there is no existence left to protect — only
        # a refresh to ask for.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc
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

    Deliberately the unscoped `get_job`: authorization here is holding the
    lease, checked below. A client that legitimately leased a job must still be
    able to report on it even if its access snapshot has since narrowed —
    refusing the report would lose finished work, not protect anything.
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
    caller: ScopedCallerDep,
    filters: Annotated[ReviewJobFilters, Depends()],
) -> list[ReviewJobRead]:
    """Job feed for the dashboard, newest first — only repos this caller can reach."""
    jobs = await list_jobs(session, caller.identity_id, filters)
    return [ReviewJobRead.model_validate(j) for j in jobs]


@router.get("/{job_id}", response_model=ReviewJobRead)
async def get_job_endpoint(job_id: UUID, session: SessionDep, caller: ScopedCallerDep) -> ReviewJobRead:
    job = await get_job_for_identity(session, identity_id=caller.identity_id, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return ReviewJobRead.model_validate(job)


@router.get("/{job_id}/comment", response_class=PlainTextResponse)
async def job_comment_endpoint(job_id: UUID, session: SessionDep, caller: ScopedCallerDep) -> str:
    """The PR comment for a reported job — what the relay posts (or posted)."""
    job = await get_job_for_identity(session, identity_id=caller.identity_id, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.verdict is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job has no report yet")
    return render_comment(job)


@router.get("/{job_id}/relay-target")
async def relay_target_endpoint(job_id: UUID, session: SessionDep, caller: ScopedCallerDep) -> dict[str, Any]:
    """Where this job's result goes (or went): github vs log mode."""
    job = await get_job_for_identity(session, identity_id=caller.identity_id, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return relay_target(job)
