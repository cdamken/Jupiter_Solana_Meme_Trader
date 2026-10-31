"""test_parallel_scheduler.py -- Tests for parallel scheduler (#33).

Verifies per-coin isolation, timeout handling, and concurrent ticks.

Run: python3 tests/test_parallel_scheduler.py
"""
import os
import sys
import tempfile
import time
import threading
import unittest.mock
import concurrent.futures

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("RPC_URL", "http://localhost:8899")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
os.environ.setdefault("COIN_TICK_TIMEOUT_S", "30")

import config  # noqa
from store.store import Store
from scheduler import _tick_one

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


print("=== test_parallel_scheduler.py ===")


# ---- Per-thread DB isolation ----
print("\n[per_thread_db]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("iso", "ISO", "IsoMint" + "1" * 34)
    s.usdc_deposit(500.0)
    s.set_coin_status(cid, "paper")

    thread_ids = []

    def record_thread():
        thread_ids.append(threading.current_thread().ident)
        ws = Store(db)
        chk("worker_store_connects", ws.usdc_balance() == 500.0)

    t = threading.Thread(target=record_thread)
    t.start()
    t.join()
    chk("worker_ran_in_separate_thread", len(thread_ids) == 1 and thread_ids[0] != threading.current_thread().ident)


# ---- _tick_one returns (slug, elapsed, error_or_none) ----
print("\n[tick_one_success]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("ok", "OK", "OkMint" + "1" * 35)
    s.usdc_deposit(500.0)
    s.set_coin_status(cid, "paper")
    coin = dict(s.get_coin("ok"))
    coin["status"] = "paper"

    with unittest.mock.patch("tick.fetch_price", return_value=1.0):
        with unittest.mock.patch("tick._alerts"):
            slug, elapsed, err = _tick_one(db, coin, "paper", 30)
            check("slug returned", slug, "ok")
            chk("elapsed is positive", elapsed > 0)
            check("no error", err, None)


# ---- _tick_one captures exceptions ----
print("\n[tick_one_error]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("err", "ERR", "ErrMint" + "1" * 34)
    s.usdc_deposit(500.0)
    s.set_coin_status(cid, "paper")
    coin = dict(s.get_coin("err"))
    coin["status"] = "paper"

    def exploding_price(*a, **kw):
        raise RuntimeError("boom")

    with unittest.mock.patch("tick.fetch_price", side_effect=exploding_price):
        with unittest.mock.patch("tick._alerts"):
            slug, elapsed, err = _tick_one(db, coin, "paper", 30)
            check("slug on error", slug, "err")
            chk("error message captured", err is not None and "boom" in err)


# ---- Parallel ticks don't interfere ----
print("\n[parallel_isolation]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)

    n_coins = 5
    for i in range(n_coins):
        slug = f"coin{i}"
        mint = f"Mint{i}" + "x" * (40 - len(f"Mint{i}"))
        cid = s.add_coin(slug, slug.upper(), mint)
        s.set_coin_status(cid, "paper")
    s.usdc_deposit(1000.0)

    coins = [dict(c) for c in s.list_coins() if c["status"] == "paper"]
    check("seeded_coins", len(coins), n_coins)

    call_log = []
    log_lock = threading.Lock()

    original_fetch = None

    def tracking_price(mint, **kw):
        tid = threading.current_thread().ident
        with log_lock:
            call_log.append((mint, tid))
        time.sleep(0.05)
        return 1.0

    with unittest.mock.patch("tick.fetch_price", side_effect=tracking_price):
        with unittest.mock.patch("tick._alerts"):
            with concurrent.futures.ThreadPoolExecutor(max_workers=n_coins) as pool:
                futures = {pool.submit(_tick_one, db, c, "paper", 30): c for c in coins}
                results = []
                for f in concurrent.futures.as_completed(futures, timeout=35):
                    results.append(f.result())

    check("all_coins_ticked", len(results), n_coins)
    chk("all_succeeded", all(r[2] is None for r in results))

    thread_set = set(tid for _, tid in call_log)
    chk("multiple_threads_used", len(thread_set) > 1)
    chk("all_mints_called", len(set(m for m, _ in call_log)) == n_coins)


# ---- Timeout doesn't crash other coins ----
print("\n[timeout_isolation]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)

    fast_cid = s.add_coin("fast", "FAST", "FastMint" + "1" * 32)
    slow_cid = s.add_coin("slow", "SLOW", "SlowMint" + "1" * 32)
    s.set_coin_status(fast_cid, "paper")
    s.set_coin_status(slow_cid, "paper")
    s.usdc_deposit(500.0)

    fast_coin = dict(s.get_coin("fast"))
    fast_coin["status"] = "paper"
    slow_coin = dict(s.get_coin("slow"))
    slow_coin["status"] = "paper"

    def slow_price(mint, **kw):
        if "Slow" in mint:
            time.sleep(3)
        return 1.0

    timeout = 2

    with unittest.mock.patch("tick.fetch_price", side_effect=slow_price):
        with unittest.mock.patch("tick._alerts"):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f_fast = pool.submit(_tick_one, db, fast_coin, "paper", timeout)
                f_slow = pool.submit(_tick_one, db, slow_coin, "paper", timeout)

                fast_slug, fast_elapsed, fast_err = f_fast.result(timeout=10)
                check("fast_coin_ok", fast_err, None)
                chk("fast_coin_quick", fast_elapsed < 2)

                slow_slug, slow_elapsed, slow_err = f_slow.result(timeout=10)
                check("slow_coin_slug", slow_slug, "slow")
                chk("slow_coin_finished_eventually", slow_elapsed >= 2)


# ---- Single coin skips ThreadPool ----
print("\n[single_coin_direct]")
import inspect
sched_src = inspect.getsource(sys.modules["scheduler"].main)
chk("single_coin_path", "n == 1" in sched_src or "elif n == 1" in sched_src)
chk("threadpool_for_multi", "ThreadPoolExecutor" in sched_src)


# ---- Config has COIN_TICK_TIMEOUT_S ----
print("\n[config]")
chk("timeout_in_config", hasattr(config, "COIN_TICK_TIMEOUT_S"))
chk("timeout_default_30", config.COIN_TICK_TIMEOUT_S == 30)


print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILED: {len(FAILURES)} test(s)")
    sys.exit(1)
else:
    print("All parallel scheduler tests passed.")
