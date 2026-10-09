"""test_golden_parity.py — Golden-vector parity harness (#8).

Replays vectors generated from SIMD's pure engines through Jupiter's engines
and asserts identical decisions (modulo documented divergences).

Run: python3 tests/test_golden_parity.py
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine import grid_decision, price_gate
from vol_engine import realized_vol, vol_steps

VECTORS_PATH = os.path.join(os.path.dirname(__file__), "golden", "vectors_v1.json")

FAILURES = []


def check(name, actual, expected, tolerance=None):
    if tolerance is not None and isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        if abs(actual - expected) <= tolerance:
            print(f"  ok  {name}")
            return
    if actual != expected:
        FAILURES.append(f"FAIL {name}: got {actual!r}, expected {expected!r}")
    else:
        print(f"  ok  {name}")


def check_close(name, actual, expected, rel_tol=1e-6):
    if actual is None and expected is None:
        print(f"  ok  {name}")
        return
    if actual is None or expected is None:
        FAILURES.append(f"FAIL {name}: got {actual!r}, expected {expected!r}")
        return
    if math.isclose(actual, expected, rel_tol=rel_tol):
        print(f"  ok  {name}")
    else:
        FAILURES.append(f"FAIL {name}: got {actual!r}, expected {expected!r} (rel_tol={rel_tol})")


def simd_lot_to_jupiter(lot):
    """Map SIMD lot fields to Jupiter lot fields."""
    return {
        "id": lot.get("id", 0),
        "tokens": lot["tok"],
        "cost": lot["cost"],
        "buy_price": lot["price"],
        "origin": lot.get("origin", "grid"),
        "trail_armed": lot.get("trail_armed", 0),
        "trail_peak": lot.get("trail_peak", 0.0),
        "levels_to": None,
    }


def run_grid_vector(v):
    """Run a grid vector through Jupiter's engine."""
    inp = v["inputs"]
    exp = v["expected"]
    name = v["name"]

    jupiter_lots = [simd_lot_to_jupiter(l) for l in inp["lots"]]

    kwargs = {}
    if "sell_step" in inp:
        kwargs["sell_step"] = inp["sell_step"]
    if "last_sell" in inp:
        kwargs["last_sell"] = inp["last_sell"]
    if "rebuy_gap_pct" in inp:
        kwargs["rebuy_gap_pct"] = inp["rebuy_gap_pct"]
    if "sell_trail" in inp:
        kwargs["sell_trail"] = inp["sell_trail"]
    if "sell_trail_pct" in inp:
        kwargs["sell_trail_pct"] = inp["sell_trail_pct"]

    result = grid_decision(
        inp["price"], inp["ref"], jupiter_lots,
        inp["buy_step"], inp["lot_usd"], inp["ceiling"], inp["cash"],
        fee=0.0,  # SIMD applies fees externally; for parity, disable Jupiter's inline fee
        **kwargs,
    )

    check(f"grid.{name}.buy_usd", result["buy_usd"], exp["buy_usd"])
    check_close(f"grid.{name}.new_ref", result["new_ref"], exp["new_ref"])
    check(f"grid.{name}.n_sells", len(result["sells"]), exp["n_sells"])
    check(f"grid.{name}.n_trail", len(result["trail_updates"]), exp["n_trail_updates"])

    if exp["sell_indices"]:
        actual_indices = []
        for s in result["sells"]:
            for i, jl in enumerate(jupiter_lots):
                if s is jl:
                    actual_indices.append(i)
                    break
        check(f"grid.{name}.sell_indices", actual_indices, exp["sell_indices"])


def run_gate_vector(v):
    """Run a price_gate vector through Jupiter's engine."""
    inp = v["inputs"]
    exp = v["expected"]
    name = v["name"]

    accepted, new_cand, new_cand_n = price_gate(
        inp["price"], inp["last"], inp["max_step_pct"],
        inp["cand"], inp["cand_n"],
    )

    check_close(f"gate.{name}.accepted", accepted, exp["accepted"])
    check_close(f"gate.{name}.cand", new_cand, exp["cand"])
    check(f"gate.{name}.cand_n", new_cand_n, exp["cand_n"])


def run_vol_vector(v):
    """Run a vol_engine vector through Jupiter's engine."""
    inp = v["inputs"]
    exp = v["expected"]
    name = v["name"]

    rv = realized_vol(inp["prices"], inp["now"])
    check_close(f"vol.{name}.realized_vol", rv, exp["realized_vol"])

    if exp["steps"] is not None:
        vs = vol_steps(rv, inp["k_buy"], inp["k_sell"],
                       inp["buy_min"], inp["buy_max"],
                       inp["sell_min"], inp["sell_max"])
        if vs is None:
            FAILURES.append(f"FAIL vol.{name}.steps: got None, expected {exp['steps']}")
        else:
            check_close(f"vol.{name}.buy_step", vs[0], exp["steps"][0])
            check_close(f"vol.{name}.sell_step", vs[1], exp["steps"][1])
    else:
        vs = vol_steps(rv, inp["k_buy"], inp["k_sell"],
                       inp["buy_min"], inp["buy_max"],
                       inp["sell_min"], inp["sell_max"])
        check(f"vol.{name}.steps_none", vs, None)


def main():
    print("=== test_golden_parity.py ===")

    if not os.path.exists(VECTORS_PATH):
        print(f"Golden vectors not found: {VECTORS_PATH}")
        print("Run: python3 tests/gen_golden_vectors.py")
        sys.exit(1)

    with open(VECTORS_PATH) as f:
        data = json.load(f)

    print(f"Vectors version: {data['version']}, hash: {data['hash']}")
    print(f"Documented divergences: {len(data['divergences'])}")
    for div in data["divergences"]:
        print(f"  [{div['engine']}] {div['description'][:80]}...")
    print()

    vectors = data["vectors"]
    for v in vectors:
        if v["engine"] == "grid":
            run_grid_vector(v)
        elif v["engine"] == "price_gate":
            run_gate_vector(v)
        elif v["engine"] == "vol":
            run_vol_vector(v)

    print()
    if FAILURES:
        print(f"\n{'='*60}")
        for f in FAILURES:
            print(f)
        print(f"\nFAILED: {len(FAILURES)} parity check(s)")
        sys.exit(1)
    else:
        total = sum(1 for _ in vectors)
        print(f"All {total} golden vectors passed parity checks.")


if __name__ == "__main__":
    main()
