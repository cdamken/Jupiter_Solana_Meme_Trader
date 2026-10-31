"""test_scheduler_integration.py -- Full tick loop integration test (#36).

Exercises the FULL scheduler loop: price fetch -> grid decide -> execute
(paper) -> DB write -> next tick, across a 20-price sequence designed to
trigger buys on dips, sells on rises, and holds in between.

Run: python3 tests/test_scheduler_integration.py
"""
import os
import sys
import tempfile
import time
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("RPC_URL", "http://localhost:8899")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

import config  # noqa -- env must be set before import
from store.store import Store
import scheduler

FAILURES = []


def chk(name, cond):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f" FAIL {name}")
        FAILURES.append(name)


def check(name, actual, expected):
    if actual != expected:
        FAILURES.append(f"FAIL {name}: got {actual!r}, expected {expected!r}")
        print(f" FAIL {name}: got {actual!r}, expected {expected!r}")
    else:
        print(f"  ok  {name}")


print("=== test_scheduler_integration.py ===")

# -- Price sequence (20 ticks) --
# Designed to trigger buys on dips (>= 4% step down), sells on rises
# (>= 4% above lot buy_price), and holds in between.
#
# Default grid: BUY_STEP_PCT=4%, SELL_STEP_PCT=4%, LOT_USD=$25
# Paper sell fee ~ 1.5% (paper_sell uses 0.985 multiplier).
#
# Tick  Price  Expected
#  0    1.000  buy (first tick, empty book, ref=None -> buy at market)
#  1    0.980  hold (2% dip from 1.00, < 4% step)
#  2    0.950  buy (5% below ref 1.00)
#  3    0.940  hold (~1% below new ref 0.95)
#  4    0.900  buy (>5% below ref 0.95)
#  5    0.880  hold (~2% below 0.90)
#  6    0.850  buy (>5% below 0.90)
#  7    0.870  hold (recovering but no lot sells yet)
#  8    0.920  hold (lot@0.90 up only ~2.2%)
#  9    0.960  sell? (lot@0.90 is +6.7%, lot@0.95 is +1.1%)
# 10    1.000  sell (lot@0.95 is +5.3%)
# 11    1.020  hold (post-sell ref shift)
# 12    1.050  sell (lot@1.00 is +5%)
# 13    1.000  hold (ref moved down)
# 14    0.950  buy (drop from high)
# 15    0.900  buy (>5% below 0.95)
# 16    0.920  hold
# 17    0.960  hold
# 18    1.000  sell (lot@0.95)
# 19    1.050  sell (lot@1.00 or lot@0.90)

PRICES = [
    1.000, 0.980, 0.950, 0.940, 0.900,
    0.880, 0.850, 0.870, 0.920, 0.960,
    1.000, 1.020, 1.050, 1.000, 0.950,
    0.900, 0.920, 0.960, 1.000, 1.050,
]

MINT = "IntgTestMint" + "1" * 32  # 44-char fake mint

# -- Minimal config keys needed for grid trading --
CONFIG_KEYS = {
    "BUY_STEP_PCT":     ("editable", "coin-allowed", "float", "4.0"),
    "SELL_STEP_PCT":    ("editable", "coin-allowed", "float", "4.0"),
    "LOT_USD":          ("editable", "coin-allowed", "float", "25.0"),
    "MAX_CAPITAL_USD":  ("editable", "coin-allowed", "float", "500.0"),
    "MAX_SLIPPAGE_BPS": ("editable", "coin-allowed", "float", "150"),
    "PRICE_GATE_PCT":   ("editable", "coin-allowed", "float", "30.0"),
    "MAX_CAPITAL_PCT":  ("editable", "fleet-only",   "float", "0"),
    "CEILING_PERCENTILE": ("editable", "coin-allowed", "float", "98.0"),
    "SELL_TRAIL":       ("editable", "coin-allowed", "bool",  "0"),
    "SELL_TRAIL_PCT":   ("editable", "coin-allowed", "float", "2.0"),
    "BUY_FLOOR":        ("editable", "coin-allowed", "bool",  "0"),
    "BUY_FLOOR_USD":    ("editable", "coin-allowed", "float", "0"),
    "FLOOR_RELATIVE":   ("editable", "coin-allowed", "bool",  "0"),
    "PAIRING_GRID":     ("editable", "coin-allowed", "bool",  "0"),
    "TOPUP_INSURANCE":  ("editable", "coin-allowed", "bool",  "0"),
    "VOL_ADAPTIVE":     ("editable", "coin-allowed", "bool",  "0"),
    "PREBUY_GRADUATE":  ("editable", "coin-allowed", "bool",  "0"),
    "BATCH_SELL":       ("editable", "coin-allowed", "bool",  "0"),
    "GAS_REFILL":       ("editable", "coin-allowed", "bool",  "0"),
    "REBUY_GAP_PCT":    ("editable", "coin-allowed", "float", "0"),
    "SELL_POLICY":      ("editable", "coin-allowed", "text",  "fifo"),
}


def seed_config(store):
    """Seed param_catalog + fleet_defaults so get_config() returns our values."""
    c = store._c
    for key, (tier, scope, typ, default) in CONFIG_KEYS.items():
        c.execute(
            "INSERT OR IGNORE INTO param_catalog (key, tier, scope, type, default_val)"
            " VALUES (?,?,?,?,?)",
            (key, tier, scope, typ, default),
        )
    c.execute(
        "INSERT INTO fleet_versions (author, reason, pinned) VALUES (?,?,1)",
        ("test", "integration test seed"),
    )
    ver_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    for key, (_, _, _, default) in CONFIG_KEYS.items():
        c.execute(
            "INSERT INTO fleet_defaults (version_id, key, value) VALUES (?,?,?)",
            (ver_id, key, default),
        )
    c.commit()


with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "integ.db")
    store = Store(db)
    seed_config(store)

    coin_id = store.add_coin("intg", "INTG", MINT, status="paper")
    store.usdc_deposit(500.0)

    coin = {"id": coin_id, "slug": "intg", "label": "INTG",
            "mint": MINT, "status": "paper", "decimals": 6}

    initial_balance = store.usdc_balance()
    check("initial balance", initial_balance, 500.0)

    # ---- Run all 20 ticks ----
    # Mock fetch_price at scheduler module level (where tick_coin binds it)
    # and suppress alert emails.
    print("\n[running 20 ticks]")
    tick_errors = 0
    balances = [initial_balance]
    lot_counts = [0]

    for i, price in enumerate(PRICES):
        with unittest.mock.patch.object(scheduler, "fetch_price", return_value=price):
            with unittest.mock.patch.object(scheduler, "_alerts"):
                try:
                    scheduler.tick_coin(store, dict(coin), force_mode="paper")
                except Exception as e:
                    tick_errors += 1
                    print(f"  tick {i} (price={price}) raised: {e}")
                    import traceback
                    traceback.print_exc()
        bal = store.usdc_balance()
        balances.append(bal)
        lots_now = store.get_lots(coin_id)
        lot_counts.append(len(lots_now))

    chk("all 20 ticks completed without exception", tick_errors == 0)

    # ---- Assertions ----
    print("\n[decision ledger]")

    decisions = store.get_decisions(coin_id, limit=100)
    check("decision count equals number of ticks", len(decisions), 20)

    # Count actions
    actions = {}
    for dec in decisions:
        a = dec["action"]
        actions[a] = actions.get(a, 0) + 1
    print(f"       actions: {actions}")

    chk("at least one buy decision", actions.get("buy", 0) + actions.get("buy+sell", 0) >= 1)
    chk("at least one sell decision", actions.get("sell", 0) + actions.get("buy+sell", 0) >= 1)
    chk("at least one hold decision", actions.get("hold", 0) >= 1)

    print("\n[balance invariants]")

    # Balance never went negative at any tick
    neg_ticks = [i for i, b in enumerate(balances) if b < -0.001]
    chk("balance never negative", len(neg_ticks) == 0)
    if neg_ticks:
        for t in neg_ticks:
            print(f"       tick {t}: balance={balances[t]:.4f}")

    final_balance = store.usdc_balance()
    print(f"       balance: {initial_balance:.2f} -> {final_balance:.2f}")

    print("\n[lot book]")

    lots = store.get_lots(coin_id)
    n_lots = len(lots)
    print(f"       lots remaining: {n_lots}")

    # Lot count = buy trades - sell trades
    trades = store.get_trades(coin_id)
    buy_trades = [t for t in trades if t["side"] == "buy"]
    sell_trades = [t for t in trades if t["side"] == "sell"]
    expected_lots = len(buy_trades) - len(sell_trades)
    print(f"       buy trades={len(buy_trades)}, sell trades={len(sell_trades)}")
    check("lot count matches buy minus sell trades", n_lots, expected_lots)

    # No lot has zero/negative tokens or cost
    bad_lots = [l for l in lots if float(l["tokens"]) <= 0 or float(l["cost"]) <= 0]
    chk("no lots with zero/negative tokens or cost", len(bad_lots) == 0)

    print("\n[capital consistency]")

    # USDC balance + lot cost total should be close to initial deposit
    # (sells return proceeds which may include profit minus fee)
    lot_cost_total = sum(float(l["cost"]) for l in lots)
    sell_proceeds = sum(float(t["usd"]) for t in sell_trades)
    buy_spent = sum(float(t["usd"]) for t in buy_trades)
    total_accounted = final_balance + lot_cost_total
    print(f"       balance ({final_balance:.2f}) + lot cost ({lot_cost_total:.2f}) = {total_accounted:.2f}")
    print(f"       buy spent={buy_spent:.2f}, sell proceeds={sell_proceeds:.2f}")

    chk("accounted capital is positive", total_accounted > 0)
    # Allow tolerance: paper sells have ~1.5% fee, so total may be slightly above
    # or below initial deposit depending on the P&L
    chk("accounted capital within 20% of initial deposit",
        abs(total_accounted - initial_balance) < initial_balance * 0.20)

    # Capital row -- deployed_usd accumulates buys (not reduced by sells),
    # so it equals the total USD spent on buys.
    cap = store.get_capital(coin_id)
    if cap:
        deployed = cap["deployed_usd"]
        chk("deployed capital >= 0", deployed >= 0)
        chk("deployed capital equals total buy spend",
            abs(deployed - buy_spent) < 0.02)
        print(f"       deployed_usd: {deployed:.2f}")
    else:
        chk("capital row exists after buys", cap is not None)

    print("\n[price history]")

    price_rows = store.price_window(coin_id, 0)
    chk("price history recorded for each accepted tick", len(price_rows) >= 15)
    print(f"       price history rows: {len(price_rows)}")

    # ---- Summary of tick-by-tick activity ----
    print("\n[tick-by-tick summary]")
    decisions_asc = list(reversed(decisions))
    for i, (price, dec) in enumerate(zip(PRICES, decisions_asc)):
        action = dec["action"]
        reason = dec["reason"]
        bal = balances[i + 1]
        lots_n = lot_counts[i + 1]
        print(f"  tick {i:2d}: price={price:.3f}  {action:9s} ({reason:12s})  "
              f"lots={lots_n}  balance={bal:.2f}")


print()
if FAILURES:
    print(f"FAILURES: {len(FAILURES)}")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
