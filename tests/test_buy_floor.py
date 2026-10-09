"""test_buy_floor.py -- Tests for buy floor, floor zone, and floor cadence.

Self-contained, stdlib only. Run: python3 tests/test_buy_floor.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine import (
    buy_floor_blocks, floor_zone_threshold, floor_cadence_decision,
    floor_reserve_cap_reached, relative_ceiling,
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

def check_false(name, val):
    if val:
        FAILURES.append(f"FAIL {name}: expected falsy, got {val!r}")
    else:
        print(f"  ok  {name}")


print("=== test_buy_floor.py ===")

# --- buy_floor_blocks ---
print("\n[buy_floor_blocks]")
check_false("floor None: never blocks", buy_floor_blocks(25, 100, None))
check_false("floor 0: never blocks", buy_floor_blocks(25, 100, 0))
check_false("cash above floor after buy", buy_floor_blocks(25, 100, 50))
check_true("cash below floor after buy", buy_floor_blocks(25, 100, 80))
check_true("cash exactly at floor", buy_floor_blocks(50, 100, 51))
check_false("cash stays above floor", buy_floor_blocks(50, 100, 50))

# crash fraction softens the floor
check_true("crash fraction=1 full floor", buy_floor_blocks(25, 100, 80, is_crash=True, crash_fraction=1.0))
check_false("crash fraction=0.5 softened", buy_floor_blocks(25, 100, 80, is_crash=True, crash_fraction=0.5))
# 100 - 25 = 75, floor = 80 * 0.5 = 40, 75 >= 40 -> not blocked

# edge: buy entire balance
check_true("buy all cash, any floor blocks", buy_floor_blocks(100, 100, 1))

# --- floor_zone_threshold ---
print("\n[floor_zone_threshold: pctl mode]")
prices = [float(i) for i in range(1, 101)]
t = floor_zone_threshold("pctl", prices, 25.0)
check_true("pctl P25 of 1..100", t is not None and t <= 26)

check("pctl too few prices", floor_zone_threshold("pctl", [5.0], 25.0), None)

print("\n[floor_zone_threshold: min48h mode]")
# 100 prices, min=1.0, band=5% -> threshold = 1.0 * 1.05 = 1.05
t = floor_zone_threshold("min48h", prices, 25.0, window_h=48, band_pct=5.0, min_points=10)
check("min48h threshold", t, 1.0 * 1.05)

# too few points
t = floor_zone_threshold("min48h", [1.0, 2.0], 25.0, min_points=60)
check("min48h too few points", t, None)

# band_pct=0 -> exact min
t = floor_zone_threshold("min48h", prices, 25.0, band_pct=0.0, min_points=10)
check("min48h band=0 is exact min", t, 1.0)

# --- floor_cadence_decision ---
print("\n[floor_cadence_decision]")
cfg = {
    "FLOOR_CADENCE": True,
    "FLOOR_CADENCE_N_H": 2,
    "FLOOR_CADENCE_M_H": 6,
    "FLOOR_CADENCE_MAX": 2,
    "LOT_USD": 25,
}

# outside zone: returns (False, None) and closes episode
buy, ep = floor_cadence_decision(False, 1000, {"start_ts": 0, "lots": 1}, None, 100, cfg)
check_false("outside zone: no buy", buy)
check("outside zone: ep is None", ep, None)

# in zone, cadence off
cfg_off = dict(cfg, FLOOR_CADENCE=False)
buy, ep = floor_cadence_decision(True, 1000, None, None, 100, cfg_off)
check_false("cadence off: no buy", buy)
check_true("cadence off: ep created", ep is not None and "start_ts" in ep)

# in zone, too early (< N hours)
now = 1000
buy, ep = floor_cadence_decision(True, now, {"start_ts": now - 3600, "lots": 0}, None, 100, cfg)
check_false("too early in zone: no buy", buy)

# in zone, N hours passed, no recent buy
now = 1000 + 2 * 3600 + 1  # just past 2h
buy, ep = floor_cadence_decision(True, now, {"start_ts": 1000, "lots": 0}, None, 100, cfg)
check_true("ready after N hours: buy", buy)
check("lots incremented", ep["lots"], 1)

# in zone, N hours but recent buy (< M hours)
last_buy = now - 3600  # 1h ago, M=6h
buy, ep = floor_cadence_decision(True, now, {"start_ts": 1000, "lots": 0}, last_buy, 100, cfg)
check_false("recent buy blocks cadence", buy)

# in zone, lots at cap
buy, ep = floor_cadence_decision(True, now, {"start_ts": 1000, "lots": 2}, None, 100, cfg)
check_false("cap reached: no buy", buy)

# in zone, not enough cash
buy, ep = floor_cadence_decision(True, now, {"start_ts": 1000, "lots": 0}, None, 10, cfg)
check_false("insufficient cash: no buy", buy)

# first entry into zone: creates episode
buy, ep = floor_cadence_decision(True, 5000, None, None, 100, cfg)
check_false("first entry: no immediate buy", buy)
check("first entry: ep.start_ts", ep["start_ts"], 5000)
check("first entry: ep.lots", ep["lots"], 0)

# --- floor_reserve_cap_reached ---
print("\n[floor_reserve_cap_reached]")
check_false("cap None: never reached", floor_reserve_cap_reached([], None))
check_false("cap 0: never reached", floor_reserve_cap_reached([], 0))
check_false("no reserve lots", floor_reserve_cap_reached(
    [{"origin": "grid"}, {"origin": "grid"}], 3))

lots_with_reserves = [
    {"origin": "reserve"}, {"origin": "reserve"}, {"origin": "reserve"},
    {"origin": "grid"},
]
check_true("3 reserve lots, cap=3: reached", floor_reserve_cap_reached(lots_with_reserves, 3))
check_false("3 reserve lots, cap=4: not reached", floor_reserve_cap_reached(lots_with_reserves, 4))

print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILURES: {len(FAILURES)}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
