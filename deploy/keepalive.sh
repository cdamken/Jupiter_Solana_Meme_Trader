#!/usr/bin/env bash
# keepalive.sh — keeps the Jupiter panel backend (127.0.0.1:8791) alive as user carlos.
# Called by cron every minute.
# Detects liveness by port, not process name (reliable).
PORT=8791
if python3 -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1',$PORT))==0 else 1)" 2>/dev/null; then
    exit 0
fi
cd "$HOME/jupiter" || exit 1
# Load .env so config.py can read RPC_URL / KEYPAIR_PATH / QUOTE_MINT
set -a; source .env; set +a
JUPITER_DB="$HOME/jupiter/store/jupiter.db" \
PANEL_PORT=$PORT \
PANEL_BASE="/carlos/jupiter/" \
PANEL_SECRET="$(cat "$HOME/jupiter/.panel_secret" 2>/dev/null || echo 'change-me')" \
  setsid python3 -m panel.app >> "$HOME/jupiter/panel.log" 2>&1 < /dev/null &
