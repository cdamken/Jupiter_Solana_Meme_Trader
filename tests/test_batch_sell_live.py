"""test_batch_sell_live.py — Tests for batch sell LIVE semantics (#7).

Tests paper_batch_sell, preflight_batch_shrink, and scheduler integration.
Run: python3 tests/test_batch_sell_live.py
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
from execute import (
    paper_batch_sell, preflight_batch_shrink, paper_sell,
    revalidate_effective_sale,
)

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

def check_close(name, actual, expected, tol=0.01):
    if abs(actual - expected) > tol:
        FAILURES.append(f"FAIL {name}: got {actual:.4f}, expected {expected:.4f}")
    else:
        print(f"  ok  {name}")


print("=== test_batch_sell_live.py ===")

# ---- paper_batch_sell: basic batch ----
print("\n[paper_batch_sell: basic]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("test", "TEST", "TestMint" + "1" * 33)
    s.usdc_deposit(300.0)
    now = time.time()

    # Create 3 lots at different prices
    lid1 = s.add_lot(cid, 100.0, 50.0, 0.50, now - 300, "grid")
    lid2 = s.add_lot(cid, 100.0, 40.0, 0.40, now - 200, "grid")
    lid3 = s.add_lot(cid, 100.0, 30.0, 0.30, now - 100, "grid")

    lots = [dict(r) for r in s.get_lots(cid)]
    check("3 lots created", len(lots), 3)

    # Batch sell at price 0.60 — all should be profitable
    txsig = paper_batch_sell(s, cid, lots, 0.60)
    check_true("batch_txsig", txsig.startswith("paper-batch-"))
    check("all lots removed", len(s.get_lots(cid)), 0)

    # Proceeds = (100*0.60 + 100*0.60 + 100*0.60) * (1 - 0.015) = 180 * 0.985 = 177.30
    expected_proceeds = 300 * 0.60 * (1 - 0.015)
    check_close("balance_after_batch", s.usdc_balance(), 300.0 + expected_proceeds)

# ---- paper_batch_sell: one lot fails no-loss ----
print("\n[paper_batch_sell: partial fail]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("test", "TEST", "TestMint" + "2" * 33)
    s.usdc_deposit(300.0)
    now = time.time()

    # Lot 1: cost=50 at 0.50 — needs price > 0.508 to clear fee
    lid1 = s.add_lot(cid, 100.0, 50.0, 0.50, now - 200, "grid")
    # Lot 2: cost=60 at 0.60 — needs price > 0.609 to clear fee
    lid2 = s.add_lot(cid, 100.0, 60.0, 0.60, now - 100, "grid")

    lots = [dict(r) for r in s.get_lots(cid)]
    # Price 0.55: lot1 passes (55*0.985=54.175 > 50), lot2 fails (55*0.985=54.175 < 60)
    txsig = paper_batch_sell(s, cid, lots, 0.55)
    check_true("partial_batch_txsig", bool(txsig))
    remaining = s.get_lots(cid)
    check("lot2_survives", len(remaining), 1)
    check("lot2_id", remaining[0]["id"], lid2)

# ---- paper_batch_sell: empty list ----
print("\n[paper_batch_sell: empty]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("test", "TEST", "TestMint" + "3" * 33)
    txsig = paper_batch_sell(s, cid, [], 1.0)
    check("empty_batch", txsig, "")

# ---- paper_batch_sell: all fail no-loss ----
print("\n[paper_batch_sell: all fail]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("test", "TEST", "TestMint" + "4" * 33)
    s.usdc_deposit(100.0)
    now = time.time()
    lid = s.add_lot(cid, 100.0, 50.0, 0.50, now, "grid")
    lots = [dict(r) for r in s.get_lots(cid)]
    # Price 0.40: proceeds = 100*0.40*0.985 = 39.4 < cost 50
    txsig = paper_batch_sell(s, cid, lots, 0.40)
    check("all_fail_blocked", txsig, "")
    check("lots_preserved", len(s.get_lots(cid)), 1)

# ---- preflight_batch_shrink ----
print("\n[preflight_batch_shrink]")
lot_a = {"id": 1, "tokens": 100.0, "cost": 50.0, "buy_price": 0.50, "origin": "grid"}
lot_b = {"id": 2, "tokens": 100.0, "cost": 40.0, "buy_price": 0.40, "origin": "grid"}
lot_c = {"id": 3, "tokens": 100.0, "cost": 90.0, "buy_price": 0.90, "origin": "grid"}

# All pass at price 1.0
passing = preflight_batch_shrink([lot_a, lot_b, lot_c], 1.0)
check("all_pass", len(passing), 3)

# lot_c fails at price 0.55 (proceeds=100*0.55*0.985=54.175 < 90)
passing2 = preflight_batch_shrink([lot_a, lot_b, lot_c], 0.55)
check("one_fails", len(passing2), 2)
ids = [l["id"] for l in passing2]
check_true("lot_c_dropped", 3 not in ids)

# Rider lots dropped first
lot_rider = {"id": 4, "tokens": 10.0, "cost": 50.0, "buy_price": 5.0, "origin": "rider"}
# At price 1.0: rider proceeds = 10*1.0*0.985 = 9.85 < 50 -> fails anyway
passing3 = preflight_batch_shrink([lot_a, lot_b, lot_rider], 1.0)
ids3 = [l["id"] for l in passing3]
check_true("rider_dropped", 4 not in ids3)
check("rider_non_riders_kept", len(passing3), 2)

# ---- paper_batch_sell applies fee ----
print("\n[batch_sell_fee]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("test", "TEST", "TestMint" + "5" * 33)
    s.usdc_deposit(100.0)
    now = time.time()
    lid = s.add_lot(cid, 100.0, 50.0, 0.50, now, "grid")
    lots = [dict(r) for r in s.get_lots(cid)]
    txsig = paper_batch_sell(s, cid, lots, 0.60)
    # batch proceeds = 100 * 0.60 * 0.985 = 59.10
    check_close("batch_fee_applied", s.usdc_balance(), 100.0 + 100.0 * 0.60 * 0.985, tol=0.02)

# ---- scheduler wiring: batch path exists ----
print("\n[scheduler_wiring]")
sched_src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "scheduler.py")).read()
check_true("imports_batch_sell", "paper_batch_sell" in sched_src)
check_true("imports_preflight", "preflight_batch_shrink" in sched_src)
check_true("batch_path_branch", "use_batch and len(sell_lots) > 1" in sched_src)
check_true("serial_fallback", "Serial single-lot sells" in sched_src)


print()
if FAILURES:
    print(f"\n{'='*60}")
    for f in FAILURES:
        print(f)
    print(f"\nFAILED: {len(FAILURES)} test(s)")
    sys.exit(1)
else:
    print("All batch sell tests passed.")
