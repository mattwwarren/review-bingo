"""Unit tests for grid primitives: tier ordering, webhook signatures, relay rendering."""

import hashlib
import hmac

from review_bingo_hub.api.webhooks import verify_signature
from review_bingo_hub.models.review_client import (
    MODEL_TIER_RANK,
    ModelTier,
    tiers_at_or_below,
)
from review_bingo_hub.models.review_job import ReviewJob
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
