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
    tx_avoids_foreign_signer,
    revalidate_effective_sale,
    confirm_transaction,
    paper_buy,
    paper_sell,
    DEFAULT_SLIPPAGE_BPS,
    GAS_REFILL_SLIPPAGE_BPS,
    JUPITER_QUOTE_URL,
    JUPITER_SWAP_URL,
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

def check_close(name, actual, expected, tol=0.01):
    if abs(actual - expected) > tol:
        FAILURES.append(f"FAIL {name}: got {actual:.4f}, expected {expected:.4f}")
    else:
        print(f"  ok  {name}")


print("=== test_execute.py ===")

# ---- effective_price_usd ----
print("\n[effective_price_usd]")

q = {"inAmount": "25000000", "outAmount": "50000000"}
p = effective_price_usd(q, 25_000_000, input_is_usdc=True)
check("buy: price = 0.5", round(p, 6), 0.5)

q2 = {"inAmount": "50000000", "outAmount": "26000000"}
p2 = effective_price_usd(q2, 50_000_000, input_is_usdc=False)
check("sell: price = 0.52", round(p2, 6), 0.52)

check("bad quote: None", effective_price_usd({"inAmount": "0", "outAmount": "100"}, 0, True), None)
check("missing fields: None", effective_price_usd({}, 0, True), None)

# ---- revalidate_effective_sale ----
print("\n[revalidate_effective_sale]")
good_lot = {"id": 1, "tokens": 100.0, "cost": 50.0, "buy_price": 0.5}
check_true("gain > fee: allowed", revalidate_effective_sale(good_lot, 0.52))
check_false("at cost: blocked", revalidate_effective_sale(good_lot, 0.50))
check_false("below cost: blocked", revalidate_effective_sale(good_lot, 0.48))
bad_lot  = {"id": 2, "tokens": 0.0, "cost": 50.0, "buy_price": 0.5}
check_false("zero tokens: blocked", revalidate_effective_sale(bad_lot, 0.52))

# ---- API endpoints (issue #2 fix 2: swap v2) ----
print("\n[api_endpoints]")
check_true("quote_url_is_v1", "api.jup.ag/swap/v1" in JUPITER_QUOTE_URL)
check_true("swap_url_is_v1", "api.jup.ag/swap/v1" in JUPITER_SWAP_URL)
check_true("no_v6_quote", "v6" not in JUPITER_QUOTE_URL)
check_true("no_v6_swap", "v6" not in JUPITER_SWAP_URL)

# ---- configurable slippage (issue #2 fix 3) ----
print("\n[configurable_slippage]")
check("default_slippage_bps", DEFAULT_SLIPPAGE_BPS, 150)
check("gas_refill_slippage_bps", GAS_REFILL_SLIPPAGE_BPS, 300)
check_true("config_has_max_slippage", hasattr(config, "MAX_SLIPPAGE_BPS"))
check("config_max_slippage_default", config.MAX_SLIPPAGE_BPS, 150)

# ---- tx signer inspection (issue #2 fix 4) ----
print("\n[tx_signer_inspection]")
# Without solders installed, tx_avoids_foreign_signer should reject (fail-closed)
try:
    from solders.transaction import VersionedTransaction  # type: ignore
    _HAS_SOLDERS = True
except ImportError:
    _HAS_SOLDERS = False

if not _HAS_SOLDERS:
    check_false("no_solders_rejects", tx_avoids_foreign_signer("AAAA", "SomeWallet"))
    print("  (solders not installed — signer check correctly rejects)")

# Verify the function exists and has the right signature
import inspect
sig = inspect.signature(tx_avoids_foreign_signer)
check("signer_check_params", list(sig.parameters.keys()), ["swap_tx_b64", "wallet_pubkey"])

# ---- confirm_transaction exists and is callable ----
print("\n[confirm_transaction]")
sig_ct = inspect.signature(confirm_transaction)
check("confirm_tx_params", list(sig_ct.parameters.keys()), ["rpc_url", "txsig", "timeout"])

# ---- paper_sell now applies fee (issue #7 partial: paper mirrors live) ----
print("\n[paper_sell_with_fee]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    coin_id = s.add_coin("test", "TEST", "TestMint" + "1" * 33)
    s.usdc_deposit(200.0)

    txsig = paper_buy(s, coin_id, 25.0, 0.5, time.time())
    check_true("paper_buy ok", txsig.startswith("paper-buy-"))
    check("balance after buy", s.usdc_balance(), 175.0)
    lots = s.get_lots(coin_id)
    check("one lot", len(lots), 1)

    lot = dict(lots[0])
    txsig2 = paper_sell(s, coin_id, lot, 0.55)
    check_true("paper_sell ok", txsig2.startswith("paper-sell-"))
    check("lot removed", len(s.get_lots(coin_id)), 0)
    # proceeds = 50 * 0.55 * (1 - 0.015) = 27.5 * 0.985 = 27.0875
    expected_bal = 175.0 + 50.0 * 0.55 * (1 - 0.015)
    check_close("balance_after_sell_with_fee", s.usdc_balance(), expected_bal)

    # paper_sell at a loss: blocked
    s.usdc_deposit(25.0)
    paper_buy(s, coin_id, 25.0, 0.5, time.time())
    lots2 = s.get_lots(coin_id)
    txsig4 = paper_sell(s, coin_id, dict(lots2[0]), 0.48)
    check("paper_sell at loss blocked", txsig4, "")

# ---- paper_buy edge cases ----
print("\n[paper_buy_edges]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    coin_id = s.add_coin("edge", "Edge", "EdgeMint" + "1" * 33)

    # no balance
    txsig = paper_buy(s, coin_id, 25.0, 0.5, time.time())
    check("no_balance", txsig, "")

    # price <= 0
    s.usdc_deposit(100.0)
    txsig = paper_buy(s, coin_id, 25.0, 0.0, time.time())
    check("zero_price", txsig, "")
    txsig = paper_buy(s, coin_id, 25.0, -1.0, time.time())
    check("negative_price", txsig, "")

    # origin param
    txsig_r = paper_buy(s, coin_id, 25.0, 0.5, time.time(), origin="reserve")
    lots_r = s.get_lots(coin_id)
    reserve_lot = [l for l in lots_r if dict(l)["origin"] == "reserve"]
    check("reserve_origin", len(reserve_lot), 1)

# ---- live_buy/live_sell accept slippage_bps parameter ----
print("\n[slippage_param]")
from execute import live_buy, live_sell
sig_lb = inspect.signature(live_buy)
check_true("live_buy_has_slippage", "slippage_bps" in sig_lb.parameters)
sig_ls = inspect.signature(live_sell)
check_true("live_sell_has_slippage", "slippage_bps" in sig_ls.parameters)

# ---- scheduler passes slippage_bps ----
print("\n[scheduler_slippage]")
sched_src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "scheduler.py")).read()
check_true("scheduler_passes_slippage", "slippage_bps=slippage_bps" in sched_src)
slippage_count = sched_src.count("slippage_bps=slippage_bps")
check("scheduler_all_live_calls", slippage_count, 4)


print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILURES: {len(FAILURES)}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
