"""test_engine.py — Unit tests for grid_decision and helpers.

Self-contained, stdlib only. Run: python3 tests/test_engine.py
Exits 0 on success, 1 on failure.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine import grid_decision, relative_ceiling, price_gate

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


print("=== test_engine.py ===")

# --- price gate: zero/nan rejected ---
print("\n[price_gate]")
d = grid_decision(0.0, None, [], 4.0, 25.0, None, 1000.0)
check("price=0 -> no buy/sell", d["buy_usd"], 0.0)
check("price=0 -> sells empty", d["sells"], [])

# --- no lots, no ref: first tick sets ref, no buy ---
d = grid_decision(0.5, None, [], 4.0, 25.0, None, 1000.0)
check("first tick: no buy", d["buy_usd"], 0.0)
check("first tick: ref set to price", d["new_ref"], 0.5)

# --- price drops buy_step: triggers buy ---
print("\n[grid_decision: buy]")
d = grid_decision(0.48, 0.5, [], 4.0, 25.0, None, 1000.0)
check("4% drop triggers buy", d["buy_usd"], 25.0)
check("new_ref follows buy price", d["new_ref"], 0.48)

# --- insufficient cash: no buy ---
d = grid_decision(0.48, 0.5, [], 4.0, 25.0, None, 10.0)
check("insufficient cash: no buy", d["buy_usd"], 0.0)

# --- ceiling blocks buy ---
d = grid_decision(0.48, 0.5, [], 4.0, 25.0, 0.40, 1000.0)
check("price above ceiling: no buy", d["buy_usd"], 0.0)

# --- sell trigger ---
print("\n[grid_decision: sell]")
lot = {"id": 1, "buy_price": 0.5, "tokens": 100.0, "cost": 50.0,
       "trail_armed": 0, "trail_peak": 0.0}
d = grid_decision(0.52, 0.5, [lot], 4.0, 25.0, None, 1000.0)
check("4% gain triggers sell", len(d["sells"]), 1)
check("sell lot is our lot", d["sells"][0]["id"], 1)

# --- hard rule: no sell at a loss ---
d = grid_decision(0.498, 0.5, [lot], 4.0, 25.0, None, 1000.0)
check("no sell at a loss", len(d["sells"]), 0)

# --- trail: arm then trigger ---
print("\n[grid_decision: trailing sell]")
lot2 = {"id": 2, "buy_price": 0.5, "tokens": 100.0, "cost": 50.0,
        "trail_armed": 0, "trail_peak": 0.0}
# Price at 4% gain -> arms the trail, does NOT sell yet
d = grid_decision(0.52, 0.5, [lot2], 4.0, 25.0, None, 1000.0,
                  sell_trail=True, sell_trail_pct=2.0)
check("trail: arm on gain, no sell yet", len(d["sells"]), 0)
check_true("trail: trail_updates populated", len(d["trail_updates"]) == 1)
_, armed, peak = d["trail_updates"][0]
check("trail: armed=1", armed, 1)
check("trail: peak = price", peak, 0.52)

# Pullback >= trail_pct -> sell (peak=0.60, pullback to 0.588 = 2%, gain=17.6% net > fee)
lot3 = {"id": 3, "buy_price": 0.5, "tokens": 100.0, "cost": 50.0,
        "trail_armed": 1, "trail_peak": 0.60}
d = grid_decision(0.588, 0.5, [lot3], 4.0, 25.0, None, 1000.0,
                  sell_trail=True, sell_trail_pct=2.0)
check("trail: pullback triggers sell", len(d["sells"]), 1)

# --- relative_ceiling ---
print("\n[relative_ceiling]")
check("ceiling: None on < 2 prices", relative_ceiling([0.5]), None)
prices = [float(i) for i in range(1, 101)]
c = relative_ceiling(prices, 98.0)
check_true("ceiling: P98 of 1..100", c is not None and c >= 97)

# --- price_gate ---
print("\n[price_gate]")
# within range: accept
p, cand, n = price_gate(1.05, 1.0, 10.0)
check("gate: within range accepted", p, 1.05)
check("gate: candidate reset", cand, 0.0)

# outlier: reject
p, cand, n = price_gate(2.0, 1.0, 10.0)
check("gate: outlier rejected", p, None)
check("gate: candidate set to price", cand, 2.0)

# consensus: same outlier 5x -> re-anchor
cand, nc = 2.0, 0
for _ in range(5):
    p, cand, nc = price_gate(2.0, 1.0, 10.0, cand, nc, consensus_n=5)
check("gate: consensus reanchors", p, 2.0)

# no baseline: always accept
p, cand, n = price_gate(999.0, 0.0, 10.0)
check("gate: no baseline always accepts", p, 999.0)

# ---
print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILURES: {len(FAILURES)}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
