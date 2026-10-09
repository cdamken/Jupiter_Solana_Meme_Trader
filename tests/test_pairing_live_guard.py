"""test_pairing_live_guard.py -- Issue #3: pairing must be blocked in live mode.

The pairing block in scheduler.py commits DB changes without executing an
on-chain swap. Until the execution layer supports combined group swaps,
pairing must be hard-refused in live mode (fail-closed).
"""
import sys
import os

os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

FAILURES = []


def chk(name, cond):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f" FAIL {name}")
        FAILURES.append(name)


# Read the scheduler source to verify the guard exists
import inspect
import scheduler

source = inspect.getsource(scheduler.tick_coin)

chk("pairing live guard: mode == 'live' check exists in tick_coin",
    'mode == "live"' in source and "PAIRING_GRID blocked" in source)

chk("pairing paper path uses mode='paper' not variable mode",
    '"paper"' in source.split("paper-pair")[0].split("usdc_commit_sell")[-1] or
    'pnl_pct, "paper"' in source)

chk("pairing live guard comes before the pairing logic",
    source.index("PAIRING_GRID blocked") < source.index("eligible_groups"))

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)}")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL OK")
