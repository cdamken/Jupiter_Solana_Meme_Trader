"""test_manual_sell.py — Tests for BOOK_SELL and LIFETIME RESET (#12/#929).

Self-contained, stdlib only.
Run: python3 tests/test_manual_sell.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine import book_sell_allowed, lifetime_reset_allowed

FAILURES = []

def check(name, actual, expected):
    if actual != expected:
        FAILURES.append(f"FAIL {name}: got {actual!r}, expected {expected!r}")
    else:
        print(f"  ok  {name}")

def check_close(name, actual, expected, tol=0.01):
    if abs(actual - expected) > tol:
        FAILURES.append(f"FAIL {name}: got {actual:.4f}, expected {expected:.4f}")
    else:
        print(f"  ok  {name}")


print("=== test_manual_sell.py ===")

def lot(tokens, cost, buy_price):
    return {"tokens": tokens, "cost": cost, "buy_price": buy_price}


# ---- BOOK_SELL ----

# Profitable book -> allowed
lots = [lot(100, 100, 1.0), lot(50, 75, 1.5)]
r = book_sell_allowed(lots, 2.0, fee=0.015, min_profit_pct=3.0)
check("book_sell_profitable_allowed", r["allowed"], True)
check_close("book_sell_profitable_pnl", r["pnl_pct"], (150 * 2.0 * 0.985 / 175 - 1) * 100)

# Unprofitable book -> blocked
r = book_sell_allowed(lots, 1.2, fee=0.015, min_profit_pct=3.0)
check("book_sell_unprofitable_blocked", r["allowed"], False)

# Exactly at threshold
lots2 = [lot(100, 100, 1.0)]
# Need price where (100 * price * 0.985 / 100 - 1) * 100 = 3.0
# price * 0.985 = 1.03 -> price = 1.03 / 0.985 = 1.04568...
threshold_price = 1.03 / 0.985
r = book_sell_allowed(lots2, threshold_price, fee=0.015, min_profit_pct=3.0)
check("book_sell_at_threshold", r["allowed"], True)

r = book_sell_allowed(lots2, threshold_price - 0.001, fee=0.015, min_profit_pct=3.0)
check("book_sell_below_threshold", r["allowed"], False)

# Empty book -> blocked
r = book_sell_allowed([], 1.0)
check("book_sell_empty", r["allowed"], False)

# Zero price -> blocked
r = book_sell_allowed(lots, 0.0)
check("book_sell_zero_price", r["allowed"], False)

# Zero fee
r = book_sell_allowed([lot(100, 100, 1.0)], 1.05, fee=0.0, min_profit_pct=3.0)
check("book_sell_no_fee_allowed", r["allowed"], True)
check_close("book_sell_no_fee_pnl", r["pnl_pct"], 5.0)


# ---- LIFETIME RESET ----

# Profitable lifetime -> allowed to reset
r = lifetime_reset_allowed(
    [lot(100, 200, 2.0)],  # book cost = 200, book underperforming
    price=1.5,             # book value = 147.75 (net), loss = -52.25
    realized_pnl=100.0,    # lifetime realized = +100
    fee=0.015,
)
check("reset_profitable_lifetime", r["allowed"], True)
check_close("reset_lifetime_net", r["lifetime_net"], 100.0 + (100 * 1.5 * 0.985 - 200))

# Negative lifetime -> blocked
r = lifetime_reset_allowed(
    [lot(100, 200, 2.0)],
    price=1.0,             # book value = 98.5, loss = -101.5
    realized_pnl=50.0,     # not enough to cover
    fee=0.015,
)
check("reset_negative_lifetime", r["allowed"], False)
check("reset_negative_net", r["lifetime_net"] < 0, True)

# Exactly break even -> not allowed (> 0 required, not >=)
r = lifetime_reset_allowed(
    [lot(100, 100, 1.0)],
    price=0.0,
    realized_pnl=100.0,
)
check("reset_zero_price", r["allowed"], False)

# Empty book -> blocked
r = lifetime_reset_allowed([], 1.0, realized_pnl=100.0)
check("reset_empty_book", r["allowed"], False)

# Book at profit -> allowed (trivially, since loss is negative)
r = lifetime_reset_allowed(
    [lot(100, 100, 1.0)],
    price=2.0,
    realized_pnl=0.0,
    fee=0.015,
)
check("reset_book_profitable", r["allowed"], True)
check_close("reset_book_profitable_net", r["lifetime_net"], 100 * 2.0 * 0.985 - 100)

# Zero realized PnL, book at loss -> blocked
r = lifetime_reset_allowed(
    [lot(100, 200, 2.0)],
    price=1.0,
    realized_pnl=0.0,
    fee=0.015,
)
check("reset_zero_realized_loss", r["allowed"], False)

# Large realized PnL absorbs the loss
r = lifetime_reset_allowed(
    [lot(1000, 10000, 10.0)],
    price=5.0,              # book value = 4925, loss = -5075
    realized_pnl=6000.0,
    fee=0.015,
)
check("reset_large_realized_covers", r["allowed"], True)
check_close("reset_large_realized_net", r["lifetime_net"], 6000 + (1000 * 5.0 * 0.985 - 10000))


print()
if FAILURES:
    print(f"\n{'='*60}")
    for f in FAILURES:
        print(f)
    print(f"\nFAILED: {len(FAILURES)} test(s)")
    sys.exit(1)
else:
    print("All manual sell tests passed.")
