#!/usr/bin/env bash
# OWID Data Tools — deploy via Cloudflare Quick Tunnel (free, no account plan needed)
#
# Starts the MCP server locally and exposes it publicly with cloudflared.
# NOTE: quick-tunnel URLs are ephemeral — they change on every run and the
# tunnel only lives while this script (or the processes) keep running.
# For a stable URL use a named tunnel + domain, or the Workers+Containers deploy
# (`npx wrangler deploy`, requires Workers Paid plan).
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"
ORIGIN="http://127.0.0.1:${PORT}"

# 1) Python deps (first run only)
if [ ! -x venv/bin/python ]; then
  echo "==> Creating venv and installing dependencies (first run)..."
  python3 -m venv venv
  venv/bin/pip install -q -r requirements.txt
fi

# 2) Start the MCP server (ignore error if already running)
if ! curl -sf "$ORIGIN/health" >/dev/null 2>&1; then
  echo "==> Starting MCP server on $ORIGIN ..."
  nohup venv/bin/python mcp_server.py >/tmp/owdtools-server.log 2>&1 &
fi
for _ in $(seq 1 30); do
  curl -sf "$ORIGIN/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -sf "$ORIGIN/health" >/dev/null 2>&1 || { echo "Server failed to start; see /tmp/owdtools-server.log"; exit 1; }

# 3) Start the public tunnel
echo "==> Starting cloudflared quick tunnel to $ORIGIN ..."
CLOUDFLARED_LOG=/tmp/owdtools-cloudflared.log
nohup cloudflared tunnel --url "$ORIGIN" >"$CLOUDFLARED_LOG" 2>&1 &

# 4) Wait for the public URL and configure the server with it
URL=""
for _ in $(seq 1 30); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CLOUDFLARED_LOG" | head -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done
if [ -z "$URL" ]; then
  echo "Tunnel failed to start; see $CLOUDFLARED_LOG"
  exit 1
fi

# Restart the server with the public URL so generated links are correct
pkill -f "[v]env/bin/python mcp_server.py" 2>/dev/null || true
sleep 1
PUBLIC_URL="$URL" nohup venv/bin/python mcp_server.py >/tmp/owdtools-server.log 2>&1 &
for _ in $(seq 1 30); do
  curl -sf "$ORIGIN/health" >/dev/null 2>&1 && break
  sleep 1
done

echo
echo "✅ OWID MCP server is live:"
echo "   MCP endpoint:  $URL/mcp"
echo "   Health check:  $URL/health"
echo
echo "   Add this URL in Gemini (Tools → MCP) or Claude (Settings → Connectors):"
echo "   $URL/mcp"
echo
echo "   Logs: /tmp/owdtools-server.log, $CLOUDFLARED_LOG"
