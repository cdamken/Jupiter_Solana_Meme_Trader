"""test_execute.py — Tests for execute.py helpers. Network fully stubbed.

Self-contained, stdlib only. Run: python3 tests/test_execute.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

import config  # noqa
from store.store import Store
from execute import (
    effective_price_usd,
    order_avoids_foreign_signer,
    revalidate_effective_sale,
    paper_buy,
    paper_sell,
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


print("=== test_execute.py ===")

# ---- effective_price_usd ----
print("\n[effective_price_usd]")

# buy: inAmount=25_000_000 USDC (25.0), outAmount=50_000_000 tokens (50.0)
q = {"inAmount": "25000000", "outAmount": "50000000"}
p = effective_price_usd(q, 25_000_000, input_is_usdc=True)
# 25 USDC / 50 tokens = 0.5 USD/token
check("buy: price = 0.5", round(p, 6), 0.5)

# sell: inAmount=50_000_000 tokens, outAmount=26_000_000 USDC (26.0)
q2 = {"inAmount": "50000000", "outAmount": "26000000"}
p2 = effective_price_usd(q2, 50_000_000, input_is_usdc=False)
# 26 USDC / 50 tokens = 0.52
check("sell: price = 0.52", round(p2, 6), 0.52)

# bad quote
check("bad quote: None", effective_price_usd({"inAmount": "0", "outAmount": "100"}, 0, True), None)
check("missing fields: None", effective_price_usd({}, 0, True), None)

# ---- order_avoids_foreign_signer ----
print("\n[order_avoids_foreign_signer]")
MY = "EaaGm5z7ppT76DYxgR6XBhNtVdMBKRsBfuQvADa3TaXo"
check_true("no signers field: safe", order_avoids_foreign_signer({}, MY))
check_true("only my key: safe", order_avoids_foreign_signer({"signers": [MY]}, MY))
check_false("foreign signer: blocked",
            order_avoids_foreign_signer({"signers": [MY, "EVil111111111111111111111111111111111111111"]}, MY))

# ---- revalidate_effective_sale ----
print("\n[revalidate_effective_sale]")
good_lot = {"id": 1, "tokens": 100.0, "cost": 50.0, "buy_price": 0.5}
check_true("gain > fee: allowed", revalidate_effective_sale(good_lot, 0.52))
check_false("at cost: blocked", revalidate_effective_sale(good_lot, 0.50))
check_false("below cost: blocked", revalidate_effective_sale(good_lot, 0.48))
bad_lot  = {"id": 2, "tokens": 0.0, "cost": 50.0, "buy_price": 0.5}
check_false("zero tokens: blocked", revalidate_effective_sale(bad_lot, 0.52))

# ---- paper_buy / paper_sell (hermetic store) ----
print("\n[paper_buy / paper_sell]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    coin_id = s.add_coin("test", "TEST", "TestMint" + "1" * 33)
    s.usdc_deposit(200.0)

    # paper_buy
    txsig = paper_buy(s, coin_id, 25.0, 0.5, time.time())
    check_true("paper_buy returns txsig", txsig.startswith("paper-buy-"))
    check("balance after buy", s.usdc_balance(), 175.0)
    lots = s.get_lots(coin_id)
    check("one lot created", len(lots), 1)
    check("lot tokens = 50", lots[0]["tokens"], 50.0)

    # paper_sell at a gain
    lot = dict(lots[0])
    txsig2 = paper_sell(s, coin_id, lot, 0.55)
    check_true("paper_sell returns txsig", txsig2.startswith("paper-sell-"))
    check("lot removed after sell", len(s.get_lots(coin_id)), 0)
    # proceeds = 50 tokens * 0.55 = 27.5 USDC
    check("balance after sell", round(s.usdc_balance(), 2), round(175.0 + 27.5, 2))

    # paper_sell at a loss: must be blocked
    s.usdc_deposit(25.0)
    txsig3 = paper_buy(s, coin_id, 25.0, 0.5, time.time())
    lots2 = s.get_lots(coin_id)
    txsig4 = paper_sell(s, coin_id, dict(lots2[0]), 0.48)
    check("paper_sell at loss blocked", txsig4, "")
    check("lot still present after blocked sell", len(s.get_lots(coin_id)), 1)

    # paper_buy with insufficient funds
    s2 = Store(os.path.join(d, "test2.db"))
    coin_id2 = s2.add_coin("c2", "C2", "C2Mint" + "1" * 35)
    # no deposit -> balance = 0
    txsig5 = paper_buy(s2, coin_id2, 25.0, 0.5, time.time())
    check("paper_buy with no balance: empty txsig", txsig5, "")

print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nHAY FALLAS: {len(FAILURES)} failures")
    sys.exit(1)
else:
    print("TODOS OK")
    sys.exit(0)
