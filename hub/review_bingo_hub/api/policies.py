"""Repo policy endpoints: the model-floor knob.

v1 leaves these unauthenticated like the rest of the hub's admin surface;
lock down before any real deployment (see PITCH.md open questions).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from review_bingo_hub.db.session import SessionDep
from review_bingo_hub.models.repo_policy import RepoPolicyRead, RepoPolicyUpsert
from review_bingo_hub.services.policy_service import get_policy, list_policies, upsert_policy

router = APIRouter(prefix="/policies", tags=["policies"])


@router.put("/{owner}/{repo}", response_model=RepoPolicyRead)
async def upsert_policy_endpoint(
    owner: str,
    repo: str,
    payload: RepoPolicyUpsert,
    session: SessionDep,
) -> RepoPolicyRead:
    """Set (or update) a repo's dispatch policy, e.g. its minimum model tier."""
    policy = await upsert_policy(session, f"{owner}/{repo}", payload)
    await session.commit()
    return RepoPolicyRead.model_validate(policy)


@router.get("/{owner}/{repo}", response_model=RepoPolicyRead)
async def get_policy_endpoint(owner: str, repo: str, session: SessionDep) -> RepoPolicyRead:
    policy = await get_policy(session, f"{owner}/{repo}")
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No policy for this repo")
    return RepoPolicyRead.model_validate(policy)


@router.get("", response_model=list[RepoPolicyRead])
async def list_policies_endpoint(session: SessionDep, offset: int = 0, limit: int = 100) -> list[RepoPolicyRead]:
    policies = await list_policies(session, offset=offset, limit=limit)
    return [RepoPolicyRead.model_validate(p) for p in policies]
