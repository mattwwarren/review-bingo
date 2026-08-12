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
                              [--no-store-github-token]
    bingo_client.py register  --hub URL --name NAME --model MODEL --provider P --enrolment-token TOKEN
    bingo_client.py check-in  [--state PATH] [--reattest [--client-id ID]]
    bingo_client.py run-once  [--state PATH]      # lease → review → report (one round)
    bingo_client.py loop      [--state PATH] [--idle-seconds N] [--client-id ID]
    bingo_client.py check-out [--state PATH]

`login` is the normal path. `register` is the same call with the credential
supplied directly — for a hub running CLIENT_ENROLMENT_MODE=dev, or for a
GitHub token you already hold.

The hub only serves work against repo access it read recently, and refuses to
lease once its snapshot ages out (`409`, "check in again"). `check-in
--reattest` re-runs the device flow and hands the hub a fresh token to refresh
it — the attended answer, because a device flow triggered on its own would sit
waiting for a code nobody is there to type.

`loop` needs an unattended one, so `login` stores what GitHub issued (0600,
client-side only, never sent to the hub except as the same one-shot access
token check-in has always carried) and `loop` renews itself from it: it
re-attests on half the TTL the hub reports, spending the refresh token when the
access token has expired. `--no-store-github-token` opts out and keeps today's
posture exactly — nothing persisted, and a manual `login` cadence becomes
yours. Either way, an attestation that cannot be renewed ends the loop with a
message rather than a 409 it retries forever.

State (hub URL + bearer token, plus the GitHub credentials unless opted out)
lives in --state (default ~/.config/review-bingo/client.json), written by
`login`/`register` and updated whenever a renewal succeeds.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx

DEFAULT_STATE_PATH = Path.home() / ".config" / "review-bingo" / "client.json"

# GitHub's device flow endpoints live on github.com, not api.github.com.
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 - URL, not a credential
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT_TYPE = "refresh_token"

# The GitHub credentials `login` stores, alongside hub_url/token in the same
# flat JSON object. All four are optional: an App with expiring tokens disabled
# issues only the first, and --no-store-github-token writes none of them.
GITHUB_TOKEN_STATE_KEYS = (
    "github_access_token",
    "github_refresh_token",
    "github_access_token_expires_at",
    "github_refresh_token_expires_at",
)
# The one key whose presence means "this client stored GitHub credentials".
PRIMARY_TOKEN_KEY = GITHUB_TOKEN_STATE_KEYS[0]

# Durable opt-out, written by `login --no-store-github-token`. Absent means
# "store" — state files from before the marker existed keep their behavior.
# Lives in state rather than in argv so every later persistence path
# (`check-in --reattest`, `loop` renewals) honors the enrolment-time choice
# without each command needing its own flag.
STORE_GITHUB_TOKEN_KEY = "store_github_token"  # noqa: S105 - marker name, not a credential
STORE_GITHUB_TOKEN_OPTED_OUT = "false"  # noqa: S105 - marker value, not a credential


def _github_storage_opted_out(state: dict[str, str]) -> bool:
    return state.get(STORE_GITHUB_TOKEN_KEY) == STORE_GITHUB_TOKEN_OPTED_OUT


ATTENDED_ONLY_NOTICE = (
    "--no-store-github-token: nothing GitHub issued was written to disk. `loop` "
    "cannot renew an attestation it has no credential for, so this client stays "
    "attended — re-run `bingo_client.py login` on your own cadence once the hub "
    "starts answering 409."
)

NO_UNATTENDED_RENEWAL_WARNING = (
    'GitHub issued no refresh token, so the App does not have "Expire user '
    'authorization tokens" enabled. Unattended renewal is therefore unavailable: '
    "`loop` will keep re-presenting the access token stored here, which never "
    "expires, but nothing can replace it once it is revoked — re-run "
    "`bingo_client.py login` then, or ask whoever runs the hub to tick that setting."
)

LOGIN_AGAIN = (
    "Cached GitHub access expired and nothing here can renew it — run "
    "`bingo_client.py login` to re-authorize this client."
)


class DeviceFlowError(RuntimeError):
    """GitHub ended the device flow without issuing a token."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _expires_at(issued_at: datetime, seconds: float | str | None) -> datetime | None:
    """Absolute expiry from GitHub's relative `*_expires_in`, when it sent one.

    GitHub omits the expiry fields as a *group* when the App has expiring tokens
    disabled — they do not arrive null — so "no field" and "never expires" are
    the same answer, and the absent case is not an error.
    """
    if seconds is None:
        return None
    return issued_at + timedelta(seconds=int(seconds))


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _parse_expiry(stored: str | None) -> datetime | None:
    return datetime.fromisoformat(stored) if stored else None


@dataclass(frozen=True)
class GithubTokenSet:
    """What GitHub issued, in both of the shapes it issues it in.

    With the App's "Expire user authorization tokens" setting on, every field is
    populated and this client can renew itself unattended. With it off, only
    `access_token` arrives — a token that never expires and can never be
    refreshed, which is a weaker position but not a broken one.
    """

    access_token: str
    refresh_token: str | None = None
    access_token_expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None

    @classmethod
    def from_grant(cls, body: dict[str, Any], issued_at: datetime) -> GithubTokenSet:
        """Read a device-flow or refresh-grant success body. Same shape, same fields."""
        return cls(
            access_token=str(body["access_token"]),
            refresh_token=str(body["refresh_token"]) if body.get("refresh_token") else None,
            access_token_expires_at=_expires_at(issued_at, body.get("expires_in")),
            refresh_token_expires_at=_expires_at(issued_at, body.get("refresh_token_expires_in")),
        )

    @classmethod
    def from_state(cls, state: dict[str, str]) -> GithubTokenSet | None:
        """The stored credentials, or None for a client that never stored any."""
        access_key, refresh_key, access_expiry_key, refresh_expiry_key = GITHUB_TOKEN_STATE_KEYS
        access_token = state.get(access_key)
        if not access_token:
            return None
        return cls(
            access_token=access_token,
            refresh_token=state.get(refresh_key),
            access_token_expires_at=_parse_expiry(state.get(access_expiry_key)),
            refresh_token_expires_at=_parse_expiry(state.get(refresh_expiry_key)),
        )

    def as_state_fields(self) -> dict[str, str]:
        """Flat state-file keys, omitted rather than null when GitHub sent nothing."""
        values = (
            self.access_token,
            self.refresh_token,
            _iso(self.access_token_expires_at),
            _iso(self.refresh_token_expires_at),
        )
        return {key: value for key, value in zip(GITHUB_TOKEN_STATE_KEYS, values, strict=True) if value is not None}


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
) -> dict[str, Any]:
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
        body: dict[str, Any] = response.json()
        if "access_token" in body:
            # The whole body, not just the token: the expiry and refresh fields
            # beside it are what make unattended renewal possible at all.
            return body

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
    now: Callable[[], datetime] = _utcnow,
) -> GithubTokenSet:
    """Run GitHub's device flow and return the credentials it issued."""
    with _owned_or_shared_client(http) as client:
        grant = _request_device_code(client, client_id, echo)
        body = _poll_for_access_token(client, client_id, grant, sleep)
        # Clock read after the poll, not before: GitHub's `expires_in` counts
        # from issuance, and polling can sit there for minutes.
        return GithubTokenSet.from_grant(body, now())


def refresh_access_token(
    client_id: str,
    refresh_token: str,
    *,
    http: httpx.Client | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> GithubTokenSet:
    """Trade a refresh token for a fresh access token, with nobody watching.

    No `client_secret`. GitHub documents it as required *unless* the token was
    generated by the device flow, and this client obtains tokens no other way —
    sending one would mean every grid member had to hold the App's secret, which
    is the thing the device flow exists to avoid.

    GitHub publishes no error taxonomy for this grant, unlike the device-poll
    codes above where each refusal means something different and is handled
    separately. So the only distinction drawn here is the one the response
    actually makes: it either carries an access token or it does not.
    """
    with _owned_or_shared_client(http) as client:
        response = client.post(
            GITHUB_ACCESS_TOKEN_URL,
            data={"client_id": client_id, "grant_type": REFRESH_GRANT_TYPE, "refresh_token": refresh_token},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        if "access_token" not in body:
            reason = body.get("error_description") or body.get("error") or body
            error_msg = f"GitHub refused the refresh grant: {reason}"
            raise DeviceFlowError(error_msg)
        return GithubTokenSet.from_grant(body, now())


def renew_github_access(
    state: dict[str, str],
    args: argparse.Namespace,
    *,
    http: httpx.Client | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> GithubTokenSet | None:
    """The best GitHub credential this client can produce without a human.

    Never raises: every failure here means "none available", and the one caller
    already has to handle that answer. Cheapest first — a stored access token
    that has not expired is spent again rather than refreshed, which is exactly
    what lets a client enrolled against an App with expiring tokens *disabled*
    (no stored expiry, no refresh token ever issued) go on re-attesting
    indefinitely instead of failing on its first renewal.
    """
    stored = GithubTokenSet.from_state(state)
    if stored is None:
        return None
    if stored.access_token_expires_at is None or stored.access_token_expires_at > now():
        return stored

    # The access token is spent, so a refresh is the only way left — and it
    # needs three things at once: a refresh token, one that has not itself
    # expired (asking GitHub would reach the same conclusion, one round trip
    # later), and the App client id to present it with.
    refresh_token = stored.refresh_token
    client_id = args.client_id or os.environ.get("GITHUB_APP_CLIENT_ID")
    refresh_expired = stored.refresh_token_expires_at is not None and stored.refresh_token_expires_at <= now()
    if refresh_token is None or refresh_expired or not client_id:
        return None
    try:
        return refresh_access_token(client_id, refresh_token, http=http, now=now)
    except (DeviceFlowError, httpx.HTTPError):
        return None


def _persist_github_tokens(state: dict[str, str], path: Path, tokens: GithubTokenSet) -> None:
    """Replace the stored GitHub credentials wholesale, in the same 0600 file.

    Cleared before updating rather than merged over: a refreshed set that no
    longer carries a refresh token must not leave the previous one behind on
    disk, still readable and no longer good for anything.

    The durable opt-out is honored here, centrally, so every caller inherits
    it: opted-out state never gains credentials, and any strays are removed —
    the "opted-out state holds no GitHub credential" invariant self-heals
    rather than trusting every past writer to have got it right.
    """
    for key in GITHUB_TOKEN_STATE_KEYS:
        state.pop(key, None)
    if not _github_storage_opted_out(state):
        state.update(tokens.as_state_fields())
    save_state(path, state)


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


def github_token_via_device_flow(args: argparse.Namespace) -> GithubTokenSet:
    """Run the device flow for `--client-id`/GITHUB_APP_CLIENT_ID, or exit explaining how.

    Shared by `login` and `check-in --reattest`, which need the identical three
    steps: find the App's client id, run the flow, turn a DeviceFlowError into a
    message rather than a traceback.
    """
    client_id = args.client_id or os.environ.get("GITHUB_APP_CLIENT_ID")
    if not client_id:
        sys.exit("No GitHub App client id — pass --client-id or set GITHUB_APP_CLIENT_ID.")
    try:
        return device_flow_login(client_id)
    except DeviceFlowError as exc:
        sys.exit(str(exc))


def cmd_login(args: argparse.Namespace) -> None:
    tokens = github_token_via_device_flow(args)

    # The hub's half is unchanged: it receives one opaque access token, reads
    # identity and repo access from it, and drops it. What changed is our half —
    # by default we keep what GitHub issued so `loop` can renew itself later.
    body = enrol_with_hub(args.hub, registration_payload(args), tokens.access_token)
    state = {"hub_url": args.hub.rstrip("/"), "token": body["token"]}

    if args.no_store_github_token:
        state[STORE_GITHUB_TOKEN_KEY] = STORE_GITHUB_TOKEN_OPTED_OUT
        print(ATTENDED_ONLY_NOTICE)
    else:
        state.update(tokens.as_state_fields())
        if tokens.refresh_token is None:
            print(NO_UNATTENDED_RENEWAL_WARNING, file=sys.stderr)

    save_state(args.state, state)
    print(f"Enrolled {args.name} ({body['client']['id']}); token stored in {args.state}")


def check_in_payload(github_token: str | None) -> dict[str, Any] | None:
    """The check-in body: a re-attestation, or nothing at all.

    None rather than `{}` without a token: httpx sends `json=None` as an empty
    body with no Content-Type, which is byte-for-byte the request the hub has
    always accepted as a plain heartbeat.
    """
    if not github_token:
        return None
    return {"github_token": github_token}


def cmd_check_in(args: argparse.Namespace) -> None:
    # Same one-shot handling as enrolment on the hub's side: it reads repo
    # access from this token and drops it. Ours keeps the set, so a `loop`
    # started afterwards inherits credentials it can renew from.
    tokens = github_token_via_device_flow(args) if args.reattest else None
    state = load_state(args.state)
    with api(state) as client:
        response = client.post(
            "/clients/check-in",
            json=check_in_payload(tokens.access_token if tokens else None),
        )
        response.raise_for_status()
    if tokens is None:
        print("Checked in — plugged into the grid.")
        return
    # Stored only once the hub accepted them: a token it refused is not one
    # `loop` should later find on disk and spend again. (For an opted-out
    # client the persist is a self-healing no-op — the token was spent
    # in-memory and the notice says so, rather than a success message
    # implying credentials this client deliberately does not keep.)
    _persist_github_tokens(state, args.state, tokens)
    if _github_storage_opted_out(state):
        print("Checked in — repo access re-attested.")
        print(ATTENDED_ONLY_NOTICE)
    else:
        print("Checked in — plugged into the grid, repo access re-attested.")


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


def _reattest_via_checkin(
    hub_client: httpx.Client,
    state: dict[str, str],
    state_path: Path,
    tokens: GithubTokenSet | None,
    ttl_seconds: int | None,
) -> int | None:
    """Check in — a re-attestation with tokens, a bare heartbeat without.

    Returns the TTL to schedule off: the one the hub just reported, or the
    caller's existing value if this response carried none. That is how the
    cadence is learned rather than invented. Credentials are persisted only once
    the hub accepted them, for the same reason `check-in --reattest` does it
    that way — a token it refused is not one to find on disk and spend again.
    """
    response = hub_client.post(
        "/clients/check-in",
        json=check_in_payload(tokens.access_token if tokens else None),
    )
    response.raise_for_status()
    if tokens is not None:
        _persist_github_tokens(state, state_path, tokens)
    body: dict[str, Any] = response.json() or {}
    reported = body.get("identity_access_ttl_seconds")
    return int(reported) if reported is not None else ttl_seconds


def _reattestation_is_due(
    attempted_at: datetime | None,
    ttl_seconds: int | None,
    now: Callable[[], datetime],
) -> bool:
    """Half of the hub's own TTL — never a cadence this client made up.

    Due immediately before a run's first attempt, because that first check-in is
    what teaches the client what the TTL is; scheduling cannot precede it. If an
    attempt has been made and no TTL was ever learned, the check-in never
    succeeded — from there the reactive 409 branch is the authority, and a blind
    retry cadence would only hammer GitHub on the way to the same answer.
    """
    if attempted_at is None:
        return True
    if ttl_seconds is None:
        return False
    return now() - attempted_at >= timedelta(seconds=ttl_seconds / 2)


def _rounds(max_rounds: int | None) -> Iterator[int]:
    """Round numbers: bounded when a test says so, endless in production."""
    return itertools.islice(itertools.count(1), max_rounds)


def _recover_from_lease_conflict(  # noqa: PLR0913 - the recovery needs every piece of the loop's state
    hub_client: httpx.Client,
    state: dict[str, str],
    state_path: Path,
    args: argparse.Namespace,
    *,
    checked_in_this_run: bool,
    ttl_seconds: int | None,
    github_http: httpx.Client | None,
    now: Callable[[], datetime],
) -> int | None:
    """Answer a 409 from leasing, or end the run explaining why it cannot be answered.

    The hub deliberately answers "check in first" and "check in again" with the
    same status and the same *kind* of message, so the text is not worth parsing
    — and parsing it would break the next time either side rewords. The ordering
    of the hub's own checks disambiguates it for free: `status != CHECKED_IN` is
    tested *before* anything can raise staleness, so a 409 arriving at a run
    that has not checked in yet can only be the first kind, which a bare
    heartbeat fixes without any GitHub credential at all.

    Once this run has checked in, only the second kind is left. If nothing can
    renew at that point the attestation is genuinely unrecoverable, and the
    honest move is to stop and name the command that fixes it — not to retry a
    409 that will never change its mind.
    """
    if not checked_in_this_run:
        return _reattest_via_checkin(hub_client, state, state_path, None, ttl_seconds)
    tokens = renew_github_access(state, args, http=github_http, now=now)
    if tokens is None:
        sys.exit(LOGIN_AGAIN)
    return _reattest_via_checkin(hub_client, state, state_path, tokens, ttl_seconds)


def run_loop(  # noqa: PLR0913 - every knob here is a seam a test drives; see the docstring
    hub_client: httpx.Client,
    state: dict[str, str],
    state_path: Path,
    args: argparse.Namespace,
    *,
    idle_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = _utcnow,
    github_http: httpx.Client | None = None,
    max_rounds: int | None = None,
) -> None:
    """Serve rounds, keeping this client's attestation fresh without a human.

    Two rules carry the design, and both are read off hub behaviour rather than
    invented here.

    Proactive re-attestation runs only for a client that stored GitHub
    credentials at `login`. A `register`-enrolled client, a dev-mode client, or
    one that opted out with --no-store-github-token has nothing to renew, and
    this stays a genuine no-op for them rather than a throttled retry —
    mirroring the hub's own inert carve-outs for dev mode and for a client with
    no linked identity.

    The other half, disambiguating a 409 from leasing, lives in
    `_recover_from_lease_conflict`.

    `max_rounds`, `now`, `sleep` and `github_http` exist so a test can drive a
    TTL boundary deterministically, without a real clock or a real GitHub.
    """
    checked_in_this_run = False
    # Both in-memory for the life of this process: scheduling state, not a
    # contract, and not something a later run should inherit a stale copy of.
    # `attempted_at` moves on every attempt whether or not it worked, so a
    # client whose renewals keep failing backs off rather than hammering GitHub.
    attempted_at: datetime | None = None
    ttl_seconds: int | None = None
    renewable = PRIMARY_TOKEN_KEY in state

    for _round in _rounds(max_rounds):
        if renewable and _reattestation_is_due(attempted_at, ttl_seconds, now):
            attempted_at = now()
            tokens = renew_github_access(state, args, http=github_http, now=now)
            if tokens is not None:
                try:
                    ttl_seconds = _reattest_via_checkin(hub_client, state, state_path, tokens, ttl_seconds)
                    checked_in_this_run = True
                except httpx.HTTPStatusError as exc:
                    # A hub hiccup during a *scheduled* refresh is not fatal: the
                    # snapshot the hub already holds may still be good, and the
                    # conflict branch below is what decides when it is not.
                    print(f"hub error during re-attestation: {exc}", file=sys.stderr)

        try:
            worked = one_round(hub_client)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != HTTPStatus.CONFLICT:
                print(f"hub error: {exc}", file=sys.stderr)
                worked = False
            else:
                try:
                    ttl_seconds = _recover_from_lease_conflict(
                        hub_client,
                        state,
                        state_path,
                        args,
                        checked_in_this_run=checked_in_this_run,
                        ttl_seconds=ttl_seconds,
                        github_http=github_http,
                        now=now,
                    )
                except httpx.HTTPStatusError as recovery_exc:
                    # Recovery's own check-in can hit the same transient hub
                    # weather as anything else; that must idle, not kill an
                    # unattended process. The genuinely-unrecoverable case
                    # exits via SystemExit inside the recovery, which this
                    # deliberately does not catch.
                    print(f"hub error during conflict recovery: {recovery_exc}", file=sys.stderr)
                    worked = False
                else:
                    checked_in_this_run = True
                    attempted_at = now()
                    # Recovered, so retry immediately rather than idling on a
                    # conflict that has just been answered.
                    worked = True

        if not worked:
            sleep(idle_seconds)


def cmd_loop(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    print(f"Looping; polling every {args.idle_seconds}s when dry. Ctrl-C to stop.")
    with api(state) as client:
        run_loop(client, state, args.state, args, idle_seconds=args.idle_seconds)


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

    # Shared by every command that talks to GitHub's OAuth endpoints: `login`
    # and `check-in --reattest` run the device flow, and `loop` spends a refresh
    # token against the same App.
    device_flow = argparse.ArgumentParser(add_help=False)
    device_flow.add_argument("--client-id", default=None, help="GitHub App client id (or set GITHUB_APP_CLIENT_ID)")

    login = sub.add_parser(
        "login",
        parents=[common, enrolment, device_flow],
        help="Authorize with GitHub (device flow) and join the grid",
    )
    login.add_argument(
        "--no-store-github-token",
        action="store_true",
        help=(
            "Spend the GitHub token on enrolment and discard it, storing nothing. "
            "Opted-out clients cannot renew unattended via `loop` — re-run `login` manually to refresh"
        ),
    )
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

    # Out of the generic loop below because it carries the device-flow flags:
    # re-attestation is the same GitHub round trip `login` makes, spent again.
    check_in = sub.add_parser("check-in", parents=[common, device_flow], help="Declare availability")
    check_in.add_argument(
        "--reattest",
        action="store_true",
        help="Also re-run the GitHub device flow and refresh the hub's view of your repo access",
    )
    check_in.set_defaults(func=cmd_check_in)

    for name, func, help_text in [
        ("check-out", cmd_check_out, "Leave the grid"),
        ("run-once", cmd_run_once, "Lease and complete a single round"),
    ]:
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.set_defaults(func=func)

    # Out of the loop above for the same reason `check-in` is: it carries the
    # device-flow flags now, because it renews its own attestation between rounds.
    loop = sub.add_parser("loop", parents=[common, device_flow], help="Keep serving rounds until interrupted")
    loop.add_argument("--idle-seconds", type=int, default=15)
    loop.set_defaults(func=cmd_loop)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
