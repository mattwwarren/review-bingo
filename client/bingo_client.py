#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""bingo-client — the thin CLI a grid member runs.

The loop is the whole product surface:

    register (once)  →  check-in  →  lease → review → report  →  check-out

The hub never sees your prompts, model choice, or review config. The review
itself is whatever REVIEW_CMD you bring: a shell command that receives the
job as JSON on stdin and prints a report as JSON on stdout:

    {"verdict": "findings", "summary": "...", "findings": [{...}, ...]}

Point it at `claude -p`, an ollama wrapper, a two-model cheap-fix/expensive-
review loop — your compute, your call. Without REVIEW_CMD the client submits
a canned demo report (clearly labelled) so the loop can be exercised offline.

Joining the grid needs a GitHub identity. `login` runs GitHub's device flow
here on your machine — you type a code into github.com, and the hub is never
in the middle: it receives the resulting user token, reads your login and repo
access from it once, and discards it.

Usage:
    bingo_client.py login     --hub URL --name NAME --model MODEL --provider P [--client-id ID]
    bingo_client.py register  --hub URL --name NAME --model MODEL --provider P --enrolment-token TOKEN
    bingo_client.py check-in  [--state PATH]
    bingo_client.py run-once  [--state PATH]      # lease → review → report (one round)
    bingo_client.py loop      [--state PATH] [--idle-seconds N]
    bingo_client.py check-out [--state PATH]

`login` is the normal path. `register` is the same call with the credential
supplied directly — for a hub running CLIENT_ENROLMENT_MODE=dev, or for a
GitHub token you already hold.

State (hub URL + bearer token) lives in --state (default
~/.config/review-bingo/client.json), written once by `login`/`register`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import httpx

DEFAULT_STATE_PATH = Path.home() / ".config" / "review-bingo" / "client.json"

# GitHub's device flow endpoints live on github.com, not api.github.com.
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 - URL, not a credential
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceFlowError(RuntimeError):
    """GitHub ended the device flow without issuing a token."""


def load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(f"No client state at {path} — run `register` first.")
    return json.loads(path.read_text())


def save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")
    path.chmod(0o600)


def api(state: dict[str, str]) -> httpx.Client:
    return httpx.Client(
        base_url=state["hub_url"],
        headers={"Authorization": f"Bearer {state['token']}"},
        timeout=30.0,
    )


def canned_report(job: dict[str, Any]) -> dict[str, Any]:
    """Demo fallback when no REVIEW_CMD is configured."""
    return {
        "verdict": "findings",
        "summary": (
            "**[demo report — no REVIEW_CMD configured]** "
            f"Pretend round for `{job['repo_full_name']}#{job['pr_number']}` "
            f"at `{job['head_sha'][:12]}`."
        ),
        "findings": [
            {
                "file": "demo/example.py",
                "line": 1,
                "title": "This finding is canned; set REVIEW_CMD to bring your own reviewer",
            }
        ],
    }


def run_review(job: dict[str, Any]) -> dict[str, Any]:
    """Run the operator's reviewer over the job, or fall back to the canned demo."""
    review_cmd = os.environ.get("REVIEW_CMD")
    if not review_cmd:
        return canned_report(job)

    completed = subprocess.run(  # noqa: S602 - operator-supplied command is the point
        review_cmd,
        shell=True,
        input=json.dumps(job),
        capture_output=True,
        text=True,
        check=True,
    )
    report: dict[str, Any] = json.loads(completed.stdout)
    return report


@contextlib.contextmanager
def _owned_or_shared_client(http: httpx.Client | None) -> Generator[httpx.Client]:
    """Use the injected client (tests), or own a fresh one and close it after."""
    if http is not None:
        yield http
        return
    with httpx.Client(timeout=30.0) as client:
        yield client


def _request_device_code(
    client: httpx.Client,
    client_id: str,
    echo: Callable[[str], None],
) -> dict[str, Any]:
    """Ask GitHub for a device code, print the code the operator must enter."""
    response = client.post(
        GITHUB_DEVICE_CODE_URL,
        data={"client_id": client_id, "scope": "read:user"},
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    grant: dict[str, Any] = response.json()
    if "device_code" not in grant:
        error_msg = f"GitHub refused the device-code request: {grant.get('error', grant)}"
        raise DeviceFlowError(error_msg)

    echo(f"\nOpen {grant['verification_uri']} and enter code:  {grant['user_code']}\n")
    return grant


def _poll_for_access_token(
    client: httpx.Client,
    client_id: str,
    grant: dict[str, Any],
    sleep: Callable[[float], None],
) -> str:
    """Poll GitHub's token endpoint until it issues a token or definitively refuses.

    Every one of GitHub's poll responses means something different, and only
    two of them mean "keep going". Treating them all as "not yet" is how a
    client ends up polling github.com forever after the user clicked Deny.
    """
    interval = float(grant.get("interval", 5))
    while True:
        sleep(interval)
        response = client.post(
            GITHUB_ACCESS_TOKEN_URL,
            data={"client_id": client_id, "device_code": grant["device_code"], "grant_type": DEVICE_GRANT_TYPE},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        body = response.json()
        if "access_token" in body:
            return str(body["access_token"])

        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            # GitHub dictates the new floor; ignoring it earns a ban, not a token.
            interval = float(body.get("interval", interval + 5))
            continue
        if error == "expired_token":
            error_msg = "The device code expired before it was authorized — run `login` again."
            raise DeviceFlowError(error_msg)
        if error == "access_denied":
            error_msg = "Authorization was denied on github.com — nothing was enrolled."
            raise DeviceFlowError(error_msg)
        error_msg = f"GitHub ended the device flow: {error or body}"
        raise DeviceFlowError(error_msg)


def device_flow_login(
    client_id: str,
    *,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = print,
) -> str:
    """Run GitHub's device flow and return a user access token."""
    with _owned_or_shared_client(http) as client:
        grant = _request_device_code(client, client_id, echo)
        return _poll_for_access_token(client, client_id, grant, sleep)


def enrol_with_hub(
    hub: str,
    payload: dict[str, Any],
    enrolment_token: str,
    *,
    http: httpx.Client | None = None,
) -> dict[str, Any]:
    """POST /clients with the enrolment credential; returns the hub's response."""
    with _owned_or_shared_client(http) as client:
        response = client.post(
            f"{hub.rstrip('/')}/clients",
            json=payload,
            headers={"Authorization": f"Bearer {enrolment_token}"},
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body


def registration_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "name": args.name,
        "model_name": args.model,
        "provider": args.provider,
        "tier": args.tier,
        "quant": args.quant,
    }


def cmd_register(args: argparse.Namespace) -> None:
    body = enrol_with_hub(args.hub, registration_payload(args), args.enrolment_token)
    save_state(args.state, {"hub_url": args.hub.rstrip("/"), "token": body["token"]})
    print(f"Registered {args.name} ({body['client']['id']}); token stored in {args.state}")


def cmd_login(args: argparse.Namespace) -> None:
    client_id = args.client_id or os.environ.get("GITHUB_APP_CLIENT_ID")
    if not client_id:
        sys.exit("No GitHub App client id — pass --client-id or set GITHUB_APP_CLIENT_ID.")

    try:
        token = device_flow_login(client_id)
    except DeviceFlowError as exc:
        sys.exit(str(exc))

    # The token goes to the hub once and is never written to disk: the hub
    # reads identity from it and drops it, and so do we.
    body = enrol_with_hub(args.hub, registration_payload(args), token)
    save_state(args.state, {"hub_url": args.hub.rstrip("/"), "token": body["token"]})
    print(f"Enrolled {args.name} ({body['client']['id']}); token stored in {args.state}")


def cmd_check_in(args: argparse.Namespace) -> None:
    with api(load_state(args.state)) as client:
        response = client.post("/clients/check-in")
        response.raise_for_status()
    print("Checked in — plugged into the grid.")


def cmd_check_out(args: argparse.Namespace) -> None:
    with api(load_state(args.state)) as client:
        response = client.post("/clients/check-out")
        response.raise_for_status()
    print("Checked out — compute is yours again.")


def one_round(client: httpx.Client) -> bool:
    """Lease and complete one job. Returns False when the queue is dry."""
    response = client.post("/jobs/lease")
    response.raise_for_status()
    lease = response.json()
    if lease is None:
        return False

    job = lease["job"]
    print(f"Leased {job['repo_full_name']}#{job['pr_number']} @ {job['head_sha'][:12]} (job {job['id']})")

    report = run_review(job)
    response = client.post(f"/jobs/{job['id']}/report", json=report)
    response.raise_for_status()
    reported = response.json()
    print(f"Reported: verdict={reported['verdict']} state={reported['state']}")
    return True


def cmd_run_once(args: argparse.Namespace) -> None:
    with api(load_state(args.state)) as client:
        if not one_round(client):
            print("Queue is dry — nothing to review.")


def cmd_loop(args: argparse.Namespace) -> None:
    print(f"Looping; polling every {args.idle_seconds}s when dry. Ctrl-C to stop.")
    with api(load_state(args.state)) as client:
        while True:
            try:
                worked = one_round(client)
            except httpx.HTTPStatusError as exc:
                print(f"hub error: {exc}", file=sys.stderr)
                worked = False
            if not worked:
                time.sleep(args.idle_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bingo-client", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="Client state file")

    enrolment = argparse.ArgumentParser(add_help=False)
    enrolment.add_argument("--hub", required=True, help="Hub base URL, e.g. http://localhost:7575")
    enrolment.add_argument("--name", required=True)
    enrolment.add_argument("--model", required=True, help="Model you review with")
    enrolment.add_argument("--provider", required=True, help="Where it runs (ollama, anthropic, vllm, ...)")
    enrolment.add_argument("--tier", default="standard", choices=["experimental", "standard", "frontier"])
    enrolment.add_argument("--quant", default=None)

    login = sub.add_parser(
        "login",
        parents=[common, enrolment],
        help="Authorize with GitHub (device flow) and join the grid",
    )
    login.add_argument("--client-id", default=None, help="GitHub App client id (or set GITHUB_APP_CLIENT_ID)")
    login.set_defaults(func=cmd_login)

    register = sub.add_parser(
        "register",
        parents=[common, enrolment],
        help="Join the grid with an enrolment credential you already hold",
    )
    # Required, with no default: the hub decides what a valid credential is,
    # and a client that guesses one just gets a 401 it cannot explain.
    register.add_argument(
        "--enrolment-token",
        required=True,
        help="GitHub user token, or the hub's CLIENT_ENROLMENT_SECRET in dev mode",
    )
    register.set_defaults(func=cmd_register)

    for name, func, help_text in [
        ("check-in", cmd_check_in, "Declare availability"),
        ("check-out", cmd_check_out, "Leave the grid"),
        ("run-once", cmd_run_once, "Lease and complete a single round"),
        ("loop", cmd_loop, "Keep serving rounds until interrupted"),
    ]:
        p = sub.add_parser(name, parents=[common], help=help_text)
        if name == "loop":
            p.add_argument("--idle-seconds", type=int, default=15)
        p.set_defaults(func=func)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
