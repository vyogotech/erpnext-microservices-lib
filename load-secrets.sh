#!/bin/bash
# load-secrets.sh — Wait for /secrets/db.env, source it, then exec the service entrypoint.
set -euo pipefail

SECRETS_FILE="/secrets/db.env"
MAX_WAIT="${SECRETS_WAIT_TIMEOUT:-120}"

echo "⏳ Waiting for ${SECRETS_FILE} (max ${MAX_WAIT}s)..."
for i in $(seq 1 "$MAX_WAIT"); do
  if [ -f "$SECRETS_FILE" ]; then
    break
  fi
  if [ "$i" = "$MAX_WAIT" ]; then
    echo "❌ ${SECRETS_FILE} not found after ${MAX_WAIT}s" >&2
    exit 1
  fi
  sleep 1
done

# Export all variables from db.env into the current environment
set -a
source "$SECRETS_FILE"
set +a

echo "🔑 Loaded DB credentials from ${SECRETS_FILE}: DB_NAME=${DB_NAME:-?}, DB_HOST=${DB_HOST:-?}"

# Execute the service entrypoint
# Supports two modes:
#   1. SERVICE_APP env var (e.g. "server:app") — uses library entrypoint which
#      adds SERVICE_PATH to sys.path, imports the app, and runs it
#   2. Legacy /app/entrypoint.py — for backward compatibility
if [ -n "${SERVICE_APP:-}" ]; then
  echo "🚀 Starting service via frappe_microservice.entrypoint (SERVICE_APP=${SERVICE_APP})..."
  exec /opt/venv/bin/python -m frappe_microservice.entrypoint
elif [ -f /app/entrypoint.py ]; then
  echo "🚀 Starting service via /app/entrypoint.py..."
  exec /opt/venv/bin/python /app/entrypoint.py
else
  echo "❌ No SERVICE_APP env var set and /app/entrypoint.py not found" >&2
  exit 1
fi
