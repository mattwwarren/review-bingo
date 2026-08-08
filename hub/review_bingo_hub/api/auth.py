"""Dashboard login: GitHub's device flow, brokered by the hub.

People, not machines — the counterpart to `api/clients.py`. A browser cannot run
the device flow itself without shipping the App's client id and holding GitHub's
answers in a page, so the hub runs it: `/auth/device/start` opens a flow and
hands back the code the person types on github.com, and `/auth/device/poll` asks
once whether they have finished. The browser owns the waiting, so a tab that gets
closed stops polling instead of leaving the hub hammering github.com for it.

Both paths are public (`RequireTokenMiddleware.PUBLIC_PATHS`) for the obvious
reason: logging in cannot require being logged in.

Two credentials pass through here and neither may ever reach a log record — not
whole, not truncated, not as a length. The GitHub user token is spent once and
dropped; the dashboard session token is minted, returned in the response body,
and thereafter exists only as a SHA-256 digest. What *is* logged is the decision:
`dashboard_session_created` on success, `enrolment_denied` on a terminal refusal,
both keyed on identity fields that say who got in without handing anyone the
means to do it again.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from review_bingo_hub.core.config import settings
from review_bingo_hub.core.logging import get_logging_context
from review_bingo_hub.db.session import SessionDep
from review_bingo_hub.services.dashboard_session_service import create_session
from review_bingo_hub.services.github_identity_service import (
    DevicePollResult,
    DevicePollStatus,
    GithubIdentityError,
    GithubIdentityServiceDep,
    GithubUnavailableError,
)
from review_bingo_hub.services.identity_service import (
    REASON_DEVICE_ACCESS_DENIED,
    REASON_DEVICE_TOKEN_EXPIRED,
    EnrolmentDeniedError,
    EnrolmentUnavailableError,
    resolve_identity_from_github_token,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

DETAIL_LOGIN_UNCONFIGURED = "Dashboard login is unavailable: this hub has no GITHUB_APP_CLIENT_ID configured"
DETAIL_GITHUB_UNREACHABLE = "Could not reach GitHub to sign you in; try again shortly"
DETAIL_DEVICE_CODE_EXPIRED = "That sign-in code expired before it was authorized — start again"
DETAIL_DEVICE_ACCESS_DENIED = "Sign-in was denied on github.com"

# Which refusals are the caller's to fix, and their reason code for the audit
# record. Kept as data rather than a branch per case so the two cannot fall out
# of step with the constants identity_service publishes.
TERMINAL_REFUSALS: dict[DevicePollStatus, tuple[str, str]] = {
    DevicePollStatus.EXPIRED: (REASON_DEVICE_TOKEN_EXPIRED, DETAIL_DEVICE_CODE_EXPIRED),
    DevicePollStatus.DENIED: (REASON_DEVICE_ACCESS_DENIED, DETAIL_DEVICE_ACCESS_DENIED),
}


class DeviceStartResponse(BaseModel):
    """What the page needs to show, and what it needs to poll with."""

    device_code: str = Field(description="Opaque handle the browser sends back on every poll")
    user_code: str = Field(description="Short code the person types on github.com")
    verification_uri: str = Field(description="Where the person types it")
    expires_in: int = Field(description="Seconds until the codes above stop working")
    interval: int = Field(description="Minimum seconds between polls, as dictated by GitHub")


class DevicePollRequest(BaseModel):
    """Poll body.

    The field is `device_code` because that is GitHub's own field name and the
    one `client/bingo_client.py`'s `_poll_for_access_token` already sends. The
    CLI and the hub broker one flow; a hub-side rename would be drift with no
    upside.
    """

    device_code: str


class DevicePollResponse(BaseModel):
    """One poll's answer, in the same vocabulary GitHub uses.

    `session_token` is populated exactly once, on the poll that authorizes: it
    is the only time the plaintext exists outside the browser's hands.
    """

    status: DevicePollStatus
    interval: int | None = Field(default=None, description="New minimum poll interval, when GitHub asked us to slow")
    session_token: str | None = Field(default=None, description="Dashboard bearer token; set only when authorized")
    expires_at: datetime | None = Field(default=None, description="When that session stops working")
    github_login: str | None = Field(default=None, description="Who signed in; display only")


def _require_login_configured() -> str:
    """The App client id, or a 503 that names the deployment gap.

    503 rather than 500 or 404: an unconfigured hub is a temporary state of this
    deployment, not a malformed request and not a route that does not exist.
    """
    client_id = settings.github_app_client_id
    if not client_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=DETAIL_LOGIN_UNCONFIGURED)
    return client_id


@router.post("/device/start", response_model=DeviceStartResponse)
async def device_start_endpoint(github: GithubIdentityServiceDep) -> DeviceStartResponse:
    """Open a device flow and hand the browser the code its person must enter."""
    _require_login_configured()
    try:
        grant = await github.request_device_code()
    except GithubUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=DETAIL_GITHUB_UNREACHABLE) from exc
    except GithubIdentityError as exc:
        # GitHub refusing our own client id is our misconfiguration, not the
        # caller's — so it reads as unavailable rather than as a bad request.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=DETAIL_LOGIN_UNCONFIGURED) from exc

    return DeviceStartResponse(
        device_code=grant.device_code,
        user_code=grant.user_code,
        verification_uri=grant.verification_uri,
        expires_in=grant.expires_in,
        interval=grant.interval,
    )


@router.post("/device/poll", response_model=DevicePollResponse)
async def device_poll_endpoint(
    payload: DevicePollRequest,
    session: SessionDep,
    github: GithubIdentityServiceDep,
) -> DevicePollResponse:
    """Ask GitHub once whether this device code has been authorized yet.

    Nothing is written unless the answer is yes: a pending, slowed, expired,
    denied, or unanswered poll leaves no session and no identity behind.
    """
    _require_login_configured()
    try:
        result = await github.poll_device_token(payload.device_code)
    except GithubUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=DETAIL_GITHUB_UNREACHABLE) from exc
    except GithubIdentityError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if result.status in TERMINAL_REFUSALS:
        _refuse(result.status)
    if result.status is not DevicePollStatus.AUTHORIZED:
        return DevicePollResponse(status=result.status, interval=result.interval)

    return await _mint_session(session, result, github)


def _refuse(poll_status: DevicePollStatus) -> None:
    """Log a terminal device refusal and raise its 400.

    Reuses `enrolment_denied` — the same event name and the same `reason` field
    shape `identity_service._denied` writes for the CLI-side flow — rather than
    introducing a second denial event. "Who was refused admission, and why" has
    to stay one question answerable from one grep.
    """
    reason, detail = TERMINAL_REFUSALS[poll_status]
    LOGGER.warning("enrolment_denied", extra={**get_logging_context(), "reason": reason})
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


async def _mint_session(
    session: SessionDep,
    result: DevicePollResult,
    github: GithubIdentityServiceDep,
) -> DevicePollResponse:
    """Spend the authorized GitHub token, then issue the dashboard's own credential."""
    if result.access_token is None:  # pragma: no cover - AUTHORIZED always carries a token
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=DETAIL_GITHUB_UNREACHABLE)

    try:
        identity = await resolve_identity_from_github_token(session, result.access_token, github)
    except EnrolmentUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.detail) from exc
    except EnrolmentDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.detail) from exc

    dashboard_session, token = await create_session(session, identity.id)

    # Logged before the commit below, matching identity_resolved's own
    # ordering: a crash between these two statements should lose the session
    # it would have audited, not mint one with no audit trail behind it.
    # Identity fields only, never credential material.
    LOGGER.info(
        "dashboard_session_created",
        extra={
            **get_logging_context(),
            "identity_id": str(identity.id),
            "github_login": identity.github_login,
        },
    )
    await session.commit()

    return DevicePollResponse(
        status=DevicePollStatus.AUTHORIZED,
        session_token=token,
        expires_at=dashboard_session.expires_at,
        github_login=identity.github_login,
    )
