"""Unit tests for grid primitives: tier ordering, strategy vocabulary, webhook signatures, relay rendering."""

import hashlib
import hmac
from typing import ClassVar

import pytest

from review_bingo_hub.api.webhooks import verify_signature
from review_bingo_hub.models.repo_policy import _ModelAllowlistFields, model_allowed
from review_bingo_hub.models.review_client import (
    MODEL_TIER_RANK,
    ModelTier,
    tiers_at_or_below,
)
from review_bingo_hub.models.review_job import ReviewJob
from review_bingo_hub.models.review_strategy import STRATEGY_REGISTRY, strategies_overlap, validate_strategies
from review_bingo_hub.services.client_service import hash_token
from review_bingo_hub.services.relay_service import render_comment


class TestModelTiers:
    def test_rank_covers_every_tier(self) -> None:
        assert set(MODEL_TIER_RANK) == set(ModelTier)

    def test_frontier_clears_every_floor(self) -> None:
        assert set(tiers_at_or_below(ModelTier.FRONTIER)) == set(ModelTier)

    def test_experimental_clears_only_experimental(self) -> None:
        assert tiers_at_or_below(ModelTier.EXPERIMENTAL) == [ModelTier.EXPERIMENTAL]

    def test_standard_excludes_frontier(self) -> None:
        tiers = tiers_at_or_below(ModelTier.STANDARD)
        assert ModelTier.FRONTIER not in tiers
        assert ModelTier.STANDARD in tiers
        assert ModelTier.EXPERIMENTAL in tiers


class TestReviewStrategy:
    """The strategy vocabulary: four registry names, plus a `custom:<name>` escape hatch."""

    def test_registry_holds_the_four_named_strategies(self) -> None:
        assert set(STRATEGY_REGISTRY) == {"security", "shallow", "full-loop", "fix-and-reverify"}

    @pytest.mark.parametrize("name", ["security", "shallow", "full-loop", "fix-and-reverify"])
    def test_each_registry_name_passes_unchanged(self, name: str) -> None:
        assert validate_strategies([name]) == [name]

    def test_custom_escape_hatch_passes(self) -> None:
        assert validate_strategies(["custom:my-lens"]) == ["custom:my-lens"]

    def test_custom_prefix_with_empty_suffix_rejected(self) -> None:
        with pytest.raises(ValueError, match="custom:"):
            validate_strategies(["custom:"])

    def test_registry_match_is_case_sensitive(self) -> None:
        with pytest.raises(ValueError, match="Security"):
            validate_strategies(["Security"])

    def test_unknown_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="nonsense"):
            validate_strategies(["nonsense"])

    def test_empty_list_passes(self) -> None:
        """No strategies is the match-any sentinel, not a strategy needing validation."""
        assert validate_strategies([]) == []

    def test_one_bad_entry_fails_the_whole_list(self) -> None:
        with pytest.raises(ValueError, match="nonsense"):
            validate_strategies(["security", "nonsense"])


class TestStrategiesOverlap:
    """The Python side of the strategy gate -- api/jobs.py's targeted-lease pre-check.

    job_service._strategy_overlap expresses the identical rule as a Postgres
    ?| predicate; these cases pin the plain-Python semantics it must stay in
    lockstep with.
    """

    def test_empty_requested_matches_any_offered(self) -> None:
        assert strategies_overlap([], []) is True
        assert strategies_overlap([], ["shallow"]) is True

    def test_overlap_passes(self) -> None:
        assert strategies_overlap(["security", "full-loop"], ["full-loop"]) is True

    def test_no_overlap_blocks(self) -> None:
        assert strategies_overlap(["security"], ["shallow"]) is False

    def test_nonempty_requested_against_empty_offered_blocks(self) -> None:
        """A client offering nothing overlaps nothing -- not symmetric with the job-side sentinel."""
        assert strategies_overlap(["security"], []) is False


class TestRepoPolicyAllowlistDefaults:
    """Both allowlist fields default to empty, which is the match-any sentinel.

    Pinned at the Pydantic layer, not just in the migration: a repo that never
    mentioned an allowlist must keep leasing to every client it leased to
    before, and a `None` slipping in here would make `model_allowed` throw
    instead of matching.
    """

    def test_accepted_models_defaults_to_empty(self) -> None:
        assert _ModelAllowlistFields().accepted_models == []

    def test_accepted_model_groups_defaults_to_empty(self) -> None:
        assert _ModelAllowlistFields().accepted_model_groups == []


class TestModelAllowed:
    """The Python side of the model-allowlist gate -- api/jobs.py's targeted-lease pre-check.

    job_service._model_allowlist_clause expresses the identical rule as Postgres
    JSONB predicates; these cases pin the plain-Python semantics it must stay in
    lockstep with, exactly as TestStrategiesOverlap does for the strategy gate.
    """

    GROUPS: ClassVar[dict[str, list[str]]] = {"frontier": ["claude-opus-4", "gpt-5"], "local": ["qwen2.5-coder"]}

    def test_both_empty_matches_any_model(self) -> None:
        assert model_allowed([], [], "anything-at-all", self.GROUPS) is True

    def test_named_model_passes(self) -> None:
        assert model_allowed(["claude-opus-4"], [], "claude-opus-4", self.GROUPS) is True

    def test_unnamed_model_blocked(self) -> None:
        assert model_allowed(["claude-opus-4"], [], "qwen2.5-coder", self.GROUPS) is False

    def test_group_membership_passes(self) -> None:
        assert model_allowed([], ["frontier"], "gpt-5", self.GROUPS) is True

    def test_non_member_of_named_group_blocked(self) -> None:
        assert model_allowed([], ["frontier"], "qwen2.5-coder", self.GROUPS) is False

    def test_effective_allowlist_is_the_union(self) -> None:
        """Either half may admit a client -- the two fields are OR'd, not AND'd."""
        assert model_allowed(["qwen2.5-coder"], ["frontier"], "qwen2.5-coder", self.GROUPS) is True
        assert model_allowed(["qwen2.5-coder"], ["frontier"], "gpt-5", self.GROUPS) is True
        assert model_allowed(["qwen2.5-coder"], ["frontier"], "mistral-7b", self.GROUPS) is False

    def test_group_with_no_definition_admits_nobody(self) -> None:
        """A group the operator has since deleted narrows the gate; it must not widen it.

        Fail-closed on absence, the opposite of the empty-list sentinel: `[]`
        means "no allowlist was declared", while `["frontier"]` against an empty
        registry means "a declared allowlist that currently matches nothing".
        """
        assert model_allowed([], ["frontier"], "claude-opus-4", {}) is False

    def test_group_resolution_reads_the_registry_it_is_handed(self) -> None:
        """Re-pointing a group changes who clears the same unchanged policy row."""
        assert model_allowed([], ["frontier"], "model-a", {"frontier": ["model-a"]}) is True
        assert model_allowed([], ["frontier"], "model-a", {"frontier": ["model-b"]}) is False


class TestWebhookSignature:
    SECRET = "test-webhook-secret"
    BODY = b'{"action": "opened"}'

    def _sign(self, body: bytes, secret: str) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_valid_signature_accepted(self) -> None:
        signature = self._sign(self.BODY, self.SECRET)
        assert verify_signature(self.BODY, signature, self.SECRET) is True

    def test_wrong_secret_rejected(self) -> None:
        signature = self._sign(self.BODY, "some-other-secret")
        assert verify_signature(self.BODY, signature, self.SECRET) is False

    def test_tampered_body_rejected(self) -> None:
        signature = self._sign(self.BODY, self.SECRET)
        assert verify_signature(b'{"action": "closed"}', signature, self.SECRET) is False

    def test_missing_header_rejected(self) -> None:
        assert verify_signature(self.BODY, None, self.SECRET) is False


class TestTokenHashing:
    def test_hash_is_sha256_hex(self) -> None:
        assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()

    def test_distinct_tokens_distinct_hashes(self) -> None:
        assert hash_token("token-a") != hash_token("token-b")


class TestRenderComment:
    def _job(self, **overrides: object) -> ReviewJob:
        defaults: dict[str, object] = {
            "repo_full_name": "acme/payments",
            "pr_number": 7,
            "head_sha": "abcdef1234567890",
            "event_action": "opened",
            "verdict": "findings",
            "summary": "Two issues worth a look.",
            "findings": [
                {"file": "src/pay.py", "line": 42, "title": "Unvalidated amount"},
                {"title": "Missing test for refund path"},
            ],
        }
        defaults.update(overrides)
        return ReviewJob(**defaults)

    def test_comment_includes_verdict_summary_and_findings(self) -> None:
        comment = render_comment(self._job())
        assert "review-bingo round" in comment
        assert "`findings`" in comment
        assert "Two issues worth a look." in comment
        assert "src/pay.py:42" in comment
        assert "Missing test for refund path" in comment
        assert "abcdef123456" in comment  # truncated head sha

    def test_comment_without_findings_omits_findings_section(self) -> None:
        comment = render_comment(self._job(findings=[], verdict="approve", summary="Clean round."))
        assert "### Findings" not in comment
        assert "`approve`" in comment
