"""engine.py -- Pure grid/lot decision engine (no I/O, no DB, no network).

Ported from SIMD's trader.py and grid_engine.py. Runs identically in backtests
and production. All functions are PURE: no I/O, no state, no side effects.

Hard rules enforced here:
- Never sell at a loss: a lot only appears in `sells` if net price (after fee)
  exceeds the lot's buy_price (the `cost` field is what matters, but buy_price
  per-token is the gate).
- A ceiling blocks new buys above it (relative ceiling from price_window percentile).
- Rebuy gap: no buy if price is within `rebuy_gap_pct`% above the last sell.

Jupiter field names (lot dicts):
  id, coin_id, tokens, cost, buy_price, ts, origin, levels_to, group_id,
  trail_armed, trail_peak.
"""
from __future__ import annotations
import math

# Minimum swap value (USD). Below this the on-chain execution will reject.
# Matches SIMD's MIN_TRADE_USD = 1.0.
MIN_TRADE_USD = 1.0


# ---------------------------------------------------------------------------
# Lot-book helpers (ported from SIMD trader.py, adapted to Jupiter field names)
# ---------------------------------------------------------------------------

def lots_tokens(lots: list[dict]) -> float:
    """Total tokens held across all open lots."""
    return sum(float(l.get("tokens", 0.0)) for l in lots)


def lots_cost(lots: list[dict]) -> float:
    """Total USDC deployed (cost basis) across all open lots."""
    return sum(float(l.get("cost", 0.0)) for l in lots)


def lots_value(lots: list[dict], price: float) -> float:
    """Mark-to-market value of all open lots at `price`."""
    return lots_tokens(lots) * price


def is_dust(lot: dict, max_usd: float | None = None) -> bool:
    """True for a lot whose cost is below the tradeable minimum.
    A dust lot cannot be sold on-chain on its own (swaps < $1 are rejected).
    `max_usd` overrides the MIN_TRADE_USD constant when provided."""
    return float(lot.get("cost", 0.0)) < (MIN_TRADE_USD if max_usd is None else max_usd)


def lot_below_min(usd: float, desired_usd: float, fraction: float) -> bool:
    """True when available cash clamped a buy below `fraction` of the intended
    lot size -- a dust remainder to skip rather than fill (huge proportional
    fees, wastes a slot). `fraction <= 0` disables (fail-open). PURE."""
    return fraction > 0 and desired_usd > 0 and usd < fraction * desired_usd


def is_reserve_buy(price: float, floor_zone: float | None) -> bool:
    """True when a buy happens inside the floor zone -- marks the lot as a
    reserve (prearmed) lot for future pair/group selling. Pure marker, no
    gate of its own. Ported from SIMD is_reserve_buy (#185)."""
    return floor_zone is not None and price is not None and price <= floor_zone


def reserve_summary(lots: list[dict]) -> tuple[int, float, float]:
    """(n_lots, total_cost_usd, avg_cost_per_token) of all reserve lots.
    Reserve lots have origin='reserve'. PURE."""
    res = [l for l in lots if l.get("origin") == "reserve"]
    n = len(res)
    cost = sum(float(l.get("cost", 0.0)) for l in res)
    tok = sum(float(l.get("tokens", 0.0)) for l in res)
    return n, cost, (cost / tok if tok > 0 else 0.0)


def pairing_reserved_ids(lots: list[dict]) -> frozenset[int]:
    """Database IDs of lots reserved for a combined pair/group sale.
    A lot is reserved if it is a leveler (has levels_to set pointing at
    another lot in this book) or is the target of such a leveler.
    Reserved lots must NOT be sold solo by the grid; they exit only in
    the combined sale. PURE. Uses stable DB ids, not Python memory addresses."""
    leveler_target_ids = {l["levels_to"] for l in lots if l.get("levels_to") is not None}
    leveler_own_ids    = {l["id"]        for l in lots if l.get("levels_to") is not None}
    return frozenset(leveler_target_ids | leveler_own_ids)


def sellable_lots(lots: list[dict], price: float,
                  fee: float = 0.0, threshold_pct: float = 0.0) -> list[dict]:
    """Lots with NET profit (after fees) >= threshold_pct% -- never at a loss.
    Applies fee to the effective price first (SIMD's correct formulation, not
    the approx gain - 2*fee). `threshold_pct=0` = any net profit. Cost-0 lots
    (free tokens from a prior partial sell) are always sellable. PURE."""
    pn = price * (1.0 - fee)
    out = []
    for l in lots:
        bp = float(l.get("buy_price", 0.0))
        if pn <= 0:
            continue
        if bp <= 0:
            # cost-0/price-0 lot: sellable at any positive price
            out.append(l)
            continue
        if pn > bp and (pn - bp) / bp * 100.0 >= threshold_pct:
            out.append(l)
    return out


def select_lots(lots: list[dict], price: float, policy: str,
                fee: float = 0.0, threshold_pct: float = 0.0,
                origin_only: str | None = None,
                skip_reserved: bool = False,
                skip_dust: bool = False) -> list[dict]:
    """Sellable lots ordered by `policy`. Gates:
      - `origin_only`: only lots with that exact origin field.
      - `skip_reserved`: exclude lots in a pairing group (levelers + their targets).
      - `skip_dust`: exclude lots below MIN_TRADE_USD (can't swap alone on-chain).
    Policies: 'fifo', 'lifo', 'highest' (buy_price), 'cheapest', 'gain'. PURE."""
    pool = lots if origin_only is None else [l for l in lots if l.get("origin") == origin_only]
    if skip_dust:
        pool = [l for l in pool if float(l.get("tokens", 0.0)) * price >= MIN_TRADE_USD]
    if skip_reserved:
        reserved = pairing_reserved_ids(lots)
        pool = [l for l in pool if l.get("id") not in reserved]
    v = sellable_lots(pool, price, fee, threshold_pct)
    if policy == "fifo":
        return list(v)
    if policy == "lifo":
        return list(reversed(v))
    if policy == "highest":
        return sorted(v, key=lambda l: -float(l.get("buy_price", 0.0)))
    if policy == "cheapest":
        return sorted(v, key=lambda l: float(l.get("buy_price", 0.0)))
    if policy == "gain":
        def _gain_key(l):
            bp = float(l.get("buy_price", 0.0))
            if bp <= 0:
                return float("-inf")   # cost-0 lots: max gain, sort first
            return -(price - bp) / bp
        return sorted(v, key=_gain_key)
    raise ValueError(f"Unknown SELL_POLICY: {policy!r}. Valid: fifo, lifo, highest, cheapest, gain")


def starter_should_buy(lots: list[dict], cash: float, price: float,
                       ceiling: float | None, last_sell_ts: float | None,
                       now: float, cfg: dict) -> bool:
    """True when the book is empty (or all dust) and we should buy one starter
    lot immediately -- no waiting for a GRID_STEP dip -- so there is always
    minimum exposure to sell into a rise. Ported from SIMD #240. PURE.
    cfg keys read: STARTER_LOT (bool), LOT_USD (float), STARTER_COOLDOWN_H (float)."""
    if not cfg.get("STARTER_LOT"):
        return False
    if any(not is_dust(l) for l in lots):
        return False   # a real lot already exists
    if cash < float(cfg.get("LOT_USD", 25.0)):
        return False
    if price is None or price <= 0 or math.isnan(price):
        return False
    if ceiling is not None and ceiling > 0 and price > ceiling:
        return False   # starter respects the ceiling
    cd = float(cfg.get("STARTER_COOLDOWN_H", 0) or 0)
    if cd > 0 and last_sell_ts is not None and now - last_sell_ts < cd * 3600:
        return False   # anti-churn cooldown
    return True


# ---------------------------------------------------------------------------
# Grid decision (updated to use corrected SIMD sellable_lots logic)
# ---------------------------------------------------------------------------

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
    sell_policy: str = "fifo",
    skip_reserved: bool = False,
) -> dict:
    """One grid tick.

    Args:
        price:         Current price in USD/token.
        ref:           Last accepted reference price (last buy or initial price).
                       None = no baseline yet, accept any price.
        lots:          Open lot dicts with keys: id, buy_price, tokens, cost,
                       trail_armed (0/1), trail_peak (float), origin, levels_to.
        buy_step:      Buy trigger: buy when price drops `buy_step`% below ref.
        lot_usd:       USD to spend on each buy.
        ceiling:       Price ceiling; no new buys above this. None = no ceiling.
        cash:          Available USDC (after any pending reserves).
        sell_step:     Sell trigger (% above buy_price). Falls back to buy_step.
        last_sell:     Price of the last executed sell (for rebuy gap).
        rebuy_gap_pct: Block new buys when price < last_sell * (1 - gap/100).
        sell_trail:    Enable trailing sell (arm+peak tracking).
        sell_trail_pct: Trailing pull-back percentage to trigger a trail sell.
        fee:           One-way fee fraction (default 1.5%; applied to effective price).
        sell_policy:   Lot ordering policy (fifo/lifo/highest/cheapest/gain).
        skip_reserved: Exclude lots paired via levels_to from solo grid sells.

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
    # Two separate gates (matching SIMD's architecture):
    # 1. No-loss gate: effective price after fee must exceed buy_price (hard rule).
    # 2. Sell step: raw gain >= step% (the price target, independent of fees).
    # The execution layer applies the actual fee; the decision engine uses raw prices
    # for step checks, exactly as SIMD does in _grid_signals (fee=0 to sellable_lots).
    reserved = pairing_reserved_ids(lots) if skip_reserved else frozenset()
    for lot in lots:
        bp = float(lot.get("buy_price", 0.0))
        if bp <= 0:
            continue

        pn = price * (1.0 - fee)          # effective price net of fee (no-loss gate)
        if pn <= bp:
            continue                       # hard rule: never at a loss after fees

        gain_pct = (price - bp) / bp * 100.0  # raw gain (sell step gate)

        if sell_trail and lot.get("trail_armed"):
            peak = float(lot.get("trail_peak", price))
            new_peak = max(peak, price)
            pullback = (new_peak - price) / new_peak * 100.0 if new_peak > 0 else 0.0
            if price >= new_peak:
                trail_updates.append((lot, 1, new_peak))
            elif pullback >= sell_trail_pct:
                if lot.get("id") not in reserved:
                    sells.append(lot)
            else:
                trail_updates.append((lot, 1, new_peak))
        elif gain_pct >= step:
            if sell_trail:
                trail_updates.append((lot, 1, price))  # arm the trail
            elif lot.get("id") not in reserved:
                sells.append(lot)

    # ---- buy pass ----
    buy_usd = 0.0
    new_ref = ref

    # Rebuy gap: skip buy if price hasn't pulled back enough from last sell
    if last_sell and last_sell > 0 and rebuy_gap_pct > 0:
        if price >= last_sell * (1.0 - rebuy_gap_pct / 100.0):
            return {"sells": sells, "buy_usd": 0.0, "new_ref": ref,
                    "trail_updates": trail_updates}

    # Ceiling blocks new buys
    if ceiling and price >= ceiling:
        return {"sells": sells, "buy_usd": 0.0, "new_ref": ref,
                "trail_updates": trail_updates}

    if ref is None:
        new_ref = price   # first tick: set ref, no buy
    elif price <= ref * (1.0 - buy_step / 100.0):
        if cash >= lot_usd:
            buy_usd = lot_usd
            new_ref = price   # ref follows each buy downward

    return {
        "sells": sells,
        "buy_usd": buy_usd,
        "new_ref": new_ref,
        "trail_updates": trail_updates,
    }


# ---------------------------------------------------------------------------
# Buy floor (#871 from SIMD)
# ---------------------------------------------------------------------------

def buy_floor_blocks(lot_usd: float, cash_usd: float, floor_usd: float | None,
                     is_crash: bool = False, crash_fraction: float = 1.0) -> bool:
    """True if a buy of `lot_usd` must be SKIPPED because it would pull the
    shared cash under the protected floor. The crash dip gets a softened floor
    (floor_usd * crash_fraction). PURE."""
    if floor_usd is None or floor_usd <= 0:
        return False
    eff_floor = floor_usd * (crash_fraction if is_crash else 1.0)
    return (cash_usd - lot_usd) < eff_floor


def floor_zone_threshold(mode: str, prices: list[float], pctl: float,
                         window_h: float = 48, band_pct: float = 5.0,
                         min_points: int = 60) -> float | None:
    """Price threshold for the floor zone (cadence + reserve marking). PURE.
    Modes:
      'pctl': FLOOR_PCTL percentile of prices (same as relative_ceiling).
      'min48h': min of the last window_h hours * (1 + band_pct/100).
    Returns None when not enough data (zone inactive, fail-open)."""
    if mode == "min48h":
        pts = [p for p in (prices or []) if p and p > 0]
        n = int(window_h * 60)
        tail = pts[-n:] if n > 0 else pts
        if len(tail) < min_points:
            return None
        return min(tail) * (1 + band_pct / 100.0)
    return relative_ceiling(prices, pctl)


def floor_cadence_decision(in_zone: bool, now: float, ep: dict | None,
                           last_buy_ts: float | None, cash: float,
                           cfg: dict) -> tuple[bool, dict | None]:
    """Cadence buy in a long floor: guarantees the reserve keeps growing even
    when the price lateralizes without hitting the next grid step. PURE.

    ep = {"start_ts": float, "lots": int} or None.
    cfg keys: FLOOR_CADENCE (bool), FLOOR_CADENCE_N_H, FLOOR_CADENCE_M_H,
              FLOOR_CADENCE_MAX, LOT_USD.
    Returns (buy_bool, new_ep)."""
    if not in_zone:
        return False, None
    ep = dict(ep) if ep else {"start_ts": now, "lots": 0}
    if not cfg.get("FLOOR_CADENCE"):
        return False, ep
    n_h = float(cfg.get("FLOOR_CADENCE_N_H", 2))
    m_h = float(cfg.get("FLOOR_CADENCE_M_H", 6))
    cap = int(cfg.get("FLOOR_CADENCE_MAX", 2))
    lot_usd = float(cfg.get("LOT_USD", 25))
    m_ok = last_buy_ts is None or now - last_buy_ts >= m_h * 3600
    ready = (now - ep["start_ts"] >= n_h * 3600
             and m_ok
             and ep["lots"] < cap
             and cash >= lot_usd)
    if ready:
        ep["lots"] += 1
    return ready, ep


def floor_reserve_cap_reached(lots: list[dict], cap: int | None) -> bool:
    """True when the live reserve lot count has reached the cap. PURE.
    cap <= 0 or None disables the check."""
    if not cap or cap <= 0:
        return False
    return sum(1 for l in (lots or []) if l.get("origin") == "reserve") >= cap


# ---------------------------------------------------------------------------
# Prebuy (#314 from SIMD prebuy_engine.py)
# ---------------------------------------------------------------------------

QUIET_MIN_SPAN_FRACTION = 0.5


def quiet_regime(hist: list[tuple[float, float]], now: float,
                 band_pct: float, window_h: float) -> bool:
    """True when the market is FLAT/quiet: all prices in the last `window_h`
    hours fit in a peak-to-trough band of `band_pct`%. `hist` is (ts, price).
    Requires >= 3 valid points spanning at least half the window. PURE."""
    since = now - window_h * 3600.0
    pts = [(t, p) for (t, p) in (hist or [])
           if isinstance(t, (int, float)) and isinstance(p, (int, float))
           and t >= since and p > 0]
    if len(pts) < 3:
        return False
    ts = [t for (t, _) in pts]
    if (max(ts) - min(ts)) < window_h * 3600.0 * QUIET_MIN_SPAN_FRACTION:
        return False
    values = [p for (_, p) in pts]
    lo, hi = min(values), max(values)
    if not (lo > 0):
        return False
    return (hi / lo - 1.0) * 100.0 <= band_pct


def prebuy_initial_state() -> dict:
    """A fresh prebuy episode state."""
    return {"lots": 0, "cooldown_until": 0.0}


def prebuy_decision(price: float, now: float, hist: list[tuple[float, float]],
                    lots: list[dict], cash: float | None, st_pre: dict | None,
                    ceiling: float | None, in_floor_zone: bool,
                    cfg: dict) -> dict:
    """Decide whether to place ONE prebuy lot when the market is flat. PURE.

    Returns {buy_usd, new_st_pre, reason}. The lot enters as origin='prebuy'
    and graduates to 'grid' when price moves far enough (graduate_prebuys).

    cfg keys: PREBUY (bool), PREBUY_BAND_PCT, PREBUY_WINDOW_H, PREBUY_MAX_N,
    PREBUY_COOLDOWN_H, PREBUY_CEIL_TOL, LOT_USD."""
    r = dict(st_pre) if st_pre else prebuy_initial_state()
    no = lambda reason: {"buy_usd": 0.0, "new_st_pre": r, "reason": reason}

    if not cfg.get("PREBUY"):
        return no("off")
    if price is None or not (price > 0):
        return no("bad_price")
    if in_floor_zone:
        return no("floor_zone")

    n_max = int(cfg.get("PREBUY_MAX_N", 2))
    cooldown_h = float(cfg.get("PREBUY_COOLDOWN_H", 24))

    if r.get("lots", 0) >= n_max and now >= r.get("cooldown_until", 0.0):
        r["lots"] = 0

    band_pct = float(cfg.get("PREBUY_BAND_PCT", 4))
    window_h = float(cfg.get("PREBUY_WINDOW_H", 12))

    if not quiet_regime(hist, now, band_pct, window_h):
        return no("not_flat")

    if now < r.get("cooldown_until", 0.0):
        return no("cooldown")
    if r["lots"] >= n_max:
        return no("episode_full")

    lot_usd = float(cfg.get("LOT_USD", 25))
    if cash is not None and cash < lot_usd:
        return no("no_cash")

    ceil_tol = float(cfg.get("PREBUY_CEIL_TOL", 1.10))
    if ceiling is not None and ceiling > 0 and price > ceiling * ceil_tol:
        return no("above_ceiling")

    r["lots"] += 1
    if r["lots"] >= n_max:
        r["cooldown_until"] = now + cooldown_h * 3600.0
    return {"buy_usd": lot_usd, "new_st_pre": r, "reason": "buy"}


def graduate_prebuys(lots: list[dict], price: float,
                     up_pct: float, down_pct: float) -> list[int]:
    """Return IDs of prebuy lots that should graduate to 'grid'. PURE.
    A prebuy lot graduates when price moves far enough from its buy_price:
      - UP:   price >= buy_price * (1 + up_pct/100)
      - DOWN: price <= buy_price * (1 - down_pct/100)
    Fail-safe: empty list on bad price or non-positive thresholds."""
    if price is None or price <= 0 or up_pct <= 0 or down_pct <= 0:
        return []
    graduated = []
    for l in lots:
        if l.get("origin") != "prebuy":
            continue
        bp = float(l.get("buy_price", 0) or 0)
        if bp <= 0:
            continue
        if price >= bp * (1 + up_pct / 100.0) or price <= bp * (1 - down_pct / 100.0):
            graduated.append(l["id"])
    return graduated


# ---------------------------------------------------------------------------
# Pairing (#412 from SIMD: combined group sales for stuck lots)
# ---------------------------------------------------------------------------

PAIRING_MIN_COST_USD = 1.0
PAIRING_COST_TOLERANCE = 0.15


def stuck_lot(lot: dict, price: float, threshold_pct: float) -> bool:
    """True if a lot is 'stuck high': buy_price >= price * (1 + threshold/100),
    meaning it can't sell on its own without a loss. Must have cost above the
    dust floor. Origin-agnostic. PURE."""
    if price <= 0:
        return False
    return (float(lot.get("cost", 0)) >= PAIRING_MIN_COST_USD
            and float(lot.get("buy_price", 0)) >= price * (1 + threshold_pct / 100.0))


def update_stuck_clock(lots: list[dict], price: float, threshold_pct: float,
                       now: float) -> dict[int, float | None]:
    """Compute stuck_since updates for lots. Returns {lot_id: new_stuck_since}
    where None means clear the clock. Does NOT mutate lots — caller persists
    the timestamps in coin_state. PURE."""
    updates = {}
    for l in lots:
        lid = l["id"]
        is_stuck = stuck_lot(l, price, threshold_pct)
        has_clock = l.get("stuck_since") is not None
        if is_stuck and not has_clock:
            updates[lid] = now
        elif not is_stuck and has_clock:
            updates[lid] = None
    return updates


def mature_target(lot: dict, now: float, stuck_days: float) -> bool:
    """A stuck lot becomes a pairing target after stuck_days continuous days. PURE."""
    since = lot.get("stuck_since")
    return since is not None and (now - since) >= stuck_days * 86400


def cost_matched(leveler_cost: float, target_cost: float,
                 tol: float = PAIRING_COST_TOLERANCE) -> bool:
    """True when a leveler's cost is within +/-tol of the target's cost. PURE."""
    if target_cost <= 0:
        return False
    return (1 - tol) * target_cost <= leveler_cost <= (1 + tol) * target_cost


def leveler_eligible(lot: dict, price: float) -> bool:
    """A lot usable as a cheap leveler: real tokens and cost, currently in profit,
    not a prebuy (keeps its own exit), not a reserve lot. PURE."""
    return (float(lot.get("tokens", 0)) > 0
            and float(lot.get("cost", 0)) > 0
            and price > 0
            and float(lot.get("buy_price", 0)) < price
            and lot.get("origin") not in ("prebuy", "reserve"))


def group_shortfall_pct(tokens: float, cost: float, price: float,
                        net_pct: float) -> float:
    """How far (%) price must rise for the group to clear +net_pct over cost.
    Lower is better. PURE."""
    if tokens <= 0 or price <= 0:
        return 0.0
    exit_price = cost * (1 + net_pct / 100.0) / tokens
    return (exit_price / price - 1.0) * 100.0


def repair_grid_links(lots: list[dict], price: float,
                      threshold_pct: float, stuck_days: float,
                      max_levelers: int, pair_profit_pct: float,
                      now: float) -> list[tuple[int, int | None]]:
    """Recompute grid pairing links from scratch. Returns list of
    (lot_id, new_levels_to) updates to apply to the DB. lot_id with
    new_levels_to=None means clear the link. PURE.

    lots must have 'stuck_since' pre-loaded (caller reads from coin_state).
    Only uses 'equal' cost-match mode (SIMD's default)."""
    link_updates: list[tuple[int, int | None]] = []

    old_linked = [l for l in lots if l.get("levels_to") is not None and l.get("grid_pair")]
    for l in old_linked:
        link_updates.append((l["id"], None))

    targets = [l for l in lots
               if stuck_lot(l, price, threshold_pct)
               and mature_target(l, now, stuck_days)]
    if not targets:
        return link_updates

    targets.sort(key=lambda l: float(l.get("buy_price", 0)))

    target_ids = {l["id"] for l in targets}
    pool = [l for l in lots
            if leveler_eligible(l, price) and l["id"] not in target_ids]
    pool.sort(key=lambda l: float(l.get("buy_price", 0)))

    used: set[int] = set()
    for t in targets:
        assigned = 0
        for lev in pool:
            if assigned >= max_levelers:
                break
            if lev["id"] in used:
                continue
            if not cost_matched(float(lev.get("cost", 0)), float(t.get("cost", 0))):
                continue
            link_updates.append((lev["id"], t["id"]))
            used.add(lev["id"])
            assigned += 1

    return link_updates


def eligible_groups(lots: list[dict], price: float,
                    pair_profit_pct: float, fee: float = 0.015
                    ) -> list[list[dict]]:
    """Groups (target + levelers) whose combined net value clears the profit
    floor. Returns list of groups, most expensive target first. PURE."""
    pn = price * (1 - fee)
    groups = []
    for t in lots:
        levelers = [l for l in lots if l.get("levels_to") == t["id"]]
        if not levelers:
            continue
        group = [t] + levelers
        cost = sum(float(l["cost"]) for l in group)
        value = sum(float(l["tokens"]) for l in group) * pn
        if cost > 0 and value >= cost * (1 + pair_profit_pct / 100.0):
            groups.append(group)
    groups.sort(key=lambda g: -float(g[0].get("buy_price", 0)))
    return groups


def revalidate_combined_sale(group: list[dict], effective_price: float,
                             floor_pct: float, fee: float = 0.015) -> bool:
    """Hard rule at group level: combined value at fill must exceed cost,
    and net value must clear floor_pct. PURE."""
    if not group:
        return False
    tokens = sum(float(l["tokens"]) for l in group)
    cost = sum(float(l["cost"]) for l in group)
    if cost <= 0 or tokens <= 0:
        return False
    value = tokens * effective_price * (1 - fee)
    quote_value = tokens * effective_price
    return value > cost and quote_value >= cost * (1 + floor_pct / 100.0)


# ---------------------------------------------------------------------------
# Topup insurance (#772 from SIMD)
# ---------------------------------------------------------------------------

def topup_buy_allowed(on: bool, lot_usd: float, lots: list[dict],
                      bought_this_tick: bool, cash_usd: float) -> bool:
    """Should an insurance top-up lot be bought? PURE.
    Fires only when: on, lot_usd>0, book EMPTY, no other buy this tick,
    enough cash. Ceiling-exempt (that's the whole point). Does NOT move ref."""
    if not on or lot_usd <= 0:
        return False
    if lots:
        return False
    if bought_this_tick:
        return False
    return cash_usd >= lot_usd


# ---------------------------------------------------------------------------
# Gas refill (#567 from SIMD)
# ---------------------------------------------------------------------------

def gas_refill_decision(sol_lamports: int | None, usdc_balance: float,
                        refill_usdc: float, low_sol: float,
                        gas_reserve_lamports: int) -> str:
    """Decide if SOL gas needs refilling. PURE.
    Returns: 'sol_ok', 'no_balance' (can't read), 'no_margin' (too low for swap),
             'no_usdc', or 'refill'."""
    if sol_lamports is None:
        return "no_balance"
    low_lamports = int(low_sol * 1e9)
    if sol_lamports >= low_lamports:
        return "sol_ok"
    if sol_lamports <= gas_reserve_lamports:
        return "no_margin"
    if usdc_balance < refill_usdc:
        return "no_usdc"
    return "refill"


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def relative_ceiling(prices: list[float], percentile: float = 98.0) -> float | None:
    """P`percentile` of the price window. Returns None if fewer than 2 prices.
    Fail-open by design: too few samples -> no ceiling (don't block buys on noise)."""
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


# ---- Batch-sale planners (#968) ----

def cap_batch_plan(plan_lots, price, max_trade_usd, max_lots):
    """Trim a SELL_POLICY-ordered plan to the batch's caps: at most max_lots lots and a
    cumulative value (tokens*price) no greater than max_trade_usd per swap. ALWAYS keeps at
    least the first (most-preferred) lot even if it alone exceeds the cap. Pure (no I/O)."""
    out = list(plan_lots) if max_lots is None else list(plan_lots[:max_lots])
    if max_trade_usd is None or not out:
        return out
    capped = [out[0]]
    running = out[0]["tokens"] * price
    for l in out[1:]:
        v = l["tokens"] * price
        if running + v > max_trade_usd:
            break
        capped.append(l)
        running += v
    return capped


def _lot_gain_pct(lot, price):
    """Gain of a lot at price, for ranking the batch shrink. A cost-0 lot (buy_price <= 0) is
    pure profit -> treated as maximal gain so it is never dropped first."""
    return float("inf") if lot["buy_price"] <= 0 else (price - lot["buy_price"]) / lot["buy_price"] * 100.0


def drop_least_gain_lot(plan_lots, price):
    """Remove exactly the least-gain lot from the plan and return the rest. Never goes below 1
    lot (the batch degrades gracefully to single-lot behavior). Pure."""
    if len(plan_lots) <= 1:
        return list(plan_lots)
    worst = min(range(len(plan_lots)), key=lambda i: _lot_gain_pct(plan_lots[i], price))
    return [l for i, l in enumerate(plan_lots) if i != worst]
