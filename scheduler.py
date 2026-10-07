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
from engine import grid_decision, relative_ceiling, price_gate
from execute import (
    fetch_price, paper_buy, paper_sell, live_buy, live_sell,
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


def _stop(signum, _frame):
    global _running
    log.info("Signal %d received, shutting down after this tick.", signum)
    _running = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


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

    buy_step      = _cfg_float(cfg, "BUY_STEP_PCT",    4.0)
    sell_step     = _cfg_float(cfg, "SELL_STEP_PCT",   buy_step)
    lot_usd       = _cfg_float(cfg, "LOT_USD",         25.0)
    max_cap_usd   = _cfg_float(cfg, "MAX_CAPITAL_USD", 200.0)
    sell_trail    = _cfg_bool(cfg,  "SELL_TRAIL",      False)
    sell_trail_pct= _cfg_float(cfg, "SELL_TRAIL_PCT",  2.0)
    ceil_pct      = _cfg_float(cfg, "CEILING_PERCENTILE", 98.0)
    max_step_pct  = _cfg_float(cfg, "PRICE_GATE_PCT",  30.0)

    # Fetch price
    price = fetch_price(mint)
    if price is None:
        log.warning("coin=%d: no price, skipping tick", coin_id)
        return

    # Price gate (reject outlier ticks)
    # State stored inline per-tick via a simple DB field would need a table;
    # for now we accept all prices from DexScreener and rely on the <= 0 guard.
    # Full gate state persistence is a future improvement.
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

    # Reference price: last buy price or None
    ref = lots[-1]["buy_price"] if lots else None

    decision = grid_decision(
        price, ref, lots, buy_step, lot_usd_effective, ceiling, cash,
        sell_step=sell_step,
        sell_trail=sell_trail,
        sell_trail_pct=sell_trail_pct,
    )

    ts = time.time()

    # Trail updates first (no swaps)
    for lot, armed, peak in decision["trail_updates"]:
        store.update_lot_trail(lot["id"], armed, peak)

    # Sells
    for lot in decision["sells"]:
        if mode == "live":
            live_sell(
                store, coin_id, lot, price,
                rpc_url=config.RPC_URL,
                keypair_path=config.KEYPAIR_PATH,
                quote_mint=config.QUOTE_MINT,
                token_mint=mint,
                gas_reserve_lamports=config.GAS_RESERVE_LAMPORTS,
            )
        else:
            paper_sell(store, coin_id, lot, price, mode="paper")

    # Buy
    if decision["buy_usd"] > 0:
        if mode == "live":
            live_buy(
                store, coin_id, decision["buy_usd"], price, ts,
                rpc_url=config.RPC_URL,
                keypair_path=config.KEYPAIR_PATH,
                quote_mint=config.QUOTE_MINT,
                token_mint=mint,
                gas_reserve_lamports=config.GAS_RESERVE_LAMPORTS,
            )
        else:
            paper_buy(store, coin_id, decision["buy_usd"], price, ts)

        # Update deployed capital
        new_deployed = deployed + decision["buy_usd"]
        store.set_capital(coin_id, new_deployed, max_cap_usd)


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
            except Exception:
                log.exception("Error ticking coin=%d slug=%s", coin["id"], coin["slug"])
        elapsed = time.time() - tick_start
        sleep_s = max(0.0, TICK_INTERVAL - elapsed)
        log.info("Tick done in %.1fs, sleeping %.1fs", elapsed, sleep_s)
        if _running:
            time.sleep(sleep_s)

    log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
