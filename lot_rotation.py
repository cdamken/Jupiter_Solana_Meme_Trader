"""lot_rotation.py -- Weekly performance-weighted lot sizing (#919).

Adapted from SIMD's analysis/weekly_lot_rotation.py for Jupiter's SQLite store.
PURE calculator: reads from the store, returns proposals, never writes anything.

Formula (locked in SIMD #919):
    R   = realized_30d / time_weighted_deployed_cost / days * 100
          ($ earned per day per $100 deployed; live trades only)
    lot = clamp( round10( LOT_AVG_ANCHOR * R / fleet_median_R ), LOT_MIN, cap )

Usage: python3 lot_rotation.py [--db PATH] [--days N]
"""
from __future__ import annotations
import datetime
import statistics


LOT_MIN = 20
LOT_MAX = 60
LOT_PROBATION = 10
MIN_HISTORY_D = 14
COOL_LOT = 10
COOL_WEEKS = 3
COOL_FRACTION = 0.5
LOT_AVG_ANCHOR = 40
STEPQ = 10


def round10(x):
    """Round to $10 steps, ties always UP."""
    return int(x / 10.0 + 0.5) * 10


def bounded_step(current, target):
    """Move at most STEPQ toward target, landing on the $10 grid."""
    if current is None:
        return target
    if target > current:
        return int(min((current // STEPQ) * STEPQ + STEPQ, target))
    if target < current:
        return int(max(-((-current) // STEPQ) * STEPQ - STEPQ, target))
    return int(current)


def realized_usd(trades, t0, t1):
    """Realized profit of sells inside [t0, t1): usd - usd/(1+pct/100)."""
    total = 0.0
    for t in trades:
        ts = t["ts_dt"]
        if t["side"] == "sell" and t0 <= ts < t1 and (t["pnl_pct"] or 0) > -100:
            pct = t["pnl_pct"] or 0
            total += t["usd"] - t["usd"] / (1 + pct / 100.0)
    return total


def deployed_cost_series(trades):
    """Replay the book's COST over time: buys add usd, sells remove the sold
    lot's cost basis. Returns [(dt, cost_after)]."""
    cost = 0.0
    series = []
    for t in trades:
        if t["side"] == "buy":
            cost += t["usd"]
        elif t["side"] == "sell" and (t["pnl_pct"] or 0) > -100:
            pct = t["pnl_pct"] or 0
            cost -= t["usd"] / (1 + pct / 100.0)
        series.append((t["ts_dt"], max(cost, 0.0)))
    return series


def time_weighted_deployed(trades, t0, t1):
    """Average deployed cost over [t0, t1], weighted by duration."""
    series = deployed_cost_series(trades)
    span = (t1 - t0).total_seconds()
    if span <= 0:
        return 0.0
    level = 0.0
    for t_dt, c in series:
        if t_dt <= t0:
            level = c
        else:
            break
    acc = 0.0
    cur = t0
    for t_dt, c in series:
        if t_dt <= t0:
            continue
        if t_dt >= t1:
            break
        acc += level * (t_dt - cur).total_seconds()
        level, cur = c, t_dt
    acc += level * (t1 - cur).total_seconds()
    return acc / span


def analyze_fleet(store, days=30, now=None, caps=None):
    """Compute lot rotation proposals for all active coins.

    Returns (rows, fleet_median) where each row is a dict with:
      coin_id, slug, realized, avg_dep, r, history_d, cur_lot, target, proposed, why
    """
    if now is None:
        now = datetime.datetime.now()
    if caps is None:
        caps = {}

    t0 = now - datetime.timedelta(days=days)
    t1 = now

    coins = store.get_active_coins()
    rows = []

    for coin in coins:
        coin_id = coin["id"]
        slug = coin["slug"]

        raw_trades = store.get_trades(coin_id)
        trades = []
        for r in raw_trades:
            try:
                ts_dt = datetime.datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            trades.append({
                "ts_dt": ts_dt,
                "side": r["side"],
                "usd": float(r["usd"]),
                "pnl_pct": float(r["pnl_pct"]) if r["pnl_pct"] is not None else 0.0,
                "mode": r["mode"],
            })
        trades = [t for t in trades if t["mode"] == "live"]
        trades.sort(key=lambda t: t["ts_dt"])

        first_live = trades[0]["ts_dt"] if trades else None
        history_d = (now - first_live).total_seconds() / 86400.0 if first_live else 0.0

        eff_days = min(days, history_d) if history_d >= MIN_HISTORY_D else days
        w0 = now - datetime.timedelta(days=eff_days)

        realized = realized_usd(trades, w0, t1)
        avg_dep = time_weighted_deployed(trades, w0, t1)

        book_cost = sum(float(l["cost"]) for l in store.get_lots(coin_id))
        replay_cost = deployed_cost_series(trades)[-1][1] if trades else 0.0
        offset = book_cost - replay_cost
        avg_dep = max(avg_dep + offset, 0.0)

        r_val = (realized / avg_dep / eff_days * 100.0) if avg_dep > 0 else 0.0

        cfg = store.get_config(coin_id)
        cur_lot_raw = cfg.get("LOT_USD")
        cur_lot = float(cur_lot_raw) if cur_lot_raw else None

        weekly_r = []
        for k in range(COOL_WEEKS):
            e1 = now - datetime.timedelta(days=7 * k)
            e0 = e1 - datetime.timedelta(days=7)
            wdep = max(time_weighted_deployed(trades, e0, e1) + offset, 0.0)
            wr = (realized_usd(trades, e0, e1) / wdep / 7 * 100.0) if wdep > 0 else 0.0
            weekly_r.append(wr)

        rows.append(dict(
            coin_id=coin_id, slug=slug, realized=realized, avg_dep=avg_dep,
            r=r_val, history_d=history_d, eff_days=eff_days, cur_lot=cur_lot,
            offset=offset, weekly_r=weekly_r,
        ))

    ranked = [x for x in rows if x["history_d"] >= MIN_HISTORY_D]
    fleet_median = statistics.median(x["r"] for x in ranked) if ranked else 0.0

    for x in rows:
        if x["history_d"] < MIN_HISTORY_D:
            x["target"] = LOT_PROBATION
            x["why"] = f"probation (only {x['history_d']:.0f}d of live history)"
        elif fleet_median <= 0:
            x["target"] = int(x["cur_lot"]) if x["cur_lot"] else LOT_MIN
            x["why"] = "fleet average degenerate; HOLDING current lot"
        elif (x["history_d"] >= COOL_WEEKS * 7
              and all(wr < COOL_FRACTION * fleet_median for wr in x["weekly_r"])):
            x["target"] = COOL_LOT
            x["why"] = (f"COOLING: R under {COOL_FRACTION:.0%} of fleet median "
                        f"{COOL_WEEKS} straight weeks")
        else:
            raw = LOT_AVG_ANCHOR * x["r"] / fleet_median
            x["target"] = max(LOT_MIN, min(LOT_MAX, round10(raw)))
            x["why"] = f"R={x['r']:.2f} vs fleet median {fleet_median:.2f}"
            if x["eff_days"] < 30:
                x["why"] += f" (over its {x['eff_days']:.0f}d of history)"
        cap = caps.get(x["slug"])
        if cap is not None and x["target"] > cap:
            x["earned"] = x["target"]
            x["target"] = cap
            x["why"] += f"; capped at ${cap} (liquidity gate)"
        x["proposed"] = bounded_step(
            x["cur_lot"] if x["cur_lot"] is not None else LOT_MIN,
            x["target"])

    return rows, fleet_median
