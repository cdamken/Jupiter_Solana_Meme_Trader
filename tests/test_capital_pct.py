"""test_capital_pct.py -- Issue #6: MAX_CAPITAL_PCT concentration cap.

Tests the pure apply_capital_pct_cap helper (verbatim from SIMD #1021).
Stdlib only.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine import apply_capital_pct_cap

FAILURES = []


def chk(name, cond):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f" FAIL {name}")
        FAILURES.append(name)


# pct OFF (0 or negative) -> cap_usd unchanged
chk("pct=0 -> cap unchanged", apply_capital_pct_cap(200, 1000, 0) == 200)
chk("pct=-5 -> cap unchanged", apply_capital_pct_cap(200, 1000, -5) == 200)
chk("pct=None -> cap unchanged", apply_capital_pct_cap(200, 1000, None) == 200)

# pct ON: ceiling = fleet_total * pct / 100
chk("30% of 1000 = 300, cap=200 -> 200 (cap is tighter)",
    apply_capital_pct_cap(200, 1000, 30) == 200)

chk("10% of 1000 = 100, cap=200 -> 100 (pct is tighter)",
    apply_capital_pct_cap(200, 1000, 10) == 100)

chk("50% of 400 = 200, cap=200 -> 200 (exact match)",
    apply_capital_pct_cap(200, 400, 50) == 200)

chk("100% of fleet -> cap unchanged (no restriction)",
    apply_capital_pct_cap(200, 200, 100) == 200)

# cap_usd=None -> ceiling only
chk("cap=None, 30% of 1000 -> 300",
    apply_capital_pct_cap(None, 1000, 30) == 300)

# fleet_total=0 -> ceiling=0 (no buys)
chk("fleet_total=0, pct=30 -> 0",
    apply_capital_pct_cap(200, 0, 30) == 0)

# small fleet
chk("5% of 100 = 5, cap=200 -> 5",
    apply_capital_pct_cap(200, 100, 5) == 5)

# float precision
chk("33.33% of 300 -> ~100",
    abs(apply_capital_pct_cap(200, 300, 33.33) - 99.99) < 0.01)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)}")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL OK")
