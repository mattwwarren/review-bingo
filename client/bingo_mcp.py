#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "mcp>=2.0"]
# ///
"""bingo-mcp — the grid, exposed to any MCP client.

Same loop as bingo_client.py, reachable from Codex or anything else that
speaks MCP:

    list_jobs  ->  lease_job(id)  ->  (you run the review)  ->  report_result

The hub still never sees your prompts or model config — the MCP client runs
the review itself, exactly like the CLI path. What the hub enforces is the
repo's policy floor, and it enforces it here too: naming a job is not a way
around it.

Registration is shared with the CLI. Run `bingo_client.py register` once;
this server reads the same state file.

    uv run client/bingo_mcp.py            # stdio, for a local MCP client
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from bingo_client import DEFAULT_STATE_PATH, api, load_state


class HubRefusedError(RuntimeError):
    """The hub declined, and the reason is something the caller can act on.

    Raised instead of letting httpx's HTTPStatusError surface: "403 Forbidden"
    tells an agent nothing about whether to pick a different job, check in, or
    stop trying.
    """


def _detail(response: httpx.Response) -> str:
    """The hub's own explanation, or the status line if it didn't give one."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip() or f"HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail) if detail else f"HTTP {response.status_code}"


def list_jobs(client: httpx.Client) -> list[dict[str, Any]]:
    """Queued review work, trimmed to what a picker needs to choose."""
    response = client.get("/jobs")
    response.raise_for_status()
    return [
        {
            "id": job["id"],
            "repo": job["repo_full_name"],
            "pr_number": job["pr_number"],
            "title": job.get("pr_title"),
            "head_sha": job["head_sha"][:12],
            "min_tier": job["min_tier"],
        }
        for job in response.json()
        if job["state"] == "queued"
    ]


def lease_job(client: httpx.Client, job_id: str) -> dict[str, Any]:
    """Take a named job. The lease is a deadline, not a reservation."""
    response = client.post(f"/jobs/{job_id}/lease")
    if response.status_code == httpx.codes.FORBIDDEN:
        raise HubRefusedError(f"{_detail(response)}. Pick another job whose floor this client clears.")
    if response.status_code == httpx.codes.CONFLICT:
        raise HubRefusedError(f"{_detail(response)}. It may have been leased already, or the PR closed.")
    if response.status_code == httpx.codes.NOT_FOUND:
        raise HubRefusedError(f"No job with id {job_id!r}. Call list_jobs for what is currently queued.")
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


def report_result(
    client: httpx.Client,
    job_id: str,
    verdict: str,
    summary: str,
    findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Submit a finished round. The hub relays it to the PR best-effort."""
    payload = {"verdict": verdict, "summary": summary, "findings": findings or []}
    response = client.post(f"/jobs/{job_id}/report", json=payload)
    if response.status_code == httpx.codes.CONFLICT:
        raise HubRefusedError(f"{_detail(response)}. The lease may have expired and been reclaimed.")
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


def _client(state_path: Path = DEFAULT_STATE_PATH) -> httpx.Client:
    return api(load_state(state_path))


server = MCPServer(
    name="review-bingo",
    instructions=(
        "Plug into a review-bingo grid: list queued PR review jobs, lease one, "
        "review it with whatever compute you have, and report back. Check in "
        "before leasing — checking in is the grid's availability signal."
    ),
)


@server.tool(description="Check in: declare this client available to take review work.")
def check_in() -> str:
    with _client() as client:
        client.post("/clients/check-in").raise_for_status()
    return "Checked in — plugged into the grid."


@server.tool(description="Check out: stop being offered review work.")
def check_out() -> str:
    with _client() as client:
        client.post("/clients/check-out").raise_for_status()
    return "Checked out — compute is yours again."


@server.tool(description="List queued review jobs, with the repo, PR, and the tier floor each requires.")
def list_queued_jobs() -> list[dict[str, Any]]:
    with _client() as client:
        return list_jobs(client)


@server.tool(
    description=(
        "Lease one named job so it is yours to review. Returns the job and the "
        "lease deadline; report before it expires or the job is requeued."
    )
)
def lease(job_id: str) -> dict[str, Any]:
    with _client() as client:
        return lease_job(client, job_id)


@server.tool(
    description=(
        "Report a finished review round for a job this client holds. verdict is "
        "an overall call such as 'approve' or 'findings'; summary is markdown "
        "relayed to the PR verbatim; findings is a list of "
        "{file, line, title} objects."
    )
)
def report(
    job_id: str,
    verdict: str,
    summary: str,
    findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    with _client() as client:
        return report_result(client, job_id, verdict, summary, findings)


if __name__ == "__main__":
    server.run(transport="stdio")
