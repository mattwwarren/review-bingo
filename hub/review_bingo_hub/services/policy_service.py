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
    """Full-replace `min_tier`/`enabled` from the payload; `default_strategies` is the one exception.

    `default_strategies` uses the same omitted-vs-explicit-empty distinction the
    check-in path already gives `offered_strategies` (`None` = not mentioned,
    leave alone; `[]` = an explicit clear): folding it into the blind
    field-by-field replace below would mean any PUT that only wants to bump
    `min_tier` silently wipes a repo's strategy gate back to match-any.
    """
    other_fields = payload.model_dump(exclude={"default_strategies"})
    policy = await get_policy(session, repo_full_name)
    if policy is None:
        policy = RepoPolicy(
            repo_full_name=repo_full_name,
            default_strategies=payload.default_strategies if payload.default_strategies is not None else [],
            **other_fields,
        )
    else:
        for field, value in other_fields.items():
            setattr(policy, field, value)
        if payload.default_strategies is not None:
            policy.default_strategies = payload.default_strategies
    session.add(policy)
    await session.flush()  # type: ignore[attr-defined]
    await session.refresh(policy)
    return policy


async def list_policies(
    session: AsyncSession,
    offset: int = 0,
    limit: int = 100,
    repo_names: frozenset[str] | None = None,
) -> list[RepoPolicy]:
    """List policies, optionally narrowed to a caller's accessible repos.

    The filter is applied in SQL, before offset/limit. Filtering the
    already-paged list in Python would hand back fewer than `limit` rows while
    more still existed for this caller — pagination silently broken for anyone
    with partial access, which is everyone once scoping is on.

    `repo_names=None` means "no filtering", matching what
    identity_service.caller_accessible_repo_names returns in dev mode; an empty
    set means "nothing visible" and is not the same thing.
    """
    if repo_names is not None and not repo_names:
        return []
    query = select(RepoPolicy)
    if repo_names is not None:
        query = query.where(col(RepoPolicy.repo_full_name).in_(repo_names))
    query = query.order_by(col(RepoPolicy.repo_full_name)).offset(offset).limit(limit)
    result = await session.execute(query)
    return list(result.scalars().all())
