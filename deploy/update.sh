#!/usr/bin/env bash
# update.sh — Pull latest code and restart services.
# Run as 'carlos' user on the server.
# Usage: bash deploy/update.sh

set -euo pipefail

INSTALL_DIR="$HOME/jupiter"
cd "$INSTALL_DIR"

echo "=== Jupiter update ==="

# 1. Pull
echo "[1/4] Pulling latest..."
git pull --ff-only

# 2. Update dependencies
echo "[2/4] Updating packages..."
source venv/bin/activate
pip install --quiet -r requirements.txt

# 3. Run tests (in this dir, never in production state dir)
echo "[3/4] Running tests..."
for f in tests/test_*.py; do
    python3 "$f" || { echo "FAIL: $f -- aborting update"; exit 1; }
done
echo "      All tests OK"

# 4. Restart services
echo "[4/4] Restarting services..."
systemctl --user restart jupiter-scheduler jupiter-panel

echo ""
echo "=== Update complete ==="
systemctl --user status jupiter-scheduler --no-pager | head -5
systemctl --user status jupiter-panel      --no-pager | head -5
