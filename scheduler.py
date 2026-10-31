from __future__ import annotations
"""scheduler.py -- Main loop. Ticks all active coins once per interval.

One process. No cron per coin. Adding a coin = INSERT into coins.
Removing a coin = UPDATE coins SET status='paused'.

Coins tick in parallel via ThreadPoolExecutor (#33). Network I/O releases
the GIL, so threads give real concurrency for the RPC/DexScreener calls
that dominate tick time. Each coin gets its own Store connection (SQLite
WAL allows concurrent readers). A hung coin times out without stalling
the rest of the fleet.

Usage:
    python3 scheduler.py              # respects DEFAULT_MODE from .env
    python3 scheduler.py --paper      # force paper mode for all coins
    python3 scheduler.py --live       # force live mode (requires LIVE=1 confirmation)
"""
import argparse
import concurrent.futures
import logging
import os
import signal
import sys
import time

import config  # noqa: F401 -- imports validate required env vars, fail-closed
from store.store import Store
from tick import tick_coin, audit_live_config
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

TICK_INTERVAL = 60

_running = True


def _stop(signum, _frame):
    global _running
    log.info("Signal %d received, shutting down after this tick.", signum)
    _running = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


def _tick_one(db_path: str, coin: dict, force_mode: str | None,
              timeout: int) -> tuple[str, float, str | None]:
    """Run one coin's tick in a worker thread with its own DB connection.
    Returns (slug, elapsed_seconds, error_message_or_None)."""
    slug = coin["slug"]
    t0 = time.time()
    try:
        worker_store = Store(db_path)
        tick_coin(worker_store, coin, force_mode=force_mode)
        return (slug, time.time() - t0, None)
    except Exception as e:
        return (slug, time.time() - t0, str(e))


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

    timeout = config.COIN_TICK_TIMEOUT_S
    log.info("Scheduler started. DB=%s tick=%ds timeout=%ds mode=%s",
             config.DB_PATH, TICK_INTERVAL, timeout, force_mode or "per-coin")

    while _running:
        tick_start = time.time()
        coins = store.list_coins()
        active = [c for c in coins if c["status"] in ("paper", "live")]
        n = len(active)
        log.info("Tick: %d active coin(s)", n)

        if n == 0:
            pass
        elif n == 1:
            coin = dict(active[0])
            try:
                tick_coin(store, coin, force_mode=force_mode)
            except Exception as e:
                log.exception("Error ticking coin=%d slug=%s", coin["id"], coin["slug"])
                _alerts.alert_error(coin["slug"], str(e), **_smtp())
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
                futures = {}
                for coin in active:
                    cd = dict(coin)
                    f = pool.submit(_tick_one, config.DB_PATH, cd, force_mode, timeout)
                    futures[f] = cd

                for f in concurrent.futures.as_completed(futures, timeout=timeout + 5):
                    coin = futures[f]
                    slug = coin["slug"]
                    try:
                        result_slug, elapsed, err = f.result(timeout=0)
                        if err:
                            log.error("coin=%d slug=%s: tick error after %.1fs: %s",
                                      coin["id"], slug, elapsed, err)
                            _alerts.alert_error(slug, err, **_smtp())
                        else:
                            log.info("coin=%d slug=%s: tick OK (%.1fs)", coin["id"], slug, elapsed)
                    except concurrent.futures.TimeoutError:
                        log.error("coin=%d slug=%s: tick TIMEOUT (>%ds), skipped this tick",
                                  coin["id"], slug, timeout)
                        _alerts.alert_error(slug, f"Tick timeout ({timeout}s)", **_smtp())
                    except Exception as e:
                        log.exception("coin=%d slug=%s: unexpected worker error", coin["id"], slug)
                        _alerts.alert_error(slug, str(e), **_smtp())

        elapsed = time.time() - tick_start
        sleep_s = max(0.0, TICK_INTERVAL - elapsed)
        log.info("Tick done in %.1fs, sleeping %.1fs", elapsed, sleep_s)
        if _running:
            time.sleep(sleep_s)

    log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
