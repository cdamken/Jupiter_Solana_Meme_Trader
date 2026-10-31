#!/usr/bin/env python3
"""tools/migrate_from_simd.py -- Migrate lots between SIMD and Jupiter.

Supports two directions:

  SIMD -> Jupiter (default):
    Reads trader_state.json from a SIMD bot directory, creates the coin in
    Jupiter's DB, and inserts every open lot.

  Jupiter -> SIMD (--to-simd):
    Reads a coin's lots from Jupiter's DB and writes a trader_state.json in
    SIMD format. READ-ONLY against Jupiter's DB.

Usage (SIMD -> Jupiter):
    python3 tools/migrate_from_simd.py \\
        --simd-dir  /home/carlos/simd          \\
        --slug      simd                        \\
        --label     SIMD                        \\
        --mint      SiMDt...                    \\
        --decimals  6                           \\
        [--status   paper]                      \\
        [--db       store/jupiter.db]

Usage (Jupiter -> SIMD):
    python3 tools/migrate_from_simd.py --to-simd \\
        --slug      simd                        \\
        --output-dir /home/carlos/simd          \\
        [--db       store/jupiter.db]           \\
        [--force]

Safety gates:
  - Default status is 'paper'; pass --status live only when you are sure.
  - Coins already present with open lots are never overwritten (SIMD -> Jupiter).
  - Lots with tok <= 0 or cost <= 0 are skipped with a warning.
  - The script is READ-ONLY against the source (SIMD dir or Jupiter DB).
  - --to-simd will NOT overwrite an existing trader_state.json unless --force.
  - No real swaps; only DB reads / file writes.
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

    # First pass: insert lots, recording old SIMD id → new Jupiter id for levels_to linking.
    simd_id_to_jupiter_id: dict[int, int] = {}
    lots_with_levels: list[tuple[int, int]] = []  # (jupiter_lot_id, simd_levels_to_id)

    for i, lot in enumerate(lots):
        tok  = float(lot.get("tok", 0.0))
        cost = float(lot.get("cost", 0.0))
        if tok <= 0 or cost <= 0:
            print(f"[skip] lot[{i}] tok={tok} cost={cost} -- skipped (zero/negative)")
            skipped += 1
            continue

        buy_price   = float(lot.get("price", cost / tok))
        ts          = float(lot.get("ts", now))
        trail_armed = int(bool(lot.get("trail_armed", False)))
        trail_peak  = float(lot.get("trail_peak", 0.0))

        # --- origin resolution ---
        # In SIMD, origin and boolean flags are separate: a lot can have
        # origin='grid' AND reserve=True (floor reserve), or origin='grid' AND
        # prebuy=True (prebuy v2). Jupiter uses origin as the single field.
        # Priority: explicit origin wins if it is not 'grid'; then check flags.
        origin = str(lot.get("origin", "grid"))
        if lot.get("reserve") and origin == "grid":
            origin = "reserve"   # floor-reserve lot; gates reserve_summary + skip-buy
        elif lot.get("prebuy") and origin == "grid":
            origin = "prebuy"    # prebuy v2 durable marker

        # Audit-only flags (no sell-path change): starter, crash, recycle.
        # Jupiter has no columns for these; they are logged as a note only.
        audit_flags = [f for f in ("starter", "crash", "recycle") if lot.get(f)]

        simd_lot_id = lot.get("id")
        lot_id = store.add_lot(coin_id, tok, cost, buy_price, ts, origin)

        if simd_lot_id is not None:
            simd_id_to_jupiter_id[simd_lot_id] = lot_id

        # levels_to: queue for second pass
        simd_levels_to = lot.get("levels_to")
        if simd_levels_to is not None:
            lots_with_levels.append((lot_id, int(simd_levels_to)))

        if trail_armed:
            store.update_lot_trail(lot_id, trail_armed, trail_peak)

        note = ""
        if audit_flags:
            note = f"  [{'+'.join(audit_flags)}]"

        print(
            f"[lot]  id={lot_id:4d}  tok={tok:.4f}  cost=${cost:.2f}"
            f"  price=${buy_price:.6f}  origin={origin}"
            + (f"  trail@{trail_peak:.6f}" if trail_armed else "")
            + note
        )
        inserted += 1
        total_cost += cost

    # Second pass: wire levels_to using the SIMD-id → Jupiter-id map.
    linked = 0
    unresolved = 0
    for jupiter_lot_id, simd_target in lots_with_levels:
        jupiter_target = simd_id_to_jupiter_id.get(simd_target)
        if jupiter_target is None:
            print(f"[warn] levels_to={simd_target} (SIMD id) not found in migrated lots -- skipped")
            unresolved += 1
            continue
        store._c.execute(
            "UPDATE lots SET levels_to = ? WHERE id = ?", (jupiter_target, jupiter_lot_id)
        )
        store._c.commit()
        print(f"[link] lot {jupiter_lot_id} → levels_to {jupiter_target}")
        linked += 1

    if linked or unresolved:
        print(f"[link] {linked} pairing link(s) set, {unresolved} unresolved")

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
        + (f" ({linked} pairing link(s) wired)" if linked else "")
        + ("\n[note] Fields NOT migrated: stuck_since, grid_pair, id (reassigned),"
           " starter/crash/recycle flags (audit-only, no Jupiter column)." if inserted else "")
    )
    if status == "paper":
        print(
            "[note] Status is PAPER. Switch to LIVE via the panel or:\n"
            f"       python3 -c \"import config,sys; sys.path.insert(0,'.'); "
            f"from store.store import Store; s=Store('{db_path}'); "
            f"s.set_coin_status({coin_id}, 'live')\""
        )


# --------------------------------------------------------------------------- #
# Reverse migration: Jupiter -> SIMD                                           #
# --------------------------------------------------------------------------- #

# Inverse of _LOT_KEY_RENAMES (Jupiter key -> SIMD key).
_JUPITER_TO_SIMD_KEY = {v: k for k, v in _LOT_KEY_RENAMES.items() if k != v}
# Value renames reversed (Jupiter value -> SIMD value).
_JUPITER_TO_SIMD_VALUES = {
    field: {v: k for k, v in mapping.items()}
    for field, mapping in _LOT_VALUE_RENAMES.items()
}


def export_to_simd(slug: str, output_dir: str, db_path: str,
                   force: bool = False) -> str:
    """Export a coin's lots from Jupiter DB to a SIMD-format trader_state.json.

    Returns the path to the written file.
    READ-ONLY against the Jupiter DB -- no modifications.
    """
    out_path = os.path.join(output_dir, "trader_state.json")

    # Safety: refuse to overwrite unless --force.
    if os.path.exists(out_path) and not force:
        raise FileExistsError(
            f"{out_path} already exists. Pass --force to overwrite."
        )

    store = Store(db_path)

    coin = store.get_coin(slug)
    if not coin:
        raise ValueError(f"Coin '{slug}' not found in Jupiter DB at {db_path}")

    coin_id = coin["id"]
    lots = store.get_lots(coin_id)
    print(f"[export] coin '{slug}' (id={coin_id}): {len(lots)} lot(s) in Jupiter")

    simd_lots = []
    for lot in lots:
        simd_lot = {
            "tok":            lot["tokens"],
            "usd":            lot["cost"],
            "precio_compra":  lot["buy_price"],
            "ts":             lot["ts"],
        }

        # Map origin back to SIMD naming.
        origin = lot["origin"]
        simd_origin = _JUPITER_TO_SIMD_VALUES.get("origin", {}).get(origin, origin)
        simd_lot["origen"] = simd_origin

        simd_lots.append(simd_lot)

    state = {"lotes": simd_lots}

    os.makedirs(output_dir, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(state, f, indent=2)

    print(f"[export] wrote {len(simd_lots)} lot(s) to {out_path}")
    return out_path


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Direction flag.
    ap.add_argument("--to-simd",   action="store_true",
                    help="Reverse migration: export from Jupiter DB to SIMD JSON")

    # Common.
    ap.add_argument("--slug",      required=True,
                    help="Coin slug in Jupiter (e.g. 'simd')")
    ap.add_argument("--db",        default=None,
                    help="Jupiter DB path (default: JUPITER_DB env or store/jupiter.db)")

    # SIMD -> Jupiter only.
    ap.add_argument("--simd-dir",  default=None,
                    help="Path to the SIMD bot directory containing trader_state.json")
    ap.add_argument("--label",     default="",
                    help="Display label (defaults to slug.upper())")
    ap.add_argument("--mint",      default=None,
                    help="Token mint address")
    ap.add_argument("--decimals",  type=int, default=6,
                    help="Token decimals (6 = pump.fun, 9 = SOL/native SPL)")
    ap.add_argument("--status",    default="paper",
                    choices=("paper", "live", "paused"),
                    help="Initial coin status in Jupiter (default: paper)")

    # Jupiter -> SIMD only.
    ap.add_argument("--output-dir", default=None,
                    help="Directory to write trader_state.json into (--to-simd)")
    ap.add_argument("--force",     action="store_true",
                    help="Overwrite existing trader_state.json (--to-simd)")

    args = ap.parse_args()
    db_path = args.db or _DB_DEFAULT

    if args.to_simd:
        # --- reverse migration ---
        output_dir = args.output_dir
        if not output_dir:
            print("[error] --output-dir is required when using --to-simd")
            sys.exit(1)
        print(f"[config] db={db_path}  output-dir={output_dir}")
        export_to_simd(
            slug=args.slug,
            output_dir=output_dir,
            db_path=db_path,
            force=args.force,
        )
    else:
        # --- forward migration (SIMD -> Jupiter) ---
        if not args.simd_dir:
            print("[error] --simd-dir is required for SIMD -> Jupiter migration")
            sys.exit(1)
        if not args.mint:
            print("[error] --mint is required for SIMD -> Jupiter migration")
            sys.exit(1)

        label = args.label or args.slug.upper()
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
