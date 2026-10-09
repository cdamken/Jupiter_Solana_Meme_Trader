"""test_lot_rotation.py -- Weekly lot rotation calculator tests.

Tests the pure functions (realized_usd, time_weighted_deployed, round10,
bounded_step) and the fleet analysis with a synthetic store. Adapted from
SIMD's test_weekly_lot_rotation.py for Jupiter's SQLite store.
Self-contained, stdlib only. Run: python3 tests/test_lot_rotation.py
"""
import sys
import os
import datetime
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

import config  # noqa
from store.store import Store
from lot_rotation import (
    round10, bounded_step, realized_usd, time_weighted_deployed,
    analyze_fleet, LOT_MIN, LOT_MAX, LOT_PROBATION, COOL_LOT,
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

def approx(a, b, tol=0.5):
    return abs(a - b) <= tol


NOW = datetime.datetime(2026, 10, 5, 8, 0)

def days_ago(n, h=12):
    return NOW - datetime.timedelta(days=n) + datetime.timedelta(hours=h - 12)


print("=== test_lot_rotation.py ===")

# ---- Pure unit tests ----
print("\n[round10]")
check("35 -> 40 (ties up)", round10(35), 40)
check("25 -> 30 (ties up)", round10(25), 30)
check("34 -> 30", round10(34), 30)
check("44 -> 40", round10(44), 40)
check("45 -> 50", round10(45), 50)

print("\n[bounded_step]")
check("up: 40->60 target => 50", bounded_step(40, 60), 50)
check("down: 40->20 target => 30", bounded_step(40, 20), 30)
check("None current -> target", bounded_step(None, 30), 30)
check("off-grid snap down: 25->10 => 20", bounded_step(25, 10), 20)
check("off-grid snap up: 25->40 => 30", bounded_step(25, 40), 30)
check("same: 40->40 => 40", bounded_step(40, 40), 40)

print("\n[realized_usd]")
trades_r = [
    {"ts_dt": days_ago(20), "side": "buy", "usd": 100.0, "pnl_pct": 0.0, "mode": "live"},
    {"ts_dt": days_ago(10), "side": "sell", "usd": 55.0, "pnl_pct": 10.0, "mode": "live"},
]
realized = realized_usd(trades_r, days_ago(30), NOW)
check_true("realized = sell proceeds - cost basis", approx(realized, 5.0, tol=0.01))

print("\n[time_weighted_deployed]")
twd = time_weighted_deployed(trades_r, days_ago(30), NOW)
check_true("time-weighted deployed", approx(twd, 50.0, tol=0.5))

# ---- Store integration: analyze_fleet ----
print("\n[analyze_fleet]")

with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    s.usdc_deposit(1000.0)

    # FAST coin: rotates hard, 35d of live history
    fast_id = s.add_coin("fast", "FAST", "fastMint1111111111111111111111111111111111111")
    s.set_coin_status(fast_id, "live")

    # Add some trades for FAST
    for i in range(3):
        buy_ts = days_ago(35 - i * 7)
        sell_ts = days_ago(32 - i * 7)
        s._c.execute(
            "INSERT INTO trades (coin_id, ts, mode, side, signal, price, tokens, usd, pnl_pct, txsig)"
            " VALUES (?, ?, 'live', 'buy', 'grid', 0.001, 20000, 20.0, null, ?)",
            (fast_id, buy_ts.strftime("%Y-%m-%d %H:%M:%S"), f"buy-{i}"))
        s._c.execute(
            "INSERT INTO trades (coin_id, ts, mode, side, signal, price, tokens, usd, pnl_pct, txsig)"
            " VALUES (?, ?, 'live', 'sell', 'grid', 0.0011, 20000, 22.0, 10.0, ?)",
            (fast_id, sell_ts.strftime("%Y-%m-%d %H:%M:%S"), f"sell-{i}"))
    s._c.commit()

    # SLOW coin: holds inventory, minimal sells, 35d
    slow_id = s.add_coin("slow", "SLOW", "slowMint1111111111111111111111111111111111111")
    s.set_coin_status(slow_id, "live")
    s._c.execute(
        "INSERT INTO trades (coin_id, ts, mode, side, signal, price, tokens, usd, pnl_pct, txsig)"
        " VALUES (?, ?, 'live', 'buy', 'grid', 0.001, 200000, 200.0, null, 'slowbuy')",
        (slow_id, days_ago(35).strftime("%Y-%m-%d %H:%M:%S")))
    s._c.execute(
        "INSERT INTO trades (coin_id, ts, mode, side, signal, price, tokens, usd, pnl_pct, txsig)"
        " VALUES (?, ?, 'live', 'sell', 'grid', 0.0011, 10000, 11.0, 4.0, 'slowsell')",
        (slow_id, days_ago(3).strftime("%Y-%m-%d %H:%M:%S")))
    s._c.commit()

    # NEWBIE coin: only 10d of history -> probation
    newbie_id = s.add_coin("newbie", "NEWBIE", "newbieMint11111111111111111111111111111111")
    s.set_coin_status(newbie_id, "paper")
    s._c.execute(
        "INSERT INTO trades (coin_id, ts, mode, side, signal, price, tokens, usd, pnl_pct, txsig)"
        " VALUES (?, ?, 'live', 'buy', 'grid', 0.001, 50000, 50.0, null, 'newbuy')",
        (newbie_id, days_ago(10).strftime("%Y-%m-%d %H:%M:%S")))
    s._c.commit()

    rows, fleet_median = analyze_fleet(s, days=30, now=NOW)
    check("3 coins analyzed", len(rows), 3)

    fast_row = next(r for r in rows if r["slug"] == "fast")
    slow_row = next(r for r in rows if r["slug"] == "slow")
    newbie_row = next(r for r in rows if r["slug"] == "newbie")

    check_true("fast R > slow R", fast_row["r"] > slow_row["r"])
    check_true("fleet median > 0", fleet_median > 0)

    check("newbie -> probation", newbie_row["target"], LOT_PROBATION)
    check_true("probation in why", "probation" in newbie_row["why"])

    check_true("fast target >= LOT_MIN", fast_row["target"] >= LOT_MIN)
    check_true("fast target <= LOT_MAX", fast_row["target"] <= LOT_MAX)

    # Liquidity gate test
    rows_capped, _ = analyze_fleet(s, days=30, now=NOW, caps={"fast": 30})
    fast_capped = next(r for r in rows_capped if r["slug"] == "fast")
    check_true("liquidity gate caps the lot", fast_capped["proposed"] <= 30)
    check_true("gate visible in why", "liquidity gate" in fast_capped.get("why", ""))

    # Degenerate fleet: no live sells at all -> HOLD
    db2 = os.path.join(d, "test2.db")
    s2 = Store(db2)
    s2.usdc_deposit(500.0)
    dead_id = s2.add_coin("dead", "DEAD", "deadMint1111111111111111111111111111111111111")
    s2.set_coin_status(dead_id, "live")
    s2._c.execute(
        "INSERT INTO trades (coin_id, ts, mode, side, signal, price, tokens, usd, pnl_pct, txsig)"
        " VALUES (?, ?, 'live', 'buy', 'grid', 0.001, 100000, 100.0, null, 'deadbuy')",
        (dead_id, days_ago(35).strftime("%Y-%m-%d %H:%M:%S")))
    s2._c.commit()
    rows_deg, med_deg = analyze_fleet(s2, days=30, now=NOW)
    check("degenerate fleet median = 0", med_deg, 0.0)
    check_true("degenerate -> HOLDING", "HOLDING" in rows_deg[0]["why"])


print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILURES: {len(FAILURES)}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
