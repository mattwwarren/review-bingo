"""GitHub webhook intake: PR activity in, review jobs out.

The GitHub App is configured to deliver pull_request events here. Payloads
are verified against GITHUB_WEBHOOK_SECRET (X-Hub-Signature-256); when the
secret is unset — local dev only — verification is skipped with a warning.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from review_bingo_hub.core.config import settings
from review_bingo_hub.db.session import SessionDep
from review_bingo_hub.models.review_job import ReviewJobBase
from review_bingo_hub.services.job_service import cancel_queued_jobs_for_pr, enqueue_job
from review_bingo_hub.services.policy_service import get_policy

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# PR actions that represent new reviewable work
REVIEWABLE_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review", "review_requested"})


def verify_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Constant-time check of GitHub's X-Hub-Signature-256 header."""
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@router.post("/github")
async def github_webhook(
    request: Request,
    session: SessionDep,
    x_github_event: Annotated[str | None, Header()] = None,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Receive a GitHub webhook delivery.

    pull_request events with a reviewable action enqueue a job (deduplicated
    per repo/PR/head sha). Everything else is acknowledged and ignored.
    """
    body = await request.body()

    if settings.github_webhook_secret:
        if not verify_signature(body, x_hub_signature_256, settings.github_webhook_secret):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")
    else:
        LOGGER.warning("webhook_signature_skipped", extra={"reason": "GITHUB_WEBHOOK_SECRET not set"})

    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"event {x_github_event!r} not handled"}

    payload = await request.json()
    action = payload.get("action")

    if action == "closed":
        try:
            repo_full_name = payload["repository"]["full_name"]
            pr_number = payload["pull_request"]["number"]
        except (KeyError, TypeError) as err:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Malformed pull_request payload",
            ) from err
        cancelled = await cancel_queued_jobs_for_pr(session, repo_full_name, pr_number)
        await session.commit()
        LOGGER.info(
            "jobs_cancelled_on_pr_close",
            extra={"repo": repo_full_name, "pr_number": pr_number, "cancelled": cancelled},
        )
        return {"status": "cancelled", "cancelled": cancelled}

    if action not in REVIEWABLE_ACTIONS:
        return {"status": "ignored", "reason": f"action {action!r} not reviewable"}

    try:
        repo_full_name = payload["repository"]["full_name"]
        pull_request = payload["pull_request"]
        pr_number = pull_request["number"]
        head_sha = pull_request["head"]["sha"]
    except (KeyError, TypeError) as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Malformed pull_request payload",
        ) from err

    policy = await get_policy(session, repo_full_name)
    job = await enqueue_job(
        session,
        ReviewJobBase(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            event_action=action,
            pr_title=pull_request.get("title"),
        ),
        policy,
    )
    await session.commit()

    if job is None:
        return {"status": "skipped", "reason": "repo disabled or active job exists for this head"}

    LOGGER.info(
        "job_enqueued",
        extra={"job_id": str(job.id), "repo": repo_full_name, "pr_number": pr_number},
    )
    return {"status": "queued", "job_id": str(job.id)}
