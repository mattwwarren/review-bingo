"""Per-repo policy lookups and upserts."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from review_bingo_hub.core.config import settings
from review_bingo_hub.models.repo_policy import RepoPolicy, RepoPolicyUpsert


class UnknownModelGroupError(ValueError):
    """A policy named a model group `Settings.model_groups` does not define.

    Defined here rather than in `core.config`: the *registry* lives there, but
    the rule that a policy may only reference a group already in it belongs to
    whoever writes RepoPolicy rows — matching how `StaleIdentityAccessError`
    sits in `job_service`, next to its raise site, with `api/policies.py`
    mapping it to a status code at the boundary.

    Caught at write time rather than at lease time on purpose. An undefined
    group resolves to nobody, so a typo'd name accepted here would leave a repo
    owner believing a floor is set while the queue silently went dry for every
    client — a failure that looks exactly like "no work available".
    """

    def __init__(self, unknown: list[str]) -> None:
        self.unknown = unknown
        self.detail = f"Unknown model group(s): {', '.join(unknown)}"
        super().__init__(self.detail)


async def get_policy(session: AsyncSession, repo_full_name: str) -> RepoPolicy | None:
    result = await session.execute(select(RepoPolicy).where(col(RepoPolicy.repo_full_name) == repo_full_name))
    return result.scalar_one_or_none()


async def upsert_policy(session: AsyncSession, repo_full_name: str, payload: RepoPolicyUpsert) -> RepoPolicy:
    """Full-replace `min_tier`/`enabled`; `default_strategies` and the model allowlist are the exceptions.

    `default_strategies`, `accepted_models`, and `accepted_model_groups` all
    get the same omitted-vs-explicit-empty distinction the check-in path
    already gives `offered_strategies` (`None` = not mentioned, leave alone;
    `[]` = an explicit clear): folding any of them into the blind
    field-by-field replace below would mean any PUT that only wants to bump
    `min_tier` silently wipes a repo's strategy gate, or its model allowlist,
    back to match-any.

    Raises:
        UnknownModelGroupError: the payload named a group `settings.model_groups`
            does not define. Checked first, before any row is fetched or
            mutated, so a rejected PUT leaves the stored policy exactly as it
            was rather than half-applied.
    """
    unknown_groups = [group for group in payload.accepted_model_groups or [] if group not in settings.model_groups]
    if unknown_groups:
        raise UnknownModelGroupError(unknown_groups)

    exempt_fields = {"default_strategies", "accepted_models", "accepted_model_groups"}
    other_fields = payload.model_dump(exclude=exempt_fields)
    policy = await get_policy(session, repo_full_name)
    if policy is None:
        policy = RepoPolicy(
            repo_full_name=repo_full_name,
            default_strategies=payload.default_strategies if payload.default_strategies is not None else [],
            accepted_models=payload.accepted_models if payload.accepted_models is not None else [],
            accepted_model_groups=(payload.accepted_model_groups if payload.accepted_model_groups is not None else []),
            **other_fields,
        )
    else:
        for field, value in other_fields.items():
            setattr(policy, field, value)
        if payload.default_strategies is not None:
            policy.default_strategies = payload.default_strategies
        if payload.accepted_models is not None:
            policy.accepted_models = payload.accepted_models
        if payload.accepted_model_groups is not None:
            policy.accepted_model_groups = payload.accepted_model_groups
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
