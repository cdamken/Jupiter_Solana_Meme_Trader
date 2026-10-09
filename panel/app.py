from __future__ import annotations
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

import functools
import hashlib
import hmac

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

import config as cfg
from store.store import Store

PANEL_BASE = os.environ.get("PANEL_BASE", "")   # e.g. "/carlos/jupiter/" when behind PHP proxy
PANEL_SECRET = os.environ.get("PANEL_SECRET", "")

# Fail-closed: refuse to boot with no secret or the placeholder default
if not PANEL_SECRET or PANEL_SECRET == "change-me-in-production":
    print("FATAL: PANEL_SECRET must be set to a real secret (not the default).", file=sys.stderr)
    print("  export PANEL_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')",
          file=sys.stderr)
    sys.exit(1)

app = Flask(__name__, template_folder="templates")

# When behind a PHP proxy that strips the path prefix, tell Flask where it's mounted
# so url_for() generates URLs the browser can follow (e.g. /carlos/jupiter/overview).
if PANEL_BASE:
    _script_name = PANEL_BASE.rstrip("/")

    class _ScriptNameMiddleware:
        def __init__(self, wsgi_app, script_name):
            self.app = wsgi_app
            self.script_name = script_name

        def __call__(self, environ, start_response):
            environ["SCRIPT_NAME"] = self.script_name
            return self.app(environ, start_response)

    app.wsgi_app = _ScriptNameMiddleware(app.wsgi_app, _script_name)
app.secret_key = PANEL_SECRET


# ---- auth: token gate for all mutations ----

def _token_ok() -> bool:
    token = request.form.get("_token") or request.headers.get("X-Panel-Token", "")
    return hmac.compare_digest(token, PANEL_SECRET)


def require_token(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _token_ok():
            flash("Authentication required (invalid or missing token)", "error")
            return redirect(request.referrer or url_for("dashboard")), 403
        return fn(*args, **kwargs)
    return wrapper

import datetime as _dt
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Berlin")

@app.template_filter("datefmt")
def _datefmt(ts):
    try:
        dt = _dt.datetime.fromtimestamp(float(ts), tz=_TZ)
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return ""

@app.template_filter("datefmt_short")
def _datefmt_short(ts):
    try:
        dt = _dt.datetime.fromtimestamp(float(ts), tz=_TZ)
        return dt.strftime("%d.%m")
    except Exception:
        return ""

def _store() -> Store:
    return Store(cfg.DB_PATH)


# ---- helpers ----

def _coin_summary(store: Store, coin: dict) -> dict:
    lots = store.get_lots(coin["id"])
    deployed = sum(lot["cost"] for lot in lots)
    total_tokens = sum(lot["tokens"] for lot in lots)
    realized = store.realized_pnl(coin["id"])
    price, price_ts = store.latest_price_ts(coin["id"])
    position_value = total_tokens * price if price else 0.0
    unrealized = position_value - deployed
    return {
        "id":         coin["id"],
        "slug":       coin["slug"],
        "label":      coin["label"],
        "mint":       coin["mint"],
        "status":     coin["status"],
        "lots":       len(lots),
        "deployed":   round(deployed, 2),
        "realized":   round(realized, 2),
        "unrealized": round(unrealized, 2),
        "price":      price,
        "price_ts":   price_ts,
        "position":   round(position_value, 2),
        "tokens":     total_tokens,
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

    fleet_trades = store.fleet_trades(100)
    total_deposited = round(store.total_deposited(), 2)

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
    coin_row = store._c.execute("SELECT * FROM coins WHERE id = ?", (coin_id,)).fetchone()
    if not coin_row:
        flash("Coin not found", "error")
        return redirect(url_for("dashboard"))
    coin = dict(coin_row)
    lots = [dict(r) for r in store.get_lots(coin_id)]
    cfg_map = store.get_config(coin_id)
    overrides = [dict(r) for r in store.coin_overrides_list(coin_id)]
    catalog = [dict(r) for r in store.param_catalog_list()]
    trades = [dict(r) for r in store.coin_trades(coin_id)]
    price, price_ts = store.latest_price_ts(coin_id)
    realized = store.realized_pnl(coin_id)
    deployed = sum(l["cost"] for l in lots)
    total_tokens = sum(l["tokens"] for l in lots)
    position = total_tokens * price if price else 0.0
    return render_template(
        "coin.html",
        coin=coin, lots=lots, cfg=cfg_map,
        overrides=overrides, catalog=catalog, trades=trades,
        price=price, price_ts=price_ts,
        realized=round(realized, 2),
        deployed=round(deployed, 2),
        position=round(position, 2),
        unrealized=round(position - deployed, 2),
    )


@app.get("/coin/wizard")
def coin_wizard():
    store = _store()
    fleet_cfg = store.get_config(0)
    lot_usd = fleet_cfg.get("LOT_USD", "10")
    max_capital = fleet_cfg.get("MAX_CAPITAL_USD", "100")
    return render_template("wizard.html", lot_usd=lot_usd, max_capital=max_capital)


@app.get("/api/validate-mint")
def api_validate_mint():
    mint = request.args.get("mint", "").strip()
    if not mint or len(mint) < 32 or len(mint) > 50:
        return jsonify({"ok": False, "error": "Invalid mint address length"})
    import re
    if not re.match(r'^[1-9A-HJ-NP-Za-km-z]+$', mint):
        return jsonify({"ok": False, "error": "Invalid base58 characters in mint"})

    from execute import _get_json, DEXSCREENER_URL
    data = _get_json(DEXSCREENER_URL.format(mint=mint))
    if not data:
        return jsonify({"ok": False, "error": "DexScreener lookup failed (network error or rate limit)"})
    pairs = data.get("pairs") or []
    if not pairs:
        return jsonify({"ok": False, "error": "No trading pairs found for this mint on DexScreener"})

    token_name = pairs[0].get("baseToken", {}).get("name", "")
    token_symbol = pairs[0].get("baseToken", {}).get("symbol", "")
    price = None
    try:
        price = float(pairs[0].get("priceUsd", 0))
        if price <= 0:
            price = None
    except (TypeError, ValueError):
        pass

    ranked = sorted(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
    pair_list = []
    for p in ranked[:10]:
        liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
        pair_list.append({
            "address": p.get("pairAddress", ""),
            "dex": p.get("dexId", ""),
            "quote": p.get("quoteToken", {}).get("symbol", ""),
            "liquidity": round(liq, 2),
            "price": p.get("priceUsd"),
        })

    return jsonify({
        "ok": True,
        "name": token_name,
        "symbol": token_symbol,
        "price": price,
        "pairs": pair_list,
    })


@app.post("/coin/add")
@require_token
def coin_add():
    slug  = request.form.get("slug", "").strip().lower()
    label = request.form.get("label", "").strip()
    mint  = request.form.get("mint", "").strip()
    pair_address = request.form.get("pair_address", "").strip()
    try:
        decimals = int(request.form.get("decimals", "6") or "6")
    except ValueError:
        decimals = 6
    try:
        lot_usd = float(request.form.get("lot_usd", "10") or "10")
    except ValueError:
        lot_usd = 10.0
    try:
        max_capital = float(request.form.get("max_capital", "100") or "100")
    except ValueError:
        max_capital = 100.0
    if not slug or not mint:
        flash("slug and mint are required", "error")
        return redirect(url_for("coin_wizard"))
    import re
    if not re.match(r'^[a-z][a-z0-9_]{0,19}$', slug):
        flash("slug must be lowercase alphanumeric, start with letter, max 20 chars", "error")
        return redirect(url_for("coin_wizard"))
    if not re.match(r'^[1-9A-HJ-NP-Za-km-z]+$', mint) or len(mint) < 32:
        flash("Invalid mint address", "error")
        return redirect(url_for("coin_wizard"))
    store = _store()
    if store.get_coin(slug):
        flash(f"Coin '{slug}' already exists", "error")
        return redirect(url_for("coin_wizard"))
    try:
        coin_id = store.add_coin(slug, label or slug.upper(), mint, decimals=decimals)
        if pair_address:
            store._c.execute(
                "INSERT INTO coin_overrides (coin_id, key, value, author, reason)"
                " VALUES (?,?,?,?,?)",
                (coin_id, "PRICE_PAIR_ADDRESS", pair_address, "wizard", "pinned at add-coin"),
            )
        store._c.execute(
            "INSERT INTO coin_overrides (coin_id, key, value, author, reason)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(coin_id, key) DO UPDATE SET value=excluded.value,"
            " author=excluded.author, reason=excluded.reason",
            (coin_id, "LOT_USD", str(lot_usd), "wizard", "probation lot from wizard"),
        )
        store._c.execute(
            "INSERT INTO coin_overrides (coin_id, key, value, author, reason)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(coin_id, key) DO UPDATE SET value=excluded.value,"
            " author=excluded.author, reason=excluded.reason",
            (coin_id, "MAX_CAPITAL_USD", str(max_capital), "wizard", "set at add-coin"),
        )
        store._c.commit()
        flash(f"Coin '{slug}' added (paper mode, lot=${lot_usd}, cap=${max_capital})", "ok")
        return redirect(url_for("coin_detail", coin_id=coin_id))
    except Exception as e:
        flash(f"Error: {e}", "error")
    return redirect(url_for("coin_wizard"))


def _audit_live_config(store: Store, coin_id: int, slug: str) -> list[str]:
    """Run the static money audit before allowing paper->live. Returns a list of failures."""
    failures = []
    cfg_map = store.get_config(coin_id)

    money_keys = ["LOT_USD", "MAX_CAPITAL_USD", "BUY_STEP_PCT", "SELL_STEP_PCT",
                  "MAX_SLIPPAGE_BPS", "MAX_TRADE_USD"]
    for k in money_keys:
        if k not in cfg_map:
            failures.append(f"Missing money-critical key: {k}")

    lot_usd = float(cfg_map.get("LOT_USD", 0))
    if lot_usd <= 0:
        failures.append(f"LOT_USD must be > 0 (got {lot_usd})")

    max_cap = float(cfg_map.get("MAX_CAPITAL_USD", 0))
    if max_cap <= 0:
        failures.append(f"MAX_CAPITAL_USD must be > 0 (got {max_cap})")

    slippage = float(cfg_map.get("MAX_SLIPPAGE_BPS", 0))
    if slippage <= 0 or slippage > 500:
        failures.append(f"MAX_SLIPPAGE_BPS must be 1-500 (got {slippage})")

    if not cfg.ALERT_EMAIL:
        failures.append("ALERT_EMAIL not set in environment")

    if not os.path.exists(cfg.KEYPAIR_PATH):
        failures.append(f"KEYPAIR_PATH not found: {cfg.KEYPAIR_PATH}")

    return failures


@app.post("/coin/<int:coin_id>/status")
@require_token
def coin_status(coin_id: int):
    status = request.form.get("status", "")
    if status not in ("paper", "live", "paused"):
        flash("Invalid status", "error")
        return redirect(url_for("coin_detail", coin_id=coin_id))

    store = _store()
    coin = store._c.execute("SELECT * FROM coins WHERE id = ?", (coin_id,)).fetchone()
    if not coin:
        flash("Coin not found", "error")
        return redirect(url_for("dashboard"))

    # Gate: paper->live requires audit + slug confirmation
    if status == "live" and coin["status"] != "live":
        confirm_slug = request.form.get("confirm_slug", "").strip().lower()
        if confirm_slug != coin["slug"]:
            flash(f"To go live, type the slug '{coin['slug']}' to confirm", "error")
            return redirect(url_for("coin_detail", coin_id=coin_id))

        failures = _audit_live_config(store, coin_id, coin["slug"])
        if failures:
            for f in failures:
                flash(f"Audit FAIL: {f}", "error")
            flash("Cannot go live — fix the audit failures above", "error")
            return redirect(url_for("coin_detail", coin_id=coin_id))

    store.set_coin_status(coin_id, status)
    flash(f"Status set to '{status}'", "ok")
    return redirect(url_for("coin_detail", coin_id=coin_id))


@app.post("/coin/<int:coin_id>/config")
@require_token
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
@require_token
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
    _lt = last_trade["ts"] if last_trade else None
    try:
        last_trade_ts = _dt.datetime.fromisoformat(str(_lt)).timestamp() if _lt else None
    except (ValueError, TypeError):
        last_trade_ts = None

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
    total_deposited = round(store.total_deposited(), 2)

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
    catalog = store.param_catalog_list()

    # Fleet defaults (pinned version)
    pinned = store._c.execute(
        "SELECT version_id FROM fleet_versions WHERE pinned=1 ORDER BY version_id DESC LIMIT 1"
    ).fetchone()
    fleet_defaults = {}
    if pinned:
        rows = store._c.execute(
            "SELECT key, value FROM fleet_defaults WHERE version_id=?",
            (pinned["version_id"],),
        ).fetchall()
        fleet_defaults = {r["key"]: r["value"] for r in rows}

    # Active coins + their effective configs + which keys are overridden
    coins = store.get_active_coins()
    coin_configs = {}
    for coin in coins:
        effective = store.get_config(coin["id"])
        ov_rows = store._c.execute(
            "SELECT key FROM coin_overrides WHERE coin_id=?", (coin["id"],)
        ).fetchall()
        override_keys = [r["key"] for r in ov_rows]
        coin_configs[coin["id"]] = {
            "effective": effective,
            "overrides": override_keys,
        }

    return render_template(
        "glossary.html",
        catalog=[dict(r) for r in catalog],
        coins=[dict(r) for r in coins],
        fleet_defaults=fleet_defaults,
        coin_configs=coin_configs,
    )


@app.get("/money")
def money():
    store = _store()
    rows = store.deposit_withdraw_history()
    balance = store.usdc_balance()
    total_dep = round(store.total_deposited(), 2)
    total_with = round(store.total_withdrawn(), 2)
    # Real vs invested: current portfolio value minus total deposits
    coins = store.list_coins()
    portfolio_value = balance
    for c in coins:
        lots = store.get_lots(c["id"])
        price = store.latest_price(c["id"])
        if price and lots:
            portfolio_value += sum(l["tokens"] for l in lots) * price
    real_vs_invested = round(portfolio_value - total_dep + total_with, 2)
    return render_template(
        "money.html",
        ledger=[dict(r) for r in rows],
        balance=round(balance, 2),
        total_deposited=total_dep,
        total_withdrawn=total_with,
        portfolio_value=round(portfolio_value, 2),
        real_vs_invested=real_vs_invested,
    )


@app.post("/withdraw")
@require_token
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
@require_token
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


@app.context_processor
def _inject_base():
    return {"panel_base": PANEL_BASE, "panel_token": PANEL_SECRET}


if __name__ == "__main__":
    port = int(os.environ.get("PANEL_PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
