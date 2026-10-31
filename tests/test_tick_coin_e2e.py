"""test_tick_coin_e2e.py -- End-to-end smoke test for tick_coin (#38).

Calls tick_coin against a real in-memory Store with fetch_price stubbed.
Catches NameError / AttributeError bugs (like the ts NameError from #38)
that pure-function tests cannot see because they never call tick_coin.

Run: python3 tests/test_tick_coin_e2e.py
"""
import os
import sys
import tempfile
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("RPC_URL",       "http://localhost:8899")
os.environ.setdefault("KEYPAIR_PATH",  "/dev/null")
os.environ.setdefault("QUOTE_MINT",    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

import config  # noqa -- env vars must be set before import
from store.store import Store
import scheduler

FAILURES = []


def chk(name, cond):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f" FAIL {name}")
        FAILURES.append(name)


print("=== test_tick_coin_e2e.py ===")

MINT = "SiMDiRbLGPTH7xMeFDHADV6f3fhDpeFVFJqy3dKqP7k"

with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "e2e.db")
    store = Store(db)

    coin_id = store.add_coin("simd", "SIMD", MINT, status="paper")
    store.usdc_deposit(500.0)

    coin = {"id": coin_id, "slug": "simd", "label": "SIMD",
            "mint": MINT, "status": "paper", "decimals": 6}

    # --- case 1: price = None (no-price path) ---
    print("\n[case 1: no-price path]")
    with unittest.mock.patch("execute.fetch_price", return_value=None):
        try:
            scheduler.tick_coin(store, dict(coin), force_mode="paper")
            chk("tick_coin completes without exception (no price)", True)
        except Exception as e:
            chk(f"tick_coin completes without exception (no price): {e}", False)

    decisions = store.get_decisions(coin_id, limit=5)
    chk("skip decision logged on no-price", any(
        d["action"] == "skip" and d["reason"] == "no_price" for d in decisions
    ))

    # --- case 2: normal price, no lots -> buy decision ---
    print("\n[case 2: buy on empty book]")
    with unittest.mock.patch("execute.fetch_price", return_value=1.0):
        try:
            scheduler.tick_coin(store, dict(coin), force_mode="paper")
            chk("tick_coin completes without exception (price=1.0)", True)
        except Exception as e:
            chk(f"tick_coin completes without exception (price=1.0): {e}", False)

    decisions2 = store.get_decisions(coin_id, limit=10)
    chk("a decision was logged after normal tick", len(decisions2) > len(decisions))

    # --- case 3: repeated ticks do not raise ---
    print("\n[case 3: five consecutive ticks]")
    errors = 0
    with unittest.mock.patch("execute.fetch_price", return_value=1.05):
        for i in range(5):
            try:
                scheduler.tick_coin(store, dict(coin), force_mode="paper")
            except Exception as e:
                errors += 1
                print(f"  tick {i+1} raised: {e}")
    chk("five consecutive ticks without exception", errors == 0)

print()
if FAILURES:
    print(f"FAILURES: {len(FAILURES)}")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
