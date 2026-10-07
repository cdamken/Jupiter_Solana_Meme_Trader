#!/usr/bin/env python3
"""tools/migrate_from_simd.py -- Import a SIMD bot's live lot-book into Jupiter.

Reads trader_state.json (or trader_estado.json) from a SIMD bot directory,
creates the coin in Jupiter's DB (if it doesn't already exist), and inserts
every open lot. Idempotent per coin: if the coin already has lots in Jupiter
the script aborts for that slug rather than double-inserting.

Usage:
    python3 tools/migrate_from_simd.py \\
        --simd-dir  /home/carlos/simd          \\  # or a local clone/copy
        --slug      simd                        \\
        --label     SIMD                        \\
        --mint      SiMDt...                    \\
        --decimals  6                           \\
        [--status   paper]                      \\  # default: paper (safe)
        [--db       store/jupiter.db]

Safety gates:
  - Default status is 'paper'; pass --status live only when you are sure.
  - Coins already present with open lots are never overwritten.
  - Lots with tok <= 0 or cost <= 0 are skipped with a warning.
  - The script is READ-ONLY against the SIMD directory.
  - No real swaps; only DB writes.
"""
import argparse
import json
import os
import sys
import time

# Allow running from the project root without installing.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import os as _os
_DB_DEFAULT = _os.environ.get(
    "JUPITER_DB",
    _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "store", "jupiter.db"),
)
from store.store import Store


# --------------------------------------------------------------------------- #
# SIMD state reading (mirrors _migrate_state in trader.py)                    #
# --------------------------------------------------------------------------- #

_STATE_KEY_RENAMES = {
    "lotes": "lots",
}
_LOT_KEY_RENAMES = {
    "reserva":       "reserve",
    "atascado_desde": "stuck_since",
    "par_grid":      "grid_pair",
    "trail_armado":  "trail_armed",
    "nivela_a":      "levels_to",
    "origen":        "origin",
    "tok":           "tok",   # canonical; no rename needed but listed for clarity
}
_LOT_VALUE_RENAMES = {"origin": {"fallido": "failed"}}


def _load_simd_state(simd_dir: str) -> dict:
    for fname in ("trader_state.json", "trader_estado.json"):
        path = os.path.join(simd_dir, fname)
        if os.path.exists(path):
            with open(path) as f:
                st = json.load(f)
            print(f"[load] read {path}")
            # migrate top-level keys
            for old, new in _STATE_KEY_RENAMES.items():
                if old in st and new not in st:
                    st[new] = st.pop(old)
            # migrate lot keys + values
            for lot in st.get("lots", []):
                if not isinstance(lot, dict):
                    continue
                for old, new in _LOT_KEY_RENAMES.items():
                    if old in lot and new not in lot:
                        lot[new] = lot.pop(old)
                for field, mapping in _LOT_VALUE_RENAMES.items():
                    if lot.get(field) in mapping:
                        lot[field] = mapping[lot[field]]
            return st
    raise FileNotFoundError(
        f"No trader_state.json or trader_estado.json found in {simd_dir}"
    )


# --------------------------------------------------------------------------- #
# Migration                                                                    #
# --------------------------------------------------------------------------- #

def migrate(simd_dir: str, slug: str, label: str, mint: str,
            decimals: int, status: str, db_path: str) -> None:
    state = _load_simd_state(simd_dir)
    lots = state.get("lots", [])
    print(f"[state] {len(lots)} lot(s) found in SIMD state")

    store = Store(db_path)

    # Check if coin already exists.
    existing = store.get_coin(slug)
    if existing:
        coin_id = existing["id"]
        existing_lots = store.get_lots(coin_id)
        if existing_lots:
            print(
                f"[abort] coin '{slug}' already exists (id={coin_id}) "
                f"with {len(existing_lots)} open lot(s) in Jupiter. "
                "Remove them first or choose a different slug."
            )
            sys.exit(1)
        print(f"[coin] '{slug}' exists (id={coin_id}), no lots yet -- reusing.")
    else:
        coin_id = store.add_coin(
            slug, label, mint,
            status=status,
            migrated_from="simd",
            decimals=decimals,
        )
        print(f"[coin] created '{slug}' id={coin_id} status={status} decimals={decimals}")

    inserted = 0
    skipped = 0
    total_cost = 0.0
    now = time.time()

    for i, lot in enumerate(lots):
        tok  = float(lot.get("tok", 0.0))
        cost = float(lot.get("cost", 0.0))
        if tok <= 0 or cost <= 0:
            print(f"[skip] lot[{i}] tok={tok} cost={cost} -- skipped (zero/negative)")
            skipped += 1
            continue

        buy_price  = float(lot.get("price", cost / tok))
        ts         = float(lot.get("ts", now))
        origin     = str(lot.get("origin", "grid"))
        trail_armed = int(bool(lot.get("trail_armed", False)))
        trail_peak  = float(lot.get("trail_peak", 0.0))

        lot_id = store.add_lot(coin_id, tok, cost, buy_price, ts, origin)
        # update trail if armed
        if trail_armed:
            store.update_lot_trail(lot_id, trail_armed, trail_peak)

        print(
            f"[lot]  id={lot_id:4d}  tok={tok:.4f}  cost=${cost:.2f}"
            f"  price=${buy_price:.6f}  origin={origin}"
            + (f"  trail@{trail_peak:.6f}" if trail_armed else "")
        )
        inserted += 1
        total_cost += cost

    # Record the deployed capital as a deposit so the ledger balance is correct.
    if inserted > 0:
        store.usdc_deposit(total_cost)
        print(
            f"\n[ledger] deposited ${total_cost:.2f} USDC (cost basis of {inserted} lot(s))"
        )

    print(
        f"\n[done] {inserted} lot(s) inserted, {skipped} skipped."
        f" Total cost basis: ${total_cost:.2f}"
        f" -- coin '{slug}' is {status.upper()} in Jupiter."
    )
    if status == "paper":
        print(
            "[note] Status is PAPER. Switch to LIVE via the panel or:\n"
            f"       python3 -c \"import config,sys; sys.path.insert(0,'.'); "
            f"from store.store import Store; s=Store('{db_path}'); "
            f"s.set_coin_status({coin_id}, 'live')\""
        )


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simd-dir",  required=True,
                    help="Path to the SIMD bot directory containing trader_state.json")
    ap.add_argument("--slug",      required=True,
                    help="Coin slug in Jupiter (e.g. 'simd')")
    ap.add_argument("--label",     default="",
                    help="Display label (defaults to slug.upper())")
    ap.add_argument("--mint",      required=True,
                    help="Token mint address")
    ap.add_argument("--decimals",  type=int, default=6,
                    help="Token decimals (6 = pump.fun, 9 = SOL/native SPL)")
    ap.add_argument("--status",    default="paper",
                    choices=("paper", "live", "paused"),
                    help="Initial coin status in Jupiter (default: paper)")
    ap.add_argument("--db",        default=None,
                    help="Jupiter DB path (default: JUPITER_DB env or store/jupiter.db)")
    args = ap.parse_args()

    db_path = args.db or _DB_DEFAULT
    label   = args.label or args.slug.upper()

    print(f"[config] db={db_path}  simd-dir={args.simd_dir}")

    if args.status == "live":
        confirm = input(
            f"WARNING: status=live will start real trading for '{args.slug}'. "
            "Type the slug to confirm: "
        ).strip()
        if confirm != args.slug:
            print("[abort] slug did not match. No changes made.")
            sys.exit(1)

    migrate(
        simd_dir=args.simd_dir,
        slug=args.slug,
        label=label,
        mint=args.mint,
        decimals=args.decimals,
        status=args.status,
        db_path=db_path,
    )


if __name__ == "__main__":
    main()
