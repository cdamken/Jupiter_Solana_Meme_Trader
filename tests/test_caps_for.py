"""test_caps_for.py -- Issue #34: concentration cap scales with coin count.

Tests the pure caps_for() helper that computes (normal_cap_pct, band_cap_pct)
from the active coin count and tuning knobs.
Stdlib only.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine import caps_for

FAILURES = []


def chk(name, cond):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f" FAIL {name}")
        FAILURES.append(name)


def approx(a, b, tol=0.01):
    return abs(a - b) < tol


# --- N=0 and N<0: return ceiling defaults ---
chk("N=0 -> (ceil_normal, ceil_band)",
    caps_for(0) == (60.0, 80.0))
chk("N=-1 -> (ceil_normal, ceil_band)",
    caps_for(-1) == (60.0, 80.0))
chk("N=-100 -> (ceil_normal, ceil_band)",
    caps_for(-100) == (60.0, 80.0))

# --- N=1: 100% fair share * 1.5 = 150% -> clamped to ceil ---
n, b = caps_for(1)
chk("N=1 normal: 1.5*100=150 -> clamped to 60",
    n == 60.0)
chk("N=1 band: 2.0*100=200 -> clamped to 80",
    b == 80.0)

# --- N=2: 50% * 1.5 = 75% -> clamped to 60 ---
n, b = caps_for(2)
chk("N=2 normal: 1.5*50=75 -> clamped to 60",
    n == 60.0)
chk("N=2 band: 2.0*50=100 -> clamped to 80",
    b == 80.0)

# --- N=5: 20% * 1.5 = 30% (no clamp) and 20% * 2.0 = 40% ---
n, b = caps_for(5)
chk("N=5 normal: 1.5*20=30 (no clamp)",
    n == 30.0)
chk("N=5 band: 2.0*20=40 (no clamp)",
    b == 40.0)

# --- N=5 reproduces SIMD's 30/40 exactly ---
chk("N=5 reproduces SIMD 30/40",
    caps_for(5) == (30.0, 40.0))

# --- N=10: 10% * 1.5 = 15% and 10% * 2.0 = 20% ---
n, b = caps_for(10)
chk("N=10 normal: 1.5*10=15",
    n == 15.0)
chk("N=10 band: 2.0*10=20",
    b == 20.0)

# --- N=15: 6.67% * 1.5 = 10% -> at floor ---
n, b = caps_for(15)
chk("N=15 normal: 1.5*6.67=10 (at floor)",
    approx(n, 10.0))
chk("N=15 band: 2.0*6.67=13.33",
    approx(b, 13.33))

# --- N=20: 5% * 1.5 = 7.5% -> clamped to floor_pct=10% ---
n, b = caps_for(20)
chk("N=20 normal: 1.5*5=7.5 -> clamped to floor 10",
    n == 10.0)
chk("N=20 band: 2.0*5=10 (at floor)",
    b == 10.0)

# --- Custom k values ---
n, b = caps_for(5, k_normal=2.0, k_band=3.0)
chk("N=5 custom k: 2.0*20=40, 3.0*20=60",
    n == 40.0 and b == 60.0)

# --- Custom floor ---
n, b = caps_for(20, floor_pct=5.0)
chk("N=20 floor=5: 1.5*5=7.5 (above floor 5)",
    n == 7.5)

# --- Custom ceilings ---
n, b = caps_for(1, ceil_normal=40.0, ceil_band=50.0)
chk("N=1 custom ceil: clamped to 40/50",
    n == 40.0 and b == 50.0)

# --- N=3: 33.33% * 1.5 = 50%, 33.33% * 2.0 = 66.67% ---
n, b = caps_for(3)
chk("N=3 normal: 50 (no clamp)",
    approx(n, 50.0))
chk("N=3 band: 66.67 (no clamp)",
    approx(b, 66.67))

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)}")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL OK")
