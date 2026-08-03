"""Relay reported reviews back to the PR as a GitHub comment.

Two modes, chosen by configuration:

- **App mode** (GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY set): authenticate as
  the GitHub App, mint an installation token for the job's repo, and post a
  comment on the PR.
- **Log mode** (default): log the rendered comment and return. This keeps the
  full loop runnable offline — the demo, tests, and local hacking never need
  GitHub credentials.

Relay is best-effort: failures land in job.relay_error and never fail the
client's report request.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import jwt

from review_bingo_hub.core.config import settings
from review_bingo_hub.core.http_client import http_client
from review_bingo_hub.models.review_job import ReviewJob

LOGGER = logging.getLogger(__name__)

APP_JWT_TTL_SECONDS = 540  # GitHub caps App JWTs at 10 minutes; stay under


def render_comment(job: ReviewJob) -> str:
    """Render the reported round as PR-comment markdown."""
    findings_md = ""
    if job.findings:
        lines = []
        for finding in job.findings:
            location = finding.get("file", "")
            if finding.get("line"):
                location += f":{finding['line']}"
            title = finding.get("title") or finding.get("summary") or "(untitled finding)"
            lines.append(f"- **{location}** — {title}" if location else f"- {title}")
        findings_md = "\n### Findings\n\n" + "\n".join(lines) + "\n"

    return (
        f"## 🎱 review-bingo round — `{job.verdict}`\n\n"
        f"{job.summary or ''}\n"
        f"{findings_md}\n"
        f"---\n"
        f"_Reviewed at `{job.head_sha[:12]}` by a grid client (tier floor: {job.min_tier})._\n"
    )


def _relay_configured() -> bool:
    return bool(settings.github_app_id and settings.github_app_private_key)


def _app_jwt() -> str:
    private_key = settings.github_app_private_key
    if private_key is None:  # pragma: no cover - guarded by _relay_configured()
        msg = "GITHUB_APP_PRIVATE_KEY not configured"
        raise RuntimeError(msg)
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + APP_JWT_TTL_SECONDS, "iss": settings.github_app_id}
    return jwt.encode(payload, private_key, algorithm="RS256")


async def _installation_token(repo_full_name: str) -> str:
    """Mint an installation access token for the repo's App installation."""
    headers = {"Authorization": f"Bearer {_app_jwt()}", "Accept": "application/vnd.github+json"}
    async with http_client() as client:
        response = await client.get(
            f"{settings.github_api_url}/repos/{repo_full_name}/installation",
            headers=headers,
        )
        response.raise_for_status()
        installation_id = response.json()["id"]

        response = await client.post(
            f"{settings.github_api_url}/app/installations/{installation_id}/access_tokens",
            headers=headers,
        )
        response.raise_for_status()
        token: str = response.json()["token"]
    return token


async def relay_result(job: ReviewJob) -> str | None:
    """Post the round's comment to the PR. Returns an error string on failure.

    In log mode the comment is logged and the relay counts as delivered.
    """
    comment = render_comment(job)

    if not _relay_configured():
        LOGGER.info(
            "relay_log_mode",
            extra={
                "repo": job.repo_full_name,
                "pr_number": job.pr_number,
                "comment": comment,
            },
        )
        return None

    try:
        token = await _installation_token(job.repo_full_name)
        async with http_client() as client:
            response = await client.post(
                f"{settings.github_api_url}/repos/{job.repo_full_name}/issues/{job.pr_number}/comments",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                json={"body": comment},
            )
            response.raise_for_status()
    except Exception as exc:
        LOGGER.warning(
            "relay_failed",
            extra={"repo": job.repo_full_name, "pr_number": job.pr_number, "error": str(exc)},
        )
        return str(exc)
    return None


def relay_target(job: ReviewJob) -> dict[str, Any]:
    """Describe where a relay would go — used by the API response."""
    return {
        "mode": "github" if _relay_configured() else "log",
        "repo": job.repo_full_name,
        "pr_number": job.pr_number,
    }
