"""test_prebuy.py -- Tests for prebuy (quiet regime, decision, graduation).

Self-contained, stdlib only. Run: python3 tests/test_prebuy.py
"""
import sys
import os
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine import (
    quiet_regime, prebuy_decision, prebuy_initial_state, graduate_prebuys,
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


print("=== test_prebuy.py ===")

# --- quiet_regime ---
print("\n[quiet_regime]")

now = 100000.0
window_h = 12.0
since = now - window_h * 3600

# flat market: all prices in a 2% band over 12h window
flat_prices = [(since + i * 60, 1.0 + 0.001 * (i % 10)) for i in range(800)]
check_true("flat market within 2% band", quiet_regime(flat_prices, now, 2.0, window_h))

# volatile market: prices swing wildly
volatile_prices = [(since + i * 60, 1.0 if i % 2 == 0 else 1.5) for i in range(800)]
check_false("volatile market > 2% band", quiet_regime(volatile_prices, now, 2.0, window_h))

# too few points
check_false("too few points", quiet_regime([(now - 100, 1.0), (now - 50, 1.01)], now, 5.0, window_h))

# points don't span half the window
recent = [(now - 100 + i, 1.0) for i in range(10)]
check_false("points don't span half window", quiet_regime(recent, now, 5.0, window_h))

# empty/None
check_false("empty hist", quiet_regime([], now, 5.0, window_h))
check_false("None hist", quiet_regime(None, now, 5.0, window_h))

# price=0 filtered out
mixed = [(since + i * 60, 1.0 if i > 2 else 0.0) for i in range(800)]
check_true("zero prices filtered", quiet_regime(mixed, now, 2.0, window_h))

# exactly at band edge: 3.9% band fits in 4% threshold
edge_prices = [(since + i * 60, 1.0 + 0.039 * (i / 799)) for i in range(800)]
check_true("at band edge 3.9% in 4%", quiet_regime(edge_prices, now, 4.0, window_h))

# just over band: prices range from 1.0 to 1.05 (5% band, asking for 4%)
over_prices = [(since + i * 60, 1.0 + 0.05 * (i / 799)) for i in range(800)]
check_false("over band 5% vs 4% threshold", quiet_regime(over_prices, now, 4.0, window_h))


# --- prebuy_decision ---
print("\n[prebuy_decision]")

cfg_on = {
    "PREBUY": True,
    "PREBUY_BAND_PCT": 4,
    "PREBUY_WINDOW_H": 12,
    "PREBUY_MAX_N": 2,
    "PREBUY_COOLDOWN_H": 24,
    "PREBUY_CEIL_TOL": 1.10,
    "LOT_USD": 25,
}

# need a flat price history for prebuy to fire
window_s = cfg_on["PREBUY_WINDOW_H"] * 3600
hist = [(now - window_s + i * 60, 1.0 + 0.001 * (i % 5))
        for i in range(int(window_s / 60) + 1)]

# off
cfg_off = dict(cfg_on, PREBUY=False)
d = prebuy_decision(1.0, now, hist, [], 100, None, None, False, cfg_off)
check("off: reason", d["reason"], "off")

# bad price
d = prebuy_decision(0.0, now, hist, [], 100, None, None, False, cfg_on)
check("bad price: reason", d["reason"], "bad_price")

# in floor zone
d = prebuy_decision(1.0, now, hist, [], 100, None, None, True, cfg_on)
check("floor zone: reason", d["reason"], "floor_zone")

# flat market, should buy
d = prebuy_decision(1.0, now, hist, [], 100, None, None, False, cfg_on)
check("flat: buy", d["reason"], "buy")
check("flat: buy_usd", d["buy_usd"], 25)
check("flat: lots=1", d["new_st_pre"]["lots"], 1)

# episode fills after N buys
st1 = {"lots": 1, "cooldown_until": 0.0}
d = prebuy_decision(1.0, now, hist, [], 100, st1, None, False, cfg_on)
check("2nd buy: reason", d["reason"], "buy")
check("2nd buy: lots=2", d["new_st_pre"]["lots"], 2)
check_true("2nd buy: cooldown set", d["new_st_pre"]["cooldown_until"] > now)

# in cooldown (2/2, cooldown in future) -> "cooldown"
st_full = {"lots": 2, "cooldown_until": now + 86400}
d = prebuy_decision(1.0, now, hist, [], 100, st_full, None, False, cfg_on)
check("in cooldown: reason", d["reason"], "cooldown")

# cooldown elapsed -> episode resets
st_expired = {"lots": 2, "cooldown_until": now - 1}
d = prebuy_decision(1.0, now, hist, [], 100, st_expired, None, False, cfg_on)
check("cooldown elapsed: buy", d["reason"], "buy")
check("cooldown elapsed: lots reset to 1", d["new_st_pre"]["lots"], 1)

# not enough cash
d = prebuy_decision(1.0, now, hist, [], 10, None, None, False, cfg_on)
check("no cash: reason", d["reason"], "no_cash")

# above ceiling tolerance
d = prebuy_decision(1.2, now, hist, [], 100, None, 1.0, False, cfg_on)
check("above ceiling: reason", d["reason"], "above_ceiling")

# below ceiling tolerance (price=1.05, ceiling=1.0, tol=1.10 -> 1.0*1.10=1.10, ok)
d = prebuy_decision(1.05, now, hist, [], 100, None, 1.0, False, cfg_on)
check("below ceiling tol: buy", d["reason"], "buy")

# not flat market
volatile = [(now - window_s + i * 60, 1.0 + 0.5 * (i % 2))
            for i in range(int(window_s / 60) + 1)]
d = prebuy_decision(1.0, now, volatile, [], 100, None, None, False, cfg_on)
check("not flat: reason", d["reason"], "not_flat")


# --- graduate_prebuys ---
print("\n[graduate_prebuys]")

lots = [
    {"id": 1, "origin": "prebuy", "buy_price": 1.0},
    {"id": 2, "origin": "prebuy", "buy_price": 1.05},
    {"id": 3, "origin": "grid",   "buy_price": 1.0},
    {"id": 4, "origin": "prebuy", "buy_price": 1.0},
]

# price up 6%: lot 1 and 4 graduate (buy_price=1.0, 6% up = 1.06)
# lot 2 (buy_price=1.05): 1.07/1.05 - 1 = 1.9%, not enough
ids = graduate_prebuys(lots, 1.07, 6.0, 10.0)
check("graduate up: ids", sorted(ids), [1, 4])

# price down 10%: lot 1 and 4 graduate (1.0 * 0.9 = 0.9)
# lot 2 (buy_price=1.05): (1.05-0.89)/1.05 = 15.2%, also graduates
ids = graduate_prebuys(lots, 0.89, 6.0, 10.0)
check("graduate down: ids", sorted(ids), [1, 2, 4])

# lot 2 (buy_price=1.05): price=1.12 -> 6.7% up, graduates
ids = graduate_prebuys(lots, 1.12, 6.0, 10.0)
check("lot2 graduates up", 2 in ids, True)

# no graduation: price within band for all lots
# lot1,4 bp=1.0: 1.03/1.0-1=3%, need 6% up or 10% down
# lot2 bp=1.05: 1.03/1.05-1=-1.9%, need 6% up or 10% down
ids = graduate_prebuys(lots, 1.03, 6.0, 10.0)
check("no graduation in band", ids, [])

# grid lots ignored
ids = graduate_prebuys([{"id": 3, "origin": "grid", "buy_price": 1.0}], 1.07, 6.0, 10.0)
check("grid lots ignored", ids, [])

# bad price
check("bad price: empty", graduate_prebuys(lots, 0, 6.0, 10.0), [])
check("None price: empty", graduate_prebuys(lots, None, 6.0, 10.0), [])

# bad thresholds
check("zero up_pct: empty", graduate_prebuys(lots, 1.07, 0, 10.0), [])
check("zero down_pct: empty", graduate_prebuys(lots, 0.89, 6.0, 0), [])


# --- store integration: price_window_rows + update_lot_origin ---
print("\n[store integration]")

os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
import config  # noqa
from store.store import Store

with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    coin_id = s.add_coin("test", "TEST", "TestMint" + "1" * 33)

    # record some prices
    base_ts = time.time() - 50000
    for i in range(100):
        s.record_price(coin_id, base_ts + i * 60, 1.0 + 0.001 * i)

    # price_window_rows
    rows = s.price_window_rows(coin_id, base_ts)
    check("price_window_rows count", len(rows), 100)
    check_true("rows have ts", "ts" in rows[0].keys())
    check_true("rows have price", "price" in rows[0].keys())

    # update_lot_origin
    s.usdc_deposit(100.0)
    lot_id = s.add_lot(coin_id, 50.0, 25.0, 0.5, time.time(), "prebuy")
    lots = s.get_lots(coin_id)
    check("lot origin before", dict(lots[0])["origin"], "prebuy")

    s.update_lot_origin(lot_id, "grid")
    lots = s.get_lots(coin_id)
    check("lot origin after graduation", dict(lots[0])["origin"], "grid")


print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILURES: {len(FAILURES)}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
