"""Read a GitHub account's identity and repo access from a user access token.

This is the hub's only outbound call made *on behalf of a person* rather than
as the App. The client completes GitHub's device flow entirely on its own
machine and hands the resulting user token to `POST /clients`; the hub spends
it here, once, and drops it.

Two rules this module exists to keep:

- **The token is never logged.** Not at DEBUG, not as a prefix, not as a
  length. Nothing here writes a log record at all — the enrolment decision is
  logged by the caller (`identity_service`), from resolved identity fields.
- **Failure is never silently generous.** An account with no matching App
  installation gets an empty access set, not a broader repo list. Errors are
  raised, never swallowed into "no access" — the caller has to be able to tell
  "GitHub said no repos" from "GitHub did not answer".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Protocol

import httpx
from fastapi import Depends

from review_bingo_hub.core.config import settings
from review_bingo_hub.core.http_client import http_client
from review_bingo_hub.models.github_identity import PermissionLevel, highest_permission

# GitHub's maximum; fewer round trips for accounts with many repositories.
PAGE_SIZE = 100

# Above this, GitHub is telling us about itself, not about the credential.
SERVER_ERROR_THRESHOLD = 500

GITHUB_ACCEPT_HEADER = "application/vnd.github+json"


class GithubIdentityError(Exception):
    """GitHub refused to answer for this credential.

    The default meaning is *rejection* — the token is bad, revoked, or lacks
    the scope. Callers turn this into a 401.
    """


class GithubUnavailableError(GithubIdentityError):
    """GitHub could not be reached, or answered with its own failure.

    Distinct from the base class because the two demand opposite responses: a
    rejection is the caller's fault and is final, an outage is nobody's fault
    and is temporary. Collapsing them would report every GitHub incident as a
    wave of invalid credentials.
    """


@dataclass(frozen=True)
class GithubUserIdentity:
    """Who the token belongs to."""

    github_user_id: int
    github_login: str


@dataclass(frozen=True)
class GithubRepoAccess:
    """One repo the token can reach, at a collapsed permission level."""

    repo_full_name: str
    permission: PermissionLevel


class GithubIdentityService(Protocol):
    """The seam tests substitute. Implementations must not log the token."""

    async def get_identity(self, token: str) -> GithubUserIdentity: ...

    async def get_repo_access(self, token: str) -> list[GithubRepoAccess]: ...


def collapse_permissions(permissions: dict[str, Any]) -> PermissionLevel:
    """Collapse GitHub's five permission booleans into one stored level."""
    if permissions.get("admin"):
        return PermissionLevel.ADMIN
    # maintain -> "write", not "admin": keeps A4's future "admin, as reported by
    # GitHub" gate literally true. Do not widen this to admit maintain-only
    # collaborators as admin — that would silently lower the bar for who can
    # change a repo's model floor.
    if permissions.get("push") or permissions.get("maintain"):
        return PermissionLevel.WRITE
    return PermissionLevel.READ


class LiveGithubIdentityService:
    """Talks to the real GitHub REST API with a user access token."""

    async def get_identity(self, token: str) -> GithubUserIdentity:
        """Resolve the account behind the token via GET /user."""
        async with http_client() as client:
            payload = await self._get(client, token, f"{settings.github_api_url}/user")
        try:
            return GithubUserIdentity(github_user_id=int(payload["id"]), github_login=str(payload["login"]))
        except (KeyError, TypeError, ValueError) as exc:
            error_msg = "GitHub /user response did not carry an id and login"
            raise GithubIdentityError(error_msg) from exc

    async def get_repo_access(self, token: str) -> list[GithubRepoAccess]:
        """Union the repos reachable through this App's installations.

        Installations belonging to *other* Apps are skipped: what they can see
        is not something this hub was granted, and borrowing it would let a
        client lease jobs for repos nobody pointed review-bingo at.
        """
        merged: dict[str, PermissionLevel] = {}
        order: list[str] = []

        async with http_client() as client:
            installations = await self._paginate(
                client, token, f"{settings.github_api_url}/user/installations", "installations"
            )
            for installation in installations:
                if not self._is_our_app(installation):
                    continue
                installation_id = installation["id"]
                repositories = await self._paginate(
                    client,
                    token,
                    f"{settings.github_api_url}/user/installations/{installation_id}/repositories",
                    "repositories",
                )
                for repository in repositories:
                    full_name = str(repository["full_name"])
                    permission = collapse_permissions(repository.get("permissions") or {})
                    if full_name in merged:
                        merged[full_name] = highest_permission(merged[full_name], permission)
                    else:
                        merged[full_name] = permission
                        order.append(full_name)

        return [GithubRepoAccess(repo_full_name=name, permission=merged[name]) for name in order]

    @staticmethod
    def _is_our_app(installation: dict[str, Any]) -> bool:
        app_id = installation.get("app_id")
        return app_id is not None and str(app_id) == str(settings.github_app_id)

    @staticmethod
    async def _get(client: httpx.AsyncClient, token: str, url: str, params: dict[str, Any] | None = None) -> Any:  # noqa: ANN401 - GitHub returns both objects and arrays
        headers = {"Authorization": f"Bearer {token}", "Accept": GITHUB_ACCEPT_HEADER}
        try:
            response = await client.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            error_msg = f"GitHub API unreachable: {type(exc).__name__}"
            raise GithubUnavailableError(error_msg) from exc

        if response.status_code >= SERVER_ERROR_THRESHOLD:
            error_msg = f"GitHub API returned {response.status_code}"
            raise GithubUnavailableError(error_msg)
        if response.is_error:
            error_msg = f"GitHub rejected the credential ({response.status_code})"
            raise GithubIdentityError(error_msg)

        try:
            return response.json()
        except ValueError as exc:
            error_msg = "GitHub API returned a non-JSON body"
            raise GithubUnavailableError(error_msg) from exc

    async def _paginate(
        self,
        client: httpx.AsyncClient,
        token: str,
        url: str,
        key: str,
    ) -> list[dict[str, Any]]:
        """Walk a GitHub envelope-paginated collection to exhaustion.

        Stops on a short page rather than trusting `total_count`: the count is
        computed before filtering and has been observed to disagree with the
        number of items actually returned.
        """
        collected: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = await self._get(client, token, url, {"per_page": PAGE_SIZE, "page": page})
            items = payload.get(key) if isinstance(payload, dict) else None
            if items is None:
                error_msg = f"GitHub response for {key} was not the expected envelope"
                raise GithubIdentityError(error_msg)
            collected.extend(items)
            if len(items) < PAGE_SIZE:
                return collected
            page += 1


def get_github_identity_service() -> GithubIdentityService:
    """FastAPI dependency seam; tests override this with a fake."""
    return LiveGithubIdentityService()


GithubIdentityServiceDep = Annotated[GithubIdentityService, Depends(get_github_identity_service)]
