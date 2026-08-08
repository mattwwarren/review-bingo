"""Tests for the MCP server's hub calls.

Run: uv run --with pytest --with pytest-asyncio --with mcp --with httpx \
         pytest client/test_bingo_mcp.py

The MCP tool wrappers are one-liners over these functions; what's worth
pinning is how hub errors become messages an agent can act on. A raw
HTTPStatusError reaching an MCP client is a dead end — it has no idea whether
to pick a different job, check in, or give up.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import bingo_mcp


def api_with(handler: Any) -> httpx.Client:
    return httpx.Client(base_url="http://hub.test", transport=httpx.MockTransport(handler))


def test_list_jobs_returns_only_leasable_work() -> None:
    payload = [
        {
            "id": "job-1",
            "repo_full_name": "acme/pay",
            "pr_number": 7,
            "pr_title": "Fix rounding",
            "head_sha": "abcdef1234567890",
            "state": "queued",
            "min_tier": "standard",
        },
        {
            "id": "job-2",
            "repo_full_name": "acme/pay",
            "pr_number": 8,
            "pr_title": "Already taken",
            "head_sha": "beefbeefbeefbeef",
            "state": "leased",
            "min_tier": "standard",
        },
    ]
    with api_with(lambda request: httpx.Response(200, json=payload)) as api:
        jobs = bingo_mcp.list_jobs(api)

    assert [j["id"] for j in jobs] == ["job-1"]
    assert jobs[0]["repo"] == "acme/pay"
    assert jobs[0]["pr_number"] == 7
    assert jobs[0]["min_tier"] == "standard"


def test_lease_job_returns_the_job_and_its_deadline() -> None:
    lease = {
        "job": {
            "id": "job-1",
            "repo_full_name": "acme/pay",
            "pr_number": 7,
            "head_sha": "abcdef1234567890",
            "state": "leased",
        },
        "lease_expires_at": "2026-08-05T03:00:00Z",
    }
    with api_with(lambda request: httpx.Response(200, json=lease)) as api:
        result = bingo_mcp.lease_job(api, "job-1")

    assert result["job"]["id"] == "job-1"
    assert result["lease_expires_at"] == "2026-08-05T03:00:00Z"


def test_lease_job_explains_a_tier_floor_rejection() -> None:
    detail = "Job requires tier 'frontier' or better; this client declares 'experimental'"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": detail})

    with api_with(handler) as api, pytest.raises(bingo_mcp.HubRefusedError) as excinfo:
        bingo_mcp.lease_job(api, "job-1")

    assert "tier" in str(excinfo.value)
    assert "another job" in str(excinfo.value)


def test_lease_job_explains_a_job_someone_else_holds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "Job is not available to lease"})

    with api_with(handler) as api, pytest.raises(bingo_mcp.HubRefusedError) as excinfo:
        bingo_mcp.lease_job(api, "job-1")

    assert "not available" in str(excinfo.value)


def test_report_result_sends_the_verdict_and_findings() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"state": "relayed", "verdict": "findings", "relay_error": None})

    findings = [{"file": "pay.py", "line": 42, "title": "Unvalidated amount"}]
    with api_with(handler) as api:
        result = bingo_mcp.report_result(api, "job-1", "findings", "One real bug.", findings)

    assert seen["url"].endswith("/jobs/job-1/report")
    assert seen["body"] == {"verdict": "findings", "summary": "One real bug.", "findings": findings}
    assert result["state"] == "relayed"
