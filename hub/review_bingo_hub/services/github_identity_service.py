"""Read a GitHub account's identity and repo access from a user access token.

This is the hub's only outbound call made *on behalf of a person* rather than
as the App. A grid client completes GitHub's device flow on its own machine and
hands the resulting user token to `POST /clients`; the hub spends it here,
once, and drops it.

B1 (#24) added the other half: a *browser* cannot run the flow itself, because
that would mean shipping the App's client id and holding GitHub's answers in a
page. So the hub brokers the same flow on the dashboard's behalf —
`request_device_code` and `poll_device_token` are that broker, and they talk to
exactly the two endpoints `client/bingo_client.py` already talks to, with the
same error taxonomy, so the two halves cannot drift.

Two rules this module exists to keep:

- **The token is never logged.** Not at DEBUG, not as a prefix, not as a
  length. Nothing here writes a log record at all — the enrolment decision is
  logged by the caller (`identity_service`), from resolved identity fields.
  This covers the token GitHub *returns* from a device poll as much as the one
  a client presents at enrolment: it is the same credential either way.
- **Failure is never silently generous.** An account with no matching App
  installation gets an empty access set, not a broader repo list. Errors are
  raised, never swallowed into "no access" — the caller has to be able to tell
  "GitHub said no repos" from "GitHub did not answer". Likewise an unrecognised
  device-poll error is raised rather than treated as "keep polling".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
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

# The device flow lives on github.com, not on the API host — the same two URLs
# client/bingo_client.py posts to. Deliberately not derived from
# `settings.github_api_url`: that setting exists so a GHE deployment can move
# the *API*, and these are a different host even on github.com itself.
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 - URL, not a credential

# GitHub's own grant type string for the device flow, quoted verbatim.
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# What the device flow asks for. Identity only: the hub reads who you are and
# which installations you can see, and neither needs repo contents.
DEVICE_SCOPE = "read:user"


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


@dataclass(frozen=True)
class DeviceCodeGrant:
    """GitHub's answer to "start a device flow": what to show, and what to poll with.

    `device_code` is the hub's half of the handshake and `user_code` is the
    person's; both have to reach the browser, because the browser is what does
    the polling and the person is what does the typing.
    """

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DevicePollStatus(StrEnum):
    """What one poll of GitHub's token endpoint means.

    Values are GitHub's own strings (plus "authorized" for the success case
    GitHub signals by *absence* of an error) so the same vocabulary survives
    from GitHub's wire format through to the JSON the dashboard reads. Five
    distinct answers, not two: "keep waiting" and "stop, and here is why" have
    to stay tellable apart, or a page nags someone who already clicked Deny.
    """

    AUTHORIZED = "authorized"
    PENDING = "authorization_pending"
    SLOW_DOWN = "slow_down"
    EXPIRED = "expired_token"
    DENIED = "access_denied"


@dataclass(frozen=True)
class DevicePollResult:
    """One poll's outcome. `access_token` is set only when AUTHORIZED."""

    status: DevicePollStatus
    access_token: str | None = None
    interval: int | None = None


class GithubIdentityService(Protocol):
    """The seam tests substitute. Implementations must not log the token."""

    async def get_identity(self, token: str) -> GithubUserIdentity: ...

    async def get_repo_access(self, token: str) -> list[GithubRepoAccess]: ...

    async def request_device_code(self) -> DeviceCodeGrant: ...

    async def poll_device_token(self, device_code: str) -> DevicePollResult: ...


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

    async def request_device_code(self) -> DeviceCodeGrant:
        """Open a device flow for the dashboard, on the App's client id.

        Raises:
            GithubIdentityError: GitHub refused the client id — the body comes
                back with an `error` where a `device_code` belongs, under an
                HTTP 200, so only reading it catches this.
            GithubUnavailableError: GitHub could not be reached.
        """
        async with http_client() as client:
            payload = await self._post_form(
                client,
                GITHUB_DEVICE_CODE_URL,
                {"client_id": settings.github_app_client_id, "scope": DEVICE_SCOPE},
            )
        if not isinstance(payload, dict) or "device_code" not in payload:
            error_msg = "GitHub refused the device-code request"
            raise GithubIdentityError(error_msg)
        try:
            return DeviceCodeGrant(
                device_code=str(payload["device_code"]),
                user_code=str(payload["user_code"]),
                verification_uri=str(payload["verification_uri"]),
                expires_in=int(payload["expires_in"]),
                interval=int(payload["interval"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            error_msg = "GitHub device-code response was missing a field the flow needs"
            raise GithubIdentityError(error_msg) from exc

    async def poll_device_token(self, device_code: str) -> DevicePollResult:
        """Ask once whether the person has authorized this device code yet.

        One poll, not a loop: the browser owns the waiting, so that a page the
        person closed stops polling on its own rather than leaving the hub
        hammering github.com on its behalf.

        Raises:
            GithubIdentityError: GitHub returned an error code outside the four
                the device flow defines. Raised rather than folded into
                "pending", because an unmapped code polled forever is exactly
                the failure this taxonomy exists to prevent.
            GithubUnavailableError: GitHub could not be reached.
        """
        async with http_client() as client:
            payload = await self._post_form(
                client,
                GITHUB_ACCESS_TOKEN_URL,
                {
                    "client_id": settings.github_app_client_id,
                    "device_code": device_code,
                    "grant_type": DEVICE_GRANT_TYPE,
                },
            )
        if not isinstance(payload, dict):
            error_msg = "GitHub device-poll response was not an object"
            raise GithubIdentityError(error_msg)

        if "access_token" in payload:
            return DevicePollResult(status=DevicePollStatus.AUTHORIZED, access_token=str(payload["access_token"]))

        error = payload.get("error")
        if error == DevicePollStatus.PENDING:
            return DevicePollResult(status=DevicePollStatus.PENDING)
        if error == DevicePollStatus.SLOW_DOWN:
            # GitHub dictates the new floor; ignoring it earns a ban, not a token.
            return DevicePollResult(status=DevicePollStatus.SLOW_DOWN, interval=self._interval_or_none(payload))
        if error == DevicePollStatus.EXPIRED:
            return DevicePollResult(status=DevicePollStatus.EXPIRED)
        if error == DevicePollStatus.DENIED:
            return DevicePollResult(status=DevicePollStatus.DENIED)

        error_msg = f"GitHub device poll returned an unrecognised error ({error!r})"
        raise GithubIdentityError(error_msg)

    @staticmethod
    def _interval_or_none(payload: dict[str, Any]) -> int | None:
        """GitHub's new poll floor, or None when it declined to name one."""
        raw = payload.get("interval")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_our_app(installation: dict[str, Any]) -> bool:
        app_id = installation.get("app_id")
        return app_id is not None and str(app_id) == str(settings.github_app_id)

    @staticmethod
    async def _post_form(client: httpx.AsyncClient, url: str, data: dict[str, Any]) -> Any:  # noqa: ANN401 - GitHub returns both grants and error envelopes
        """POST a form to github.com and return the parsed body.

        A sibling of `_get` rather than a generalisation of it: the device flow
        posts form bodies to a different host with no Authorization header at
        all, and folding the two would put a credential-bearing request and a
        credential-free one behind one signature.
        """
        try:
            response = await client.post(url, data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            error_msg = f"GitHub device flow unreachable: {type(exc).__name__}"
            raise GithubUnavailableError(error_msg) from exc

        if response.status_code >= SERVER_ERROR_THRESHOLD:
            error_msg = f"GitHub device flow returned {response.status_code}"
            raise GithubUnavailableError(error_msg)
        if response.is_error:
            error_msg = f"GitHub rejected the device-flow request ({response.status_code})"
            raise GithubIdentityError(error_msg)

        try:
            return response.json()
        except ValueError as exc:
            error_msg = "GitHub device flow returned a non-JSON body"
            raise GithubUnavailableError(error_msg) from exc

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
