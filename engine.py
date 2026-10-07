"""engine.py — Pure grid decision engine (no I/O, no DB, no network).

Ported from SIMD's grid_engine.py. Runs identically in backtests and production.
A tick calls grid_decision() with the current price and book state; the scheduler
applies the result (sends swaps, updates the DB).

Hard rules enforced here:
- Never sell at a loss: a lot only appears in `sells` if price > lot.buy_price (net of fee).
- A ceiling blocks new buys above it (relative ceiling from price_window percentile).
- Rebuy gap: no buy if price is within `rebuy_gap_pct`% above the last sell.
"""
from __future__ import annotations
import math


def grid_decision(
    price: float,
    ref: float | None,
    lots: list[dict],
    buy_step: float,
    lot_usd: float,
    ceiling: float | None,
    cash: float,
    *,
    sell_step: float | None = None,
    last_sell: float | None = None,
    rebuy_gap_pct: float = 0.0,
    sell_trail: bool = False,
    sell_trail_pct: float = 2.0,
    fee: float = 0.015,
) -> dict:
    """One grid tick.

    Args:
        price:         Current price in USD/token.
        ref:           Last accepted reference price (last buy or initial price).
                       None = no baseline yet, accept any price.
        lots:          List of open lot dicts with keys: id, buy_price, tokens, cost,
                       trail_armed (0/1), trail_peak (float).
        buy_step:      Buy trigger: buy when price drops `buy_step`% below ref.
        lot_usd:       USD to spend on each buy.
        ceiling:       Price ceiling; no new buys above this. None = no ceiling.
        cash:          Available USDC (after any pending reserves).
        sell_step:     Sell trigger: sell when price rises `sell_step`% above buy_price.
                       Falls back to buy_step if None (symmetric grid).
        last_sell:     Price of the last executed sell (for rebuy gap). None = no gate.
        rebuy_gap_pct: Block new buys when price < last_sell * (1 + gap/100).
        sell_trail:    Enable trailing sell (arm+peak tracking).
        sell_trail_pct: Trailing pull-back percentage to trigger a trail sell.
        fee:           Round-trip fee fraction (default 1.5% per side = 3% round-trip).

    Returns dict with:
        sells:         List of lot dicts ready to sell this tick.
        buy_usd:       USD to spend on a new buy (0 = no buy).
        new_ref:       Updated reference price after a buy (or unchanged).
        trail_updates: List of (lot, trail_armed, trail_peak) to persist.
    """
    if price <= 0 or math.isnan(price):
        return {"sells": [], "buy_usd": 0.0, "new_ref": ref, "trail_updates": []}

    step = sell_step if sell_step is not None else buy_step
    sells: list[dict] = []
    trail_updates: list[tuple] = []

    # ---- sell pass ----
    for lot in lots:
        bp = lot.get("buy_price", lot.get("price", 0.0))
        if bp <= 0:
            continue
        gain_pct = (price - bp) / bp * 100.0
        net_pct = gain_pct - fee * 100.0 * 2     # approx round-trip cost
        if net_pct <= 0:
            continue                              # hard rule: never at a loss

        if sell_trail and lot.get("trail_armed"):
            peak = lot.get("trail_peak", price)
            new_peak = max(peak, price)
            pullback = (new_peak - price) / new_peak * 100.0 if new_peak > 0 else 0.0
            if price >= new_peak:
                trail_updates.append((lot, 1, new_peak))
            elif pullback >= sell_trail_pct:
                sells.append(lot)               # trail triggered
            else:
                trail_updates.append((lot, 1, new_peak))
        elif gain_pct >= step:
            if sell_trail:
                # Arm the trail instead of selling immediately
                trail_updates.append((lot, 1, price))
            else:
                sells.append(lot)

    # ---- buy pass ----
    buy_usd = 0.0
    new_ref = ref

    # Rebuy gap: skip buy if price hasn't pulled back enough from last sell
    if last_sell and last_sell > 0 and rebuy_gap_pct > 0:
        if price >= last_sell * (1 - rebuy_gap_pct / 100.0):
            return {"sells": sells, "buy_usd": 0.0, "new_ref": ref,
                    "trail_updates": trail_updates}

    # Ceiling blocks new buys
    if ceiling and price >= ceiling:
        return {"sells": sells, "buy_usd": 0.0, "new_ref": ref,
                "trail_updates": trail_updates}

    if ref is None:
        # No baseline yet: set ref but don't buy on first tick
        new_ref = price
    elif price <= ref * (1 - buy_step / 100.0):
        # Price dropped enough below ref: trigger a buy
        if cash >= lot_usd:
            buy_usd = lot_usd
            new_ref = price             # ref follows each buy downward

    return {
        "sells": sells,
        "buy_usd": buy_usd,
        "new_ref": new_ref,
        "trail_updates": trail_updates,
    }


def relative_ceiling(prices: list[float], percentile: float = 98.0) -> float | None:
    """P`percentile` of the price window. Returns None if fewer than 2 prices."""
    if len(prices) < 2:
        return None
    s = sorted(prices)
    idx = min(len(s) - 1, int(len(s) * percentile / 100.0))
    return s[idx]


def price_gate(price: float, last: float, max_step_pct: float,
               cand: float = 0.0, cand_n: int = 0,
               consensus_n: int = 5) -> tuple[float | None, float, int]:
    """Reject a price tick that jumps too far from the last accepted price.

    Returns (accepted_price_or_None, new_cand, new_cand_n).
    Re-anchors after `consensus_n` consecutive ticks that agree with each other
    but disagree with `last` (market moved, not a bad tick).
    """
    if price <= 0 or math.isnan(price):
        return None, cand, cand_n
    if last <= 0:
        return price, 0.0, 0        # no baseline yet, always accept
    step = abs(price / last - 1.0) * 100.0
    if step <= max_step_pct:
        return price, 0.0, 0        # within range, accept and reset candidate
    # Outlier: check if it agrees with the running candidate
    if cand > 0 and abs(price / cand - 1.0) * 100.0 <= max_step_pct:
        cand_n += 1
        if cand_n >= consensus_n:
            return price, 0.0, 0    # consensus: re-anchor to the new level
        return None, cand, cand_n   # still building consensus
    # New outlier direction: start a fresh candidate
    return None, price, 1
