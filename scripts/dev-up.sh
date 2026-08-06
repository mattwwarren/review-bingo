#!/usr/bin/env bash
# Bring up a *persistent* local hub — the stack you point a real GitHub App at.
#
# Unlike scripts/demo.sh, nothing is torn down on exit: Postgres keeps its data
# in a named volume and survives hub restarts, so a PR opened an hour ago is
# still leasable. Ctrl-C stops the hub only; scripts/dev-down.sh stops Postgres.
#
# Different container and port from the demo on purpose, so both can coexist.
set -euo pipefail

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

HUB_PORT="${HUB_PORT:-7575}"
# Localhost by default. HUB_HOST=0.0.0.0 puts the hub on the LAN so another
# device (a phone, a laptop across the room) can open the dashboard — but the
# hub has no auth, so anything on that network can also post webhooks, register
# clients, and lease jobs. Opt in deliberately; don't make it the default.
HUB_HOST="${HUB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-55433}"
PG_IMAGE="${PG_IMAGE:-postgres:18.1-alpine}"
PG_CONTAINER="bingo-dev-pg"
PG_VOLUME="bingo-dev-pgdata"
PEM_PATH="hub/.secrets/github-app-private-key.pem"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

export DATABASE_URL="postgresql+asyncpg://app:app@127.0.0.1:$DB_PORT/bingo"

if [[ -f "$PEM_PATH" ]]; then
  # The PEM stays a file; only the process env carries it. Env vars outrank
  # .env in pydantic-settings, so this is authoritative.
  GITHUB_APP_PRIVATE_KEY="$(cat "$PEM_PATH")"
  export GITHUB_APP_PRIVATE_KEY
fi

say "Postgres ($PG_IMAGE on :$DB_PORT, volume $PG_VOLUME)"
if [[ -z "$(docker ps -q -f "name=^${PG_CONTAINER}$")" ]]; then
  docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$PG_CONTAINER" \
    -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app -e POSTGRES_DB=bingo \
    -v "$PG_VOLUME:/var/lib/postgresql/data" \
    -p "$DB_PORT:5432" "$PG_IMAGE" >/dev/null
  echo "   started"
else
  echo "   already running"
fi
until docker exec "$PG_CONTAINER" pg_isready -U app -d bingo >/dev/null 2>&1; do sleep 0.5; done

if curl -sf "localhost:$HUB_PORT/ping" >/dev/null 2>&1; then
  echo "Something is already listening on :$HUB_PORT — stop it or set HUB_PORT." >&2
  exit 1
fi

say "Migrating"
(cd hub && uv run alembic upgrade head >/dev/null)
echo "   at head"

say "Config check"
if [[ -f "$PEM_PATH" ]] && grep -q '^GITHUB_APP_ID=..*' hub/.env 2>/dev/null; then
  echo "   relay: GITHUB — reports will post real PR comments"
else
  echo "   relay: LOG — no App key/id found, comments only hit the hub log"
  echo "          (run scripts/github-app-setup.sh to wire the App)"
fi
if grep -q '^GITHUB_WEBHOOK_SECRET=..*' hub/.env 2>/dev/null; then
  echo "   webhook signatures: VERIFIED"
else
  echo "   webhook signatures: SKIPPED — any caller can enqueue jobs"
fi

say "Hub on $HUB_HOST:$HUB_PORT — Ctrl-C stops it (Postgres stays up)"
cat <<EOF
   Dashboard:
     http://localhost:$HUB_PORT/dashboard
EOF
if [[ "$HUB_HOST" != "127.0.0.1" && "$HUB_HOST" != "localhost" ]]; then
  LAN_IP="$(ip -4 -o addr show scope global 2>/dev/null | awk 'NR==1{split($4, a, "/"); print a[1]}')"
  cat <<EOF
     http://${LAN_IP:-<this-machine>}:$HUB_PORT/dashboard   (other devices on this network)

   Reachable from the LAN, and the hub has no auth — anything on this network
   can enqueue jobs and lease work. Fine at home; not on shared wifi.
EOF
fi
cat <<EOF

   Point the App's webhook at your tunnel:
     ngrok http $HUB_PORT
     webhook URL = https://<your-ngrok-host>/webhooks/github

   Watch it work:
     curl -s localhost:$HUB_PORT/jobs | python3 -m json.tool
     uv run client/bingo_client.py loop --state ~/.config/review-bingo/client.json
EOF

cd hub
exec uv run uvicorn review_bingo_hub.main:app --host "$HUB_HOST" --port "$HUB_PORT" ${HUB_RELOAD:+--reload}
