#!/usr/bin/env bash
set -e

HERMES_HOME="${HERMES_HOME:-/data/hermes}"
AGENT_NAME="${CRUSTOCEAN_HANDLE:-hermes-agent}"
mkdir -p "$HERMES_HOME"

# Fetch persona + config from the Crustocean API (cloud Hermes agents).
# Falls back to bundled defaults if the API is unreachable.
python /app/fetch_config.py

# If fetch_config didn't write files, sync from bundled defaults
if [ ! -f "$HERMES_HOME/config.yaml" ] && [ -f /app/hermes-defaults/config.yaml ]; then
    cp /app/hermes-defaults/config.yaml "$HERMES_HOME/config.yaml"
fi
if [ ! -f "$HERMES_HOME/SOUL.md" ] && [ -f /app/hermes-defaults/SOUL.md ]; then
    cp /app/hermes-defaults/SOUL.md "$HERMES_HOME/SOUL.md"
fi

# Sync default skills (won't overwrite user-created skills)
mkdir -p "$HERMES_HOME/skills"
if [ -d /app/hermes-defaults/skills ]; then
    cp -n /app/hermes-defaults/skills/*.md "$HERMES_HOME/skills/" 2>/dev/null || true
fi

cd /app/hermes-agent

echo "Starting $AGENT_NAME (Hermes Agent + Crustocean gateway) ..."
echo "  HERMES_HOME=$HERMES_HOME"
echo "  CRUSTOCEAN_API_URL=${CRUSTOCEAN_API_URL:-https://api.crustocean.chat}"
echo "  CRUSTOCEAN_HANDLE=${CRUSTOCEAN_HANDLE}"
echo "  CRUSTOCEAN_AGENCIES=${CRUSTOCEAN_AGENCIES:-lobby}"

exec python -u /app/start_gateway.py
