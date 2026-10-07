"""store.py — SQLite access layer for Jupiter.

One connection per thread (threading.local). Schema applied at open().
All money mutations go through the usdc_ledger reserve/commit/release protocol
so concurrent per-coin decisions in one tick never double-spend.
"""
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
                 status: str = "paper", migrated_from: str | None = None) -> int:
        cur = self._c.execute(
            "INSERT INTO coins (slug, label, mint, status, migrated_from) VALUES (?,?,?,?,?)",
            (slug, label, mint, status, migrated_from),
        )
        self._c.commit()
        return cur.lastrowid

    def set_coin_status(self, coin_id: int, status: str):
        self._c.execute(
            "UPDATE coins SET status = ? WHERE id = ?", (status, coin_id)
        )
        self._c.commit()

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
        self._c.commit()
        return cur.lastrowid

    def remove_lot(self, lot_id: int):
        self._c.execute("DELETE FROM lots WHERE id = ?", (lot_id,))
        self._c.commit()

    def update_lot_trail(self, lot_id: int, trail_armed: int, trail_peak: float):
        self._c.execute(
            "UPDATE lots SET trail_armed = ?, trail_peak = ? WHERE id = ?",
            (trail_armed, trail_peak, lot_id),
        )
        self._c.commit()

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
        with self._c:
            free = self.usdc_balance()
            if free < amount:
                return False
            self._ledger_entry(-amount, "reserve", coin_id=coin_id)
            return True

    def usdc_release(self, amount: float, coin_id: int):
        """Release a previously reserved amount (swap failed or skipped)."""
        with self._c:
            self._ledger_entry(amount, "release", coin_id=coin_id)

    def usdc_commit_buy(self, amount: float, coin_id: int, txsig: str):
        """Mark a buy as executed (reservation already applied the debit)."""
        with self._c:
            self._c.execute(
                "INSERT INTO trades (coin_id, ts, mode, side, signal, price, tokens, usd, pnl_pct, txsig)"
                " SELECT c.id, datetime('now'), c.status, 'buy', 'grid', 0, 0, ?, null, ?"
                " FROM coins c WHERE c.id = ?",
                (amount, txsig, coin_id),
            )

    def usdc_commit_sell(self, proceeds: float, coin_id: int, txsig: str,
                         price: float, tokens: float, pnl_pct: float, mode: str):
        with self._c:
            self._ledger_entry(proceeds, "sell", coin_id=coin_id, txsig=txsig)
            self._c.execute(
                "INSERT INTO trades (coin_id, ts, mode, side, signal, price, tokens, usd, pnl_pct, txsig)"
                " VALUES (?, datetime('now'), ?, 'sell', 'grid', ?, ?, ?, ?, ?)",
                (coin_id, mode, price, tokens, proceeds, pnl_pct, txsig),
            )

    def usdc_deposit(self, amount: float):
        with self._c:
            self._ledger_entry(amount, "deposit")
            self._c.commit()

    # ---- price history ----

    def record_price(self, coin_id: int, ts: float, price: float):
        self._c.execute(
            "INSERT OR REPLACE INTO price_history (coin_id, ts, price) VALUES (?,?,?)",
            (coin_id, ts, price),
        )
        self._c.commit()

    def price_window(self, coin_id: int, since_ts: float) -> list[float]:
        rows = self._c.execute(
            "SELECT price FROM price_history WHERE coin_id = ? AND ts >= ? ORDER BY ts ASC",
            (coin_id, since_ts),
        ).fetchall()
        return [r["price"] for r in rows]

    # ---- capital ----

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
        self._c.commit()
