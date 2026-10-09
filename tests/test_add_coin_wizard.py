"""test_add_coin_wizard.py — Tests for add-coin wizard (#16).

Tests mint validation logic, coin_add with wizard fields, and slug validation.
Run: python3 tests/test_add_coin_wizard.py
"""
import os
import sys
import tempfile
import re

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


print("=== test_add_coin_wizard.py ===")

# ---- base58 validation regex (same as panel/app.py) ----
BASE58 = re.compile(r'^[1-9A-HJ-NP-Za-km-z]+$')
SLUG_RE = re.compile(r'^[a-z][a-z0-9_]{0,19}$')

print("\n[base58 validation]")
check_true("valid_base58", bool(BASE58.match("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")))
check_true("reject_O", not BASE58.match("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1O"))
check_true("reject_0", not BASE58.match("0PjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
check_true("reject_I", not BASE58.match("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1I"))
check_true("reject_l", not BASE58.match("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1l"))
check_true("reject_plus", not BASE58.match("EPjFWdd5AufqSSqeM2qN1xzybapC8+"))
check_true("reject_empty", not BASE58.match(""))

print("\n[slug validation]")
check_true("slug_valid", bool(SLUG_RE.match("bonk")))
check_true("slug_valid_underscores", bool(SLUG_RE.match("my_coin_2")))
check_true("slug_reject_uppercase", not SLUG_RE.match("BONK"))
check_true("slug_reject_number_start", not SLUG_RE.match("2bonk"))
check_true("slug_reject_too_long", not SLUG_RE.match("a" * 21))
check_true("slug_reject_dash", not SLUG_RE.match("my-coin"))
check_true("slug_reject_empty", not SLUG_RE.match(""))

def _seed_catalog(store):
    for key, scope in [("LOT_USD", "coin-allowed"), ("MAX_CAPITAL_USD", "coin-allowed"),
                       ("PRICE_PAIR_ADDRESS", "coin-only")]:
        store._c.execute(
            "INSERT OR IGNORE INTO param_catalog (key, tier, scope, type, label, help)"
            " VALUES (?,?,?,?,?,?)",
            (key, "editable", scope, "text", key, "test"),
        )
    store._c.commit()

print("\n[wizard coin_add with overrides]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    _seed_catalog(s)
    s.usdc_deposit(500.0)

    coin_id = s.add_coin("test", "Test Coin", "So1111111111111111111111111111111111111111111")

    s._c.execute(
        "INSERT INTO coin_overrides (coin_id, key, value, author, reason)"
        " VALUES (?,?,?,?,?)",
        (coin_id, "LOT_USD", "10", "wizard", "probation lot from wizard"),
    )
    s._c.execute(
        "INSERT INTO coin_overrides (coin_id, key, value, author, reason)"
        " VALUES (?,?,?,?,?)",
        (coin_id, "MAX_CAPITAL_USD", "200", "wizard", "set at add-coin"),
    )
    s._c.execute(
        "INSERT INTO coin_overrides (coin_id, key, value, author, reason)"
        " VALUES (?,?,?,?,?)",
        (coin_id, "PRICE_PAIR_ADDRESS", "pair123abc", "wizard", "pinned at add-coin"),
    )
    s._c.commit()

    cfg = s.get_config(coin_id)
    check("override_lot_usd", cfg.get("LOT_USD"), "10")
    check("override_max_capital", cfg.get("MAX_CAPITAL_USD"), "200")
    check("override_pair_address", cfg.get("PRICE_PAIR_ADDRESS"), "pair123abc")

    coin = s.get_coin("test")
    check("coin_status_paper", coin["status"], "paper")
    check("coin_slug", coin["slug"], "test")

print("\n[duplicate slug rejection]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    s.add_coin("bonk", "BONK", "So1111111111111111111111111111111111111111111")
    existing = s.get_coin("bonk")
    check_true("dupe_found", existing is not None)

print("\n[born paper enforced]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    cid = s.add_coin("safe", "Safe", "So2222222222222222222222222222222222222222222")
    coin = s._c.execute("SELECT status FROM coins WHERE id=?", (cid,)).fetchone()
    check("born_paper", coin["status"], "paper")

print("\n[panel route existence]")
try:
    import panel.app as papp
    rules = [r.rule for r in papp.app.url_map.iter_rules()]
    check_true("wizard_route", "/coin/wizard" in rules)
    check_true("validate_mint_route", "/api/validate-mint" in rules)
    check_true("coin_add_route", "/coin/add" in rules)
except ImportError:
    # Flask not installed — verify routes via source code
    app_src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "panel", "app.py")).read()
    check_true("wizard_route", '@app.get("/coin/wizard")' in app_src)
    check_true("validate_mint_route", '@app.get("/api/validate-mint")' in app_src)
    check_true("coin_add_route", '@app.post("/coin/add")' in app_src)

print("\n[wizard template exists]")
wizard_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "panel", "templates", "wizard.html")
check_true("wizard_template", os.path.isfile(wizard_path))

with open(wizard_path) as f:
    wiz = f.read()
check_true("has_step1", "Step 1" in wiz)
check_true("has_step2", "Step 2" in wiz)
check_true("has_step3", "Step 3" in wiz)
check_true("has_validate_call", "validate_mint" in wiz or "validate-mint" in wiz)
check_true("has_pair_selection", "pair_sel" in wiz or "selectPair" in wiz)
check_true("has_lot_usd", "lot_usd" in wiz)
check_true("has_max_capital", "max_capital" in wiz)
check_true("born_paper_note", "paper" in wiz.lower())


print()
if FAILURES:
    print(f"\n{'='*60}")
    for f in FAILURES:
        print(f)
    print(f"\nFAILED: {len(FAILURES)} test(s)")
    sys.exit(1)
else:
    print("All add-coin wizard tests passed.")
