#!/usr/bin/env bash
# install.sh — First-time setup on a fresh Ubuntu server.
# Run as the 'carlos' user (not root).
# Usage: bash deploy/install.sh

set -euo pipefail

REPO="https://github.com/cdamken/Jupiter_Solana_Meme_Trader"
INSTALL_DIR="$HOME/jupiter"

echo "=== Jupiter install ==="
echo "Target: $INSTALL_DIR"

# 1. Clone repo
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "[1/7] Repo already cloned, pulling latest..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "[1/7] Cloning repo..."
    git clone "$REPO" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# 2. Python venv
echo "[2/7] Creating venv..."
python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "      packages installed"

# 3. .env check
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[3/7] .env created from .env.example"
    echo "      FILL IN: RPC_URL, KEYPAIR_PATH, QUOTE_MINT before continuing"
    echo "      Then re-run this script."
    exit 0
else
    echo "[3/7] .env already exists"
fi

# 4. Validate required keys
missing=0
for key in RPC_URL KEYPAIR_PATH QUOTE_MINT; do
    if ! grep -q "^${key}=" .env || grep -q "^${key}=$" .env; then
        echo "      ERROR: $key is missing or empty in .env"
        missing=1
    fi
done
if [ "$missing" -eq 1 ]; then
    echo "Fix .env and re-run."
    exit 1
fi
echo "[4/7] .env validated"

# 5. Bootstrap DB
echo "[5/7] Bootstrapping DB..."
python3 bootstrap.py

# 6. Run tests (never against live DB)
echo "[6/7] Running tests..."
for f in tests/test_*.py; do
    python3 "$f" || { echo "FAIL: $f"; exit 1; }
done
echo "      All tests OK"

# 7. Install systemd services
echo "[7/7] Installing systemd services..."
SVCDIR="$HOME/.config/systemd/user"
mkdir -p "$SVCDIR"

# Patch WorkingDirectory to the actual install path
sed "s|/home/carlos/jupiter|$INSTALL_DIR|g" deploy/jupiter-scheduler.service > "$SVCDIR/jupiter-scheduler.service"
sed "s|/home/carlos/jupiter|$INSTALL_DIR|g" deploy/jupiter-panel.service      > "$SVCDIR/jupiter-panel.service"

systemctl --user daemon-reload
systemctl --user enable jupiter-scheduler jupiter-panel
systemctl --user start  jupiter-scheduler jupiter-panel

echo ""
echo "=== Done ==="
echo "Scheduler: systemctl --user status jupiter-scheduler"
echo "Panel:     systemctl --user status jupiter-panel"
echo "Panel URL: http://localhost:5001"
echo ""
echo "To enable LIVE trading when ready:"
echo "  1. Add LIVE=1 to .env"
echo "  2. Edit deploy/jupiter-scheduler.service: change Environment=LIVE=0 to LIVE=1"
echo "  3. systemctl --user daemon-reload && systemctl --user restart jupiter-scheduler"
