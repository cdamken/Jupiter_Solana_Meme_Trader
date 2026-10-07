"""panel/app.py — Flask panel for Jupiter multi-coin bot.

Routes:
  GET  /                  dashboard: all coins, balances, P&L
  GET  /coin/<id>         coin detail: lots, config, recent trades
  POST /coin/add          add a new coin (slug, label, mint)
  POST /coin/<id>/status  change coin status (paper/live/paused)
  GET  /coin/<id>/config  edit coin config overrides
  POST /coin/<id>/config  save one config override
  POST /coin/<id>/config/delete  delete one override
  POST /deposit           add USDC to ledger (bootstrap/top-up)
  GET  /health            simple liveness probe

Run alongside the scheduler (separate process or thread):
  JUPITER_DB=store/jupiter.db flask --app panel.app run --port 5001
Or from project root:
  python3 -m panel.app
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

import config as cfg
from store.store import Store

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("PANEL_SECRET", "change-me-in-production")

import datetime as _dt

@app.template_filter("datefmt")
def _datefmt(ts):
    try:
        return _dt.datetime.utcfromtimestamp(float(ts)).strftime("%m-%d")
    except Exception:
        return ""

def _store() -> Store:
    return Store(cfg.DB_PATH)


# ---- helpers ----

def _coin_summary(store: Store, coin: dict) -> dict:
    lots = store.get_lots(coin["id"])
    deployed = sum(lot["cost"] for lot in lots)
    unrealized = 0.0          # we don't fetch live price here; show 0 as placeholder
    trade_rows = store._c.execute(
        "SELECT side, usd, pnl_pct FROM trades WHERE coin_id = ? ORDER BY ts DESC LIMIT 20",
        (coin["id"],),
    ).fetchall()
    realized = sum(
        row["usd"] - row["usd"] / (1 + row["pnl_pct"] / 100.0)
        for row in trade_rows
        if row["side"] == "sell" and row["pnl_pct"] is not None
    )
    return {
        "id":         coin["id"],
        "slug":       coin["slug"],
        "label":      coin["label"],
        "mint":       coin["mint"],
        "status":     coin["status"],
        "lots":       len(lots),
        "deployed":   round(deployed, 2),
        "realized":   round(realized, 2),
    }


# ---- routes ----

@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": time.time()})


@app.get("/")
def dashboard():
    store = _store()
    coins = store.list_coins()
    summaries = [_coin_summary(store, dict(c)) for c in coins]
    balance = store.usdc_balance()

    # Fleet-wide recent trades (last 100, all coins)
    fleet_trades = store._c.execute(
        "SELECT t.ts, c.id AS coin_id, c.slug, c.label, t.mode, t.side,"
        " t.price, t.tokens, t.usd, t.pnl_pct, t.txsig"
        " FROM trades t JOIN coins c ON c.id = t.coin_id"
        " ORDER BY t.ts DESC LIMIT 100"
    ).fetchall()

    # Total deposits (sum of all 'deposit' ledger entries)
    dep_row = store._c.execute(
        "SELECT COALESCE(SUM(delta), 0) AS total FROM usdc_ledger WHERE kind = 'deposit'"
    ).fetchone()
    total_deposited = round(dep_row["total"], 2) if dep_row else 0.0

    return render_template(
        "dashboard.html",
        coins=summaries,
        balance=round(balance, 2),
        fleet_trades=[dict(r) for r in fleet_trades],
        total_deposited=total_deposited,
    )


@app.get("/coin/<int:coin_id>")
def coin_detail(coin_id: int):
    store = _store()
    coin = store._c.execute("SELECT * FROM coins WHERE id = ?", (coin_id,)).fetchone()
    if not coin:
        flash("Coin not found", "error")
        return redirect(url_for("dashboard"))
    lots = [dict(r) for r in store.get_lots(coin_id)]
    cfg_map = store.get_config(coin_id)
    overrides = store._c.execute(
        "SELECT key, value, author, reason, ts FROM coin_overrides WHERE coin_id = ? ORDER BY key",
        (coin_id,),
    ).fetchall()
    catalog = store._c.execute(
        "SELECT key, tier, scope, type, label, help, default_val, min, max FROM param_catalog ORDER BY key",
    ).fetchall()
    trades = store._c.execute(
        "SELECT ts, mode, side, signal, price, tokens, usd, pnl_pct, txsig"
        " FROM trades WHERE coin_id = ? ORDER BY ts DESC LIMIT 50",
        (coin_id,),
    ).fetchall()
    return render_template(
        "coin.html",
        coin=dict(coin), lots=lots, cfg=cfg_map,
        overrides=[dict(r) for r in overrides],
        catalog=[dict(r) for r in catalog],
        trades=[dict(r) for r in trades],
    )


@app.post("/coin/add")
def coin_add():
    slug  = request.form.get("slug", "").strip().lower()
    label = request.form.get("label", "").strip()
    mint  = request.form.get("mint", "").strip()
    try:
        decimals = int(request.form.get("decimals", "6") or "6")
    except ValueError:
        decimals = 6
    if not slug or not mint:
        flash("slug and mint are required", "error")
        return redirect(url_for("dashboard"))
    store = _store()
    try:
        store.add_coin(slug, label or slug.upper(), mint, decimals=decimals)
        flash(f"Coin '{slug}' added (paper mode, decimals={decimals})", "ok")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("dashboard"))


@app.post("/coin/<int:coin_id>/status")
def coin_status(coin_id: int):
    status = request.form.get("status", "")
    if status not in ("paper", "live", "paused"):
        flash("Invalid status", "error")
        return redirect(url_for("coin_detail", coin_id=coin_id))
    store = _store()
    store.set_coin_status(coin_id, status)
    flash(f"Status set to '{status}'", "ok")
    return redirect(url_for("coin_detail", coin_id=coin_id))


@app.post("/coin/<int:coin_id>/config")
def coin_config_save(coin_id: int):
    key    = request.form.get("key", "").strip()
    value  = request.form.get("value", "").strip()
    author = request.form.get("author", "panel").strip()
    reason = request.form.get("reason", "").strip()
    if not key or not value:
        flash("key and value are required", "error")
        return redirect(url_for("coin_detail", coin_id=coin_id))
    if not reason:
        flash("reason is required for every override", "error")
        return redirect(url_for("coin_detail", coin_id=coin_id))
    store = _store()
    try:
        store._c.execute(
            "INSERT INTO coin_overrides (coin_id, key, value, author, reason)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(coin_id, key) DO UPDATE SET value=excluded.value,"
            " author=excluded.author, reason=excluded.reason, ts=datetime('now')",
            (coin_id, key, value, author, reason),
        )
        store._c.commit()
        flash(f"Override '{key}' saved", "ok")
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("coin_detail", coin_id=coin_id))


@app.post("/coin/<int:coin_id>/config/delete")
def coin_config_delete(coin_id: int):
    key = request.form.get("key", "").strip()
    store = _store()
    store._c.execute(
        "DELETE FROM coin_overrides WHERE coin_id = ? AND key = ?", (coin_id, key)
    )
    store._c.commit()
    flash(f"Override '{key}' deleted", "ok")
    return redirect(url_for("coin_detail", coin_id=coin_id))


@app.get("/overview")
def overview():
    store = _store()
    coins = store.list_coins()
    summaries = [_coin_summary(store, dict(c)) for c in coins]

    # Scheduler heartbeat: check if a scheduler process wrote a recent heartbeat.
    # We probe /health on ourselves (same process = panel-only) or look for a sentinel file.
    # Simple heuristic: last trade timestamp across all coins.
    last_trade = store._c.execute(
        "SELECT MAX(ts) AS ts FROM trades"
    ).fetchone()
    last_trade_ts = last_trade["ts"] if last_trade and last_trade["ts"] else None

    # Ledger summary: recent non-trade entries (reserve/release orphans, deposits)
    recent_ledger = store._c.execute(
        "SELECT l.ts, l.kind, l.delta, c.slug"
        " FROM usdc_ledger l LEFT JOIN coins c ON c.id = l.coin_id"
        " ORDER BY l.id DESC LIMIT 30"
    ).fetchall()

    # Lots age: days since oldest open lot per coin
    lots_age = {}
    now_ts = time.time()
    for s in summaries:
        if s["lots"] > 0:
            oldest = store._c.execute(
                "SELECT MIN(ts) AS ts FROM lots WHERE coin_id = ?", (s["id"],)
            ).fetchone()
            if oldest and oldest["ts"]:
                days = (now_ts - oldest["ts"]) / 86400
                lots_age[s["id"]] = round(days, 1)

    # Sparkline data: last 30 trades per coin {coin_id: [{ts, price, side}]}
    sparklines = {}
    for s in summaries:
        rows = store._c.execute(
            "SELECT strftime('%s', ts) AS ts_unix, price, side"
            " FROM trades WHERE coin_id = ? ORDER BY ts ASC LIMIT 30",
            (s["id"],),
        ).fetchall()
        if rows:
            sparklines[s["id"]] = [
                {"ts": float(r["ts_unix"]), "price": float(r["price"]), "side": r["side"]}
                for r in rows if r["price"] and r["ts_unix"]
            ]

    balance = store.usdc_balance()
    dep_row = store._c.execute(
        "SELECT COALESCE(SUM(delta), 0) AS total FROM usdc_ledger WHERE kind = 'deposit'"
    ).fetchone()
    total_deposited = round(dep_row["total"], 2) if dep_row else 0.0

    return render_template(
        "overview.html",
        coins=summaries,
        balance=round(balance, 2),
        total_deposited=total_deposited,
        last_trade_ts=last_trade_ts,
        recent_ledger=[dict(r) for r in recent_ledger],
        lots_age=lots_age,
        sparklines=sparklines,
        now_ts=now_ts,
    )


@app.get("/glossary")
def glossary():
    store = _store()
    catalog = store._c.execute(
        "SELECT key, tier, scope, type, label, help, default_val, recommended, min, max"
        " FROM param_catalog ORDER BY scope, key"
    ).fetchall()
    return render_template("glossary.html", catalog=[dict(r) for r in catalog])


@app.get("/money")
def money():
    store = _store()
    rows = store._c.execute(
        "SELECT id, ts, kind, delta, coin_id, txsig FROM usdc_ledger"
        " WHERE kind IN ('deposit', 'withdraw')"
        " ORDER BY ts DESC LIMIT 200"
    ).fetchall()
    balance = store.usdc_balance()
    dep_row = store._c.execute(
        "SELECT COALESCE(SUM(delta), 0) AS total FROM usdc_ledger WHERE kind = 'deposit'"
    ).fetchone()
    total_deposited = round(dep_row["total"], 2) if dep_row else 0.0
    with_row = store._c.execute(
        "SELECT COALESCE(SUM(ABS(delta)), 0) AS total FROM usdc_ledger WHERE kind = 'withdraw'"
    ).fetchone()
    total_withdrawn = round(with_row["total"], 2) if with_row else 0.0
    return render_template(
        "money.html",
        ledger=[dict(r) for r in rows],
        balance=round(balance, 2),
        total_deposited=total_deposited,
        total_withdrawn=total_withdrawn,
    )


@app.post("/withdraw")
def withdraw():
    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        flash("Invalid amount", "error")
        return redirect(url_for("money"))
    if amount <= 0:
        flash("Amount must be > 0", "error")
        return redirect(url_for("money"))
    store = _store()
    if amount > store.usdc_balance():
        flash("Insufficient balance", "error")
        return redirect(url_for("money"))
    with store._c:
        store._c.execute(
            "INSERT INTO usdc_ledger (kind, delta) VALUES ('withdraw', ?)",
            (-amount,),
        )
    flash(f"Withdrawal of ${amount:.2f} USDC recorded", "ok")
    return redirect(url_for("money"))


@app.post("/deposit")
def deposit():
    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        flash("Invalid amount", "error")
        return redirect(url_for("dashboard"))
    if amount <= 0:
        flash("Amount must be > 0", "error")
        return redirect(url_for("dashboard"))
    store = _store()
    store.usdc_deposit(amount)
    flash(f"Deposited ${amount:.2f} USDC", "ok")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    port = int(os.environ.get("PANEL_PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
