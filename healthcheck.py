"""healthcheck.py — Independent read-only watchdog for Jupiter.

Run from cron or manually. Checks DB integrity, scheduler liveness,
price feed staleness, ledger consistency, config audit, and exec bits.
Never mutates state (SIMD #889 doctrine).

Usage:
    python3 healthcheck.py                     # all checks
    python3 healthcheck.py --check db          # single check
    python3 healthcheck.py --json              # machine-readable output

Exit codes: 0 = all green, 1 = warnings, 2 = critical failures.
"""
from __future__ import annotations
import argparse
import json
import os
import pathlib
import sqlite3
import stat
import sys
import time

# Healthcheck is read-only. It opens the DB with a SEPARATE read-only connection
# (not via Store) so it cannot accidentally mutate anything.

TICK_CADENCE_S = 120      # alert if last tick older than 2 * expected 60s
PRICE_STALE_S = 300       # 5 min without an accepted price = stale
WAL_MAX_MB = 100          # WAL file > 100 MB is unusual
GATE_STUCK_TICKS = 10     # price gate rejecting N consecutive ticks = stuck


def _open_readonly(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    uri = f"file:{db_path}?mode=ro"
    c = sqlite3.connect(uri, uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


class CheckResult:
    def __init__(self, name: str):
        self.name = name
        self.status = "ok"
        self.messages: list[str] = []

    def warn(self, msg: str):
        self.messages.append(f"WARN: {msg}")
        if self.status == "ok":
            self.status = "warn"

    def crit(self, msg: str):
        self.messages.append(f"CRIT: {msg}")
        self.status = "crit"

    def info(self, msg: str):
        self.messages.append(f"INFO: {msg}")

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "messages": self.messages}


def check_db(db_path: str) -> CheckResult:
    r = CheckResult("db_integrity")
    c = _open_readonly(db_path)
    try:
        ic = c.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            r.crit(f"integrity_check: {ic}")
        else:
            r.info("integrity_check: ok")

        jm = c.execute("PRAGMA journal_mode").fetchone()[0]
        if jm != "wal":
            r.warn(f"journal_mode is {jm}, expected wal")

        fk = c.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            r.crit(f"foreign_key_check: {len(fk)} violation(s)")

        wal_path = db_path + "-wal"
        if os.path.exists(wal_path):
            wal_mb = os.path.getsize(wal_path) / (1024 * 1024)
            if wal_mb > WAL_MAX_MB:
                r.warn(f"WAL file is {wal_mb:.0f} MB (> {WAL_MAX_MB} MB)")
            else:
                r.info(f"WAL size: {wal_mb:.1f} MB")
    finally:
        c.close()
    return r


def check_liveness(db_path: str) -> CheckResult:
    r = CheckResult("scheduler_liveness")
    c = _open_readonly(db_path)
    try:
        coins = c.execute(
            "SELECT id, slug, status FROM coins WHERE status IN ('paper','live')"
        ).fetchall()
        if not coins:
            r.info("No active coins")
            return r

        now = time.time()
        for coin in coins:
            last = c.execute(
                "SELECT MAX(ts) as last_ts FROM price_history WHERE coin_id = ?",
                (coin["id"],),
            ).fetchone()
            last_ts = last["last_ts"] if last and last["last_ts"] else None
            if last_ts is None:
                r.warn(f"{coin['slug']}: no price history at all")
            else:
                age = now - last_ts
                if age > TICK_CADENCE_S:
                    r.warn(f"{coin['slug']}: last tick {age:.0f}s ago (threshold {TICK_CADENCE_S}s)")
                else:
                    r.info(f"{coin['slug']}: last tick {age:.0f}s ago")
    finally:
        c.close()
    return r


def check_price_feed(db_path: str) -> CheckResult:
    r = CheckResult("price_feed")
    c = _open_readonly(db_path)
    try:
        coins = c.execute(
            "SELECT id, slug FROM coins WHERE status IN ('paper','live')"
        ).fetchall()
        now = time.time()
        for coin in coins:
            last = c.execute(
                "SELECT ts, price FROM price_history WHERE coin_id = ? ORDER BY ts DESC LIMIT 1",
                (coin["id"],),
            ).fetchone()
            if last is None:
                r.warn(f"{coin['slug']}: no price data")
                continue
            age = now - last["ts"]
            if age > PRICE_STALE_S:
                r.warn(f"{coin['slug']}: price stale ({age:.0f}s, last={last['price']:.8f})")
            else:
                r.info(f"{coin['slug']}: price fresh ({age:.0f}s ago, {last['price']:.8f})")

            # Gate stuck detection: check coin_state for consecutive rejections
            gate_n_row = c.execute(
                "SELECT value FROM coin_state WHERE coin_id = ? AND key = 'price_gate_cand_n'",
                (coin["id"],),
            ).fetchone()
            if gate_n_row:
                gate_n = int(gate_n_row["value"])
                if gate_n >= GATE_STUCK_TICKS:
                    r.warn(f"{coin['slug']}: price gate stuck in consensus ({gate_n} consecutive rejections)")
    finally:
        c.close()
    return r


def check_ledger(db_path: str) -> CheckResult:
    r = CheckResult("ledger_consistency")
    c = _open_readonly(db_path)
    try:
        rows = c.execute(
            "SELECT id, delta, balance_after, kind, coin_id FROM usdc_ledger ORDER BY id ASC"
        ).fetchall()
        if not rows:
            r.info("Ledger empty")
            return r

        # Verify balance_after chain is contiguous
        running = 0.0
        for row in rows:
            expected = running + row["delta"]
            if abs(row["balance_after"] - expected) > 0.005:
                r.crit(f"Ledger gap at id={row['id']}: expected {expected:.4f}, got {row['balance_after']:.4f}")
                break
            running = row["balance_after"]
        else:
            r.info(f"Ledger chain intact ({len(rows)} entries, balance={running:.2f})")

        # Check for orphaned reserves (reserve with no subsequent release/buy)
        orphans = c.execute("""
            SELECT r.id, r.coin_id, ABS(r.delta) AS amount
            FROM usdc_ledger r
            WHERE r.kind = 'reserve'
              AND NOT EXISTS (
                SELECT 1 FROM usdc_ledger f
                WHERE f.coin_id = r.coin_id
                  AND f.kind IN ('release', 'buy')
                  AND f.id > r.id
              )
        """).fetchall()
        if orphans:
            total = sum(o["amount"] for o in orphans)
            r.warn(f"{len(orphans)} orphaned reserve(s) locking ${total:.2f}")
        else:
            r.info("No orphaned reserves")
    finally:
        c.close()
    return r


def check_config_audit(db_path: str) -> CheckResult:
    r = CheckResult("config_audit")
    c = _open_readonly(db_path)
    try:
        live_coins = c.execute(
            "SELECT id, slug FROM coins WHERE status = 'live'"
        ).fetchall()
        if not live_coins:
            r.info("No live coins to audit")
            return r

        # Get pinned fleet version
        pinned = c.execute(
            "SELECT version_id FROM fleet_versions WHERE pinned = 1 ORDER BY version_id DESC LIMIT 1"
        ).fetchone()
        if not pinned:
            r.warn("No pinned fleet version")
            return r

        # Money-critical keys that must be explicit for live coins
        money_keys = {"LOT_USD", "MAX_CAPITAL_USD", "BUY_STEP_PCT", "SELL_STEP_PCT"}

        for coin in live_coins:
            cfg = {}
            fleet_rows = c.execute(
                "SELECT key, value FROM fleet_defaults WHERE version_id = ?",
                (pinned["version_id"],),
            ).fetchall()
            cfg.update({row["key"]: row["value"] for row in fleet_rows})
            overrides = c.execute(
                "SELECT key, value FROM coin_overrides WHERE coin_id = ?",
                (coin["id"],),
            ).fetchall()
            cfg.update({row["key"]: row["value"] for row in overrides})

            missing = money_keys - set(cfg.keys())
            if missing:
                r.warn(f"{coin['slug']}: missing money-critical keys: {', '.join(sorted(missing))}")
            else:
                r.info(f"{coin['slug']}: money-critical keys present")
    finally:
        c.close()
    return r


def check_exec_bits(base_dir: str) -> CheckResult:
    r = CheckResult("exec_bits")
    base = pathlib.Path(base_dir)

    scripts_that_need_exec = [
        "deploy/keepalive.sh",
        "deploy/update.sh",
        "deploy/install.sh",
    ]

    for rel in scripts_that_need_exec:
        p = base / rel
        if not p.exists():
            r.info(f"{rel}: not present (skipped)")
            continue
        mode = p.stat().st_mode
        if not (mode & stat.S_IXUSR):
            r.warn(f"{rel}: missing +x (user execute bit not set)")
        else:
            r.info(f"{rel}: +x ok")

    # Python entry points invoked by systemd/cron — these should be invoked as
    # `python3 script.py`, so exec bit is nice-to-have, not critical. But if a
    # unit file uses `ExecStart=/path/to/script.py` directly, it needs +x.
    service_files = list((base / "deploy").glob("*.service")) if (base / "deploy").exists() else []
    for sf in service_files:
        text = sf.read_text()
        for line in text.splitlines():
            if line.strip().startswith("ExecStart=") and not line.strip().startswith("ExecStart=/usr"):
                # Extract the script path from ExecStart
                exec_path = line.split("=", 1)[1].strip().split()[0]
                if exec_path.endswith(".py"):
                    full = base / exec_path.lstrip("/")
                    if full.exists() and not (full.stat().st_mode & stat.S_IXUSR):
                        r.warn(f"{sf.name} ExecStart references {exec_path} without +x")

    return r


ALL_CHECKS = {
    "db": check_db,
    "liveness": check_liveness,
    "price_feed": check_price_feed,
    "ledger": check_ledger,
    "config": check_config_audit,
    "exec": check_exec_bits,
}


def run_healthcheck(db_path: str, base_dir: str,
                    checks: list[str] | None = None,
                    as_json: bool = False) -> int:
    results: list[CheckResult] = []
    run_checks = checks or list(ALL_CHECKS.keys())

    for name in run_checks:
        fn = ALL_CHECKS.get(name)
        if fn is None:
            print(f"Unknown check: {name}", file=sys.stderr)
            continue
        try:
            if name == "exec":
                result = fn(base_dir)
            else:
                result = fn(db_path)
            results.append(result)
        except Exception as e:
            cr = CheckResult(name)
            cr.crit(f"Check raised: {e}")
            results.append(cr)

    if as_json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            icon = {"ok": "+", "warn": "!", "crit": "X"}[r.status]
            print(f"[{icon}] {r.name}: {r.status.upper()}")
            for msg in r.messages:
                print(f"    {msg}")
        print()
        crits = sum(1 for r in results if r.status == "crit")
        warns = sum(1 for r in results if r.status == "warn")
        if crits:
            print(f"CRITICAL: {crits} check(s) failed")
        elif warns:
            print(f"WARNING: {warns} check(s) have warnings")
        else:
            print("All checks passed.")

    has_crit = any(r.status == "crit" for r in results)
    has_warn = any(r.status == "warn" for r in results)
    if has_crit:
        return 2
    if has_warn:
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="Jupiter healthcheck (read-only)")
    parser.add_argument("--db", default=os.environ.get("JUPITER_DB", "store/jupiter.db"),
                        help="Path to the SQLite DB")
    parser.add_argument("--base", default=".",
                        help="Base directory of the Jupiter install")
    parser.add_argument("--check", action="append",
                        help="Run only specific check(s). Can repeat.")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--list", action="store_true",
                        help="List available checks")
    args = parser.parse_args()

    if args.list:
        for name in ALL_CHECKS:
            print(f"  {name}")
        return

    sys.exit(run_healthcheck(args.db, args.base, args.check, args.json))


if __name__ == "__main__":
    main()
