"""bootstrap.py — Seed a fresh Jupiter DB with param_catalog and fleet defaults.

Run once after cloning, before starting the scheduler:
    python3 bootstrap.py

Safe to re-run: INSERT OR IGNORE / INSERT OR REPLACE.
"""
import os
import sys

os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

import config as cfg
from store.store import Store

CATALOG = [
    # (key, tier, scope, type, min, max, default_val, recommended, label, help)
    ("BUY_STEP_PCT",      "editable",       "coin-allowed", "float", "0.5",  "30",   "4.0",   "4.0",   "Buy step %",          "Buy when price drops this % below ref"),
    ("SELL_STEP_PCT",     "editable",       "coin-allowed", "float", "0.5",  "50",   "4.0",   "4.0",   "Sell step %",         "Sell when price rises this % above buy_price"),
    ("LOT_USD",           "editable",       "coin-allowed", "float", "1",    "500",  "25.0",  "25.0",  "Lot size USD",        "USD to spend per buy"),
    ("MAX_CAPITAL_USD",   "editable",       "coin-allowed", "float", "10",   "5000", "200.0", "200.0", "Max capital USD",     "Cap on total deployed USD for this coin"),
    ("SELL_TRAIL",        "editable",       "coin-allowed", "bool",  None,   None,   "0",     "0",     "Trailing sell",       "1 = arm trail at sell_step, 0 = sell immediately"),
    ("SELL_TRAIL_PCT",    "editable",       "coin-allowed", "float", "0.5",  "20",   "2.0",   "2.0",   "Trail pull-back %",   "Trigger sell when price pulls back this % from peak"),
    ("CEILING_PERCENTILE","editable",       "coin-allowed", "float", "50",   "100",  "98.0",  "98.0",  "Ceiling percentile",  "P-N of 14d price window used as buy ceiling"),
    ("PRICE_GATE_PCT",    "editable",       "coin-allowed", "float", "5",    "100",  "30.0",  "30.0",  "Price gate %",        "Reject ticks that jump more than this % from last"),
    ("QUOTE_MINT",        "locked-visible", "fleet-only",   "text",  None,   None,   "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", None, "Quote mint", "Always USDC. Never override per coin (incident #356)"),
    ("DEFAULT_MODE",      "editable",       "fleet-only",   "enum",  None,   None,   "paper", "paper", "Default mode",        "paper or live"),
]

FLEET_DEFAULTS = {row[0]: row[6] for row in CATALOG}


def main():
    store = Store(cfg.DB_PATH)
    c = store._c

    print(f"Bootstrapping DB: {cfg.DB_PATH}")

    # param_catalog
    inserted = 0
    for row in CATALOG:
        key, tier, scope, typ, mn, mx, default, rec, label, help_ = row
        cur = c.execute(
            "INSERT OR IGNORE INTO param_catalog"
            " (key, tier, scope, type, min, max, default_val, recommended, label, help)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key, tier, scope, typ, mn, mx, default, rec, label, help_),
        )
        inserted += cur.rowcount
    c.commit()
    print(f"  param_catalog: {inserted} rows inserted ({len(CATALOG)} total)")

    # fleet_versions + fleet_defaults (version 1, pinned)
    existing = c.execute("SELECT version_id FROM fleet_versions WHERE pinned=1").fetchone()
    if existing:
        print(f"  fleet_defaults: version {existing['version_id']} already pinned, skipping")
    else:
        cur = c.execute(
            "INSERT INTO fleet_versions (author, reason, pinned) VALUES (?, ?, 1)",
            ("bootstrap", "initial seed from bootstrap.py"),
        )
        ver_id = cur.lastrowid
        for key, value in FLEET_DEFAULTS.items():
            c.execute(
                "INSERT OR REPLACE INTO fleet_defaults (version_id, key, value) VALUES (?,?,?)",
                (ver_id, key, value),
            )
        c.commit()
        print(f"  fleet_defaults: version {ver_id} created with {len(FLEET_DEFAULTS)} keys (pinned)")

    print("Done. Start the scheduler with: python3 scheduler.py --paper")


if __name__ == "__main__":
    main()
