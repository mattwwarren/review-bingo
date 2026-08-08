#!/usr/bin/env bash
# One-time GitHub App wiring for a local hub.
#
# Turns what GitHub hands you at App creation into the two things the hub
# actually reads:
#
#   hub/.secrets/github-app-private-key.pem  — exported as GITHUB_APP_PRIVATE_KEY
#                                              by scripts/dev-up.sh (keeps the
#                                              multiline PEM out of .env)
#   hub/.env                                 — GITHUB_APP_ID, GITHUB_APP_CLIENT_ID,
#                                              GITHUB_WEBHOOK_SECRET
#
# Usage:
#   scripts/github-app-setup.sh path/to/app-manifest.json
#   scripts/github-app-setup.sh path/to/key.pem --app-id 123456 [--client-id Iv23li...] [--webhook-secret SECRET]
#
# The JSON form is what the App-manifest flow returns (id + client_id + pem +
# webhook_secret in one blob). The .pem form is the plain "Generate a private
# key" download, where the App ID and client id live on the App's settings page
# and the webhook secret is whatever you typed into the form.
#
# MANUAL STEP THIS SCRIPT CANNOT DO FOR YOU: on the App's settings page, tick
# "Enable Device Flow". Grid clients enrol by completing that flow against
# github.com; without it, `bingo_client.py login` gets a device-code request
# back with error=device_flow_disabled and no client can ever join.
set -euo pipefail

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

SECRETS_DIR="hub/.secrets"
PEM_PATH="$SECRETS_DIR/github-app-private-key.pem"
ENV_PATH="hub/.env"

SOURCE=""
APP_ID=""
CLIENT_ID=""
WEBHOOK_SECRET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-id) APP_ID="$2"; shift 2 ;;
    --client-id) CLIENT_ID="$2"; shift 2 ;;
    --webhook-secret) WEBHOOK_SECRET="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) SOURCE="$1"; shift ;;
  esac
done

[[ -n "$SOURCE" ]] || {
  echo "usage: $0 <manifest.json|key.pem> [--app-id N] [--client-id ID] [--webhook-secret S]" >&2
  exit 2
}
[[ -f "$SOURCE" ]] || { echo "no such file: $SOURCE" >&2; exit 2; }

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

# Sniff by parsing, not by looking for the PEM header — a manifest JSON is one
# long line that *contains* "BEGIN RSA PRIVATE KEY" inside the escaped pem field.
if ! python3 -c 'import json,sys; sys.exit(0 if isinstance(json.load(open(sys.argv[1])), dict) else 1)' "$SOURCE" 2>/dev/null; then
  echo "== Reading PEM: $SOURCE"
  [[ -n "$APP_ID" ]] || { echo "PEM input needs --app-id (find it on the App's settings page)" >&2; exit 2; }
  cp "$SOURCE" "$PEM_PATH"
else
  echo "== Reading App manifest JSON: $SOURCE"
  # Command substitution, not `read < <(...)`: a process substitution's exit
  # status is invisible to `set -e`, so a python failure here would leave the
  # id/secret empty and the script would carry on and write a broken .env
  # (silently reusing a stale key from an earlier run).
  MANIFEST_FIELDS="$(python3 - "$SOURCE" "$PEM_PATH" <<'PY'
import json, pathlib, sys

blob = json.loads(pathlib.Path(sys.argv[1]).read_text())
pem = blob.get("pem")
if not pem:
    sys.exit("JSON has no 'pem' key — is this a GitHub App manifest response?")
pathlib.Path(sys.argv[2]).write_text(pem if pem.endswith("\n") else pem + "\n")
print(blob.get("id") or "-", blob.get("client_id") or "-", blob.get("webhook_secret") or "-")
PY
)"
  read -r JSON_APP_ID JSON_CLIENT_ID JSON_SECRET <<<"$MANIFEST_FIELDS"
  # Manifest carries everything; explicit flags still win if you pass them.
  [[ -n "$APP_ID" ]] || APP_ID="$JSON_APP_ID"
  [[ -n "$CLIENT_ID" || "$JSON_CLIENT_ID" == "-" ]] || CLIENT_ID="$JSON_CLIENT_ID"
  [[ -n "$WEBHOOK_SECRET" || "$JSON_SECRET" == "-" ]] || WEBHOOK_SECRET="$JSON_SECRET"
fi

[[ -n "$APP_ID" && "$APP_ID" != "-" ]] || {
  echo "no App ID resolved — pass --app-id, or check the manifest has an 'id'" >&2
  exit 2
}

chmod 600 "$PEM_PATH"

# Fail fast on a key the relay could not actually use: sign an App JWT with it
# exactly the way relay_service does (PyJWT, RS256).
echo "== Verifying the key can sign an RS256 App JWT"
(cd hub && uv run python - "../$PEM_PATH" "$APP_ID" <<'PY'
import pathlib, sys, time

import jwt

key = pathlib.Path(sys.argv[1]).read_text()
now = int(time.time())
jwt.encode({"iat": now - 60, "exp": now + 540, "iss": sys.argv[2]}, key, algorithm="RS256")
print("   key OK")
PY
)

if [[ -z "$WEBHOOK_SECRET" ]]; then
  WEBHOOK_SECRET="$(openssl rand -hex 32)"
  GENERATED_SECRET=1
fi

python3 - "$ENV_PATH" "$APP_ID" "$WEBHOOK_SECRET" "$CLIENT_ID" <<'PY'
import pathlib, sys

path, app_id, secret, client_id = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
header = [
    "# review-bingo hub — local dev config.",
    "# GITHUB_APP_PRIVATE_KEY is NOT here: the multiline PEM lives in",
    "# .secrets/github-app-private-key.pem and dev-up.sh exports it.",
    "# DATABASE_URL is exported by dev-up.sh too, so it matches the container it starts.",
    "",
]
updates = {"GITHUB_APP_ID": app_id, "GITHUB_WEBHOOK_SECRET": secret}
# Only written when known: an empty GITHUB_APP_CLIENT_ID line would read as
# "configured, to the empty string" and send clients into a device flow that
# github.com rejects with no useful message.
if client_id and client_id != "-":
    updates["GITHUB_APP_CLIENT_ID"] = client_id

lines = path.read_text().splitlines() if path.exists() else list(header)
seen = set()
for i, line in enumerate(lines):
    key = line.split("=", 1)[0].strip()
    if key in updates:
        lines[i] = f"{key}={updates[key]}"
        seen.add(key)
lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
path.write_text("\n".join(lines).rstrip("\n") + "\n")
PY

echo
echo "Wrote:"
echo "  $PEM_PATH  (0600)"
echo "  $ENV_PATH  (GITHUB_APP_ID=$APP_ID, GITHUB_WEBHOOK_SECRET set)"
if [[ -n "$CLIENT_ID" && "$CLIENT_ID" != "-" ]]; then
  echo "                (GITHUB_APP_CLIENT_ID=$CLIENT_ID)"
else
  echo
  echo "No client id resolved, so grid clients cannot run the device flow yet."
  echo "Find it on the App's settings page (starts 'Iv23li') and re-run with --client-id."
fi
echo
echo "Before any client can enrol: tick 'Enable Device Flow' on the App's settings page."
echo
if [[ -n "${GENERATED_SECRET:-}" ]]; then
  echo "No webhook secret was supplied, so one was generated. Paste this into the"
  echo "App's 'Webhook secret' field or deliveries will 401:"
  echo
  echo "  $WEBHOOK_SECRET"
  echo
fi
echo "Next: scripts/dev-up.sh"
