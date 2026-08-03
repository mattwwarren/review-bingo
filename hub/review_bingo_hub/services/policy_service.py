"""Per-repo policy lookups and upserts."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.models.repo_policy import RepoPolicy, RepoPolicyUpsert


async def get_policy(session: AsyncSession, repo_full_name: str) -> RepoPolicy | None:
    result = await session.execute(select(RepoPolicy).where(col(RepoPolicy.repo_full_name) == repo_full_name))
    return result.scalar_one_or_none()


async def upsert_policy(session: AsyncSession, repo_full_name: str, payload: RepoPolicyUpsert) -> RepoPolicy:
    policy = await get_policy(session, repo_full_name)
    if policy is None:
        policy = RepoPolicy(repo_full_name=repo_full_name, **payload.model_dump())
    else:
        for field, value in payload.model_dump().items():
            setattr(policy, field, value)
    session.add(policy)
    await session.flush()  # type: ignore[attr-defined]
    await session.refresh(policy)
    return policy


async def list_policies(session: AsyncSession, offset: int = 0, limit: int = 100) -> list[RepoPolicy]:
    result = await session.execute(
        select(RepoPolicy).order_by(col(RepoPolicy.repo_full_name)).offset(offset).limit(limit)
    )
    return list(result.scalars().all())
