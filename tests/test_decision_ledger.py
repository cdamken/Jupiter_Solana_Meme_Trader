"""test_decision_ledger.py — Tests for the decision ledger (#502).

Self-contained, stdlib only. Uses a temp DB.
Run: python3 tests/test_decision_ledger.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("JUPITER_DB", ":memory:")
os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

import config  # noqa
from store.store import Store

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


print("=== test_decision_ledger.py ===")

with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("TEST", "Test Coin", "So1111111111111111111111111111111111111111111")

    # --- log_decision basic ---
    ts = time.time()
    s.log_decision(cid, ts, "buy", "grid_step", 0.05, {"buy_usd": 25.0})
    s._auto_commit()
    rows = s.get_decisions(cid)
    check("log_decision_count", len(rows), 1)
    check("log_decision_action", rows[0]["action"], "buy")
    check("log_decision_reason", rows[0]["reason"], "grid_step")
    check("log_decision_price", rows[0]["price"], 0.05)
    detail = json.loads(rows[0]["detail"])
    check("log_decision_detail_buy_usd", detail["buy_usd"], 25.0)

    # --- multiple decisions ---
    s.log_decision(cid, ts + 60, "sell", "grid_step", 0.06, {"n_sells": 1})
    s.log_decision(cid, ts + 120, "hold", "no_signal", 0.055, None)
    s._auto_commit()
    rows = s.get_decisions(cid)
    check("multi_count", len(rows), 3)
    check("multi_order_desc", rows[0]["action"], "hold")

    # --- get_decisions limit ---
    rows2 = s.get_decisions(cid, limit=2)
    check("limit_count", len(rows2), 2)

    # --- null detail ---
    hold_row = rows[0]
    check("null_detail", hold_row["detail"], None)

    # --- skip action (no price) ---
    s.log_decision(cid, ts + 180, "skip", "no_price", None, {"streak": 3})
    s._auto_commit()
    rows = s.get_decisions(cid)
    skip_row = rows[0]
    check("skip_action", skip_row["action"], "skip")
    check("skip_reason", skip_row["reason"], "no_price")
    check("skip_price_null", skip_row["price"], None)
    skip_detail = json.loads(skip_row["detail"])
    check("skip_streak", skip_detail["streak"], 3)

    # --- price gate skip ---
    s.log_decision(cid, ts + 240, "skip", "price_gate", 0.10,
                   {"last": 0.05, "cand_n": 1})
    s._auto_commit()
    rows = s.get_decisions(cid)
    gate_row = rows[0]
    check("gate_action", gate_row["action"], "skip")
    check("gate_reason", gate_row["reason"], "price_gate")
    check("gate_price", gate_row["price"], 0.10)

    # --- topup action ---
    s.log_decision(cid, ts + 300, "topup", "topup", 0.04,
                   {"buy_usd": 15.0, "mode": "paper"})
    s._auto_commit()
    rows = s.get_decisions(cid)
    check("topup_action", rows[0]["action"], "topup")

    # --- prune_decisions ---
    # Insert an old decision (31 days ago)
    old_ts = time.time() - 31 * 86400
    s.log_decision(cid, old_ts, "hold", "no_signal", 0.03, None)
    s._auto_commit()
    before = len(s.get_decisions(cid, limit=100))
    s.prune_decisions(max_age_days=30)
    after = len(s.get_decisions(cid, limit=100))
    check("prune_removed_old", after, before - 1)

    # --- per-coin isolation ---
    cid2 = s.add_coin("TEST2", "Test Coin 2", "So2222222222222222222222222222222222222222222")
    s.log_decision(cid2, ts, "buy", "prebuy", 1.0, {"buy_usd": 10.0})
    s._auto_commit()
    rows_c1 = s.get_decisions(cid, limit=100)
    rows_c2 = s.get_decisions(cid2, limit=100)
    check("isolation_c2_count", len(rows_c2), 1)
    check_true("isolation_c1_unchanged", len(rows_c1) > 1)

    # --- buy+sell action ---
    s.log_decision(cid, ts + 360, "buy+sell", "grid_step", 0.07,
                   {"buy_usd": 25.0, "n_sells": 2})
    s._auto_commit()
    rows = s.get_decisions(cid)
    check("buy_sell_action", rows[0]["action"], "buy+sell")

print()
if FAILURES:
    print(f"\n{'='*60}")
    for f in FAILURES:
        print(f)
    print(f"\nFAILED: {len(FAILURES)} test(s)")
    sys.exit(1)
else:
    print("All decision ledger tests passed.")
