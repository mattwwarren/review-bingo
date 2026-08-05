#!/usr/bin/env bash
# Stop the persistent dev Postgres started by scripts/dev-up.sh.
# Data survives unless you pass --wipe.
set -euo pipefail

PG_CONTAINER="bingo-dev-pg"
PG_VOLUME="bingo-dev-pgdata"

docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 && echo "stopped $PG_CONTAINER" || echo "$PG_CONTAINER not running"

if [[ "${1:-}" == "--wipe" ]]; then
  docker volume rm "$PG_VOLUME" >/dev/null 2>&1 && echo "wiped $PG_VOLUME" || echo "$PG_VOLUME not present"
fi
