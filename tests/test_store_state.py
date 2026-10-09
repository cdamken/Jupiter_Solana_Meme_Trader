"""test_store_state.py — Tests for coin_state KV table and Store methods.

Self-contained, stdlib only. Run: python3 tests/test_store_state.py
"""
import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

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


print("=== test_store_state.py ===")

# Fresh DB in a temp dir
tmp = tempfile.mkdtemp()
db_path = os.path.join(tmp, "test.db")
store = Store(db_path)

# Seed a coin
coin_id = store.add_coin("test", "Test Coin", "So1111111111111111111111111111111111111111111")

# --- state_get: missing key returns default ---
print("\n[state_get defaults]")
check("missing key -> None", store.state_get(coin_id, "ref"), None)
check("missing key -> custom default", store.state_get(coin_id, "ref", "0.5"), "0.5")
check("get_float missing -> 0.0", store.state_get_float(coin_id, "ref"), 0.0)
check("get_float missing -> custom", store.state_get_float(coin_id, "ref", 99.0), 99.0)
check("get_int missing -> 0", store.state_get_int(coin_id, "count"), 0)

# --- state_set + get ---
print("\n[state_set + get]")
store.state_set_commit(coin_id, "ref", "0.00123")
check("set then get", store.state_get(coin_id, "ref"), "0.00123")
check("get_float", store.state_get_float(coin_id, "ref"), 0.00123)

# --- upsert (overwrite) ---
print("\n[state_set upsert]")
store.state_set_commit(coin_id, "ref", "0.00456")
check("upsert overwrites", store.state_get(coin_id, "ref"), "0.00456")

# --- state_get_int ---
print("\n[state_get_int]")
store.state_set_commit(coin_id, "price_gate_cand_n", 3)
check("get_int", store.state_get_int(coin_id, "price_gate_cand_n"), 3)

# --- state_mget (bulk read) ---
print("\n[state_mget]")
store.state_set_commit(coin_id, "last_sell_ts", "1700000000.0")
all_state = store.state_mget(coin_id)
check_true("mget has ref", "ref" in all_state)
check_true("mget has last_sell_ts", "last_sell_ts" in all_state)
check("mget ref value", all_state["ref"], "0.00456")

# --- state_mset (bulk write) ---
print("\n[state_mset]")
store.state_mset_commit(coin_id, {
    "floor_episode": "1",
    "price_gate_last": "0.05",
    "price_gate_cand": "0.0",
    "price_gate_cand_n": "0",
})
check("mset floor_episode", store.state_get(coin_id, "floor_episode"), "1")
check("mset price_gate_last", store.state_get_float(coin_id, "price_gate_last"), 0.05)

# --- state_delete ---
print("\n[state_delete]")
store.state_delete(coin_id, "floor_episode")
check("delete removes key", store.state_get(coin_id, "floor_episode"), None)
check_true("delete leaves others", store.state_get(coin_id, "ref") is not None)

# --- isolation between coins ---
print("\n[coin isolation]")
coin2_id = store.add_coin("test2", "Test Coin 2", "So2222222222222222222222222222222222222222222")
store.state_set_commit(coin2_id, "ref", "9.99")
check("coin1 ref unchanged", store.state_get(coin_id, "ref"), "0.00456")
check("coin2 ref independent", store.state_get(coin2_id, "ref"), "9.99")

# --- CASCADE delete: coin deletion clears its state ---
print("\n[cascade delete]")
store._c.execute("DELETE FROM coins WHERE id = ?", (coin2_id,))
store._c.commit()
check("cascade: state gone after coin delete", store.state_get(coin2_id, "ref"), None)
check("cascade: coin1 unaffected", store.state_get(coin_id, "ref"), "0.00456")

# --- begin/commit transaction batching ---
print("\n[transaction batching]")
store.begin()
store.state_set(coin_id, "tx_test_a", "aaa")
store.state_set(coin_id, "tx_test_b", "bbb")
store.commit()
check("tx batch: a written", store.state_get(coin_id, "tx_test_a"), "aaa")
check("tx batch: b written", store.state_get(coin_id, "tx_test_b"), "bbb")

# rollback discards writes
store.begin()
store.state_set(coin_id, "tx_test_a", "ROLLED_BACK")
store.rollback()
check("rollback: a unchanged", store.state_get(coin_id, "tx_test_a"), "aaa")

# _auto_commit is suppressed inside begin/commit
store.begin()
store.state_set_commit(coin_id, "tx_test_c", "ccc")
# still inside the tx, so rolling back should discard it
store.rollback()
check("auto_commit suppressed in tx", store.state_get(coin_id, "tx_test_c"), None)

# --- invalid float/int casting returns default ---
print("\n[type casting edge cases]")
store.state_set_commit(coin_id, "bad_float", "not_a_number")
check("bad float -> default", store.state_get_float(coin_id, "bad_float"), 0.0)
check("bad int -> default", store.state_get_int(coin_id, "bad_float"), 0)

# Cleanup
import shutil
shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILURES: {len(FAILURES)}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
