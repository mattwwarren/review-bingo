"""Integration tests for the grid's spine: webhook → policy floor → lease → report → relay.

Relay runs in log mode here (no GitHub App credentials in test settings), so
the full loop exercises everything except the actual GitHub POST.
"""

import hashlib
import hmac
import json
from http import HTTPStatus
from typing import Any

import pytest
from httpx import AsyncClient

from review_bingo_hub.core.config import settings

PR_WEBHOOK_HEADERS = {"X-GitHub-Event": "pull_request"}


def pr_payload(
    repo: str = "acme/payments",
    number: int = 7,
    sha: str = "abcdef1234567890",
    action: str = "opened",
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {"number": number, "head": {"sha": sha}, "title": "Fix rounding"},
    }


async def register_and_check_in(client: AsyncClient, name: str, tier: str) -> tuple[str, dict[str, str]]:
    """Register a grid client of the given tier, check it in, return (id, auth headers)."""
    response = await client.post(
        "/clients",
        json={"name": name, "model_name": "test-model", "provider": "test", "tier": tier},
    )
    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    headers = {"Authorization": f"Bearer {body['token']}"}
    response = await client.post("/clients/check-in", headers=headers)
    assert response.status_code == HTTPStatus.OK
    return body["client"]["id"], headers


@pytest.mark.asyncio
async def test_webhook_enqueues_and_dedupes(client: AsyncClient) -> None:
    response = await client.post("/webhooks/github", json=pr_payload(sha="dedupe-sha-1"), headers=PR_WEBHOOK_HEADERS)
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "queued"
    job_id = response.json()["job_id"]

    # Redelivery of the same head: skipped, no second job
    response = await client.post("/webhooks/github", json=pr_payload(sha="dedupe-sha-1"), headers=PR_WEBHOOK_HEADERS)
    assert response.json()["status"] == "skipped"

    response = await client.get(f"/jobs/{job_id}")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["state"] == "queued"


@pytest.mark.asyncio
async def test_non_pr_events_ignored(client: AsyncClient) -> None:
    response = await client.post(
        "/webhooks/github", json={"zen": "Design for failure."}, headers={"X-GitHub-Event": "ping"}
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "ignored"

    response = await client.post("/webhooks/github", json=pr_payload(action="labeled"), headers=PR_WEBHOOK_HEADERS)
    assert response.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_policy_floor_blocks_low_tier_client(client: AsyncClient) -> None:
    repo = "acme/banking"
    response = await client.put(f"/policies/{repo}", json={"min_tier": "frontier"})
    assert response.status_code == HTTPStatus.OK
    assert response.json()["min_tier"] == "frontier"

    response = await client.post(
        "/webhooks/github", json=pr_payload(repo=repo, sha="floor-sha-1"), headers=PR_WEBHOOK_HEADERS
    )
    assert response.json()["status"] == "queued"

    # Experimental client clears no frontier floor: queue looks dry to it
    _, low_headers = await register_and_check_in(client, "toy-box", "experimental")
    response = await client.post("/jobs/lease", headers=low_headers)
    assert response.status_code == HTTPStatus.OK
    assert response.json() is None

    # Frontier client gets the job
    _, high_headers = await register_and_check_in(client, "big-rig", "frontier")
    response = await client.post("/jobs/lease", headers=high_headers)
    assert response.status_code == HTTPStatus.OK
    lease = response.json()
    assert lease is not None
    assert lease["job"]["repo_full_name"] == repo


@pytest.mark.asyncio
async def test_full_round_trip_report_relays_in_log_mode(client: AsyncClient) -> None:
    response = await client.post("/webhooks/github", json=pr_payload(sha="round-trip-sha"), headers=PR_WEBHOOK_HEADERS)
    assert response.json()["status"] == "queued"

    _, headers = await register_and_check_in(client, "marge-mac-mini", "standard")
    response = await client.post("/jobs/lease", headers=headers)
    lease = response.json()
    assert lease is not None
    job_id = lease["job"]["id"]
    assert lease["job"]["state"] == "leased"

    report = {
        "verdict": "findings",
        "summary": "One real bug.",
        "findings": [{"file": "src/pay.py", "line": 42, "title": "Unvalidated amount"}],
    }
    response = await client.post(f"/jobs/{job_id}/report", json=report, headers=headers)
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["state"] == "relayed"  # log-mode relay counts as delivered
    assert body["verdict"] == "findings"
    assert body["relay_error"] is None
    assert body["findings"][0]["line"] == 42

    response = await client.get(f"/jobs/{job_id}/relay-target")
    assert response.json()["mode"] == "log"

    response = await client.get(f"/jobs/{job_id}/comment")
    assert response.status_code == HTTPStatus.OK
    assert "review-bingo round" in response.text
    assert "src/pay.py:42" in response.text


@pytest.mark.asyncio
async def test_lease_requires_check_in(client: AsyncClient) -> None:
    response = await client.post(
        "/clients", json={"name": "lurker", "model_name": "m", "provider": "p", "tier": "standard"}
    )
    headers = {"Authorization": f"Bearer {response.json()['token']}"}

    response = await client.post("/jobs/lease", headers=headers)
    assert response.status_code == HTTPStatus.CONFLICT

    response = await client.post("/jobs/lease")
    assert response.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.asyncio
async def test_report_requires_lease_ownership(client: AsyncClient) -> None:
    response = await client.post("/webhooks/github", json=pr_payload(sha="ownership-sha"), headers=PR_WEBHOOK_HEADERS)
    assert response.json()["status"] == "queued"

    _, holder_headers = await register_and_check_in(client, "holder", "standard")
    response = await client.post("/jobs/lease", headers=holder_headers)
    job_id = response.json()["job"]["id"]

    _, interloper_headers = await register_and_check_in(client, "interloper", "standard")
    response = await client.post(
        f"/jobs/{job_id}/report",
        json={"verdict": "approve", "summary": "not mine"},
        headers=interloper_headers,
    )
    assert response.status_code == HTTPStatus.CONFLICT


@pytest.mark.asyncio
async def test_disabled_repo_queues_nothing(client: AsyncClient) -> None:
    repo = "acme/frozen"
    await client.put(f"/policies/{repo}", json={"min_tier": "experimental", "enabled": False})

    response = await client.post(
        "/webhooks/github", json=pr_payload(repo=repo, sha="frozen-sha"), headers=PR_WEBHOOK_HEADERS
    )
    assert response.json()["status"] == "skipped"


@pytest.mark.asyncio
async def test_webhook_signature_enforced_when_secret_set(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "grid-webhook-secret"
    monkeypatch.setattr(settings, "github_webhook_secret", secret)

    body = json.dumps(pr_payload(sha="signed-sha")).encode()
    response = await client.post("/webhooks/github", content=body, headers=PR_WEBHOOK_HEADERS)
    assert response.status_code == HTTPStatus.UNAUTHORIZED

    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    response = await client.post(
        "/webhooks/github",
        content=body,
        headers={**PR_WEBHOOK_HEADERS, "X-Hub-Signature-256": signature, "Content-Type": "application/json"},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_client_roster_lists_capabilities(client: AsyncClient) -> None:
    await register_and_check_in(client, "roster-client", "frontier")
    response = await client.get("/clients")
    assert response.status_code == HTTPStatus.OK
    names = {c["name"]: c for c in response.json()}
    assert "roster-client" in names
    assert names["roster-client"]["status"] == "checked_in"
    assert names["roster-client"]["tier"] == "frontier"
