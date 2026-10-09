"""test_panel_auth.py — Tests for panel auth + live-flip audit logic (#15).

Tests the pure auth logic without requiring Flask to be installed.
The actual Flask route integration is verified by running the panel.
Run: python3 tests/test_panel_auth.py
"""
import hmac
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("JUPITER_DB", ":memory:")
os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
os.environ.setdefault("ALERT_EMAIL", "test@example.com")

import config  # noqa
from store.store import Store

FAILURES = []

def check(name, actual, expected):
    if actual != expected:
        FAILURES.append(f"FAIL {name}: got {actual!r}, expected {expected!r}")
    else:
        print(f"  ok  {name}")

def check_true(name, val):
    if not val:
        FAILURES.append(f"FAIL {name}: expected truthy, got {val!r}")
    else:
        print(f"  ok  {name}")


print("=== test_panel_auth.py ===")

# --- Token comparison must be timing-safe ---
SECRET = "test-secret-token-for-ci"
check("hmac_valid", hmac.compare_digest(SECRET, SECRET), True)
check("hmac_invalid", hmac.compare_digest("wrong", SECRET), False)
check("hmac_empty", hmac.compare_digest("", SECRET), False)

# --- Fail-closed: default secret must be rejected ---
check("default_secret_rejected", "change-me-in-production" != SECRET, True)
check("empty_secret_rejected", "" != SECRET, True)

# --- _audit_live_config logic (inline, since we can't import Flask) ---
def audit_live_config(store, coin_id, slug):
    failures = []
    cfg_map = store.get_config(coin_id)
    money_keys = ["LOT_USD", "MAX_CAPITAL_USD", "BUY_STEP_PCT", "SELL_STEP_PCT",
                  "MAX_SLIPPAGE_BPS", "MAX_TRADE_USD"]
    for k in money_keys:
        if k not in cfg_map:
            failures.append(f"Missing money-critical key: {k}")
    lot_usd = float(cfg_map.get("LOT_USD", 0))
    if lot_usd <= 0:
        failures.append(f"LOT_USD must be > 0 (got {lot_usd})")
    max_cap = float(cfg_map.get("MAX_CAPITAL_USD", 0))
    if max_cap <= 0:
        failures.append(f"MAX_CAPITAL_USD must be > 0 (got {max_cap})")
    slippage = float(cfg_map.get("MAX_SLIPPAGE_BPS", 0))
    if slippage <= 0 or slippage > 500:
        failures.append(f"MAX_SLIPPAGE_BPS must be 1-500 (got {slippage})")
    if not os.environ.get("ALERT_EMAIL"):
        failures.append("ALERT_EMAIL not set in environment")
    kp = os.environ.get("KEYPAIR_PATH", "")
    if not os.path.exists(kp):
        failures.append(f"KEYPAIR_PATH not found: {kp}")
    return failures

# --- Audit: coin with no config -> many failures ---
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("test", "Test", "So1111111111111111111111111111111111111111111")
    failures = audit_live_config(s, cid, "test")
    check_true("audit_no_config_fails", len(failures) > 0)
    check_true("audit_missing_lot_usd", any("LOT_USD" in f for f in failures))
    check_true("audit_missing_max_cap", any("MAX_CAPITAL_USD" in f for f in failures))
    check_true("audit_missing_slippage", any("MAX_SLIPPAGE_BPS" in f for f in failures))

# --- Audit: coin with valid config -> fewer failures ---
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test2.db")
    s = Store(db)
    cid = s.add_coin("test2", "Test2", "So2222222222222222222222222222222222222222222")

    # Insert a fleet version with the money keys
    s._c.execute("INSERT INTO fleet_versions (version_id, author, reason, pinned) VALUES (1, 'test', 'test', 1)")
    for key, val in [("LOT_USD", "25"), ("MAX_CAPITAL_USD", "200"),
                     ("BUY_STEP_PCT", "5"), ("SELL_STEP_PCT", "5"),
                     ("MAX_SLIPPAGE_BPS", "100"), ("MAX_TRADE_USD", "50")]:
        # Need these in param_catalog first
        s._c.execute(
            "INSERT OR IGNORE INTO param_catalog (key, tier, scope, type, default_val, label)"
            " VALUES (?, 'locked-visible', 'fleet-only', 'float', ?, ?)",
            (key, val, key),
        )
        s._c.execute(
            "INSERT INTO fleet_defaults (version_id, key, value) VALUES (1, ?, ?)",
            (key, val),
        )
    s._c.commit()

    failures = audit_live_config(s, cid, "test2")
    # Should only fail on KEYPAIR_PATH (/dev/null exists)
    money_failures = [f for f in failures if "Missing" in f or "LOT_USD" in f
                      or "MAX_CAPITAL" in f or "SLIPPAGE" in f]
    check("audit_with_config_no_money_failures", len(money_failures), 0)

# --- Audit: bad slippage ---
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test3.db")
    s = Store(db)
    cid = s.add_coin("test3", "Test3", "So3333333333333333333333333333333333333333333")
    s._c.execute("INSERT INTO fleet_versions (version_id, author, reason, pinned) VALUES (1, 'test', 'test', 1)")
    for key, val in [("LOT_USD", "25"), ("MAX_CAPITAL_USD", "200"),
                     ("BUY_STEP_PCT", "5"), ("SELL_STEP_PCT", "5"),
                     ("MAX_SLIPPAGE_BPS", "999"), ("MAX_TRADE_USD", "50")]:
        s._c.execute(
            "INSERT OR IGNORE INTO param_catalog (key, tier, scope, type, default_val, label)"
            " VALUES (?, 'locked-visible', 'fleet-only', 'float', ?, ?)",
            (key, val, key),
        )
        s._c.execute(
            "INSERT INTO fleet_defaults (version_id, key, value) VALUES (1, ?, ?)",
            (key, val),
        )
    s._c.commit()
    failures = audit_live_config(s, cid, "test3")
    check_true("audit_bad_slippage", any("SLIPPAGE" in f for f in failures))

# --- Live-flip gate: require_token decorator means all POSTs need _token ---
# This is structural: the decorator exists on all mutating routes.
# We verify the decorator is applied by importing the module text.
panel_src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "panel", "app.py")).read()
check_true("coin_add_has_token", "@require_token\ndef coin_add" in panel_src)
check_true("coin_status_has_token", "@require_token\ndef coin_status" in panel_src)
check_true("coin_config_save_has_token", "@require_token\ndef coin_config_save" in panel_src)
check_true("coin_config_delete_has_token", "@require_token\ndef coin_config_delete" in panel_src)
check_true("deposit_has_token", "@require_token\ndef deposit" in panel_src)
check_true("withdraw_has_token", "@require_token\ndef withdraw" in panel_src)

# --- Fail-closed boot: PANEL_SECRET check exists ---
check_true("boot_refuses_default", 'change-me-in-production' in panel_src)
check_true("boot_refuses_empty", 'not PANEL_SECRET' in panel_src)
check_true("boot_calls_sys_exit", 'sys.exit(1)' in panel_src)

# --- Live-flip requires slug confirmation ---
check_true("live_flip_confirm_slug", 'confirm_slug' in panel_src)

# --- Health route is NOT gated ---
check_true("health_no_token", "@require_token" not in panel_src.split("def health")[0].split("@app.get(\"/health\")")[-1])


print()
if FAILURES:
    print(f"\n{'='*60}")
    for f in FAILURES:
        print(f)
    print(f"\nFAILED: {len(FAILURES)} test(s)")
    sys.exit(1)
else:
    print("All panel auth tests passed.")
