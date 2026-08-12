"""Tests for the client's GitHub device flow and enrolment call.

Run: uv run --with pytest --with httpx pytest client/test_bingo_client.py

The device flow runs entirely between the operator's machine and github.com —
the hub is never in the middle and never sees the user's GitHub credentials.
What's worth pinning is the poll loop: GitHub answers "not yet" far more often
than it answers with a token, and every one of its refusal codes means
something different. Treating them all as "keep polling" is how a client ends
up hammering github.com forever after the user clicked Deny.
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

import bingo_client


def http_with(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class RecordingSleep:
    """Stand-in for time.sleep that records what it was asked to wait."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def device_code_grant(**overrides: Any) -> dict[str, Any]:
    grant = {
        "device_code": "3584d83530557fdd1f46af8289938c8ef79f9dc5",
        "user_code": "WDJB-MJHT",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 899,
        "interval": 5,
    }
    grant.update(overrides)
    return grant


def test_device_flow_login_polls_until_authorized() -> None:
    responses = [
        httpx.Response(200, json=device_code_grant()),
        httpx.Response(200, json={"error": "authorization_pending"}),
        httpx.Response(200, json={"error": "authorization_pending"}),
        httpx.Response(200, json={"access_token": "gho_realtoken", "token_type": "bearer", "scope": ""}),
    ]
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "body": request.content.decode()})
        return responses.pop(0)

    sleep = RecordingSleep()
    with http_with(handler) as http:
        token = bingo_client.device_flow_login("Iv23liCLIENTID", http=http, sleep=sleep, echo=lambda _: None)

    assert token.access_token == "gho_realtoken"  # noqa: S105 - fixture value
    assert seen[0]["url"] == bingo_client.GITHUB_DEVICE_CODE_URL
    assert "Iv23liCLIENTID" in seen[0]["body"]
    assert seen[1]["url"] == bingo_client.GITHUB_ACCESS_TOKEN_URL
    assert sleep.waits == [5, 5, 5]


def test_device_flow_login_honors_slow_down_interval() -> None:
    responses = [
        httpx.Response(200, json=device_code_grant(interval=5)),
        httpx.Response(200, json={"error": "slow_down", "interval": 10}),
        httpx.Response(200, json={"access_token": "gho_realtoken"}),
    ]
    sleep = RecordingSleep()
    with http_with(lambda request: responses.pop(0)) as http:
        token = bingo_client.device_flow_login("Iv23liCLIENTID", http=http, sleep=sleep, echo=lambda _: None)

    assert token.access_token == "gho_realtoken"  # noqa: S105 - fixture value
    # First poll at the granted interval, second at the one GitHub demanded.
    assert sleep.waits == [5, 10]


def test_device_flow_login_raises_on_expired_token() -> None:
    responses = [
        httpx.Response(200, json=device_code_grant()),
        httpx.Response(200, json={"error": "expired_token"}),
    ]
    with (
        http_with(lambda request: responses.pop(0)) as http,
        pytest.raises(bingo_client.DeviceFlowError) as excinfo,
    ):
        bingo_client.device_flow_login("Iv23liCLIENTID", http=http, sleep=RecordingSleep(), echo=lambda _: None)

    assert "expired" in str(excinfo.value)


def test_device_flow_login_raises_on_access_denied() -> None:
    responses = [
        httpx.Response(200, json=device_code_grant()),
        httpx.Response(200, json={"error": "access_denied"}),
    ]
    with (
        http_with(lambda request: responses.pop(0)) as http,
        pytest.raises(bingo_client.DeviceFlowError) as excinfo,
    ):
        bingo_client.device_flow_login("Iv23liCLIENTID", http=http, sleep=RecordingSleep(), echo=lambda _: None)

    assert "denied" in str(excinfo.value)


def test_device_flow_login_raises_when_github_rejects_the_client_id() -> None:
    responses = [httpx.Response(200, json={"error": "unauthorized_client"})]
    with (
        http_with(lambda request: responses.pop(0)) as http,
        pytest.raises(bingo_client.DeviceFlowError),
    ):
        bingo_client.device_flow_login("nope", http=http, sleep=RecordingSleep(), echo=lambda _: None)


def test_cmd_register_requires_enrolment_token_flag() -> None:
    parser = bingo_client.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "register",
                "--hub",
                "http://hub.test",
                "--name",
                "box",
                "--model",
                "m",
                "--provider",
                "p",
            ]
        )


def test_cmd_register_accepts_an_enrolment_token() -> None:
    parser = bingo_client.build_parser()

    args = parser.parse_args(
        [
            "register",
            "--hub",
            "http://hub.test",
            "--name",
            "box",
            "--model",
            "m",
            "--provider",
            "p",
            "--enrolment-token",
            "gho_realtoken",
        ]
    )

    assert args.enrolment_token == "gho_realtoken"  # noqa: S105 - fixture value


def test_cmd_register_sends_token_as_bearer_header() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"client": {"id": "client-1"}, "token": "hub-minted-token"})

    payload = {"name": "box", "model_name": "m", "provider": "p", "tier": "standard", "quant": None}
    with http_with(handler) as http:
        body = bingo_client.enrol_with_hub("http://hub.test/", payload, "gho_realtoken", http=http)

    assert seen["url"] == "http://hub.test/clients"
    assert seen["authorization"] == "Bearer gho_realtoken"
    assert seen["body"] == payload
    assert body["token"] == "hub-minted-token"  # noqa: S105 - fixture value


def test_enrol_with_hub_surfaces_a_rejected_credential() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Enrolment credential rejected"})

    with http_with(handler) as http, pytest.raises(httpx.HTTPStatusError):
        bingo_client.enrol_with_hub("http://hub.test", {}, "bad-token", http=http)


# ---------------------------------------------------------------------------
# check-in re-attestation
# ---------------------------------------------------------------------------
#
# Re-attestation is opt-in because `loop` runs unattended: a check-in that
# re-ran the device flow by default would stop a long-running client dead,
# waiting for a human to type a code into github.com that nobody is watching
# for. So the default stays a bare heartbeat and the hub's TTL, not the CLI,
# decides the refresh cadence.


def test_check_in_parser_defaults_to_no_reattest() -> None:
    parser = bingo_client.build_parser()

    args = parser.parse_args(["check-in"])

    assert args.reattest is False


def test_check_in_parser_accepts_reattest_and_client_id() -> None:
    parser = bingo_client.build_parser()

    args = parser.parse_args(["check-in", "--reattest", "--client-id", "Iv23liCLIENTID"])

    assert args.reattest is True
    assert args.client_id == "Iv23liCLIENTID"


def test_check_in_payload_is_none_without_a_token() -> None:
    """None, not {}: httpx sends it as an empty body, which is the old heartbeat exactly."""
    assert bingo_client.check_in_payload(None) is None


def test_check_in_payload_carries_the_token() -> None:
    assert bingo_client.check_in_payload("gho_realtoken") == {"github_token": "gho_realtoken"}


# ---------------------------------------------------------------------------
# Unattended re-attestation: token sets, refresh grants, and `loop`
# ---------------------------------------------------------------------------
#
# `check-in --reattest` above is the *attended* answer to an expiring access
# snapshot: a human is there to type a code into github.com. `loop` is not
# attended, so it needs a credential it can spend on its own — GitHub's refresh
# token — and a cadence it did not invent. Both halves are pinned below: what
# the client stores, what it sends GitHub, and what makes it give up and tell
# an operator to run `login` rather than hammering a 409 forever.

GITHUB_EPOCH = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)

# GitHub's documented lifetimes for the two credentials: 8h and ~6 months.
ACCESS_TOKEN_LIFETIME = 28800
REFRESH_TOKEN_LIFETIME = 15811200


class FakeClock:
    """Hand-advanced stand-in for `datetime.now(UTC)`.

    A TTL boundary is the whole subject here, and the only way to cross one
    deterministically is to move the clock by hand.
    """

    def __init__(self, start: datetime = GITHUB_EPOCH) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def expiring_token_grant(**overrides: Any) -> dict[str, Any]:
    """GitHub's success body when "Expire user authorization tokens" is ON.

    Shaped from GitHub's published contract ("Refreshing user access tokens" on
    docs.github.com), which is the closest thing to an observation available
    here: neither test environment has a live GitHub App to capture a real
    response from. The field names, and the fact that they arrive as a *group*,
    come from those docs — not captured, and not invented from what this client
    happens to want.
    """
    body: dict[str, Any] = {
        "access_token": "ghu_expiringtoken",
        "expires_in": ACCESS_TOKEN_LIFETIME,
        "refresh_token": "ghr_refreshtoken",
        "refresh_token_expires_in": REFRESH_TOKEN_LIFETIME,
        "scope": "",
        "token_type": "bearer",
    }
    body.update(overrides)
    return body


def bare_token_grant() -> dict[str, Any]:
    """The same endpoint's body when the App's expiring-tokens setting is OFF.

    Per the same docs the expiry fields are absent *as a group*, not present and
    null — which is exactly the distinction the client has to survive, since it
    is what decides whether unattended renewal is possible at all.
    """
    return {"access_token": "gho_nonexpiring", "token_type": "bearer", "scope": ""}


def leased_job(job_id: str = "job-1") -> dict[str, Any]:
    """A `POST /jobs/lease` body. Our own hub's shape, so hand-written is fine."""
    return {
        "job": {
            "id": job_id,
            "repo_full_name": "acme/payments",
            "pr_number": 7,
            "head_sha": "0123456789abcdef0123456789abcdef01234567",
        },
        "lease_expires_at": "2026-08-12T12:00:00+00:00",
    }


class RecordingHub:
    """Mock-transport stand-in for the hub, counting the calls `run_loop` makes.

    `seconds_per_round` moves the caller's clock on every lease, so a run whose
    rounds all *succeed* (and therefore never idle) can still cross a TTL
    boundary — reviewing a PR takes time in the real world too.
    """

    def __init__(
        self,
        *,
        leases: list[httpx.Response] | None = None,
        ttl_seconds: int = 8 * 60 * 60,
        clock: FakeClock | None = None,
        seconds_per_round: float = 0.0,
    ) -> None:
        self.leases = leases if leases is not None else []
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.seconds_per_round = seconds_per_round
        self.paths: list[str] = []
        self.check_in_bodies: list[Any] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path == "/clients/check-in":
            self.check_in_bodies.append(json.loads(request.content) if request.content else None)
            return httpx.Response(
                200,
                json={"status": "checked_in", "identity_access_ttl_seconds": self.ttl_seconds},
            )
        if path == "/jobs/lease":
            if self.clock is not None:
                self.clock.advance(self.seconds_per_round)
            return self.leases.pop(0) if self.leases else httpx.Response(200, json=None)
        if path.endswith("/report"):
            return httpx.Response(200, json={"verdict": "findings", "state": "reported"})
        raise AssertionError(f"unexpected hub call: {path}")

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="http://hub.test", transport=httpx.MockTransport(self.handler))

    @property
    def check_ins(self) -> int:
        return self.paths.count("/clients/check-in")

    @property
    def leases_served(self) -> int:
        return self.paths.count("/jobs/lease")


def forbidden_github(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no GitHub call expected, got {request.url}")


def login_args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    return bingo_client.build_parser().parse_args(
        [
            "login",
            "--hub",
            "http://hub.test",
            "--name",
            "box",
            "--model",
            "m",
            "--provider",
            "p",
            "--client-id",
            "Iv23liCLIENTID",
            "--state",
            str(tmp_path / "client.json"),
            *extra,
        ]
    )


def loop_args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    return bingo_client.build_parser().parse_args(
        ["loop", "--state", str(tmp_path / "client.json"), "--client-id", "Iv23liCLIENTID", *extra]
    )


def enrolled_state(**extra: str) -> dict[str, str]:
    state = {"hub_url": "http://hub.test", "token": "hub-minted"}
    state.update(extra)
    return state


def write_state(tmp_path: Path, state: dict[str, str]) -> Path:
    path = tmp_path / "client.json"
    path.write_text(json.dumps(state))
    return path


# --- the token set the device flow now hands back -------------------------


def test_device_flow_login_returns_the_whole_token_set() -> None:
    responses = [
        httpx.Response(200, json=device_code_grant()),
        httpx.Response(200, json=expiring_token_grant()),
    ]
    clock = FakeClock()
    with http_with(lambda request: responses.pop(0)) as http:
        tokens = bingo_client.device_flow_login(
            "Iv23liCLIENTID", http=http, sleep=RecordingSleep(), echo=lambda _: None, now=clock
        )

    assert tokens.access_token == "ghu_expiringtoken"  # noqa: S105 - fixture value
    assert tokens.refresh_token == "ghr_refreshtoken"  # noqa: S105 - fixture value
    assert tokens.access_token_expires_at == GITHUB_EPOCH + timedelta(seconds=ACCESS_TOKEN_LIFETIME)
    assert tokens.refresh_token_expires_at == GITHUB_EPOCH + timedelta(seconds=REFRESH_TOKEN_LIFETIME)


def test_device_flow_login_token_set_is_bare_when_expiry_is_disabled() -> None:
    """Expiring tokens off: no refresh token, and no expiry to compare against."""
    responses = [
        httpx.Response(200, json=device_code_grant()),
        httpx.Response(200, json=bare_token_grant()),
    ]
    with http_with(lambda request: responses.pop(0)) as http:
        tokens = bingo_client.device_flow_login(
            "Iv23liCLIENTID", http=http, sleep=RecordingSleep(), echo=lambda _: None, now=FakeClock()
        )

    assert tokens.access_token == "gho_nonexpiring"  # noqa: S105 - fixture value
    assert tokens.refresh_token is None
    assert tokens.access_token_expires_at is None
    assert tokens.refresh_token_expires_at is None


# --- spending a refresh token ---------------------------------------------


def test_refresh_access_token_sends_the_documented_grant_and_no_client_secret() -> None:
    """GitHub documents `client_secret` as *not* required for device-flow tokens.

    Sending one anyway would mean a grid client had to hold the App's secret,
    which is the thing the device flow exists to avoid.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = dict(urllib.parse.parse_qsl(request.content.decode()))
        return httpx.Response(200, json=expiring_token_grant())

    clock = FakeClock()
    with http_with(handler) as http:
        tokens = bingo_client.refresh_access_token("Iv23liCLIENTID", "ghr_oldrefresh", http=http, now=clock)

    assert seen["url"] == bingo_client.GITHUB_ACCESS_TOKEN_URL
    assert seen["body"] == {
        "client_id": "Iv23liCLIENTID",
        "grant_type": "refresh_token",
        "refresh_token": "ghr_oldrefresh",
    }
    assert tokens.access_token == "ghu_expiringtoken"  # noqa: S105 - fixture value
    assert tokens.refresh_token == "ghr_refreshtoken"  # noqa: S105 - fixture value
    assert tokens.access_token_expires_at == GITHUB_EPOCH + timedelta(seconds=ACCESS_TOKEN_LIFETIME)


def test_refresh_access_token_raises_when_github_returns_no_token() -> None:
    """GitHub documents no error taxonomy for this grant, so surface its text verbatim.

    Guessing a code would be inventing a contract for a system we do not own;
    the response either carries an access token or it does not.
    """
    refusal = {"error": "bad_refresh_token", "error_description": "The refresh token passed is incorrect or expired."}
    with (
        http_with(lambda request: httpx.Response(200, json=refusal)) as http,
        pytest.raises(bingo_client.DeviceFlowError) as excinfo,
    ):
        bingo_client.refresh_access_token("Iv23liCLIENTID", "ghr_oldrefresh", http=http)

    assert "incorrect or expired" in str(excinfo.value)


# --- what `login` writes down ---------------------------------------------


def test_cmd_login_persists_the_github_token_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    tokens = bingo_client.GithubTokenSet(
        access_token="ghu_expiringtoken",  # noqa: S106 - fixture value
        refresh_token="ghr_refreshtoken",  # noqa: S106 - fixture value
        access_token_expires_at=clock() + timedelta(seconds=ACCESS_TOKEN_LIFETIME),
        refresh_token_expires_at=clock() + timedelta(seconds=REFRESH_TOKEN_LIFETIME),
    )
    monkeypatch.setattr(bingo_client, "device_flow_login", lambda *a, **k: tokens)
    monkeypatch.setattr(
        bingo_client, "enrol_with_hub", lambda *a, **k: {"client": {"id": "client-1"}, "token": "hub-minted"}
    )
    args = login_args(tmp_path)

    bingo_client.cmd_login(args)

    state = json.loads(args.state.read_text())
    assert state["github_access_token"] == "ghu_expiringtoken"
    assert state["github_refresh_token"] == "ghr_refreshtoken"
    assert state["github_access_token_expires_at"] == (clock() + timedelta(seconds=ACCESS_TOKEN_LIFETIME)).isoformat()
    assert state["github_refresh_token_expires_at"] == (clock() + timedelta(seconds=REFRESH_TOKEN_LIFETIME)).isoformat()
    assert args.state.stat().st_mode & 0o777 == 0o600


def test_cmd_login_warns_once_when_github_issues_no_refresh_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No refresh token means no unattended renewal — say so, once, and name the setting."""
    tokens = bingo_client.GithubTokenSet(access_token="gho_nonexpiring")  # noqa: S106 - fixture value
    monkeypatch.setattr(bingo_client, "device_flow_login", lambda *a, **k: tokens)
    monkeypatch.setattr(
        bingo_client, "enrol_with_hub", lambda *a, **k: {"client": {"id": "client-1"}, "token": "hub-minted"}
    )
    args = login_args(tmp_path)

    bingo_client.cmd_login(args)

    stderr = capsys.readouterr().err
    assert stderr.count("Expire user authorization tokens") == 1
    assert "loop" in stderr
    state = json.loads(args.state.read_text())
    assert state["github_access_token"] == "gho_nonexpiring"
    assert "github_refresh_token" not in state


def test_cmd_login_no_store_flag_persists_nothing_from_github(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The opt-out keeps today's posture exactly: the token is spent and dropped."""
    tokens = bingo_client.GithubTokenSet(
        access_token="ghu_expiringtoken",  # noqa: S106 - fixture value
        refresh_token="ghr_refreshtoken",  # noqa: S106 - fixture value
    )
    monkeypatch.setattr(bingo_client, "device_flow_login", lambda *a, **k: tokens)
    monkeypatch.setattr(
        bingo_client, "enrol_with_hub", lambda *a, **k: {"client": {"id": "client-1"}, "token": "hub-minted"}
    )
    args = login_args(tmp_path, "--no-store-github-token")

    bingo_client.cmd_login(args)

    state = json.loads(args.state.read_text())
    assert set(state) == {"hub_url", "token"}
    captured = capsys.readouterr()
    notice = captured.out + captured.err
    assert "--no-store-github-token" in notice
    assert "loop" in notice


def test_cmd_check_in_reattest_persists_the_refreshed_token_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = write_state(tmp_path, enrolled_state())
    clock = FakeClock()
    tokens = bingo_client.GithubTokenSet(
        access_token="ghu_freshtoken",  # noqa: S106 - fixture value
        refresh_token="ghr_freshrefresh",  # noqa: S106 - fixture value
        access_token_expires_at=clock() + timedelta(seconds=ACCESS_TOKEN_LIFETIME),
    )
    monkeypatch.setattr(bingo_client, "device_flow_login", lambda *a, **k: tokens)
    hub = RecordingHub()
    monkeypatch.setattr(bingo_client, "api", lambda state: hub.client())
    args = bingo_client.build_parser().parse_args(
        ["check-in", "--reattest", "--client-id", "Iv23liCLIENTID", "--state", str(state_path)]
    )

    bingo_client.cmd_check_in(args)

    assert hub.check_in_bodies == [{"github_token": "ghu_freshtoken"}]
    state = json.loads(state_path.read_text())
    assert state["github_access_token"] == "ghu_freshtoken"
    assert state["github_refresh_token"] == "ghr_freshrefresh"


# --- picking a usable credential without asking anybody -------------------


def test_renew_github_access_reuses_a_still_valid_stored_token(tmp_path: Path) -> None:
    clock = FakeClock()
    state = enrolled_state(
        github_access_token="ghu_stillgood",
        github_access_token_expires_at=(clock() + timedelta(hours=1)).isoformat(),
    )

    with http_with(forbidden_github) as http:
        tokens = bingo_client.renew_github_access(state, loop_args(tmp_path), http=http, now=clock)

    assert tokens is not None
    assert tokens.access_token == "ghu_stillgood"  # noqa: S105 - fixture value


def test_renew_github_access_reuses_a_token_that_never_expires(tmp_path: Path) -> None:
    """Expiring tokens disabled: no stored expiry, so the token stays usable forever.

    This is what keeps an unattended `login`-enrolled client re-attesting on an
    App that cannot issue refresh tokens at all — warned once at `login`, not
    broken.
    """
    state = enrolled_state(github_access_token="gho_nonexpiring")

    with http_with(forbidden_github) as http:
        tokens = bingo_client.renew_github_access(state, loop_args(tmp_path), http=http, now=FakeClock())

    assert tokens is not None
    assert tokens.access_token == "gho_nonexpiring"  # noqa: S105 - fixture value


def test_renew_github_access_spends_the_refresh_token_when_the_access_token_expired(tmp_path: Path) -> None:
    clock = FakeClock()
    state = enrolled_state(
        github_access_token="ghu_stale",
        github_refresh_token="ghr_oldrefresh",
        github_access_token_expires_at=(clock() - timedelta(seconds=1)).isoformat(),
        github_refresh_token_expires_at=(clock() + timedelta(days=100)).isoformat(),
    )

    with http_with(lambda request: httpx.Response(200, json=expiring_token_grant())) as http:
        tokens = bingo_client.renew_github_access(state, loop_args(tmp_path), http=http, now=clock)

    assert tokens is not None
    assert tokens.access_token == "ghu_expiringtoken"  # noqa: S105 - fixture value


def test_renew_github_access_returns_none_without_a_stored_token(tmp_path: Path) -> None:
    """A `register`-enrolled client has nothing to renew, and asks nobody."""
    with http_with(forbidden_github) as http:
        tokens = bingo_client.renew_github_access(enrolled_state(), loop_args(tmp_path), http=http, now=FakeClock())

    assert tokens is None


def test_renew_github_access_returns_none_when_the_refresh_token_itself_expired(tmp_path: Path) -> None:
    """Skip the round trip: GitHub would reach the same conclusion, slower."""
    clock = FakeClock()
    state = enrolled_state(
        github_access_token="ghu_stale",
        github_refresh_token="ghr_oldrefresh",
        github_access_token_expires_at=(clock() - timedelta(seconds=1)).isoformat(),
        github_refresh_token_expires_at=(clock() - timedelta(seconds=1)).isoformat(),
    )

    with http_with(forbidden_github) as http:
        tokens = bingo_client.renew_github_access(state, loop_args(tmp_path), http=http, now=clock)

    assert tokens is None


def test_renew_github_access_returns_none_when_github_refuses_the_refresh(tmp_path: Path) -> None:
    clock = FakeClock()
    state = enrolled_state(
        github_access_token="ghu_stale",
        github_refresh_token="ghr_oldrefresh",
        github_access_token_expires_at=(clock() - timedelta(seconds=1)).isoformat(),
    )
    refusal = {"error": "bad_refresh_token"}

    with http_with(lambda request: httpx.Response(200, json=refusal)) as http:
        tokens = bingo_client.renew_github_access(state, loop_args(tmp_path), http=http, now=clock)

    assert tokens is None


# --- the loop itself ------------------------------------------------------


def test_run_loop_reattests_across_the_ttl_with_a_non_expiring_token(tmp_path: Path) -> None:
    """The half-TTL cadence, learned from the hub and driven by a moving clock.

    Nothing here ever goes back to GitHub: the stored token has no expiry, so
    every re-attestation re-presents the same one. That is the whole point —
    an App with expiring tokens disabled must still be able to run unattended.
    """
    clock = FakeClock()
    ttl = 7200
    state = enrolled_state(github_access_token="gho_nonexpiring")
    state_path = write_state(tmp_path, state)
    hub = RecordingHub(
        leases=[httpx.Response(200, json=leased_job(f"job-{n}")) for n in range(3)],
        ttl_seconds=ttl,
        clock=clock,
        seconds_per_round=ttl / 2,
    )

    with hub.client() as hub_client, http_with(forbidden_github) as github:
        bingo_client.run_loop(
            hub_client,
            state,
            state_path,
            loop_args(tmp_path),
            idle_seconds=1,
            sleep=RecordingSleep(),
            now=clock,
            github_http=github,
            max_rounds=3,
        )

    assert hub.leases_served == 3
    assert hub.check_ins == 3
    assert hub.check_in_bodies == [{"github_token": "gho_nonexpiring"}] * 3


def test_run_loop_refreshes_an_expired_access_token_and_persists_it(tmp_path: Path) -> None:
    clock = FakeClock()
    state = enrolled_state(
        github_access_token="ghu_stale",
        github_refresh_token="ghr_oldrefresh",
        github_access_token_expires_at=(clock() - timedelta(seconds=1)).isoformat(),
    )
    state_path = write_state(tmp_path, state)
    hub = RecordingHub(leases=[httpx.Response(200, json=leased_job())])
    github_calls: list[str] = []

    def github_handler(request: httpx.Request) -> httpx.Response:
        github_calls.append(str(request.url))
        return httpx.Response(200, json=expiring_token_grant())

    with hub.client() as hub_client, http_with(github_handler) as github:
        bingo_client.run_loop(
            hub_client,
            state,
            state_path,
            loop_args(tmp_path),
            idle_seconds=1,
            sleep=RecordingSleep(),
            now=clock,
            github_http=github,
            max_rounds=1,
        )

    assert github_calls == [bingo_client.GITHUB_ACCESS_TOKEN_URL]
    assert hub.check_in_bodies == [{"github_token": "ghu_expiringtoken"}]
    persisted = json.loads(state_path.read_text())
    assert persisted["github_access_token"] == "ghu_expiringtoken"
    assert persisted["github_refresh_token"] == "ghr_refreshtoken"
    assert (
        persisted["github_access_token_expires_at"] == (clock() + timedelta(seconds=ACCESS_TOKEN_LIFETIME)).isoformat()
    )


def test_run_loop_recovers_from_a_staleness_409_by_refreshing(tmp_path: Path) -> None:
    """A 409 *after* this run already checked in is the staleness kind — renew and retry."""
    clock = FakeClock()
    state = enrolled_state(
        github_access_token="ghu_stale",
        github_refresh_token="ghr_oldrefresh",
        github_access_token_expires_at=(clock() - timedelta(seconds=1)).isoformat(),
    )
    state_path = write_state(tmp_path, state)
    hub = RecordingHub(
        leases=[
            httpx.Response(409, json={"detail": "Cached GitHub access has expired; check in again"}),
            httpx.Response(200, json=leased_job()),
        ]
    )

    with hub.client() as hub_client, http_with(lambda r: httpx.Response(200, json=expiring_token_grant())) as github:
        bingo_client.run_loop(
            hub_client,
            state,
            state_path,
            loop_args(tmp_path),
            idle_seconds=1,
            sleep=RecordingSleep(),
            now=clock,
            github_http=github,
            max_rounds=2,
        )

    assert hub.leases_served == 2
    assert hub.check_ins == 2
    assert hub.check_in_bodies == [{"github_token": "ghu_expiringtoken"}] * 2


def test_run_loop_exits_pointing_at_login_when_a_post_checkin_409_cannot_be_renewed(tmp_path: Path) -> None:
    """The one fatal path. Never a bare 409 loop, and never a silent one."""
    clock = FakeClock()
    state = enrolled_state(
        github_access_token="ghu_stale",
        github_refresh_token="ghr_oldrefresh",
        github_access_token_expires_at=(clock() - timedelta(seconds=1)).isoformat(),
    )
    state_path = write_state(tmp_path, state)
    conflict = {"detail": "Cached GitHub access has expired; check in again"}
    hub = RecordingHub(leases=[httpx.Response(409, json=conflict), httpx.Response(409, json=conflict)])

    with (
        hub.client() as hub_client,
        http_with(lambda r: httpx.Response(200, json={"error": "bad_refresh_token"})) as github,
        pytest.raises(SystemExit) as excinfo,
    ):
        bingo_client.run_loop(
            hub_client,
            state,
            state_path,
            loop_args(tmp_path),
            idle_seconds=1,
            sleep=RecordingSleep(),
            now=clock,
            github_http=github,
            max_rounds=3,
        )

    assert "bingo_client.py login" in str(excinfo.value)


def test_run_loop_treats_a_first_409_as_check_in_before_leasing(tmp_path: Path) -> None:
    """A `register`-enrolled client running `loop` must not read this as a credential failure.

    The hub checks `status != CHECKED_IN` *before* it checks staleness, so a 409
    reaching a run that has not checked in yet can only be "check in before
    leasing" — which a bare heartbeat fixes, with no GitHub credential involved.
    Mirrors the hub's own inert carve-out for a client with no linked identity.
    """
    state = enrolled_state()
    state_path = write_state(tmp_path, state)
    hub = RecordingHub(
        leases=[
            httpx.Response(409, json={"detail": "Check in before leasing"}),
            httpx.Response(200, json=leased_job()),
        ]
    )

    with hub.client() as hub_client, http_with(forbidden_github) as github:
        bingo_client.run_loop(
            hub_client,
            state,
            state_path,
            loop_args(tmp_path),
            idle_seconds=1,
            sleep=RecordingSleep(),
            now=FakeClock(),
            github_http=github,
            max_rounds=2,
        )

    assert hub.check_in_bodies == [None]
    assert hub.leases_served == 2


def test_run_loop_never_reattests_proactively_without_a_stored_token(tmp_path: Path) -> None:
    """Genuinely inert, not throttled: zero check-ins, not fewer check-ins."""
    state = enrolled_state()
    state_path = write_state(tmp_path, state)
    hub = RecordingHub(leases=[httpx.Response(200, json=leased_job(f"job-{n}")) for n in range(4)])

    with hub.client() as hub_client, http_with(forbidden_github) as github:
        bingo_client.run_loop(
            hub_client,
            state,
            state_path,
            loop_args(tmp_path),
            idle_seconds=1,
            sleep=RecordingSleep(),
            now=FakeClock(),
            github_http=github,
            max_rounds=4,
        )

    assert hub.check_ins == 0
    assert hub.leases_served == 4
