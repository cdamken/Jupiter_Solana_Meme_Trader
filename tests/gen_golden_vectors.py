#!/usr/bin/env python3
"""gen_golden_vectors.py — Generate golden vectors from SIMD's pure engines.

Runs SIMD's grid_engine, price_gate, vol_engine against a matrix of scenarios
and serializes the inputs + decisions as JSON. Jupiter's parity test replays
these through its own engines and asserts identical decisions.

Usage:
    python3 tests/gen_golden_vectors.py [--simd-dir ~/damkencloud/Claude/SIMD/Code]

Output: tests/golden/vectors_v1.json
"""
import argparse
import importlib.util
import json
import os
import sys
import hashlib


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gen_grid_vectors(grid_engine):
    """Generate grid_decision vectors from SIMD."""
    vectors = []

    # SIMD lots use: tok, cost, price, origin, trail_armed, trail_peak
    def lot(tok, cost, price, origin="grid", trail_armed=0, trail_peak=0.0, lot_id=None):
        d = {"tok": tok, "cost": cost, "price": price, "origin": origin,
             "trail_armed": trail_armed, "trail_peak": trail_peak}
        if lot_id is not None:
            d["id"] = lot_id
        return d

    scenarios = [
        # (name, price, ref, lots, buy_step, lot_usd, ceiling, cash, kwargs)
        ("no_ref_first_tick", 1.0, None, [], 5.0, 25.0, None, 1000.0, {}),
        ("hold_in_range", 1.0, 1.0, [], 5.0, 25.0, None, 1000.0, {}),
        ("buy_signal", 0.94, 1.0, [], 5.0, 25.0, None, 1000.0, {}),
        ("buy_signal_no_cash", 0.94, 1.0, [], 5.0, 25.0, None, 10.0, {}),
        ("sell_one_lot", 1.10, 1.0, [lot(100, 100, 1.0)], 5.0, 25.0, None, 1000.0, {}),
        ("sell_not_enough_gain", 1.04, 1.0, [lot(100, 100, 1.0)], 5.0, 25.0, None, 1000.0, {}),
        ("ceiling_blocks_buy", 0.90, 1.0, [], 5.0, 25.0, 0.85, 1000.0, {}),
        ("ceiling_allows_buy", 0.90, 1.0, [], 5.0, 25.0, 0.95, 1000.0, {}),
        ("ref_rises", 1.10, 1.0, [], 5.0, 25.0, None, 1000.0, {}),
        ("multi_lot_sell", 1.10, 1.0,
         [lot(50, 50, 1.0), lot(75, 75, 1.0)], 5.0, 25.0, None, 1000.0, {}),
        ("asymmetric_steps", 0.94, 1.0,
         [lot(100, 100, 1.0)], 5.0, 25.0, None, 1000.0,
         {"sell_step": 8.0}),
        ("asymmetric_sell_not_reached", 1.06, 1.0,
         [lot(100, 100, 1.0)], 5.0, 25.0, None, 1000.0,
         {"sell_step": 8.0}),
        ("rebuy_gap_blocks", 0.96, 1.0, [], 5.0, 25.0, None, 1000.0,
         {"last_sell": 1.0, "rebuy_gap_pct": 5.0}),
        ("rebuy_gap_allows", 0.94, 1.0, [], 5.0, 25.0, None, 1000.0,
         {"last_sell": 1.0, "rebuy_gap_pct": 5.0}),
        ("price_zero", 0.0, 1.0, [], 5.0, 25.0, None, 1000.0, {}),
        ("price_negative", -1.0, 1.0, [], 5.0, 25.0, None, 1000.0, {}),
    ]

    for name, price, ref, lots, buy_step, lot_usd, ceiling, cash, kwargs in scenarios:
        result = grid_engine.grid_decision(
            price, ref, lots, buy_step, lot_usd, ceiling, cash, **kwargs)
        vectors.append({
            "name": name,
            "engine": "grid",
            "inputs": {
                "price": price, "ref": ref,
                "lots": lots, "buy_step": buy_step,
                "lot_usd": lot_usd, "ceiling": ceiling,
                "cash": cash, **kwargs,
            },
            "expected": {
                "n_sells": len(result["sells"]),
                "sell_indices": [lots.index(s) for s in result["sells"]],
                "buy_usd": result["buy_usd"],
                "new_ref": result["new_ref"],
                "n_trail_updates": len(result["trail_updates"]),
            },
        })

    return vectors


def gen_gate_vectors(price_gate_mod):
    """Generate price_gate vectors from SIMD."""
    vectors = []

    scenarios = [
        ("accept_normal", 1.0, 0.98, 50.0, 0, 0),
        ("reject_spike", 2.0, 1.0, 50.0, 0, 0),
        ("no_baseline", 1.0, 0, 50.0, 0, 0),
        ("negative_price", -1.0, 1.0, 50.0, 0, 0),
        ("zero_price", 0.0, 1.0, 50.0, 0, 0),
        ("consensus_building", 2.0, 1.0, 50.0, 2.0, 3),
        ("consensus_reached", 2.0, 1.0, 50.0, 2.0, 4),
        ("new_candidate", 3.0, 1.0, 50.0, 2.0, 2),
        ("exact_threshold", 1.5, 1.0, 50.0, 0, 0),
        ("just_over_threshold", 1.51, 1.0, 50.0, 0, 0),
    ]

    for name, price, last, max_step, cand, cand_n in scenarios:
        raw = price_gate_mod.gate(price, last, max_step, cand, cand_n)
        # SIMD returns a string: "reject CAND CAND_N" or the price
        if raw.startswith("reject"):
            parts = raw.split()
            accepted = None
            out_cand = float(parts[1])
            out_cand_n = int(parts[2])
        else:
            accepted = float(raw)
            out_cand = 0.0
            out_cand_n = 0

        vectors.append({
            "name": name,
            "engine": "price_gate",
            "inputs": {
                "price": price, "last": last, "max_step_pct": max_step,
                "cand": cand, "cand_n": cand_n,
            },
            "expected": {
                "accepted": accepted,
                "cand": out_cand,
                "cand_n": out_cand_n,
            },
        })

    return vectors


def gen_vol_vectors(vol_engine_mod):
    """Generate vol_engine vectors from SIMD."""
    vectors = []

    now = 1700000000.0
    # Generate price pairs at 1-minute intervals
    base_prices = [
        (now - 120 * 3600 + i * 60, 1.0 + 0.01 * (i % 10 - 5))
        for i in range(200)
    ]

    rv = vol_engine_mod.realized_vol(base_prices, now)
    vs = vol_engine_mod.vol_steps(rv, 0.20, 1.30, 2.0, 10.0, 6.0, 25.0)

    vectors.append({
        "name": "basic_vol_calc",
        "engine": "vol",
        "inputs": {
            "prices": base_prices,
            "now": now,
            "k_buy": 0.20, "k_sell": 1.30,
            "buy_min": 2.0, "buy_max": 10.0,
            "sell_min": 6.0, "sell_max": 25.0,
        },
        "expected": {
            "realized_vol": round(rv, 8) if rv is not None else None,
            "steps": [round(vs[0], 8), round(vs[1], 8)] if vs else None,
        },
    })

    # Flat market (low vol)
    flat_prices = [(now - 120 * 3600 + i * 60, 1.0) for i in range(200)]
    rv_flat = vol_engine_mod.realized_vol(flat_prices, now)
    vs_flat = vol_engine_mod.vol_steps(rv_flat, 0.20, 1.30, 2.0, 10.0, 6.0, 25.0)
    vectors.append({
        "name": "flat_market",
        "engine": "vol",
        "inputs": {
            "prices": flat_prices, "now": now,
            "k_buy": 0.20, "k_sell": 1.30,
            "buy_min": 2.0, "buy_max": 10.0,
            "sell_min": 6.0, "sell_max": 25.0,
        },
        "expected": {
            "realized_vol": round(rv_flat, 8) if rv_flat is not None else None,
            "steps": [round(vs_flat[0], 8), round(vs_flat[1], 8)] if vs_flat else None,
        },
    })

    # Too few data points
    short_prices = [(now - 60, 1.0), (now, 1.01)]
    rv_short = vol_engine_mod.realized_vol(short_prices, now)
    vectors.append({
        "name": "too_few_points",
        "engine": "vol",
        "inputs": {
            "prices": short_prices, "now": now,
            "k_buy": 0.20, "k_sell": 1.30,
            "buy_min": 2.0, "buy_max": 10.0,
            "sell_min": 6.0, "sell_max": 25.0,
        },
        "expected": {
            "realized_vol": round(rv_short, 8) if rv_short is not None else None,
            "steps": None,
        },
    })

    return vectors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simd-dir",
                        default=os.path.expanduser("~/damkencloud/Claude/SIMD/Code"))
    args = parser.parse_args()

    simd = args.simd_dir
    if not os.path.isdir(simd):
        print(f"SIMD directory not found: {simd}", file=sys.stderr)
        sys.exit(1)

    grid_engine = load_module("grid_engine", os.path.join(simd, "grid_engine.py"))
    price_gate = load_module("price_gate", os.path.join(simd, "price_gate.py"))
    vol_engine = load_module("vol_engine", os.path.join(simd, "vol_engine.py"))

    all_vectors = []
    all_vectors.extend(gen_grid_vectors(grid_engine))
    all_vectors.extend(gen_gate_vectors(price_gate))
    all_vectors.extend(gen_vol_vectors(vol_engine))

    content = json.dumps(all_vectors, indent=2)
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]

    output = {
        "version": "v1",
        "generated_from": "SIMD pure engines",
        "hash": content_hash,
        "divergences": [
            {
                "engine": "grid",
                "description": "Jupiter grid_decision includes an inline fee gate (net_pct > 0 after 1.5% fee). SIMD applies fees externally in trader.py/sellable_lots. A SIMD sell at exactly +5% step would pass SIMD but fail Jupiter's fee gate.",
                "resolution": "deliberate improvement — Jupiter is stricter (safer)"
            },
            {
                "engine": "grid",
                "description": "Jupiter grid_decision adds sell_policy ordering (fifo/lifo/highest/cheapest/gain). SIMD returns sells in lot-book iteration order.",
                "resolution": "deliberate extension — Jupiter adds lot ordering, does not change which lots qualify"
            },
        ],
        "vectors": all_vectors,
    }

    out_path = os.path.join(os.path.dirname(__file__), "golden", "vectors_v1.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Generated {len(all_vectors)} vectors -> {out_path}")
    print(f"  grid: {sum(1 for v in all_vectors if v['engine'] == 'grid')}")
    print(f"  price_gate: {sum(1 for v in all_vectors if v['engine'] == 'price_gate')}")
    print(f"  vol: {sum(1 for v in all_vectors if v['engine'] == 'vol')}")
    print(f"  hash: {content_hash}")


if __name__ == "__main__":
    main()
