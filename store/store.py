"""store.py — SQLite access layer for Jupiter.

One connection per thread (threading.local). Schema applied at open().
All money mutations go through the usdc_ledger reserve/commit/release protocol
so concurrent per-coin decisions in one tick never double-spend.
"""
from __future__ import annotations
import os
import sqlite3
import threading
import pathlib

_local = threading.local()
_SCHEMA = pathlib.Path(__file__).parent / "schema.sql"


def _conn(db_path: str) -> sqlite3.Connection:
    if not hasattr(_local, "conns"):
        _local.conns = {}
    if db_path not in _local.conns:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        c = sqlite3.connect(db_path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.executescript(_SCHEMA.read_text())
        c.commit()
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"DB integrity_check failed: {ok}")
        _local.conns[db_path] = c
    return _local.conns[db_path]


class Store:
    def __init__(self, db_path: str):
        self._path = db_path
        self._c = _conn(db_path)
        self._in_tx = False

    def begin(self):
        self._c.execute("BEGIN IMMEDIATE")
        self._in_tx = True

    def commit(self):
        self._c.commit()
        self._in_tx = False

    def rollback(self):
        self._c.rollback()
        self._in_tx = False

    def _auto_commit(self):
        if not self._in_tx:
            self._c.commit()

    # ---- coins ----

    def get_coin(self, slug: str) -> sqlite3.Row | None:
        return self._c.execute(
            "SELECT * FROM coins WHERE slug = ?", (slug,)
        ).fetchone()

    def list_coins(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self._c.execute(
                "SELECT * FROM coins WHERE status = ?", (status,)
            ).fetchall()
        return self._c.execute("SELECT * FROM coins").fetchall()

    def add_coin(self, slug: str, label: str, mint: str,
                 status: str = "paper", migrated_from: str | None = None,
                 decimals: int = 6) -> int:
        cur = self._c.execute(
            "INSERT INTO coins (slug, label, mint, status, migrated_from, decimals)"
            " VALUES (?,?,?,?,?,?)",
            (slug, label, mint, status, migrated_from, decimals),
        )
        self._auto_commit()
        return cur.lastrowid

    def set_coin_status(self, coin_id: int, status: str):
        self._c.execute(
            "UPDATE coins SET status = ? WHERE id = ?", (status, coin_id)
        )
        self._auto_commit()

    def get_active_coins(self) -> list[sqlite3.Row]:
        return self._c.execute(
            "SELECT * FROM coins WHERE status IN ('paper','live')"
        ).fetchall()

    def get_trades(self, coin_id: int) -> list[sqlite3.Row]:
        return self._c.execute(
            "SELECT * FROM trades WHERE coin_id = ? ORDER BY ts ASC", (coin_id,)
        ).fetchall()

    # ---- config resolution ----

    def get_config(self, coin_id: int) -> dict:
        """Effective config for a coin: pinned fleet_defaults merged with coin_overrides."""
        pinned = self._c.execute(
            "SELECT version_id FROM fleet_versions WHERE pinned = 1 ORDER BY version_id DESC LIMIT 1"
        ).fetchone()
        result = {}
        if pinned:
            rows = self._c.execute(
                "SELECT key, value FROM fleet_defaults WHERE version_id = ?",
                (pinned["version_id"],),
            ).fetchall()
            result = {r["key"]: r["value"] for r in rows}
        # coin overrides win
        overrides = self._c.execute(
            "SELECT key, value FROM coin_overrides WHERE coin_id = ?", (coin_id,)
        ).fetchall()
        result.update({r["key"]: r["value"] for r in overrides})
        return result

    # ---- lots ----

    def get_lots(self, coin_id: int) -> list[sqlite3.Row]:
        return self._c.execute(
            "SELECT * FROM lots WHERE coin_id = ? ORDER BY ts ASC", (coin_id,)
        ).fetchall()

    def add_lot(self, coin_id: int, tokens: float, cost: float,
                buy_price: float, ts: float, origin: str) -> int:
        cur = self._c.execute(
            "INSERT INTO lots (coin_id, tokens, cost, buy_price, ts, origin)"
            " VALUES (?,?,?,?,?,?)",
            (coin_id, tokens, cost, buy_price, ts, origin),
        )
        self._auto_commit()
        return cur.lastrowid

    def remove_lot(self, lot_id: int):
        self._c.execute("DELETE FROM lots WHERE id = ?", (lot_id,))
        self._auto_commit()

    def update_lot_trail(self, lot_id: int, trail_armed: int, trail_peak: float):
        self._c.execute(
            "UPDATE lots SET trail_armed = ?, trail_peak = ? WHERE id = ?",
            (trail_armed, trail_peak, lot_id),
        )
        self._auto_commit()

    def update_lot_origin(self, lot_id: int, origin: str):
        self._c.execute(
            "UPDATE lots SET origin = ? WHERE id = ?", (origin, lot_id),
        )
        self._auto_commit()

    def set_lot_levels_to(self, lot_id: int, target_id: int | None):
        self._c.execute(
            "UPDATE lots SET levels_to = ? WHERE id = ?", (target_id, lot_id),
        )
        self._auto_commit()

    def remove_lots(self, lot_ids: list[int]):
        if not lot_ids:
            return
        placeholders = ",".join("?" * len(lot_ids))
        self._c.execute(f"DELETE FROM lots WHERE id IN ({placeholders})", lot_ids)
        self._auto_commit()

    # ---- USDC ledger (reserve / commit / release) ----

    def usdc_balance(self) -> float:
        row = self._c.execute(
            "SELECT balance_after FROM usdc_ledger ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["balance_after"] if row else 0.0

    def _ledger_entry(self, delta: float, kind: str,
                      coin_id: int | None = None, txsig: str | None = None):
        bal = self.usdc_balance() + delta
        self._c.execute(
            "INSERT INTO usdc_ledger (delta, balance_after, kind, coin_id, txsig)"
            " VALUES (?,?,?,?,?)",
            (delta, bal, kind, coin_id, txsig),
        )

    def usdc_reserve(self, amount: float, coin_id: int) -> bool:
        """Lock `amount` USDC for a pending buy. Returns False if insufficient balance."""
        free = self.usdc_balance()
        if free < amount:
            return False
        self._ledger_entry(-amount, "reserve", coin_id=coin_id)
        self._auto_commit()
        return True

    def usdc_release(self, amount: float, coin_id: int):
        """Release a previously reserved amount (swap failed or skipped)."""
        self._ledger_entry(amount, "release", coin_id=coin_id)
        self._auto_commit()

    def usdc_commit_buy(self, amount: float, coin_id: int, txsig: str):
        """Mark a buy as executed (reservation already applied the debit).

        Writes a delta=0 'buy' marker to usdc_ledger so that reconcile_reserves()
        can distinguish a completed buy (reserve + buy marker) from an orphaned
        one (reserve only, no follow-up entry).
        """
        self._ledger_entry(0.0, "buy", coin_id=coin_id, txsig=txsig)
        self._c.execute(
            "INSERT INTO trades (coin_id, ts, mode, side, signal, price, tokens, usd, pnl_pct, txsig)"
            " SELECT c.id, datetime('now'), c.status, 'buy', 'grid', 0, 0, ?, null, ?"
            " FROM coins c WHERE c.id = ?",
            (amount, txsig, coin_id),
        )
        self._auto_commit()

    def usdc_commit_sell(self, proceeds: float, coin_id: int, txsig: str,
                         price: float, tokens: float, pnl_pct: float, mode: str):
        self._ledger_entry(proceeds, "sell", coin_id=coin_id, txsig=txsig)
        self._c.execute(
            "INSERT INTO trades (coin_id, ts, mode, side, signal, price, tokens, usd, pnl_pct, txsig)"
            " VALUES (?, datetime('now'), ?, 'sell', 'grid', ?, ?, ?, ?, ?)",
            (coin_id, mode, price, tokens, proceeds, pnl_pct, txsig),
        )
        self._auto_commit()

    def usdc_deposit(self, amount: float):
        self._ledger_entry(amount, "deposit")
        self._auto_commit()

    # ---- price history ----

    def record_price(self, coin_id: int, ts: float, price: float):
        self._c.execute(
            "INSERT OR REPLACE INTO price_history (coin_id, ts, price) VALUES (?,?,?)",
            (coin_id, ts, price),
        )
        self._auto_commit()

    def price_window(self, coin_id: int, since_ts: float) -> list[float]:
        rows = self._c.execute(
            "SELECT price FROM price_history WHERE coin_id = ? AND ts >= ? ORDER BY ts ASC",
            (coin_id, since_ts),
        ).fetchall()
        return [r["price"] for r in rows]

    def price_window_rows(self, coin_id: int, since_ts: float) -> list[sqlite3.Row]:
        return self._c.execute(
            "SELECT ts, price FROM price_history WHERE coin_id = ? AND ts >= ? ORDER BY ts ASC",
            (coin_id, since_ts),
        ).fetchall()

    # ---- decisions ----

    def log_decision(self, coin_id: int, ts: float, action: str,
                     reason: str, price: float | None = None,
                     detail: dict | None = None):
        import json as _json
        self._c.execute(
            "INSERT INTO decisions (coin_id, ts, action, reason, price, detail)"
            " VALUES (?,?,?,?,?,?)",
            (coin_id, ts, action, reason, price,
             _json.dumps(detail) if detail else None),
        )

    def get_decisions(self, coin_id: int, limit: int = 50) -> list[sqlite3.Row]:
        return self._c.execute(
            "SELECT * FROM decisions WHERE coin_id = ? ORDER BY ts DESC LIMIT ?",
            (coin_id, limit),
        ).fetchall()

    def prune_decisions(self, max_age_days: int = 30):
        import time
        cutoff = time.time() - max_age_days * 86400
        self._c.execute("DELETE FROM decisions WHERE ts < ?", (cutoff,))
        self._auto_commit()

    # ---- capital ----

    def fleet_deployed_usd(self) -> float:
        """Total deployed USD across all active coins."""
        row = self._c.execute(
            "SELECT COALESCE(SUM(deployed_usd), 0.0) AS total FROM capital"
            " WHERE coin_id IN (SELECT id FROM coins WHERE status IN ('paper','live'))"
        ).fetchone()
        return row["total"]

    def get_capital(self, coin_id: int) -> sqlite3.Row | None:
        return self._c.execute(
            "SELECT * FROM capital WHERE coin_id = ?", (coin_id,)
        ).fetchone()

    def set_capital(self, coin_id: int, deployed_usd: float, max_usd: float | None = None):
        self._c.execute(
            "INSERT INTO capital (coin_id, deployed_usd, max_usd) VALUES (?,?,?)"
            " ON CONFLICT(coin_id) DO UPDATE SET deployed_usd=excluded.deployed_usd,"
            " max_usd=excluded.max_usd, ts=datetime('now')",
            (coin_id, deployed_usd, max_usd),
        )
        self._auto_commit()

    # ---- coin_state (per-coin KV) ----

    def state_get(self, coin_id: int, key: str, default: str | None = None) -> str | None:
        row = self._c.execute(
            "SELECT value FROM coin_state WHERE coin_id = ? AND key = ?",
            (coin_id, key),
        ).fetchone()
        return row["value"] if row else default

    def state_get_float(self, coin_id: int, key: str, default: float = 0.0) -> float:
        v = self.state_get(coin_id, key)
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def state_get_int(self, coin_id: int, key: str, default: int = 0) -> int:
        v = self.state_get(coin_id, key)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def state_set(self, coin_id: int, key: str, value) -> None:
        self._c.execute(
            "INSERT INTO coin_state (coin_id, key, value) VALUES (?,?,?)"
            " ON CONFLICT(coin_id, key) DO UPDATE SET value = excluded.value",
            (coin_id, key, str(value)),
        )

    def state_set_commit(self, coin_id: int, key: str, value) -> None:
        self.state_set(coin_id, key, value)
        self._auto_commit()

    def state_mget(self, coin_id: int) -> dict[str, str]:
        rows = self._c.execute(
            "SELECT key, value FROM coin_state WHERE coin_id = ?", (coin_id,)
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def state_mset(self, coin_id: int, updates: dict[str, str | float | int]) -> None:
        for k, v in updates.items():
            self.state_set(coin_id, k, v)

    def state_mset_commit(self, coin_id: int, updates: dict[str, str | float | int]) -> None:
        self.state_mset(coin_id, updates)
        self._auto_commit()

    def state_delete(self, coin_id: int, key: str) -> None:
        self._c.execute(
            "DELETE FROM coin_state WHERE coin_id = ? AND key = ?",
            (coin_id, key),
        )
        self._auto_commit()

    # ---- startup reconciliation ----

    def reconcile_reserves(self) -> int:
        """Release any 'reserve' ledger entries orphaned by a crash.

        A reserve entry is orphaned when there is no later 'release' or 'buy'
        entry for the same coin_id, meaning the scheduler crashed after reserving
        USDC but before committing or releasing it. Each orphan is released so the
        balance is restored and the USDC is spendable again.

        Returns the number of orphaned entries released.
        """
        import logging
        log = logging.getLogger("store.reconcile")

        # Find every reserve entry that has no subsequent release or buy for its coin
        orphans = self._c.execute(
            """
            SELECT r.id, r.coin_id, ABS(r.delta) AS amount
            FROM usdc_ledger r
            WHERE r.kind = 'reserve'
              AND NOT EXISTS (
                SELECT 1 FROM usdc_ledger f
                WHERE f.coin_id = r.coin_id
                  AND f.kind IN ('release', 'buy')
                  AND f.id > r.id
              )
            ORDER BY r.id ASC
            """
        ).fetchall()

        for row in orphans:
            amount = row["amount"]
            coin_id = row["coin_id"]
            log.warning(
                "reconcile: releasing orphaned reserve id=%d coin=%s amount=%.2f",
                row["id"], coin_id, amount,
            )
            self._ledger_entry(amount, "release", coin_id=coin_id)
            self._auto_commit()

        return len(orphans)
