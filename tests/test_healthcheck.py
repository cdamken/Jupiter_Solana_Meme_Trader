"""test_healthcheck.py — Tests for the read-only healthcheck.

Self-contained, stdlib only. Uses temp DBs.
Run: python3 tests/test_healthcheck.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("JUPITER_DB", ":memory:")
os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

from store.store import Store
from healthcheck import (
    check_db, check_liveness, check_price_feed,
    check_ledger, check_config_audit, check_exec_bits,
    run_healthcheck, TICK_CADENCE_S, PRICE_STALE_S, GATE_STUCK_TICKS,
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


print("=== test_healthcheck.py ===")

# Helper: seed a test DB with schema and return the path
def make_db(d):
    db = os.path.join(d, "test.db")
    s = Store(db)
    return db, s


# --- check_db: clean DB ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    r = check_db(db)
    check("db_clean_status", r.status, "ok")
    check_true("db_clean_integrity_ok", any("integrity_check: ok" in m for m in r.messages))

# --- check_db: missing DB ---
with tempfile.TemporaryDirectory() as d:
    try:
        check_db(os.path.join(d, "nope.db"))
        FAILURES.append("FAIL db_missing: expected FileNotFoundError")
    except FileNotFoundError:
        print("  ok  db_missing_raises")

# --- check_liveness: no coins ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    r = check_liveness(db)
    check("liveness_no_coins", r.status, "ok")

# --- check_liveness: fresh price ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    cid = s.add_coin("FRESH", "Fresh", "mint1111111111111111111111111111111111111111111")
    s.record_price(cid, time.time(), 1.0)
    r = check_liveness(db)
    check("liveness_fresh", r.status, "ok")

# --- check_liveness: stale price ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    cid = s.add_coin("STALE", "Stale", "mint2222222222222222222222222222222222222222222")
    s.record_price(cid, time.time() - TICK_CADENCE_S - 60, 1.0)
    r = check_liveness(db)
    check("liveness_stale", r.status, "warn")

# --- check_price_feed: stale ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    cid = s.add_coin("PSTALE", "PStale", "mint3333333333333333333333333333333333333333333")
    s.record_price(cid, time.time() - PRICE_STALE_S - 60, 0.5)
    r = check_price_feed(db)
    check("price_stale", r.status, "warn")
    check_true("price_stale_msg", any("stale" in m for m in r.messages))

# --- check_price_feed: fresh ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    cid = s.add_coin("PFRESH", "PFresh", "mint4444444444444444444444444444444444444444444")
    s.record_price(cid, time.time(), 0.5)
    r = check_price_feed(db)
    check("price_fresh", r.status, "ok")

# --- check_price_feed: gate stuck ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    cid = s.add_coin("GATE", "Gate", "mint5555555555555555555555555555555555555555555")
    s.record_price(cid, time.time(), 0.5)
    s.state_set_commit(cid, "price_gate_cand_n", str(GATE_STUCK_TICKS + 1))
    r = check_price_feed(db)
    check("gate_stuck", r.status, "warn")
    check_true("gate_stuck_msg", any("stuck" in m for m in r.messages))

# --- check_ledger: empty ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    r = check_ledger(db)
    check("ledger_empty", r.status, "ok")

# --- check_ledger: clean chain ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    s.usdc_deposit(1000.0)
    cid = s.add_coin("LC", "LC", "mint6666666666666666666666666666666666666666666")
    s.usdc_reserve(100.0, cid)
    s.usdc_release(100.0, cid)
    r = check_ledger(db)
    check("ledger_clean", r.status, "ok")
    check_true("ledger_intact_msg", any("intact" in m for m in r.messages))
    check_true("ledger_no_orphans", any("No orphaned" in m for m in r.messages))

# --- check_ledger: orphaned reserve ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    s.usdc_deposit(1000.0)
    cid = s.add_coin("LO", "LO", "mint7777777777777777777777777777777777777777777")
    s.usdc_reserve(50.0, cid)
    # No release or buy -> orphan
    r = check_ledger(db)
    check("ledger_orphan", r.status, "warn")
    check_true("ledger_orphan_msg", any("orphaned" in m for m in r.messages))

# --- check_config_audit: no live coins ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    s.add_coin("PAPER", "Paper", "mint8888888888888888888888888888888888888888888")
    r = check_config_audit(db)
    check("config_no_live", r.status, "ok")

# --- check_exec_bits: missing script ---
with tempfile.TemporaryDirectory() as d:
    r = check_exec_bits(d)
    check("exec_no_scripts", r.status, "ok")

# --- check_exec_bits: script without +x ---
with tempfile.TemporaryDirectory() as d:
    deploy = os.path.join(d, "deploy")
    os.makedirs(deploy)
    script = os.path.join(deploy, "keepalive.sh")
    with open(script, "w") as f:
        f.write("#!/bin/bash\necho hi\n")
    os.chmod(script, 0o644)
    r = check_exec_bits(d)
    check("exec_no_x", r.status, "warn")
    check_true("exec_no_x_msg", any("+x" in m for m in r.messages))

# --- check_exec_bits: script with +x ---
with tempfile.TemporaryDirectory() as d:
    deploy = os.path.join(d, "deploy")
    os.makedirs(deploy)
    script = os.path.join(deploy, "keepalive.sh")
    with open(script, "w") as f:
        f.write("#!/bin/bash\necho hi\n")
    os.chmod(script, 0o755)
    r = check_exec_bits(d)
    check("exec_has_x", r.status, "ok")

# --- run_healthcheck: exit code 0 for clean DB ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    code = run_healthcheck(db, d, as_json=True)
    check("run_clean_exit", code, 0)

# --- run_healthcheck: exit code 1 for warnings ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    cid = s.add_coin("W", "W", "mint9999999999999999999999999999999999999999999")
    s.record_price(cid, time.time() - TICK_CADENCE_S - 60, 1.0)
    code = run_healthcheck(db, d, checks=["liveness"], as_json=True)
    check("run_warn_exit", code, 1)

# --- JSON output is parseable ---
with tempfile.TemporaryDirectory() as d:
    db, s = make_db(d)
    import io
    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    run_healthcheck(db, d, as_json=True)
    sys.stdout = old_stdout
    parsed = json.loads(buf.getvalue())
    check_true("json_parseable", isinstance(parsed, list))
    check_true("json_has_results", len(parsed) > 0)
    check("json_first_status", parsed[0]["status"], "ok")

print()
if FAILURES:
    print(f"\n{'='*60}")
    for f in FAILURES:
        print(f)
    print(f"\nFAILED: {len(FAILURES)} test(s)")
    sys.exit(1)
else:
    print("All healthcheck tests passed.")
