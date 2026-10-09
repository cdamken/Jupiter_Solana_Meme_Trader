"""test_batch_sale.py -- Batch-sale planning helpers.

Ported from SIMD's test_batch_sale.py with Jupiter field names (tokens, buy_price).
Self-contained, stdlib only. Run: python3 tests/test_batch_sale.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine import cap_batch_plan, _lot_gain_pct, drop_least_gain_lot

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


def lot(tokens, buy_price, cost=None, origin="grid"):
    return {"tokens": float(tokens), "buy_price": float(buy_price),
            "cost": (tokens * buy_price if cost is None else float(cost)),
            "origin": origin, "id": id(tokens)}


print("=== test_batch_sale.py ===")

P = 0.0010
plan = [lot(10000, 0.0008), lot(10000, 0.0007), lot(10000, 0.0006)]

# ---- cap_batch_plan: count cap ----
print("\n[cap_batch_plan: count cap]")
check("max_lots trims to first N", len(cap_batch_plan(plan, P, None, 2)), 2)
check("max_lots=None keeps all", len(cap_batch_plan(plan, P, None, None)), 3)

# ---- cap_batch_plan: value cap ----
print("\n[cap_batch_plan: value cap]")
check("value cap stops before exceeding ($25 -> 2 lots of $10)",
      len(cap_batch_plan(plan, P, 25.0, 10)), 2)
check("value cap fits exactly ($30 -> all 3)",
      len(cap_batch_plan(plan, P, 30.0, 10)), 3)
check("always keeps >= 1 lot even if it alone exceeds cap",
      len(cap_batch_plan([lot(100000, 0.0008)], P, 25.0, 10)), 1)
check("max_trade_usd=None leaves value cap off",
      len(cap_batch_plan(plan, P, None, 10)), 3)
check("empty plan -> empty", cap_batch_plan([], P, 25.0, 2), [])

# both caps together
check("both caps: min(count,value) governs",
      len(cap_batch_plan(plan, P, 30.0, 2)), 2)

# prefix order preserved
got = cap_batch_plan(plan, P, 25.0, 10)
check_true("capped plan is a prefix of input order",
           len(got) == 2 and got[0]["buy_price"] == 0.0008 and got[1]["buy_price"] == 0.0007)

# ---- _lot_gain_pct ----
print("\n[_lot_gain_pct]")
import math
check_true("positive gain", abs(_lot_gain_pct(lot(10000, 0.0008), P) - 25.0) < 0.01)
check_true("free lot -> inf", _lot_gain_pct(lot(5000, 0.0, cost=0.0), P) == float("inf"))

# ---- drop_least_gain_lot ----
print("\n[drop_least_gain_lot]")
d = drop_least_gain_lot(plan, P)
check("drops exactly one", len(d), 2)
check_true("drops the least-gain lot (0.0008)",
           all(l["buy_price"] != 0.0008 for l in d))
check_true("preserves order of the rest",
           d[0]["buy_price"] == 0.0007 and d[1]["buy_price"] == 0.0006)

# free-token lot survives
dg = drop_least_gain_lot([lot(5000, 0.0, cost=0.0), lot(10000, 0.0009)], P)
check("free-token lot never dropped first", len(dg), 1)
check_true("free-token lot is the survivor", dg[0]["buy_price"] == 0.0)

check("len 1 -> unchanged", len(drop_least_gain_lot([lot(10000, 0.0008)], P)), 1)
check("len 0 -> empty", drop_least_gain_lot([], P), [])

# iterative shrink converges to highest-gain lot
cur = list(plan)
while len(cur) > 1:
    cur = drop_least_gain_lot(cur, P)
check("iterative shrink -> highest-gain lot", len(cur), 1)
check_true("survivor is the 0.0006 lot", cur[0]["buy_price"] == 0.0006)


print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILURES: {len(FAILURES)}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
