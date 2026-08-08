"""Integration tests for the grid's spine: webhook → policy floor → lease → report → relay.

Relay runs in log mode here (no GitHub App credentials in test settings), so
the full loop exercises everything except the actual GitHub POST.
"""

import asyncio
import hashlib
import hmac
import json
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from review_bingo_hub.core.config import settings
from review_bingo_hub.tests.conftest import PLACEHOLDER_AUTH_HEADER

PR_WEBHOOK_HEADERS = {"X-GitHub-Event": "pull_request"}

# Captured from a real GitHub App delivery on 2026-08-04 (installation id redacted).
# Kept verbatim rather than hand-written: a payload we invent agrees with whatever
# we already believe about GitHub's shape, so it can never contradict us.
CLOSED_DELIVERY_FIXTURE = Path(__file__).parents[1] / "fixtures" / "github" / "pull_request_closed.json"


def closed_payload() -> dict[str, Any]:
    """A fresh copy of the captured pull_request.closed delivery."""
    payload: dict[str, Any] = json.loads(CLOSED_DELIVERY_FIXTURE.read_text())
    return payload


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

    # Reading a job is a client-authenticated call, so this test needs one.
    _, headers = await register_and_check_in(client, "dedupe-reader", "standard")
    response = await client.get(f"/jobs/{job_id}", headers=headers)
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

    response = await client.get(f"/jobs/{job_id}/relay-target", headers=headers)
    assert response.json()["mode"] == "log"

    response = await client.get(f"/jobs/{job_id}/comment", headers=headers)
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
async def test_closed_pr_cancels_its_queued_job(client: AsyncClient) -> None:
    closed = closed_payload()
    repo = closed["repository"]["full_name"]
    number = closed["pull_request"]["number"]
    sha = closed["pull_request"]["head"]["sha"]

    response = await client.post(
        "/webhooks/github",
        json=pr_payload(repo=repo, number=number, sha=sha),
        headers=PR_WEBHOOK_HEADERS,
    )
    assert response.json()["status"] == "queued"
    job_id = response.json()["job_id"]

    # Registered before the cancellation so its headers can authenticate the read below.
    _, reader_headers = await register_and_check_in(client, "cancellation-reader", "frontier")

    response = await client.post("/webhooks/github", json=closed, headers=PR_WEBHOOK_HEADERS)
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "cancelled"
    assert response.json()["cancelled"] == 1

    response = await client.get(f"/jobs/{job_id}", headers=reader_headers)
    assert response.json()["state"] == "cancelled"

    # The whole point: a client checking in afterwards must not be handed merged code.
    _, headers = await register_and_check_in(client, "after-the-merge", "frontier")
    response = await client.post("/jobs/lease", headers=headers)
    assert response.json() is None


@pytest.mark.asyncio
async def test_closed_pr_leaves_a_round_already_in_flight_alone(client: AsyncClient) -> None:
    """A leased job survives its PR closing — the holder still gets to report.

    Guard test: cancellation targets QUEUED only, so this passes without any
    further change. It exists to pin the boundary, not because it went red.
    """
    closed = closed_payload()
    repo = closed["repository"]["full_name"]
    number = closed["pull_request"]["number"]
    sha = closed["pull_request"]["head"]["sha"]

    await client.post(
        "/webhooks/github",
        json=pr_payload(repo=repo, number=number, sha=sha),
        headers=PR_WEBHOOK_HEADERS,
    )
    _, headers = await register_and_check_in(client, "mid-flight", "frontier")
    response = await client.post("/jobs/lease", headers=headers)
    job_id = response.json()["job"]["id"]

    response = await client.post("/webhooks/github", json=closed, headers=PR_WEBHOOK_HEADERS)
    assert response.json()["cancelled"] == 0

    response = await client.get(f"/jobs/{job_id}", headers=headers)
    assert response.json()["state"] == "leased"

    response = await client.post(
        f"/jobs/{job_id}/report",
        json={"verdict": "approve", "summary": "finished after the merge"},
        headers=headers,
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["state"] == "relayed"


@pytest.mark.asyncio
async def test_targeted_lease_hands_over_the_named_job(client: AsyncClient) -> None:
    """Targeting takes the job you asked for, not the oldest one in the queue."""
    older = await client.post("/webhooks/github", json=pr_payload(sha="target-older"), headers=PR_WEBHOOK_HEADERS)
    wanted = await client.post(
        "/webhooks/github", json=pr_payload(number=8, sha="target-wanted"), headers=PR_WEBHOOK_HEADERS
    )
    older_id = older.json()["job_id"]
    wanted_id = wanted.json()["job_id"]

    _, headers = await register_and_check_in(client, "picker", "frontier")
    response = await client.post(f"/jobs/{wanted_id}/lease", headers=headers)
    assert response.status_code == HTTPStatus.OK
    lease = response.json()
    assert lease["job"]["id"] == wanted_id
    assert lease["job"]["state"] == "leased"
    assert lease["lease_expires_at"] is not None

    response = await client.get(f"/jobs/{older_id}", headers=headers)
    assert response.json()["state"] == "queued"


@pytest.mark.asyncio
async def test_targeted_lease_still_enforces_the_policy_floor(client: AsyncClient) -> None:
    """Naming a job must not be a way around its repo's model floor."""
    repo = "acme/vault"
    await client.put(f"/policies/{repo}", json={"min_tier": "frontier"})
    response = await client.post(
        "/webhooks/github", json=pr_payload(repo=repo, sha="floor-target"), headers=PR_WEBHOOK_HEADERS
    )
    job_id = response.json()["job_id"]

    _, low_headers = await register_and_check_in(client, "toy-target", "experimental")
    response = await client.post(f"/jobs/{job_id}/lease", headers=low_headers)
    assert response.status_code == HTTPStatus.FORBIDDEN

    response = await client.get(f"/jobs/{job_id}", headers=low_headers)
    assert response.json()["state"] == "queued"


@pytest.mark.asyncio
async def test_tier_floor_demo_scenario_blocks_experimental_admits_frontier(client: AsyncClient) -> None:
    """Pins the exact scenario scripts/demo-tiers.sh walks, as a durable CI assertion.

    A floor above experimental: an experimental client sees a dry queue and is
    refused the job by name, while a frontier client leases that very job
    through both /jobs/lease and the targeted endpoint.
    """
    repo = "acme/tier-demo"
    response = await client.put(f"/policies/{repo}", json={"min_tier": "standard"})
    assert response.status_code == HTTPStatus.OK
    assert response.json()["min_tier"] == "standard"

    response = await client.post(
        "/webhooks/github", json=pr_payload(repo=repo, sha="tier-demo-sha"), headers=PR_WEBHOOK_HEADERS
    )
    assert response.json()["status"] == "queued"
    job_id = response.json()["job_id"]

    # Experimental client: queue looks dry, and naming the job directly is a 403, not a 404.
    _, low_headers = await register_and_check_in(client, "tier-demo-toy-box", "experimental")
    response = await client.post("/jobs/lease", headers=low_headers)
    assert response.status_code == HTTPStatus.OK
    assert response.json() is None

    response = await client.post(f"/jobs/{job_id}/lease", headers=low_headers)
    assert response.status_code == HTTPStatus.FORBIDDEN

    # Frontier client clears the floor and leases the same job the experimental client was refused.
    _, high_headers = await register_and_check_in(client, "tier-demo-big-rig", "frontier")
    response = await client.post("/jobs/lease", headers=high_headers)
    assert response.status_code == HTTPStatus.OK
    lease = response.json()
    assert lease is not None
    assert lease["job"]["id"] == job_id


@pytest.mark.asyncio
async def test_targeted_lease_rejects_a_job_someone_else_holds(client: AsyncClient) -> None:
    response = await client.post("/webhooks/github", json=pr_payload(sha="held-target"), headers=PR_WEBHOOK_HEADERS)
    job_id = response.json()["job_id"]

    _, holder = await register_and_check_in(client, "target-holder", "standard")
    assert (await client.post(f"/jobs/{job_id}/lease", headers=holder)).status_code == HTTPStatus.OK

    _, latecomer = await register_and_check_in(client, "target-latecomer", "standard")
    response = await client.post(f"/jobs/{job_id}/lease", headers=latecomer)
    assert response.status_code == HTTPStatus.CONFLICT


@pytest.mark.asyncio
async def test_concurrent_targeted_leases_have_exactly_one_winner(client: AsyncClient) -> None:
    response = await client.post("/webhooks/github", json=pr_payload(sha="race-target"), headers=PR_WEBHOOK_HEADERS)
    job_id = response.json()["job_id"]

    _, racer_a = await register_and_check_in(client, "racer-a", "frontier")
    _, racer_b = await register_and_check_in(client, "racer-b", "frontier")

    results = await asyncio.gather(
        client.post(f"/jobs/{job_id}/lease", headers=racer_a),
        client.post(f"/jobs/{job_id}/lease", headers=racer_b),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result

    codes = sorted(r.status_code for r in results)  # type: ignore[union-attr]
    assert codes == [HTTPStatus.OK, HTTPStatus.CONFLICT]


@pytest.mark.asyncio
async def test_client_roster_lists_capabilities(client: AsyncClient) -> None:
    _, headers = await register_and_check_in(client, "roster-client", "frontier")
    response = await client.get("/clients", headers=headers)
    assert response.status_code == HTTPStatus.OK
    names = {c["name"]: c for c in response.json()}
    assert "roster-client" in names
    assert names["roster-client"]["status"] == "checked_in"
    assert names["roster-client"]["tier"] == "frontier"


@pytest.mark.asyncio
async def test_default_test_client_registers_under_dev_mode(client: AsyncClient) -> None:
    """Every register_and_check_in() above depends on this equality holding.

    The client fixture sends PLACEHOLDER_AUTH_HEADER on every request, and the
    autouse config-isolation fixture pins CLIENT_ENROLMENT_SECRET to the same
    string. If those two drift apart, the whole grid suite fails at
    registration with a 401 that says nothing about why.
    """
    assert settings.client_enrolment_mode == "dev"
    assert f"Bearer {settings.client_enrolment_secret}" == PLACEHOLDER_AUTH_HEADER

    _, headers = await register_and_check_in(client, "dev-mode-client", "standard")
    assert headers["Authorization"] != PLACEHOLDER_AUTH_HEADER
