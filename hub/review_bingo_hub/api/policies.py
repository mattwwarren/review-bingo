"""Repo policy endpoints: the model-floor knob, and who may turn it.

Setting a repo's minimum model tier is an owner's decision, so a write takes
GitHub-recorded `admin` on that repo — push access is not enough. Reads are
scoped to what the caller can already see, and a repo outside that set answers
exactly like a repo with no policy: a distinguishable refusal would make these
endpoints an oracle for which repos exist on the hub.

Under CLIENT_ENROLMENT_MODE=dev the shared enrolment secret stands in for the
admin check — the same carve-out POST /clients has, not a second one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from review_bingo_hub.db.session import SessionDep
from review_bingo_hub.models.repo_policy import RepoPolicyRead, RepoPolicyUpsert
from review_bingo_hub.services.identity_service import (
    EnrolmentDeniedError,
    PolicyCallerUnauthenticatedError,
    PolicyWriteForbiddenError,
    authorize_policy_write,
    caller_accessible_repo_names,
)
from review_bingo_hub.services.policy_service import get_policy, list_policies, upsert_policy

router = APIRouter(prefix="/policies", tags=["policies"])

_bearer = HTTPBearer(auto_error=False)

NO_POLICY_DETAIL = "No policy for this repo"


@asynccontextmanager
async def _map_policy_auth_errors() -> AsyncIterator[None]:
    """Translate the identity_service domain errors both dependencies below can raise.

    One mapping in one place: `PolicyCallerUnauthenticatedError`/`EnrolmentDeniedError`
    both mean "this credential resolves to nobody" (401) regardless of which
    dependency hit them, so neither should carry its own copy of the mapping.
    """
    try:
        yield
    except (PolicyCallerUnauthenticatedError, EnrolmentDeniedError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.detail) from exc


async def get_policy_write_authorization(
    owner: str,
    repo: str,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Refuse the request unless this caller administers this repo."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Policy write credential required")
    async with _map_policy_auth_errors():
        try:
            await authorize_policy_write(session, credentials.credentials, f"{owner}/{repo}")
        except PolicyWriteForbiddenError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.detail) from exc


RepoAdminDep = Annotated[None, Depends(get_policy_write_authorization)]


async def get_caller_accessible_repos(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> frozenset[str] | None:
    """The repo set to narrow policy reads to; None means no narrowing (dev mode)."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Policy read credential required")
    async with _map_policy_auth_errors():
        return await caller_accessible_repo_names(session, credentials.credentials)


CallerAccessibleReposDep = Annotated[frozenset[str] | None, Depends(get_caller_accessible_repos)]


@router.put("/{owner}/{repo}", response_model=RepoPolicyRead)
async def upsert_policy_endpoint(
    owner: str,
    repo: str,
    payload: RepoPolicyUpsert,
    session: SessionDep,
    _authorized: RepoAdminDep,
) -> RepoPolicyRead:
    """Set (or update) a repo's dispatch policy, e.g. its minimum model tier.

    Requires repo admin. `_authorized` carries no value — it is here so the
    check runs as a dependency, before the handler body and before any write.
    """
    policy = await upsert_policy(session, f"{owner}/{repo}", payload)
    await session.commit()
    return RepoPolicyRead.model_validate(policy)


@router.get("/{owner}/{repo}", response_model=RepoPolicyRead)
async def get_policy_endpoint(
    owner: str,
    repo: str,
    session: SessionDep,
    accessible: CallerAccessibleReposDep,
) -> RepoPolicyRead:
    """A repo's policy, if the caller can see the repo at all.

    Invisible repo and absent policy share one branch and one message on
    purpose: answering them differently would confirm a repo's existence to
    anyone who can enrol.
    """
    repo_full_name = f"{owner}/{repo}"
    if accessible is not None and repo_full_name not in accessible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NO_POLICY_DETAIL)
    policy = await get_policy(session, repo_full_name)
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NO_POLICY_DETAIL)
    return RepoPolicyRead.model_validate(policy)


@router.get("", response_model=list[RepoPolicyRead])
async def list_policies_endpoint(
    session: SessionDep,
    accessible: CallerAccessibleReposDep,
    offset: int = 0,
    limit: int = 100,
) -> list[RepoPolicyRead]:
    policies = await list_policies(session, offset=offset, limit=limit, repo_names=accessible)
    return [RepoPolicyRead.model_validate(p) for p in policies]
