#!/usr/bin/env bash
# One-command demo of the full review-bingo round trip, no GitHub needed:
#
#   ephemeral Postgres → hub (relay in log mode) → repo policy → fake PR
#   webhook → client registers + checks in → leases → reports → job relayed
#
# Requires: docker, uv, curl. Run from anywhere inside the repo.
set -euo pipefail

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

HUB_PORT="${HUB_PORT:-7575}"
DB_PORT="${DB_PORT:-55432}"
PG_IMAGE="${PG_IMAGE:-postgres:18.1-alpine}"
STATE_FILE="$(mktemp -t bingo-demo-client.XXXXXX.json)"
HUB_LOG="$(mktemp -t bingo-demo-hub.XXXXXX.log)"

cleanup() {
  # uv run wraps uvicorn in a child process, so kill by exact command line —
  # killing just the wrapper orphans the server and leaves it on the port.
  pkill -f "uvicorn review_bingo_hub.main:app --port $HUB_PORT" 2>/dev/null || true
  docker stop bingo-demo-pg >/dev/null 2>&1 || true
  rm -f "$STATE_FILE"
}
trap cleanup EXIT

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

say "Starting ephemeral Postgres ($PG_IMAGE on :$DB_PORT)"
docker rm -f bingo-demo-pg >/dev/null 2>&1 || true
docker run -d --rm --name bingo-demo-pg \
  -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app -e POSTGRES_DB=bingo \
  -p "$DB_PORT:5432" "$PG_IMAGE" >/dev/null
until docker exec bingo-demo-pg pg_isready -U app -d bingo >/dev/null 2>&1; do sleep 0.5; done

export DATABASE_URL="postgresql+asyncpg://app:app@127.0.0.1:$DB_PORT/bingo"

if curl -sf "localhost:$HUB_PORT/ping" >/dev/null 2>&1; then
  echo "Something is already listening on :$HUB_PORT — stop it or set HUB_PORT." >&2
  exit 1
fi

say "Migrating and starting the hub on :$HUB_PORT (relay: log mode)"
(cd hub && uv run alembic upgrade head >/dev/null)
(cd hub && exec uv run uvicorn review_bingo_hub.main:app --port "$HUB_PORT") >"$HUB_LOG" 2>&1 &
for _ in $(seq 1 60); do
  curl -sf "localhost:$HUB_PORT/ping" >/dev/null && break
  sleep 0.5
done
curl -sf "localhost:$HUB_PORT/ping" >/dev/null || { echo "hub failed to start; log:"; tail -20 "$HUB_LOG"; exit 1; }

say "Setting a policy floor: acme/payments requires tier >= standard"
curl -sf -X PUT "localhost:$HUB_PORT/policies/acme/payments" \
  -H 'content-type: application/json' \
  -d '{"min_tier": "standard"}' | python3 -m json.tool

say "Delivering a fake pull_request webhook (PR opened)"
curl -sf -X POST "localhost:$HUB_PORT/webhooks/github" \
  -H 'X-GitHub-Event: pull_request' -H 'content-type: application/json' \
  -d @scripts/demo-webhook.json | python3 -m json.tool

say "Client joins the grid (marge-mac-mini, standard tier) and checks in"
uv run client/bingo_client.py register --state "$STATE_FILE" \
  --hub "http://localhost:$HUB_PORT" --name marge-mac-mini \
  --model qwen2.5-coder-32b --provider ollama --quant q4_K_M --tier standard
uv run client/bingo_client.py check-in --state "$STATE_FILE"

say "One round: lease -> review (canned; set REVIEW_CMD to bring your own) -> report"
uv run client/bingo_client.py run-once --state "$STATE_FILE"

say "Job state after the round"
JOB_ID="$(curl -sf "localhost:$HUB_PORT/jobs" | python3 -c '
import json, sys
jobs = json.load(sys.stdin)
for j in jobs:
    print(f"{j['"'"'repo_full_name'"'"']}#{j['"'"'pr_number'"'"']} @ {j['"'"'head_sha'"'"'][:12]}  state={j['"'"'state'"'"']}  verdict={j['"'"'verdict'"'"']}", file=sys.stderr)
print(jobs[0]["id"])
')"

say "The comment that would land on the PR (relay is in log mode)"
curl -sf "localhost:$HUB_PORT/jobs/$JOB_ID/comment"

say "Client checks out"
uv run client/bingo_client.py check-out --state "$STATE_FILE"

echo
echo "BINGO — full round trip complete."
