"""test_vol_engine.py -- Volatility-adaptive step engine tests.

Ported from SIMD's test_vol_engine.py. Verifies the canonical spec: hourly buckets with
LAST price wins, LOG returns between consecutive PRESENT buckets, POPULATION std (/n),
vol_1d = std*sqrt(24)*100, fail-open below 25 buckets, and the clamped k*vol mapping.
Self-contained, stdlib only. Run: python3 tests/test_vol_engine.py
"""
import sys
import os
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import vol_engine as ve

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

def approx(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


def pair(k, price, off=60):
    return (3600 * k + off, price)


print("=== test_vol_engine.py ===")

NOW = 3600 * 1000

# ---- hourly_buckets ----
print("\n[hourly_buckets]")

pairs = [pair(900, 1.0, off=10), pair(900, 2.0, off=3500), pair(901, 3.0)]
series = ve.hourly_buckets(pairs, NOW)
check("last price in hour wins", series, [2.0, 3.0])

pairs_oo = [pair(900, 2.0, off=3500), pair(900, 1.0, off=10)]
check("out-of-order: keeps largest ts", ve.hourly_buckets(pairs_oo, NOW), [2.0])

pairs_win = [pair(850, 99.0), pair(900, 1.0), pair(925, 1.0)]
check("window drops old buckets", ve.hourly_buckets(pairs_win, NOW), [1.0, 1.0])

check("non-positive prices dropped",
      ve.hourly_buckets([pair(900, 0.0), pair(901, -5.0), pair(902, 4.0)], NOW), [4.0])

# ---- realized_vol ----
print("\n[realized_vol]")

r = 1.01
pairs_const = [pair(900 + i, r ** i) for i in range(30)]
check_true("constant ratio -> vol 0", approx(ve.realized_vol(pairs_const, NOW), 0.0))

a = 0.05
prices = [1.0]
sign = 1
for _ in range(26):
    prices.append(prices[-1] * math.exp(sign * a))
    sign = -sign
pairs_alt = [pair(900 + i, prices[i]) for i in range(27)]
expected = a * math.sqrt(24) * 100.0
check_true("alternating +-a: vol = a*sqrt(24)*100",
           approx(ve.realized_vol(pairs_alt, NOW), expected, tol=1e-7))

pairs_few = [pair(900 + i, 1.0 + i) for i in range(24)]
check("24 buckets -> None (fail-open)", ve.realized_vol(pairs_few, NOW), None)

pairs_25 = [pair(900 + i, 1.0 + i) for i in range(25)]
check_true("25 buckets -> measured", ve.realized_vol(pairs_25, NOW) is not None)

check("empty -> None", ve.realized_vol([], NOW), None)

# ---- vol_steps ----
print("\n[vol_steps]")

K_BUY, K_SELL = 0.20, 1.30
BMIN, BMAX, SMIN, SMAX = 2.0, 10.0, 6.0, 25.0

def pv(vol):
    return ve.vol_steps(vol, K_BUY, K_SELL, BMIN, BMAX, SMIN, SMAX)

buy, sell = pv(20.0)
check_true("mid buy = k_buy*vol (4.0)", approx(buy, 4.0))
check_true("sell clamped to max (25)", approx(sell, 25.0))

buy, sell = pv(3.0)
check_true("low vol: buy clamped to min (2)", approx(buy, 2.0))
check_true("low vol: sell clamped to min (6)", approx(sell, 6.0))

buy, sell = pv(100.0)
check_true("high vol: buy clamped to max (10)", approx(buy, 10.0))
check_true("high vol: sell clamped to max (25)", approx(sell, 25.0))

buy, sell = pv(15.0)
check_true("both linear at vol=15 (buy 3.0, sell 19.5)",
           approx(buy, 3.0) and approx(sell, 19.5))

check("None passthrough", pv(None), None)

# ---- adaptive_saturated ----
print("\n[adaptive_saturated]")

now_sat = 100000.0
samples_pinned = [(now_sat - i * 3600, 25.0, True, False) for i in range(24)]
sat, which, hrs = ve.adaptive_saturated(samples_pinned, SMIN, SMAX, now_sat)
check_true("all pinned at max -> saturated", sat and which == "max")

samples_mixed = [(now_sat - i * 3600, 15.0 + (i % 3), True, False) for i in range(24)]
sat2, _, _ = ve.adaptive_saturated(samples_mixed, SMIN, SMAX, now_sat)
check("mixed values -> not saturated", sat2, False)

samples_few = [(now_sat - i * 3600, 25.0, True, False) for i in range(3)]
sat3, _, _ = ve.adaptive_saturated(samples_few, SMIN, SMAX, now_sat)
check("too few samples -> not saturated", sat3, False)

samples_off = [(now_sat - i * 3600, 25.0, False, False) for i in range(24)]
sat4, _, _ = ve.adaptive_saturated(samples_off, SMIN, SMAX, now_sat)
check("adaptive off -> not saturated", sat4, False)


print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILURES: {len(FAILURES)}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
