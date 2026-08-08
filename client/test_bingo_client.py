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

import json
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

    assert token == "gho_realtoken"  # noqa: S105 - fixture value
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

    assert token == "gho_realtoken"  # noqa: S105 - fixture value
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
