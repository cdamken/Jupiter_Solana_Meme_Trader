from __future__ import annotations
"""scheduler.py — Main loop. Ticks all active coins once per interval.

One process. No cron per coin. Adding a coin = INSERT into coins.
Removing a coin = UPDATE coins SET status='paused'.

Usage:
    python3 scheduler.py              # respects DEFAULT_MODE from .env
    python3 scheduler.py --paper      # force paper mode for all coins
    python3 scheduler.py --live       # force live mode (requires LIVE=1 confirmation)
"""
import argparse
import logging
import os
import signal
import sys
import time

import config  # noqa: F401 -- imports validate required env vars, fail-closed
from store.store import Store
import json as _json
from engine import (
    grid_decision, relative_ceiling, price_gate,
    buy_floor_blocks, floor_zone_threshold, floor_cadence_decision,
    floor_reserve_cap_reached, is_reserve_buy,
    prebuy_decision, graduate_prebuys,
    update_stuck_clock, repair_grid_links, eligible_groups,
    revalidate_combined_sale, gas_refill_decision,
    topup_buy_allowed,
    cap_batch_plan, drop_least_gain_lot,
    apply_capital_pct_cap,
)
from vol_engine import realized_vol, vol_steps
from execute import (
    fetch_price, paper_buy, paper_sell, live_buy, live_sell,
    sol_balance_lamports, gas_refill, load_keypair,
)
import alerts as _alerts

def _smtp() -> dict:
    return dict(
        smtp_host=config.SMTP_HOST,
        smtp_port=config.SMTP_PORT,
        smtp_user=config.SMTP_USER,
        smtp_pass=config.SMTP_PASS,
        to=config.ALERT_EMAIL,
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scheduler")

TICK_INTERVAL  = 60          # seconds between ticks (one full coin sweep)
PRICE_WINDOW_S = 14 * 86400  # 14 days for ceiling percentile

_running = True
_no_price_streak: dict[int, int] = {}   # coin_id -> consecutive ticks without price


def _stop(signum, _frame):
    global _running
    log.info("Signal %d received, shutting down after this tick.", signum)
    _running = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


MONEY_CRITICAL_KEYS = ("MAX_CAPITAL_USD", "LOT_USD", "MAX_SLIPPAGE_BPS")


def audit_live_config(cfg: dict, slug: str) -> list[str]:
    """Validate that all money-critical keys are explicitly present for a live coin.
    Returns a list of missing/invalid keys (empty = pass)."""
    missing = []
    for key in MONEY_CRITICAL_KEYS:
        val = cfg.get(key)
        if val is None or str(val).strip() == "":
            missing.append(key)
            continue
        try:
            f = float(val)
            if f <= 0:
                missing.append(key)
        except (TypeError, ValueError):
            missing.append(key)
    return missing


def _cfg_float(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _cfg_bool(cfg: dict, key: str, default: bool = False) -> bool:
    v = cfg.get(key, "")
    if v == "":
        return default
    return str(v).lower() in ("1", "true", "yes", "on")


def tick_coin(store: Store, coin: dict, force_mode: str | None = None):
    """One tick for a single coin. Reads price, decides, executes."""
    coin_id   = coin["id"]
    mint      = coin["mint"]
    mode      = force_mode or coin["status"]    # paper / live
    if mode not in ("paper", "live"):
        return

    cfg = store.get_config(coin_id)

    if mode == "live":
        bad_keys = audit_live_config(cfg, coin["slug"])
        if bad_keys:
            log.error("coin=%d slug=%s: LIVE tick refused, missing/invalid money-critical config: %s",
                      coin_id, coin["slug"], ", ".join(bad_keys))
            _alerts.alert_error(coin["slug"],
                                f"LIVE tick refused: missing config keys: {', '.join(bad_keys)}",
                                **_smtp())
            return

    buy_step      = _cfg_float(cfg, "BUY_STEP_PCT",    4.0)
    sell_step     = _cfg_float(cfg, "SELL_STEP_PCT",   buy_step)
    lot_usd       = _cfg_float(cfg, "LOT_USD",         25.0)
    max_cap_usd   = _cfg_float(cfg, "MAX_CAPITAL_USD", 200.0)
    slippage_bps  = int(_cfg_float(cfg, "MAX_SLIPPAGE_BPS", 150))
    capital_pct   = _cfg_float(cfg, "MAX_CAPITAL_PCT", 0.0)
    if capital_pct > 0:
        fleet_total = store.fleet_deployed_usd() + store.usdc_balance()
        max_cap_usd = apply_capital_pct_cap(max_cap_usd, fleet_total, capital_pct)
    sell_trail    = _cfg_bool(cfg,  "SELL_TRAIL",      False)
    sell_trail_pct= _cfg_float(cfg, "SELL_TRAIL_PCT",  2.0)
    ceil_pct      = _cfg_float(cfg, "CEILING_PERCENTILE", 98.0)
    max_step_pct  = _cfg_float(cfg, "PRICE_GATE_PCT",  30.0)

    # Fetch price (liquidity pick + optional pair pinning, #1030)
    pair_address = cfg.get("PRICE_PAIR_ADDRESS", "").strip() or None
    price = fetch_price(mint, pair_address=pair_address)
    if price is None:
        streak = _no_price_streak.get(coin_id, 0) + 1
        _no_price_streak[coin_id] = streak
        log.warning("coin=%d slug=%s: no price (streak=%d)", coin_id, coin["slug"], streak)
        _alerts.alert_no_price(coin["slug"], streak, **_smtp())
        return
    _no_price_streak[coin_id] = 0

    # Price gate (reject outlier ticks) - all gate state in one tx
    gate_last = store.state_get_float(coin_id, "price_gate_last")
    gate_cand = store.state_get_float(coin_id, "price_gate_cand")
    gate_n    = store.state_get_int(coin_id,   "price_gate_cand_n")
    accepted, gate_cand, gate_n = price_gate(
        price, gate_last, max_step_pct, gate_cand, gate_n,
    )
    if accepted is None:
        store.state_mset_commit(coin_id, {
            "price_gate_cand":   gate_cand,
            "price_gate_cand_n": gate_n,
        })
        log.warning("coin=%d slug=%s: price %.8f rejected by gate (last=%.8f)",
                    coin_id, coin["slug"], price, gate_last)
        return
    price = accepted
    store.state_mset_commit(coin_id, {
        "price_gate_last":   price,
        "price_gate_cand":   gate_cand,
        "price_gate_cand_n": gate_n,
    })
    store.record_price(coin_id, time.time(), price)

    # Relative ceiling
    prices = store.price_window(coin_id, time.time() - PRICE_WINDOW_S)
    ceiling = relative_ceiling(prices, ceil_pct)

    # Lot book
    lots = [dict(r) for r in store.get_lots(coin_id)]

    # Capital check: skip buy if we'd exceed the cap
    cap = store.get_capital(coin_id)
    deployed = cap["deployed_usd"] if cap else 0.0
    cash = store.usdc_balance()
    if deployed + lot_usd > max_cap_usd:
        lot_usd_effective = 0.0         # no more buys for this coin
    else:
        lot_usd_effective = lot_usd

    # Reference price from coin_state (persists across restarts)
    ref_str = store.state_get(coin_id, "ref")
    ref = float(ref_str) if ref_str is not None else None

    last_sell_price = store.state_get_float(coin_id, "last_sell_price") or None
    rebuy_gap_pct  = _cfg_float(cfg, "REBUY_GAP_PCT", 0.0)
    sell_policy     = cfg.get("SELL_POLICY", "fifo")

    # --- Vol-adaptive steps (#250): override buy/sell steps from realized volatility ---
    use_vol = _cfg_bool(cfg, "VOL_ADAPTIVE", False)
    if use_vol:
        vol_k_buy   = _cfg_float(cfg, "VOL_K_BUY", 0.20)
        vol_k_sell  = _cfg_float(cfg, "VOL_K_SELL", 1.30)
        vol_buy_min = _cfg_float(cfg, "BUY_STEP_MIN", 2.0)
        vol_buy_max = _cfg_float(cfg, "BUY_STEP_MAX", 10.0)
        vol_sell_min = _cfg_float(cfg, "SELL_STEP_MIN", 6.0)
        vol_sell_max = _cfg_float(cfg, "SELL_STEP_MAX", 25.0)
        vol_pairs = [(float(r["ts"]), float(r["price"]))
                     for r in store.price_window_rows(coin_id, time.time() - 120 * 3600)]
        rv = realized_vol(vol_pairs, time.time())
        vs = vol_steps(rv, vol_k_buy, vol_k_sell,
                       vol_buy_min, vol_buy_max, vol_sell_min, vol_sell_max)
        if vs is not None:
            buy_step, sell_step = vs
            log.info("coin=%d slug=%s: vol-adaptive steps buy=%.1f%% sell=%.1f%% (vol_1d=%.2f)",
                     coin_id, coin["slug"], buy_step, sell_step, rv)

    decision = grid_decision(
        price, ref, lots, buy_step, lot_usd_effective, ceiling, cash,
        sell_step=sell_step,
        last_sell=last_sell_price,
        rebuy_gap_pct=rebuy_gap_pct,
        sell_trail=sell_trail,
        sell_trail_pct=sell_trail_pct,
        sell_policy=sell_policy,
    )

    ts = time.time()

    # --- Buy floor gate (#871) ---
    use_buy_floor  = _cfg_bool(cfg, "BUY_FLOOR", False)
    buy_floor_usd  = _cfg_float(cfg, "BUY_FLOOR_USD", 0.0)

    if use_buy_floor and decision["buy_usd"] > 0:
        if buy_floor_blocks(decision["buy_usd"], cash, buy_floor_usd):
            log.info("coin=%d slug=%s: buy floor blocked buy (cash=%.2f floor=%.2f lot=%.2f)",
                     coin_id, coin["slug"], cash, buy_floor_usd, decision["buy_usd"])
            decision["buy_usd"] = 0.0
            decision["new_ref"] = ref  # restore ref, don't anchor on a blocked dip

    # --- Floor zone + cadence (#185) ---
    use_floor_rel    = _cfg_bool(cfg, "FLOOR_RELATIVE", False)
    floor_pctl       = _cfg_float(cfg, "FLOOR_PCTL", 25.0)
    floor_zone_mode  = cfg.get("FLOOR_ZONE_MODE", "pctl")
    floor_zone_win_h = _cfg_float(cfg, "FLOOR_ZONE_WINDOW_H", 48.0)
    floor_zone_band  = _cfg_float(cfg, "FLOOR_ZONE_BAND_PCT", 5.0)
    floor_res_max    = int(_cfg_float(cfg, "FLOOR_RESERVE_MAX_LOTS", 0))

    floor_zone_val = None
    in_floor_zone = False
    if use_floor_rel:
        floor_window_s = _cfg_float(cfg, "FLOOR_WINDOW_D", 7.0) * 86400
        floor_prices = store.price_window(coin_id, time.time() - floor_window_s)
        floor_zone_val = floor_zone_threshold(
            floor_zone_mode, floor_prices, floor_pctl,
            floor_zone_win_h, floor_zone_band)
        if floor_zone_val is not None:
            in_floor_zone = price <= floor_zone_val
            store.state_set_commit(coin_id, "floor_zone", floor_zone_val)

    # Floor cadence: buy in a long floor zone even without a grid step
    ep_raw = store.state_get(coin_id, "floor_episode")
    floor_ep = _json.loads(ep_raw) if ep_raw else None
    last_buy_ts_raw = store.state_get(coin_id, "last_buy_ts")
    last_buy_ts = float(last_buy_ts_raw) if last_buy_ts_raw else None

    cad_buy, new_ep = floor_cadence_decision(in_floor_zone, ts, floor_ep, last_buy_ts, cash, cfg)

    if cad_buy and floor_reserve_cap_reached(lots, floor_res_max):
        cad_buy = False
        new_ep = floor_ep  # restore, don't burn quota
        log.info("coin=%d slug=%s: floor reserve cap reached, skipping cadence buy", coin_id, coin["slug"])

    # Buy floor gate also applies to cadence buys
    if cad_buy and use_buy_floor and buy_floor_blocks(lot_usd, cash, buy_floor_usd):
        cad_buy = False
        new_ep = floor_ep
        log.info("coin=%d slug=%s: buy floor blocked cadence buy", coin_id, coin["slug"])

    # Persist floor episode only when cadence didn't fire.
    # When cad_buy=True, the episode is written inside the buy tx instead.
    if not cad_buy and new_ep != floor_ep:
        store.state_set_commit(coin_id, "floor_episode", _json.dumps(new_ep) if new_ep else "")

    # If cadence wants to buy and grid isn't already buying, inject a grid buy
    if cad_buy and decision["buy_usd"] <= 0:
        decision["buy_usd"] = lot_usd
        log.info("coin=%d slug=%s: floor cadence buy triggered (lot=$%.2f)", coin_id, coin["slug"], lot_usd)

    # --- Prebuy (#314): buy in a flat/quiet market ---
    prebuy_fired = False
    if decision["buy_usd"] <= 0 and not cad_buy:
        pre_raw = store.state_get(coin_id, "prebuy_ep")
        pre_ep = _json.loads(pre_raw) if pre_raw else None
        prebuy_window_h = _cfg_float(cfg, "PREBUY_WINDOW_H", 12.0)
        price_hist = [(float(r["ts"]), float(r["price"])) for r in store.price_window_rows(coin_id, ts - prebuy_window_h * 3600)]
        pre_d = prebuy_decision(price, ts, price_hist, lots, cash, pre_ep,
                                ceiling, in_floor_zone, cfg)
        if pre_d["buy_usd"] > 0:
            if use_buy_floor and buy_floor_blocks(pre_d["buy_usd"], cash, buy_floor_usd):
                log.info("coin=%d slug=%s: buy floor blocked prebuy", coin_id, coin["slug"])
                pre_d["buy_usd"] = 0.0
                pre_d["new_st_pre"] = pre_ep
            else:
                decision["buy_usd"] = pre_d["buy_usd"]
                prebuy_fired = True
                log.info("coin=%d slug=%s: prebuy triggered (flat market, lot=$%.2f, reason=%s)",
                         coin_id, coin["slug"], pre_d["buy_usd"], pre_d["reason"])
        if pre_d["new_st_pre"] != pre_ep:
            if not prebuy_fired:
                store.state_set_commit(coin_id, "prebuy_ep", _json.dumps(pre_d["new_st_pre"]))

    # --- Prebuy graduation (#413): graduate prebuy lots before sells ---
    use_prebuy_grad = _cfg_bool(cfg, "PREBUY_GRADUATE", False)
    if use_prebuy_grad:
        grad_up = _cfg_float(cfg, "PREBUY_GRAD_UP_PCT", 6.0)
        grad_down = _cfg_float(cfg, "PREBUY_GRAD_DOWN_PCT", 10.0)
        grad_ids = graduate_prebuys(lots, price, grad_up, grad_down)
        for gid in grad_ids:
            store.update_lot_origin(gid, "grid")
            log.info("coin=%d slug=%s: prebuy lot %d graduated to grid", coin_id, coin["slug"], gid)

    # --- Pairing (#412): combined group sale for stuck lots ---
    # LIVE guard (#3): pairing has no on-chain swap path yet; block in live mode
    use_pairing = _cfg_bool(cfg, "PAIRING_GRID", False)
    if use_pairing and mode == "live":
        log.warning("coin=%d slug=%s: PAIRING_GRID blocked in live mode (no swap path, issue #3)",
                    coin_id, coin["slug"])
        use_pairing = False
    if use_pairing:
        pair_threshold = _cfg_float(cfg, "PAIRING_GRID_THRESHOLD_PCT", 20.0)
        pair_stuck_days = _cfg_float(cfg, "PAIRING_GRID_STUCK_DAYS", 7.0)
        pair_max_lev = int(_cfg_float(cfg, "PAIRING_MAX_LEVELERS", 1))
        pair_profit = _cfg_float(cfg, "PAIR_PROFIT_PCT", 3.0)

        for l in lots:
            ss_raw = store.state_get(coin_id, f"stuck_since_{l['id']}")
            l["stuck_since"] = float(ss_raw) if ss_raw else None
            l["grid_pair"] = l.get("levels_to") is not None

        clock_updates = update_stuck_clock(lots, price, pair_threshold, ts)
        for lid, val in clock_updates.items():
            if val is not None:
                store.state_set_commit(coin_id, f"stuck_since_{lid}", val)
            else:
                store.state_delete(coin_id, f"stuck_since_{lid}")

        link_updates = repair_grid_links(
            lots, price, pair_threshold, pair_stuck_days,
            pair_max_lev, pair_profit, ts)
        for lid, target_id in link_updates:
            store.set_lot_levels_to(lid, target_id)
            lot_d = next((l for l in lots if l["id"] == lid), None)
            if lot_d:
                lot_d["levels_to"] = target_id
                lot_d["grid_pair"] = target_id is not None

        groups = eligible_groups(lots, price, pair_profit)
        if groups:
            group = groups[0]
            group_ids = [l["id"] for l in group]
            total_tokens = sum(float(l["tokens"]) for l in group)
            total_cost = sum(float(l["cost"]) for l in group)
            if revalidate_combined_sale(group, price, pair_profit):
                proceeds = total_tokens * price
                pnl_pct = (proceeds / total_cost - 1.0) * 100.0 if total_cost > 0 else 0.0
                store.begin()
                try:
                    store.remove_lots(group_ids)
                    store.usdc_commit_sell(proceeds, coin_id,
                                          f"paper-pair-{group_ids[0]}",
                                          price, total_tokens, pnl_pct, "paper")
                    store.state_mset(coin_id, {
                        "last_sell_price": price,
                        "last_sell_ts": ts,
                    })
                    store.commit()
                except Exception:
                    store.rollback()
                    raise
                sold_ids = set(group_ids)
                decision["sells"] = [l for l in decision["sells"] if l["id"] not in sold_ids]
                for lid in group_ids:
                    store.state_delete(coin_id, f"stuck_since_{lid}")
                log.info("coin=%d slug=%s: PAIR SALE %d lots, tokens=%.4f cost=%.2f proceeds=%.2f pnl=%.1f%%",
                         coin_id, coin["slug"], len(group), total_tokens, total_cost, proceeds, pnl_pct)
                _alerts.alert_trade("sell", coin["slug"], total_tokens, price,
                                    proceeds, pnl_pct, mode, **_smtp())

    # --- Batch sell (#968): cap the sell plan by count and value ---
    use_batch = _cfg_bool(cfg, "BATCH_SELL", False)
    if use_batch and decision["sells"]:
        batch_max_lots = int(_cfg_float(cfg, "BATCH_SELL_MAX_LOTS", 2))
        if batch_max_lots < 1:
            batch_max_lots = 1
        max_trade_usd = _cfg_float(cfg, "MAX_TRADE_USD", 50.0)
        decision["sells"] = cap_batch_plan(
            decision["sells"], price, max_trade_usd, batch_max_lots)

    # Trail updates first (no swaps)
    for lot, armed, peak in decision["trail_updates"]:
        store.update_lot_trail(lot["id"], armed, peak)

    # Sells - lot deletion + trade row + state in one tx
    slug = coin["slug"]
    for lot in decision["sells"]:
        store.begin()
        try:
            if mode == "live":
                txsig = live_sell(
                    store, coin_id, lot, price,
                    rpc_url=config.RPC_URL,
                    keypair_path=config.KEYPAIR_PATH,
                    quote_mint=config.QUOTE_MINT,
                    token_mint=mint,
                    gas_reserve_lamports=config.GAS_RESERVE_LAMPORTS,
                    token_decimals=coin.get("decimals", 6),
                    slippage_bps=slippage_bps,
                )
            else:
                txsig = paper_sell(store, coin_id, lot, price, mode="paper")
            if txsig:
                store.state_mset(coin_id, {
                    "last_sell_price": price,
                    "last_sell_ts": ts,
                })
                store.state_delete(coin_id, f"stuck_since_{lot['id']}")
            store.commit()
        except Exception:
            store.rollback()
            raise
        if txsig:
            pnl = (price - lot["buy_price"]) / lot["buy_price"] * 100.0
            _alerts.alert_trade("sell", slug, lot["tokens"], price,
                                lot["tokens"] * price, pnl, mode, **_smtp())

    # --- Gas refill: after sells, live mode only ---
    use_gas_refill = _cfg_bool(cfg, "GAS_REFILL", False)
    if use_gas_refill and mode == "live":
        refill_usdc = _cfg_float(cfg, "GAS_REFILL_USDC", 10.0)
        low_sol = _cfg_float(cfg, "GAS_REFILL_LOW_SOL", 0.01)
        _wallet = str(load_keypair(config.KEYPAIR_PATH).pubkey())
        sol_lam = sol_balance_lamports(config.RPC_URL, _wallet)
        gas_dec = gas_refill_decision(sol_lam, store.usdc_balance(), refill_usdc,
                                      low_sol, config.GAS_RESERVE_LAMPORTS)
        if gas_dec == "refill":
            txsig_gas = gas_refill(store, refill_usdc, config.RPC_URL,
                                   config.KEYPAIR_PATH, config.QUOTE_MINT,
                                   slippage_bps=slippage_bps * 2)
            if txsig_gas:
                log.info("coin=%d slug=%s: gas refill %.2f USDC -> SOL tx=%s",
                         coin_id, coin["slug"], refill_usdc, txsig_gas)
                _alerts.alert_error(coin["slug"],
                                    f"Gas refill: {refill_usdc:.0f} USDC -> SOL ({txsig_gas})",
                                    **_smtp())
        elif gas_dec == "no_margin":
            log.error("coin=%d slug=%s: SOL critically low, manual top-up needed", coin_id, coin["slug"])
            _alerts.alert_error(coin["slug"], "SOL critically low - manual top-up needed", **_smtp())
        elif gas_dec == "no_usdc":
            log.warning("coin=%d slug=%s: SOL low but no USDC for gas refill", coin_id, coin["slug"])

    # Buy - lot creation + trade row + ref + capital + last_buy_ts in one tx
    if decision["buy_usd"] > 0:
        if prebuy_fired:
            buy_origin = "prebuy"
        elif in_floor_zone and is_reserve_buy(price, floor_zone_val):
            buy_origin = "reserve"
        else:
            buy_origin = "grid"
        store.begin()
        try:
            if mode == "live":
                txsig = live_buy(
                    store, coin_id, decision["buy_usd"], price, ts,
                    rpc_url=config.RPC_URL,
                    keypair_path=config.KEYPAIR_PATH,
                    quote_mint=config.QUOTE_MINT,
                    token_mint=mint,
                    gas_reserve_lamports=config.GAS_RESERVE_LAMPORTS,
                    token_decimals=coin.get("decimals", 6),
                    origin=buy_origin,
                    slippage_bps=slippage_bps,
                )
            else:
                txsig = paper_buy(store, coin_id, decision["buy_usd"], price, ts,
                                  origin=buy_origin)
            if decision["new_ref"] is not None:
                store.state_set(coin_id, "ref", decision["new_ref"])
            store.state_set(coin_id, "last_buy_ts", ts)
            if new_ep is not None and cad_buy:
                store.state_set(coin_id, "floor_episode", _json.dumps(new_ep))
            if prebuy_fired:
                store.state_set(coin_id, "prebuy_ep", _json.dumps(pre_d["new_st_pre"]))
            new_deployed = deployed + decision["buy_usd"]
            store.set_capital(coin_id, new_deployed, max_cap_usd)
            store.commit()
        except Exception:
            store.rollback()
            raise
        if txsig:
            tokens_approx = decision["buy_usd"] / price
            _alerts.alert_trade("buy", slug, tokens_approx, price,
                                decision["buy_usd"], None, mode, **_smtp())
    elif decision["new_ref"] is not None and decision["new_ref"] != ref:
        store.state_set_commit(coin_id, "ref", decision["new_ref"])

    # --- Topup insurance (#772): ceiling-exempt buy on empty book ---
    bought_this_tick = decision["buy_usd"] > 0
    use_topup = _cfg_bool(cfg, "TOPUP_INSURANCE", False)
    topup_lot_usd = _cfg_float(cfg, "TOPUP_LOT_USD", 15.0)
    fresh_lots = [dict(r) for r in store.get_lots(coin_id)]
    if topup_buy_allowed(use_topup, topup_lot_usd, fresh_lots,
                         bought_this_tick, store.usdc_balance()):
        store.begin()
        try:
            if mode == "live":
                txsig = live_buy(
                    store, coin_id, topup_lot_usd, price, ts,
                    rpc_url=config.RPC_URL,
                    keypair_path=config.KEYPAIR_PATH,
                    quote_mint=config.QUOTE_MINT,
                    token_mint=mint,
                    gas_reserve_lamports=config.GAS_RESERVE_LAMPORTS,
                    token_decimals=coin.get("decimals", 6),
                    origin="topup",
                    slippage_bps=slippage_bps,
                )
            else:
                txsig = paper_buy(store, coin_id, topup_lot_usd, price, ts,
                                  origin="topup")
            store.state_set(coin_id, "last_buy_ts", ts)
            new_deployed = (cap["deployed_usd"] if cap else 0.0) + topup_lot_usd
            store.set_capital(coin_id, new_deployed, max_cap_usd)
            store.commit()
        except Exception:
            store.rollback()
            raise
        if txsig:
            tokens_approx = topup_lot_usd / price
            log.info("coin=%d slug=%s: TOPUP insurance lot $%.2f at price=%.6f",
                     coin_id, coin["slug"], topup_lot_usd, price)
            _alerts.alert_trade("buy", slug, tokens_approx, price,
                                topup_lot_usd, None, mode, **_smtp())


def main():
    parser = argparse.ArgumentParser(description="Jupiter multi-coin scheduler")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--paper", action="store_true", help="Force paper mode for all coins")
    group.add_argument("--live",  action="store_true",
                       help="Allow live execution (coins must be status=live in DB)")
    args = parser.parse_args()

    if args.live and os.environ.get("LIVE") != "1":
        log.error("--live requires LIVE=1 in the environment. Refusing to start.")
        sys.exit(1)

    force_mode = "paper" if args.paper else None

    store = Store(config.DB_PATH)
    released = store.reconcile_reserves()
    if released:
        log.warning("Startup reconciliation: released %d orphaned reserve(s)", released)

    if args.live or force_mode is None:
        live_coins = store.list_coins("live")
        for lc in live_coins:
            lcfg = store.get_config(lc["id"])
            bad = audit_live_config(lcfg, lc["slug"])
            if bad:
                log.error("Boot audit FAILED for live coin '%s': missing %s",
                          lc["slug"], ", ".join(bad))
                sys.exit(1)
        if live_coins:
            log.info("Boot audit: %d live coin(s) passed config validation", len(live_coins))

    log.info("Scheduler started. DB=%s tick=%ds mode=%s",
             config.DB_PATH, TICK_INTERVAL, force_mode or "per-coin")

    while _running:
        tick_start = time.time()
        coins = store.list_coins()
        active = [c for c in coins if c["status"] in ("paper", "live")]
        log.info("Tick: %d active coins", len(active))
        for coin in active:
            try:
                tick_coin(store, dict(coin), force_mode=force_mode)
            except Exception as e:
                log.exception("Error ticking coin=%d slug=%s", coin["id"], coin["slug"])
                _alerts.alert_error(coin["slug"], str(e), **_smtp())
        elapsed = time.time() - tick_start
        sleep_s = max(0.0, TICK_INTERVAL - elapsed)
        log.info("Tick done in %.1fs, sleeping %.1fs", elapsed, sleep_s)
        if _running:
            time.sleep(sleep_s)

    log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
