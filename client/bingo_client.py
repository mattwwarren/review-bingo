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

Usage:
    bingo_client.py register  --hub URL --name NAME --model MODEL --provider P [--tier TIER] [--quant Q]
    bingo_client.py check-in  [--state PATH]
    bingo_client.py run-once  [--state PATH]      # lease → review → report (one round)
    bingo_client.py loop      [--state PATH] [--idle-seconds N]
    bingo_client.py check-out [--state PATH]

State (hub URL + bearer token) lives in --state (default
~/.config/review-bingo/client.json), written once by `register`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

DEFAULT_STATE_PATH = Path.home() / ".config" / "review-bingo" / "client.json"


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


def cmd_register(args: argparse.Namespace) -> None:
    payload = {
        "name": args.name,
        "model_name": args.model,
        "provider": args.provider,
        "tier": args.tier,
        "quant": args.quant,
    }
    response = httpx.post(f"{args.hub.rstrip('/')}/clients", json=payload, timeout=30.0)
    response.raise_for_status()
    body = response.json()
    save_state(args.state, {"hub_url": args.hub.rstrip("/"), "token": body["token"]})
    print(f"Registered {args.name} ({body['client']['id']}); token stored in {args.state}")


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="bingo-client", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="Client state file")

    register = sub.add_parser("register", parents=[common], help="Join the grid and store the minted token")
    register.add_argument("--hub", required=True, help="Hub base URL, e.g. http://localhost:7575")
    register.add_argument("--name", required=True)
    register.add_argument("--model", required=True, help="Model you review with")
    register.add_argument("--provider", required=True, help="Where it runs (ollama, anthropic, vllm, ...)")
    register.add_argument("--tier", default="standard", choices=["experimental", "standard", "frontier"])
    register.add_argument("--quant", default=None)
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
