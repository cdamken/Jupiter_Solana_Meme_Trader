"""test_topup.py -- Tests for topup insurance decision logic.

Self-contained, stdlib only. Run: python3 tests/test_topup.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine import topup_buy_allowed

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

def check_false(name, val):
    if val:
        FAILURES.append(f"FAIL {name}: expected falsy, got {val!r}")
    else:
        print(f"  ok  {name}")


print("=== test_topup.py ===")

LOT = 15.0
a_lot = [{"tokens": 1000.0, "cost": 15.0, "buy_price": 0.015, "origin": "topup"}]

print("\n[topup_buy_allowed]")

# happy path: empty book, on, cash, no prior buy
check_true("empty book + on + cash -> buy",
           topup_buy_allowed(True, LOT, [], False, 100.0))

# off: flag gates everything
check_false("off -> no buy", topup_buy_allowed(False, LOT, [], False, 100.0))

# lot_usd <= 0
check_false("lot_usd=0 -> no buy", topup_buy_allowed(True, 0.0, [], False, 100.0))
check_false("lot_usd=-1 -> no buy", topup_buy_allowed(True, -1.0, [], False, 100.0))

# non-empty book
check_false("non-empty book -> no buy", topup_buy_allowed(True, LOT, a_lot, False, 100.0))

# another buy already fired
check_false("bought_this_tick -> no buy", topup_buy_allowed(True, LOT, [], True, 100.0))

# cash gate
check_false("cash < lot -> no buy", topup_buy_allowed(True, LOT, [], False, 14.99))
check_true("cash == lot -> buy", topup_buy_allowed(True, LOT, [], False, 15.0))
check_true("cash > lot -> buy", topup_buy_allowed(True, LOT, [], False, 15.01))


print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILURES: {len(FAILURES)}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
