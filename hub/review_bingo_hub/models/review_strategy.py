"""The review strategy vocabulary: what kind of round a job wants, and a client runs.

The hub's second dispatch gate, and — like the model floor — a *matching*
vocabulary, not a prompting one. A strategy name says nothing about how a
review is conducted; it says only which jobs and which clients believe they are
talking about the same kind of work. What "security" means in practice stays
entirely client-side, exactly as PITCH.md requires.

Four names are registered because they are the ones the grid can coordinate on
today. `custom:<name>` is the escape hatch for everything else: a private lens
two cooperating parties agree on without waiting for this file to change. It is
deliberately unvalidated past its non-empty suffix — the hub has no way to know
what a private name means, and pretending otherwise would make it the arbiter
of review content it explicitly refuses to be.

Matching is exact and case-sensitive on purpose. A vocabulary that quietly
accepts "Security" for "security" is a vocabulary in which a repo owner can
believe a floor is set while every client sails past it.
"""

from __future__ import annotations

import re

STRATEGY_REGISTRY: frozenset[str] = frozenset(
    {
        "security",
        "shallow",
        "full-loop",
        "fix-and-reverify",
    }
)

CUSTOM_STRATEGY_PREFIX = "custom:"

# `.+` rather than `.*`: a bare "custom:" names nothing, so two parties cannot
# be agreeing on anything by sending it.
_CUSTOM_STRATEGY_PATTERN = re.compile(rf"{re.escape(CUSTOM_STRATEGY_PREFIX)}.+", re.DOTALL)


def validate_strategies(values: list[str]) -> list[str]:
    """Return `values` unchanged, or raise ValueError naming the first bad entry.

    Fail-closed on any element: one unrecognised name invalidates the whole
    list rather than being dropped from it, because a silently-dropped strategy
    is a job that leases to clients its owner never agreed to.

    An empty list is valid and means "no constraint" — it is the match-any
    sentinel, not itself a strategy.
    """
    for value in values:
        if value in STRATEGY_REGISTRY:
            continue
        if _CUSTOM_STRATEGY_PATTERN.fullmatch(value):
            continue
        registered = ", ".join(sorted(STRATEGY_REGISTRY))
        msg = f"Unknown review strategy {value!r}; expected one of {registered}, or {CUSTOM_STRATEGY_PREFIX}<name>"
        raise ValueError(msg)
    return values


def strategies_overlap(requested: list[str], offered: list[str]) -> bool:
    """The strategy gate's matching rule, in-process: empty `requested` is match-any, else any-overlap.

    `services.job_service._strategy_overlap` expresses this same rule as a
    Postgres JSONB predicate so `lease_next_job`/`lease_specific_job` can gate
    inside their locking `SELECT` without a second query; this function is the
    one other place the rule needs to run in plain Python — the targeted-lease
    endpoint's pre-check (`api/jobs.py`), which already has both lists loaded
    and would otherwise be a second, independent encoding of the same rule.
    Keep the two in lockstep by construction: change the semantics here, then
    carry the change into the SQL expression, not the other way around.
    """
    if not requested:
        return True
    return bool(set(requested) & set(offered))
