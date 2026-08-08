"""Integration tests for the hub-brokered GitHub device flow the dashboard logs in with.

The CLI has run this flow on its own machine since A1 (#20); B1 gives the
browser the same admission path with the hub in the middle, because a page
cannot hold an App client secret and must not be handed one.

Three properties are asserted directly rather than inferred:

* **Every admission decision names an audited event.** Success logs
  `dashboard_session_created`; a terminal refusal reuses `enrolment_denied`
  with a device-specific reason. A login surface that decides who gets in and
  records nothing is a decision nobody can review after the fact.
* **Two secrets, neither of them ever logged or stored in the clear.** The
  GitHub user token the hub spends, and the dashboard session token it mints.
  The session token is the newer risk: it is a credential this code *creates*,
  so nothing upstream has already established the habit of not printing it.
* **Nothing is written until GitHub says yes.** A pending, slowed, expired,
  denied, or unanswered poll leaves no `dashboard_session` and no
  `github_identity` row behind.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from review_bingo_hub.api.auth import DETAIL_GITHUB_UNREACHABLE, DETAIL_LOGIN_UNCONFIGURED
from review_bingo_hub.core.config import settings
from review_bingo_hub.models.dashboard_session import DashboardSession
from review_bingo_hub.models.github_identity import GithubIdentity
from review_bingo_hub.services.client_service import hash_token
from review_bingo_hub.services.github_identity_service import (
    DevicePollResult,
    DevicePollStatus,
    GithubIdentityError,
    GithubUnavailableError,
)
from review_bingo_hub.tests.integration.conftest import (
    DEVICE_CLIENT_ID,
    GITHUB_TOKEN,
    FakeGithubIdentityService,
    device_grant,
    dump,
    marge,
    readable,
    records_named,
    use_github_mode,
)

DEVICE_CODE = "3584d83530557fdd1f46af8289938c8ef79f9dc5"


def configured_fake(
    monkeypatch: pytest.MonkeyPatch,
    *,
    poll_results: list[DevicePollResult] | None = None,
    error: Exception | None = None,
) -> FakeGithubIdentityService:
    """A hub wired for the device flow, with GitHub's answers scripted in advance."""
    fake = FakeGithubIdentityService(
        identity=marge(),
        repo_access=[readable("acme/payments")],
        device_grant=device_grant(),
        poll_results=list(poll_results or []),
        error=error,
    )
    use_github_mode(monkeypatch, fake)
    monkeypatch.setattr(settings, "github_app_client_id", DEVICE_CLIENT_ID)
    return fake


async def row_counts(session: AsyncSession) -> tuple[int, int]:
    """(dashboard sessions, github identities) — the two tables a login may write."""
    session.expire_all()
    sessions = (await session.execute(select(DashboardSession))).scalars().all()
    identities = (await session.execute(select(GithubIdentity))).scalars().all()
    return len(sessions), len(identities)


# ---------------------------------------------------------------------------
# POST /auth/device/start
# ---------------------------------------------------------------------------


async def test_device_start_returns_the_code_the_operator_must_enter(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_fake(monkeypatch)

    response = await client.post("/auth/device/start")

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["user_code"] == "WDJB-MJHT"
    assert body["verification_uri"] == "https://github.com/login/device"
    assert body["interval"] == 5
    assert body["expires_in"] == 899
    # The browser has to hand this back on every poll, so it must come through.
    assert body["device_code"] == DEVICE_CODE


async def test_device_start_is_503_when_the_app_client_id_is_unset(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No client id means no device flow — a deployment fact, not a caller's fault."""
    fake = FakeGithubIdentityService(forbidden=True)
    use_github_mode(monkeypatch, fake)
    monkeypatch.setattr(settings, "github_app_client_id", None)

    response = await client.post("/auth/device/start")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert fake.calls == 0


async def test_device_start_is_503_when_github_is_unreachable(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_fake(monkeypatch, error=GithubUnavailableError("connection refused"))

    response = await client.post("/auth/device/start")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["detail"] == DETAIL_GITHUB_UNREACHABLE


async def test_device_start_is_503_when_github_refuses_our_client_id(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client id GitHub rejects is our misconfiguration, not the caller's mistake.

    So it reads as 503 alongside the unset-client-id case, rather than as a 4xx
    that would tell the person at the browser to try something different. There
    is nothing they can do differently.
    """
    configured_fake(monkeypatch, error=GithubIdentityError("unauthorized_client"))

    response = await client.post("/auth/device/start")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["detail"] == DETAIL_LOGIN_UNCONFIGURED


# ---------------------------------------------------------------------------
# POST /auth/device/poll — the request contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [{}, {"device_code": 12345}, {"code": DEVICE_CODE}])
async def test_device_poll_requires_a_string_device_code(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    """The field is `device_code`, and it is a string.

    Pinned rather than left implicit: GitHub's own poll contract names this
    field, and so does `client/bingo_client.py`'s `_poll_for_access_token`. A
    hub-side rename would be drift between two halves of the same flow.
    """
    configured_fake(monkeypatch)

    response = await client.post("/auth/device/poll", json=payload)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_device_poll_forwards_the_device_code_it_was_given(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = configured_fake(
        monkeypatch, poll_results=[DevicePollResult(status=DevicePollStatus.PENDING)]
    )

    await client.post("/auth/device/poll", json={"device_code": DEVICE_CODE})

    assert fake.device_codes_seen == [DEVICE_CODE]


# ---------------------------------------------------------------------------
# POST /auth/device/poll — the non-terminal answers
# ---------------------------------------------------------------------------


async def test_device_poll_pending_writes_nothing(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Not yet" is an ordinary answer, not an error, and it mints nothing."""
    configured_fake(monkeypatch, poll_results=[DevicePollResult(status=DevicePollStatus.PENDING)])

    response = await client.post("/auth/device/poll", json={"device_code": DEVICE_CODE})

    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "authorization_pending"
    assert response.json()["session_token"] is None
    assert await row_counts(session) == (0, 0)


async def test_device_poll_slow_down_carries_the_new_interval(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub dictates the new floor; a client that ignores it earns a ban, not a token."""
    configured_fake(
        monkeypatch,
        poll_results=[DevicePollResult(status=DevicePollStatus.SLOW_DOWN, interval=10)],
    )

    response = await client.post("/auth/device/poll", json={"device_code": DEVICE_CODE})

    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "slow_down"
    assert response.json()["interval"] == 10
    assert await row_counts(session) == (0, 0)


# ---------------------------------------------------------------------------
# POST /auth/device/poll — the terminal refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        (DevicePollStatus.EXPIRED, "device_token_expired"),
        (DevicePollStatus.DENIED, "device_access_denied"),
    ],
)
async def test_device_poll_terminal_refusal_is_400_and_audited(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: tuple[DevicePollStatus, str],
) -> None:
    """A refusal reuses `enrolment_denied` with a device-specific reason.

    A second denial event would split one audit question — "who was refused
    admission, and why" — across two log names nobody remembers to grep both of.
    """
    poll_status, reason = case
    configured_fake(monkeypatch, poll_results=[DevicePollResult(status=poll_status)])

    with caplog.at_level(logging.DEBUG):
        response = await client.post("/auth/device/poll", json={"device_code": DEVICE_CODE})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    denied = records_named(caplog, "enrolment_denied")
    assert len(denied) == 1
    assert denied[0].levelno == logging.WARNING
    assert denied[0].__dict__["reason"] == reason
    assert await row_counts(session) == (0, 0)


async def test_device_poll_fails_closed_when_github_is_unreachable(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outage is transient and says so (503), and still writes nothing."""
    configured_fake(monkeypatch, error=GithubUnavailableError("connection refused"))

    response = await client.post("/auth/device/poll", json={"device_code": DEVICE_CODE})

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert await row_counts(session) == (0, 0)


async def test_device_poll_is_400_when_github_answers_with_an_error_we_do_not_know(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unmapped GitHub error stops the flow rather than reading as "not yet"."""
    configured_fake(monkeypatch, error=GithubIdentityError("incorrect_client_credentials"))

    response = await client.post("/auth/device/poll", json={"device_code": DEVICE_CODE})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert await row_counts(session) == (0, 0)


@pytest.mark.parametrize(
    "case",
    [
        (GithubUnavailableError("connection refused"), HTTPStatus.SERVICE_UNAVAILABLE),
        (GithubIdentityError("Bad credentials"), HTTPStatus.UNAUTHORIZED),
    ],
)
async def test_device_poll_fails_closed_when_the_identity_read_fails_after_authorization(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[Exception, HTTPStatus],
) -> None:
    """GitHub can authorize the code and then fail the identity read seconds later.

    The two are separate calls, so this window is real. Whichever way it fails,
    no session is minted — a login that half-succeeded would be a credential
    scoped by an access set the hub never actually read.
    """
    identity_error, expected_status = case
    fake = configured_fake(
        monkeypatch,
        poll_results=[DevicePollResult(status=DevicePollStatus.AUTHORIZED, access_token=GITHUB_TOKEN)],
    )
    fake.identity_error = identity_error

    response = await client.post("/auth/device/poll", json={"device_code": DEVICE_CODE})

    assert response.status_code == expected_status
    assert await row_counts(session) == (0, 0)


# ---------------------------------------------------------------------------
# POST /auth/device/poll — success
# ---------------------------------------------------------------------------


async def test_device_poll_success_mints_a_session_stored_only_as_a_hash(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_fake(
        monkeypatch,
        poll_results=[DevicePollResult(status=DevicePollStatus.AUTHORIZED, access_token=GITHUB_TOKEN)],
    )

    response = await client.post("/auth/device/poll", json={"device_code": DEVICE_CODE})

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["status"] == "authorized"
    assert body["github_login"] == "marge-bouvier"
    assert body["expires_at"] is not None
    session_token = body["session_token"]
    assert session_token

    session.expire_all()
    row = (await session.execute(select(DashboardSession))).scalars().one()
    identity = (await session.execute(select(GithubIdentity))).scalars().one()
    assert row.identity_id == identity.id
    # The plaintext token exists exactly once, in the response above. What
    # reaches disk is its digest, mirroring review_client.token_hash.
    assert row.token_hash == hash_token(session_token)
    assert session_token not in repr(row.__dict__)


async def test_device_poll_success_logs_dashboard_session_created(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The login names an audited event, exactly as every other admission decision does."""
    configured_fake(
        monkeypatch,
        poll_results=[DevicePollResult(status=DevicePollStatus.AUTHORIZED, access_token=GITHUB_TOKEN)],
    )

    with caplog.at_level(logging.DEBUG):
        response = await client.post("/auth/device/poll", json={"device_code": DEVICE_CODE})

    assert response.status_code == HTTPStatus.OK
    session.expire_all()
    identity = (await session.execute(select(GithubIdentity))).scalars().one()

    created = records_named(caplog, "dashboard_session_created")
    assert len(created) == 1
    assert created[0].levelno == logging.INFO
    assert created[0].__dict__["identity_id"] == str(identity.id)
    assert created[0].__dict__["github_login"] == "marge-bouvier"


async def test_device_poll_logs_neither_the_github_token_nor_the_session_token(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two secrets pass through this handler and neither may reach a log record.

    Fragments included: a prefix or suffix narrows a brute-force search space,
    so "only the first eight characters" is a leak with extra steps. The session
    token gets its own check because it is the one value this flow *mints* —
    nothing upstream has already established the habit of not printing it.
    """
    configured_fake(
        monkeypatch,
        poll_results=[DevicePollResult(status=DevicePollStatus.AUTHORIZED, access_token=GITHUB_TOKEN)],
    )

    with caplog.at_level(logging.DEBUG):
        response = await client.post("/auth/device/poll", json={"device_code": DEVICE_CODE})

    body = response.json()
    session_token = body["session_token"]
    for record in caplog.records:
        rendered = dump(record)
        for secret in (GITHUB_TOKEN, session_token):
            assert secret not in rendered
            assert secret[:8] not in rendered
            assert secret[-8:] not in rendered

    # The GitHub token is spent and dropped: it must never reach the response
    # body at all, in any field. The session token *is* meant to be there,
    # under its one designated field — so check every other field instead of
    # asserting its absence from the whole body.
    for field, value in body.items():
        rendered_value = str(value)
        assert GITHUB_TOKEN not in rendered_value
        assert GITHUB_TOKEN[:8] not in rendered_value
        assert GITHUB_TOKEN[-8:] not in rendered_value
        if field != "session_token":
            assert session_token not in rendered_value


async def test_dashboard_login_reuses_an_identity_a_cli_enrolment_already_created(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One GitHub account is one identity, however many ways it plugs in.

    The account is the unit of admission, not the machine and not the browser
    tab — so a person who already enrolled a client from the CLI must not end up
    with a second `github_identity` row when they open the dashboard.
    """
    fake = configured_fake(monkeypatch)
    registered = await client.post(
        "/clients",
        json={"name": "marge-mac-mini", "model_name": "test-model", "provider": "test", "tier": "standard"},
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
    )
    assert registered.status_code == HTTPStatus.CREATED

    fake.poll_results = [DevicePollResult(status=DevicePollStatus.AUTHORIZED, access_token=GITHUB_TOKEN)]
    response = await client.post("/auth/device/poll", json={"device_code": DEVICE_CODE})

    assert response.status_code == HTTPStatus.OK
    session.expire_all()
    identities = (await session.execute(select(GithubIdentity))).scalars().all()
    assert len(identities) == 1
