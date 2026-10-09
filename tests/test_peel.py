"""test_peel.py — Tests for group-ladder peel (#11).

Tests peel_eligible and peel_plan pure engine functions.
Run: python3 tests/test_peel.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("JUPITER_DB", ":memory:")
os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

import config  # noqa
from engine import peel_eligible, peel_plan

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


print("=== test_peel.py ===")

# Test lots: 5 lots at different buy prices
lots = [
    {"id": 1, "tokens": 100.0, "cost": 10.0, "buy_price": 0.10, "origin": "grid"},
    {"id": 2, "tokens": 100.0, "cost": 20.0, "buy_price": 0.20, "origin": "grid"},
    {"id": 3, "tokens": 100.0, "cost": 30.0, "buy_price": 0.30, "origin": "grid"},
    {"id": 4, "tokens": 100.0, "cost": 40.0, "buy_price": 0.40, "origin": "grid"},
    {"id": 5, "tokens": 100.0, "cost": 50.0, "buy_price": 0.50, "origin": "grid"},
]
# total cost = 150, total tokens = 500

# ---- peel_eligible ----
print("\n[peel_eligible]")

# At price 0.60: proceeds = 500*0.60*0.985 = 295.50, cost = 150, pct = 97% -> eligible
check_true("high_price_eligible", peel_eligible(lots, 0.60))

# At price 0.32: proceeds = 500*0.32*0.985 = 157.60, cost = 150, pct = 5.07% -> eligible (>3%)
check_true("marginal_eligible", peel_eligible(lots, 0.32))

# At price 0.31: proceeds = 500*0.31*0.985 = 152.675, cost = 150, pct = 1.78% -> not eligible (<3%)
check_false("marginal_not_eligible", peel_eligible(lots, 0.31))

# At price 0.10: proceeds = 500*0.10*0.985 = 49.25, cost = 150 -> not eligible
check_false("low_price_not_eligible", peel_eligible(lots, 0.10))

# Empty lots
check_false("empty_lots", peel_eligible([], 1.0))

# Custom min_profit_pct
check_true("custom_pct_5", peel_eligible(lots, 0.60, min_profit_pct=5.0))
check_false("custom_pct_100", peel_eligible(lots, 0.32, min_profit_pct=100.0))


# ---- peel_plan ----
print("\n[peel_plan]")

# High price: should peel cheapest lots first, up to 40% of book cost
plan = peel_plan(lots, 0.60)
check_true("peel_nonempty", len(plan) > 0)
# max_peel_cost = 150 * 0.40 = 60
# lot1 (cost=10) + lot2 (cost=20) + lot3 (cost=30) = 60 -> fits exactly
# Check that remaining [lot4,lot5] (cost=90) still eligible at 0.60:
# proceeds = 200*0.60*0.985 = 118.2, pct = (118.2-90)/90 = 31.3% -> yes
check("peel_cheapest_first", plan[0]["id"], 1)
check_true("peel_ascending", all(plan[i]["buy_price"] <= plan[i+1]["buy_price"]
                                  for i in range(len(plan)-1)))

# Check cap: total cost of peeled lots <= 40% of book
peeled_cost = sum(l["cost"] for l in plan)
check_true("peel_within_cap", peeled_cost <= 150 * 0.40 + 0.01)

# Low price: proceeds barely cover cost -> no peel
plan_low = peel_plan(lots, 0.31)
check("low_price_no_peel", len(plan_low), 0)

# Price where only some lots individually pass
# At price 0.35: all lots except lot5 (cost=50, proceeds=100*0.35*0.985=34.475 < 50) -> let's check
# Actually peel_eligible checks aggregate, not individual. Let me use a different setup.
single_expensive = [
    {"id": 10, "tokens": 10.0, "cost": 100.0, "buy_price": 10.0, "origin": "grid"},
    {"id": 11, "tokens": 100.0, "cost": 10.0, "buy_price": 0.10, "origin": "grid"},
]
# At price 1.0: total proceeds = 110*1.0*0.985 = 108.35, total cost = 110
# agg pct = (108.35-110)/110 = -1.5% -> not eligible
check_false("mixed_not_eligible", peel_eligible(single_expensive, 1.0))

# At price 1.2: total proceeds = 110*1.2*0.985 = 130.02, total cost = 110
# agg pct = (130.02-110)/110 = 18.2% -> eligible
check_true("mixed_eligible", peel_eligible(single_expensive, 1.2))

# Peel plan should peel lot 11 (cheapest) first, but lot 10 proceeds =
# 10*1.2*0.985 = 11.82 < 100 -> remaining lot 10 alone not eligible -> peel blocked
plan_mixed = peel_plan(single_expensive, 1.2)
check("mixed_no_peel", len(plan_mixed), 0)

# All lots same price: no one lot is cheaper
same_lots = [
    {"id": 20, "tokens": 100.0, "cost": 25.0, "buy_price": 0.25, "origin": "grid"},
    {"id": 21, "tokens": 100.0, "cost": 25.0, "buy_price": 0.25, "origin": "grid"},
    {"id": 22, "tokens": 100.0, "cost": 25.0, "buy_price": 0.25, "origin": "grid"},
    {"id": 23, "tokens": 100.0, "cost": 25.0, "buy_price": 0.25, "origin": "grid"},
]
# At price 0.40: total proceeds = 400*0.40*0.985 = 157.6, total cost = 100, pct = 57.6%
plan_same = peel_plan(same_lots, 0.40)
# max_peel_cost = 100*0.40 = 40. That's 1 lot (cost=25) fits, 2 lots (50) exceeds
check("same_price_peel_count", len(plan_same), 1)

# Remaining 3 lots: proceeds = 300*0.40*0.985 = 118.2, cost = 75, pct = 57.6% -> still eligible
check_true("remaining_eligible", peel_eligible(
    [l for l in same_lots if l["id"] != plan_same[0]["id"]], 0.40))

# Edge: single lot book -> peel would leave empty book -> no peel
single_lot = [{"id": 30, "tokens": 100.0, "cost": 10.0, "buy_price": 0.10, "origin": "grid"}]
plan_single = peel_plan(single_lot, 1.0)
check("single_lot_no_peel", len(plan_single), 0)

# Empty lots
plan_empty = peel_plan([], 1.0)
check("empty_no_peel", len(plan_empty), 0)


print()
if FAILURES:
    print(f"\n{'='*60}")
    for f in FAILURES:
        print(f)
    print(f"\nFAILED: {len(FAILURES)} test(s)")
    sys.exit(1)
else:
    print("All peel tests passed.")
