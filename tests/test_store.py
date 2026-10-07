"""test_store.py — Integration tests for the SQLite store.

Self-contained, stdlib only. Uses a temp DB (never touches production).
Run: python3 tests/test_store.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("JUPITER_DB", ":memory:")  # guard, overridden below
os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

import config  # noqa -- env vars must be set before import
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


print("=== test_store.py ===")

with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)

    # ---- coins ----
    print("\n[coins]")
    coin_id = s.add_coin("simd", "SIMD", "simdMint111111111111111111111111111111111111")
    check("add_coin returns id", type(coin_id), int)
    coin = s.get_coin("simd")
    check_true("get_coin finds it", coin is not None)
    check("coin slug", coin["slug"], "simd")
    check("coin status default paper", coin["status"], "paper")
    s.set_coin_status(coin_id, "live")
    check("set status live", s.get_coin("simd")["status"], "live")
    coins = s.list_coins(status="live")
    check("list_coins by status", len(coins), 1)

    # ---- USDC ledger ----
    print("\n[usdc_ledger]")
    check("initial balance 0", s.usdc_balance(), 0.0)
    s.usdc_deposit(100.0)
    check("after deposit 100", s.usdc_balance(), 100.0)

    ok = s.usdc_reserve(30.0, coin_id)
    check("reserve 30: ok", ok, True)
    check("balance after reserve", s.usdc_balance(), 70.0)

    ok2 = s.usdc_reserve(80.0, coin_id)
    check("reserve 80 when only 70 free: fail", ok2, False)
    check("balance unchanged after fail", s.usdc_balance(), 70.0)

    s.usdc_release(30.0, coin_id)
    check("release restores balance", s.usdc_balance(), 100.0)

    # ---- lots ----
    print("\n[lots]")
    s.usdc_reserve(25.0, coin_id)
    lot_id = s.add_lot(coin_id, 50.0, 25.0, 0.5, time.time(), "paper")
    check("add_lot returns id", type(lot_id), int)
    s.usdc_commit_buy(25.0, coin_id, "test-txsig")

    lots = s.get_lots(coin_id)
    check("get_lots count", len(lots), 1)
    check("lot tokens", lots[0]["tokens"], 50.0)

    s.update_lot_trail(lot_id, 1, 0.52)
    lots = s.get_lots(coin_id)
    check("update_lot_trail armed", lots[0]["trail_armed"], 1)
    check("update_lot_trail peak", lots[0]["trail_peak"], 0.52)

    # sell
    s.usdc_commit_sell(26.0, coin_id, "sell-txsig", 0.52, 50.0, 4.0, "paper")
    s.remove_lot(lot_id)
    check("lots empty after sell", len(s.get_lots(coin_id)), 0)
    check("balance after sell", round(s.usdc_balance(), 2), round(75.0 + 26.0, 2))

    # ---- price history ----
    print("\n[price_history]")
    now = time.time()
    s.record_price(coin_id, now - 100, 0.50)
    s.record_price(coin_id, now - 50,  0.52)
    s.record_price(coin_id, now,        0.51)
    window = s.price_window(coin_id, now - 200)
    check("price_window count", len(window), 3)
    check("first price", window[0], 0.50)

    # ---- config / fleet_defaults ----
    print("\n[config]")
    cfg = s.get_config(coin_id)
    # No fleet defaults set yet -> empty dict
    check("empty config is dict", type(cfg), dict)

    # ---- capital ----
    print("\n[capital]")
    s.set_capital(coin_id, 50.0, 200.0)
    cap = s.get_capital(coin_id)
    check("capital deployed", cap["deployed_usd"], 50.0)
    check("capital max", cap["max_usd"], 200.0)
    s.set_capital(coin_id, 75.0)    # upsert without max
    cap = s.get_capital(coin_id)
    check("capital upsert deployed", cap["deployed_usd"], 75.0)

    # ---- coin decimals ----
    print("\n[coin decimals]")
    coin_id9 = s.add_coin("wsol", "Wrapped SOL", "So11111111111111111111111111111111111111112",
                           decimals=9)
    wsol = s.get_coin("wsol")
    check_true("wsol coin found", wsol is not None)
    check("wsol decimals=9", wsol["decimals"], 9)
    simd = s.get_coin("simd")
    check("simd decimals default 6", simd["decimals"], 6)

    # ---- reconcile_reserves ----
    print("\n[reconcile_reserves]")
    s2 = Store(db)
    s2.usdc_deposit(200.0)
    s2.usdc_reserve(40.0, coin_id)      # orphan: no matching release/buy follows
    s2.usdc_reserve(20.0, coin_id9)     # orphan #2
    # Running balance: 100 (deposit) - 30 (reserve) + 30 (release)
    #   - 25 (reserve) + 0 (commit_buy marker) + 26 (commit_sell)
    #   + 200 (deposit) - 40 (orphan) - 20 (orphan) = 241
    bal_before = s2.usdc_balance()
    check("balance after two orphaned reserves", round(bal_before, 2), 241.0)
    released = s2.reconcile_reserves()
    check("reconcile released 2 orphans", released, 2)
    bal_after = s2.usdc_balance()
    check("balance restored after reconcile", round(bal_after, 2), 301.0)
    # A second reconcile should find nothing new
    released2 = s2.reconcile_reserves()
    check("second reconcile: nothing to release", released2, 0)

print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nHAY FALLAS: {len(FAILURES)} failures")
    sys.exit(1)
else:
    print("TODOS OK")
    sys.exit(0)
