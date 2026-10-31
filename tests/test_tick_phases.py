"""test_tick_phases.py -- Tests for tick.py extracted phase functions (#35).

Verifies that the refactored tick phases produce the same behavior
as the original monolithic tick_coin.

Run: python3 tests/test_tick_phases.py
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

import config  # noqa
from store.store import Store
from tick import (
    audit_live_config, _cfg_float, _cfg_bool,
    phase_price, phase_buy_modifiers, phase_sells,
    phase_buy, phase_topup, log_tick_decision,
    MONEY_CRITICAL_KEYS,
)

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


print("=== test_tick_phases.py ===")


# ---- _cfg_float / _cfg_bool ----
print("\n[_cfg_float]")
check("float default", _cfg_float({}, "K", 5.0), 5.0)
check("float from cfg", _cfg_float({"K": "3.5"}, "K", 5.0), 3.5)
check("float bad value", _cfg_float({"K": "abc"}, "K", 5.0), 5.0)
check("float None", _cfg_float({"K": None}, "K", 5.0), 5.0)

print("\n[_cfg_bool]")
check("bool default false", _cfg_bool({}, "K", False), False)
check("bool default true", _cfg_bool({}, "K", True), True)
check("bool 1", _cfg_bool({"K": "1"}, "K", False), True)
check("bool true", _cfg_bool({"K": "true"}, "K", False), True)
check("bool yes", _cfg_bool({"K": "YES"}, "K", False), True)
check("bool 0", _cfg_bool({"K": "0"}, "K", True), False)
check("bool empty string uses default", _cfg_bool({"K": ""}, "K", True), True)


# ---- audit_live_config ----
print("\n[audit_live_config]")
good_cfg = {"MAX_CAPITAL_USD": "200", "LOT_USD": "25", "MAX_SLIPPAGE_BPS": "150"}
check("good config passes", audit_live_config(good_cfg, "test"), [])

bad_cfg = {"LOT_USD": "25", "MAX_SLIPPAGE_BPS": "150"}
bad = audit_live_config(bad_cfg, "test")
chk("missing key detected", "MAX_CAPITAL_USD" in bad)

zero_cfg = {"MAX_CAPITAL_USD": "0", "LOT_USD": "25", "MAX_SLIPPAGE_BPS": "150"}
bad2 = audit_live_config(zero_cfg, "test")
chk("zero value detected", "MAX_CAPITAL_USD" in bad2)


# ---- phase_price ----
print("\n[phase_price]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("test", "TEST", "TestMint" + "1" * 33)

    cfg = {"PRICE_GATE_PCT": "30.0"}
    coin = {"id": cid, "slug": "test", "mint": "TestMint" + "1" * 33}

    with unittest.mock.patch("tick.fetch_price", return_value=None):
        result = phase_price(s, cid, coin, cfg, time.time())
        check("no price returns None", result, None)

    with unittest.mock.patch("tick.fetch_price", return_value=1.0):
        result = phase_price(s, cid, coin, cfg, time.time())
        check("first price accepted", result, 1.0)

    with unittest.mock.patch("tick.fetch_price", return_value=1.05):
        result = phase_price(s, cid, coin, cfg, time.time())
        check("small step accepted", result, 1.05)

    with unittest.mock.patch("tick.fetch_price", return_value=100.0):
        result = phase_price(s, cid, coin, cfg, time.time())
        check("huge step rejected", result, None)


# ---- phase_sells (paper mode, serial) ----
print("\n[phase_sells]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("test", "TEST", "TestMint" + "1" * 33)
    s.usdc_deposit(200.0)

    lot_id = s.add_lot(cid, 50.0, 25.0, 0.50, time.time(), "grid")
    lot = dict(s.get_lots(cid)[0])

    decision = {
        "sells": [lot],
        "trail_updates": [],
    }
    coin = {"id": cid, "slug": "test", "mint": "TestMint" + "1" * 33, "decimals": 6}
    cfg = {"MAX_SLIPPAGE_BPS": "150"}

    with unittest.mock.patch("tick._alerts"):
        phase_sells(s, cid, coin, cfg, decision, 0.60, time.time(), "paper")

    remaining = s.get_lots(cid)
    check("lot sold in paper mode", len(remaining), 0)


# ---- phase_buy (paper mode) ----
print("\n[phase_buy]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("test", "TEST", "TestMint" + "1" * 33)
    s.usdc_deposit(200.0)

    decision = {"buy_usd": 25.0, "new_ref": 1.0, "sells": [], "trail_updates": []}
    coin = {"id": cid, "slug": "test", "mint": "TestMint" + "1" * 33, "decimals": 6}
    cfg = {"MAX_SLIPPAGE_BPS": "150"}

    with unittest.mock.patch("tick._alerts"):
        bought = phase_buy(s, cid, coin, cfg, decision, 1.0, time.time(), "paper",
                           0.0, 200.0, None,
                           cad_buy=False, prebuy_fired=False, in_floor_zone=False,
                           new_ep=None, floor_zone_val=None, pre_d=None)

    chk("buy returned True", bought)
    lots = s.get_lots(cid)
    check("lot created after buy", len(lots), 1)
    check("balance decreased", s.usdc_balance(), 175.0)


# ---- phase_buy skips when buy_usd=0 ----
print("\n[phase_buy skip]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("test", "TEST", "TestMint" + "1" * 33)
    s.usdc_deposit(200.0)

    decision = {"buy_usd": 0.0, "new_ref": 1.0, "sells": [], "trail_updates": []}
    coin = {"id": cid, "slug": "test", "mint": "TestMint" + "1" * 33, "decimals": 6}
    cfg = {"MAX_SLIPPAGE_BPS": "150"}

    bought = phase_buy(s, cid, coin, cfg, decision, 1.0, time.time(), "paper",
                       0.0, 200.0, None,
                       cad_buy=False, prebuy_fired=False, in_floor_zone=False,
                       new_ep=None, floor_zone_val=None, pre_d=None)

    check("no buy returns False", bought, False)
    check("balance unchanged", s.usdc_balance(), 200.0)


# ---- log_tick_decision ----
print("\n[log_tick_decision]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("test", "TEST", "TestMint" + "1" * 33)

    decision = {"buy_usd": 25.0, "new_ref": 1.0, "sells": [], "trail_updates": []}
    log_tick_decision(s, cid, time.time(), 1.0, decision, "paper", {})
    decs = s.get_decisions(cid, limit=1)
    check("decision logged", len(decs), 1)
    check("action is buy", decs[0]["action"], "buy")

    decision2 = {"buy_usd": 0.0, "sells": [], "trail_updates": []}
    log_tick_decision(s, cid, time.time(), 1.0, decision2, "paper", {})
    decs2 = s.get_decisions(cid, limit=2)
    check("hold decision logged", decs2[0]["action"], "hold")


# ---- Full tick_coin still works (regression) ----
print("\n[tick_coin e2e regression]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("tc", "TC", "TCMint" + "1" * 34)
    s.usdc_deposit(500.0)
    coin = dict(s.get_coin("tc"))
    s.set_coin_status(cid, "paper")
    coin["status"] = "paper"

    from tick import tick_coin
    with unittest.mock.patch("tick.fetch_price", return_value=1.0):
        with unittest.mock.patch("tick._alerts"):
            try:
                tick_coin(s, coin, force_mode="paper")
                chk("tick_coin completes without exception", True)
            except Exception as e:
                chk(f"tick_coin threw: {e}", False)


print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILED: {len(FAILURES)} test(s)")
    sys.exit(1)
else:
    print("All tick_phases tests passed.")
