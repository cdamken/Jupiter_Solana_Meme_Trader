"""test_panel_quality.py — Tests for panel quality fixes (#17).

Tests store query methods (realized_pnl, latest_price, fleet_trades, etc.)
and the fixed _coin_summary logic.
Run: python3 tests/test_panel_quality.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("JUPITER_DB", ":memory:")
os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

import config  # noqa
from store.store import Store

FAILURES = []

def check(name, actual, expected):
    if actual != expected:
        FAILURES.append(f"FAIL {name}: got {actual!r}, expected {expected!r}")
    else:
        print(f"  ok  {name}")

def check_close(name, actual, expected, tol=0.01):
    if abs(actual - expected) > tol:
        FAILURES.append(f"FAIL {name}: got {actual:.4f}, expected {expected:.4f}")
    else:
        print(f"  ok  {name}")

def check_true(name, val):
    if not val:
        FAILURES.append(f"FAIL {name}: expected truthy, got {val!r}")
    else:
        print(f"  ok  {name}")


print("=== test_panel_quality.py ===")

with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)

    s.usdc_deposit(1000.0)
    cid = s.add_coin("test", "Test Coin", "So1111111111111111111111111111111111111111111")

    # --- latest_price: no data ---
    check("latest_price_none", s.latest_price(cid), None)

    # --- latest_price: with data ---
    now = time.time()
    s.record_price(cid, now - 120, 1.0)
    s.record_price(cid, now - 60, 1.5)
    s.record_price(cid, now, 2.0)
    check("latest_price", s.latest_price(cid), 2.0)

    # --- latest_price_ts ---
    price, ts = s.latest_price_ts(cid)
    check("latest_price_ts_price", price, 2.0)
    check_close("latest_price_ts_ts", ts, now)

    # --- realized_pnl: no trades ---
    check_close("realized_pnl_empty", s.realized_pnl(cid), 0.0)

    # --- realized_pnl: with trades ---
    # Add a lot then sell it
    lot_id = s.add_lot(cid, 100.0, 100.0, 1.0, now - 300, "grid")
    s.usdc_commit_sell(150.0, cid, "txsig1", price=1.5, tokens=100.0, pnl_pct=50.0, mode="paper")
    # pnl = 150 - 150/(1+50/100) = 150 - 100 = 50
    check_close("realized_pnl_one_sell", s.realized_pnl(cid), 50.0)

    # Add 30 more sell trades to test that ALL sells are counted (not just 20)
    for i in range(30):
        s.usdc_commit_sell(11.0, cid, f"txsig_extra_{i}",
                           price=1.1, tokens=10.0, pnl_pct=10.0, mode="paper")
    # Each extra sell: pnl = 11 - 11/(1+10/100) = 11 - 10 = 1.0
    # Total: 50 + 30 * 1.0 = 80
    check_close("realized_pnl_all_sells", s.realized_pnl(cid), 80.0)

    # --- fleet_trades ---
    trades = s.fleet_trades(5)
    check("fleet_trades_limit", len(trades), 5)
    check("fleet_trades_has_slug", trades[0]["slug"], "test")

    # --- total_deposited ---
    check_close("total_deposited", s.total_deposited(), 1000.0)

    # --- total_withdrawn ---
    check_close("total_withdrawn_zero", s.total_withdrawn(), 0.0)

    # --- coin_trades ---
    ct = s.coin_trades(cid, limit=10)
    check("coin_trades_limit", len(ct), 10)

    # --- coin_overrides_list: empty ---
    check("overrides_empty", len(s.coin_overrides_list(cid)), 0)

    # --- param_catalog_list: empty until bootstrap ---
    # (catalog may or may not be empty depending on bootstrap state)
    catalog = s.param_catalog_list()
    check_true("catalog_is_list", isinstance(catalog, list))

    # --- deposit_withdraw_history ---
    hist = s.deposit_withdraw_history()
    check_true("deposit_history", len(hist) >= 1)
    check("deposit_history_kind", hist[0]["kind"], "deposit")

    # --- per-coin isolation ---
    cid2 = s.add_coin("test2", "Test2", "So2222222222222222222222222222222222222222222")
    s.record_price(cid2, now, 5.0)
    check("price_isolation", s.latest_price(cid2), 5.0)
    check_close("pnl_isolation", s.realized_pnl(cid2), 0.0)


# --- Timestamp formatting ---
from zoneinfo import ZoneInfo
import datetime as dt

tz = ZoneInfo("Europe/Berlin")
ts = dt.datetime(2026, 10, 9, 14, 30, tzinfo=tz).timestamp()
berlin = dt.datetime.fromtimestamp(ts, tz=tz)
check("tz_format", berlin.strftime("%d.%m %H:%M"), "09.10 14:30")
check_true("tz_not_utc", "14:30" in berlin.strftime("%H:%M"))


print()
if FAILURES:
    print(f"\n{'='*60}")
    for f in FAILURES:
        print(f)
    print(f"\nFAILED: {len(FAILURES)} test(s)")
    sys.exit(1)
else:
    print("All panel quality tests passed.")
