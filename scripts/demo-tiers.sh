#!/usr/bin/env bash
# Demo of tier-floor enforcement (RFC 0001 B2), no GitHub needed:
#
#   ephemeral Postgres → hub (dev enrolment mode) → repo floor set to
#   "standard" → fake PR webhook → an experimental client is refused the
#   job (dry queue, then 403 on naming it directly) → a frontier client
#   leases that exact job
#
# Distinct port/container from scripts/demo.sh so both can run side by side.
# Requires: docker, uv, curl, python3. Run from anywhere inside the repo.
set -euo pipefail

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

HUB_PORT="${HUB_PORT:-7576}"
DB_PORT="${DB_PORT:-55434}"
PG_IMAGE="${PG_IMAGE:-postgres:18.1-alpine}"
REPO="acme/banking"
EXP_STATE="$(mktemp -t bingo-tiers-exp.XXXXXX.json)"
FRONTIER_STATE="$(mktemp -t bingo-tiers-frontier.XXXXXX.json)"
HUB_LOG="$(mktemp -t bingo-tiers-hub.XXXXXX.log)"

# Same named bypass scripts/demo.sh uses: joining the grid for real is answered
# by GitHub's device flow, but this demo runs offline with no GitHub App, so a
# shared secret stands in. The hub logs a warning at startup and on every
# enrolment — that noise is the point, it makes the bypass auditable.
export CLIENT_ENROLMENT_MODE=dev
export CLIENT_ENROLMENT_SECRET=demo-tiers-secret

cleanup() {
  # uv run wraps uvicorn in a child process, so kill by exact command line —
  # killing just the wrapper orphans the server and leaves it on the port.
  pkill -f "uvicorn review_bingo_hub.main:app --port $HUB_PORT" 2>/dev/null || true
  docker stop bingo-tiers-pg >/dev/null 2>&1 || true
  rm -f "$EXP_STATE" "$FRONTIER_STATE"
}
trap cleanup EXIT

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# Reads a JSON field out of stdin (dotted path via python3, e.g. "job.id").
json_field() {
  python3 -c '
import json, sys
value = json.load(sys.stdin)
for key in sys.argv[1].split("."):
    value = value[key]
print(value)
' "$1"
}

say "Starting ephemeral Postgres ($PG_IMAGE on :$DB_PORT)"
docker rm -f bingo-tiers-pg >/dev/null 2>&1 || true
docker run -d --rm --name bingo-tiers-pg \
  -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app -e POSTGRES_DB=bingo \
  -p "$DB_PORT:5432" "$PG_IMAGE" >/dev/null
until docker exec bingo-tiers-pg pg_isready -U app -d bingo >/dev/null 2>&1; do sleep 0.5; done

export DATABASE_URL="postgresql+asyncpg://app:app@127.0.0.1:$DB_PORT/bingo"

if curl -sf "localhost:$HUB_PORT/ping" >/dev/null 2>&1; then
  echo "Something is already listening on :$HUB_PORT — stop it or set HUB_PORT." >&2
  exit 1
fi

say "Migrating and starting the hub on :$HUB_PORT"
(cd hub && uv run alembic upgrade head >/dev/null)
(cd hub && exec uv run uvicorn review_bingo_hub.main:app --port "$HUB_PORT") >"$HUB_LOG" 2>&1 &
for _ in $(seq 1 60); do
  curl -sf "localhost:$HUB_PORT/ping" >/dev/null && break
  sleep 0.5
done
curl -sf "localhost:$HUB_PORT/ping" >/dev/null || { echo "hub failed to start; log:"; tail -20 "$HUB_LOG"; exit 1; }

say "Setting a policy floor: $REPO requires tier >= standard"
curl -sf -X PUT "localhost:$HUB_PORT/policies/$REPO" \
  -H "Authorization: Bearer $CLIENT_ENROLMENT_SECRET" \
  -H 'content-type: application/json' \
  -d '{"min_tier": "standard"}' | python3 -m json.tool

say "Delivering a fake pull_request webhook (PR opened) for $REPO"
WEBHOOK_BODY="$(curl -sf -X POST "localhost:$HUB_PORT/webhooks/github" \
  -H 'X-GitHub-Event: pull_request' -H 'content-type: application/json' \
  -d "{\"action\": \"opened\", \"repository\": {\"full_name\": \"$REPO\"}, \"pull_request\": {\"number\": 42, \"title\": \"Reconcile ledger rounding\", \"head\": {\"sha\": \"tiersdemo1234567890abcdef1234567890abcd\"}}}")"
echo "$WEBHOOK_BODY" | python3 -m json.tool
JOB_ID="$(echo "$WEBHOOK_BODY" | json_field job_id)"
echo "job_id=$JOB_ID"

say "Experimental client joins the grid (tier below the floor) and checks in"
uv run client/bingo_client.py register --state "$EXP_STATE" \
  --hub "http://localhost:$HUB_PORT" --name tier-demo-toy-box \
  --model tiny-experimental --provider ollama --tier experimental \
  --enrolment-token "$CLIENT_ENROLMENT_SECRET"
uv run client/bingo_client.py check-in --state "$EXP_STATE"
EXP_AUTH="Authorization: Bearer $(json_field token <"$EXP_STATE")"

say "Experimental client leases: the queue looks dry (floor blocks it)"
LEASE_BODY="$(curl -sf -X POST "localhost:$HUB_PORT/jobs/lease" -H "$EXP_AUTH")"
[[ "$LEASE_BODY" == "null" ]] || fail "expected null lease for experimental client, got: $LEASE_BODY"
echo "lease body: $LEASE_BODY (as expected)"

say "Experimental client names the job directly: refused with 403, not a leak"
STATUS="$(curl -s -o /dev/null -w '%{http_code}' -X POST "localhost:$HUB_PORT/jobs/$JOB_ID/lease" -H "$EXP_AUTH")"
[[ "$STATUS" == "403" ]] || fail "expected 403 on targeted lease for experimental client, got: $STATUS"
echo "status: $STATUS (as expected)"

say "Frontier client joins the grid (tier clears the floor) and checks in"
uv run client/bingo_client.py register --state "$FRONTIER_STATE" \
  --hub "http://localhost:$HUB_PORT" --name tier-demo-big-rig \
  --model frontier-model --provider anthropic --tier frontier \
  --enrolment-token "$CLIENT_ENROLMENT_SECRET"
uv run client/bingo_client.py check-in --state "$FRONTIER_STATE"
FRONTIER_AUTH="Authorization: Bearer $(json_field token <"$FRONTIER_STATE")"

say "Frontier client leases: gets the exact job the experimental client was refused"
LEASE_BODY="$(curl -sf -X POST "localhost:$HUB_PORT/jobs/lease" -H "$FRONTIER_AUTH")"
echo "$LEASE_BODY" | python3 -m json.tool
LEASED_JOB_ID="$(echo "$LEASE_BODY" | json_field job.id)"
[[ "$LEASED_JOB_ID" == "$JOB_ID" ]] || fail "expected frontier client to lease job $JOB_ID, got: $LEASED_JOB_ID"
echo "leased job: $LEASED_JOB_ID (matches $JOB_ID)"

echo
echo "BINGO — tier floor demo complete."
