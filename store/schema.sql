-- Jupiter_Solana_Meme_Trader -- v1 store schema (#1). SQLite in WAL mode.
-- A COIN IS DATA (a row), never a directory. One app, one store, one scheduler
-- over all coins. Secrets (keypair, SMTP, API keys, RPC) are NOT in this DB.
-- Hard rules (quote=USDC, never-sell-at-a-loss, SOL gas reserve) are CODE, not rows.
--
-- Apply at boot; the app runs `PRAGMA integrity_check` and refuses to start if it
-- fails (Dot). WAL = concurrent readers + 1 writer. Source of truth for cash stays
-- ON-CHAIN (#385): decisions read the live balance; usdc_ledger here is the
-- anti-theft mirror + the reservation locks.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- A coin = one row. Add-coin from the page inserts here (status='paper'); a coin
-- flips paper->live IN PLACE, so per-ROW mode is kept in trades (status is not enough).
CREATE TABLE IF NOT EXISTS coins (
    id            INTEGER PRIMARY KEY,
    slug          TEXT UNIQUE NOT NULL,
    label         TEXT NOT NULL,
    mint          TEXT UNIQUE NOT NULL,   -- a mint identifies a coin: no two rows share one
    -- quote is ALWAYS USDC (hard rule in code); intentionally not a column.
    status        TEXT NOT NULL DEFAULT 'paper'
                  CHECK (status IN ('paper','live','paused')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    migrated_from TEXT
);

-- Parameter catalog (docs/param_catalog_v1.csv). tier's 4th value is 'hidden'
-- (secrets) for forward-compat, though secrets never actually land in the DB.
-- 'text' is a TYPE, not a tier.
CREATE TABLE IF NOT EXISTS param_catalog (
    key          TEXT PRIMARY KEY,
    tier         TEXT NOT NULL
                 CHECK (tier IN ('editable','locked-visible','experimental','hidden')),
    scope        TEXT NOT NULL
                 CHECK (scope IN ('fleet-only','coin-allowed','coin-only')),
    type         TEXT NOT NULL
                 CHECK (type IN ('enum','float','int','bool','text')),
    min          REAL, max REAL,
    default_val  TEXT, recommended TEXT,
    label        TEXT, help TEXT
);

-- Fleet defaults: each version is a FULL SNAPSHOT (copy-forward unchanged keys),
-- so "what config is live" is one indexed read, not a history walk (Dot). Header
-- row carries author/reason; healthcheck pins `pinned`.
CREATE TABLE IF NOT EXISTS fleet_versions (
    version_id   INTEGER PRIMARY KEY,
    author       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    ts           TEXT NOT NULL DEFAULT (datetime('now')),
    pinned       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fleet_defaults (
    version_id   INTEGER NOT NULL REFERENCES fleet_versions(version_id),
    key          TEXT NOT NULL REFERENCES param_catalog(key),
    value        TEXT NOT NULL,
    PRIMARY KEY (version_id, key)          -- full snapshot: every key present per version
);

-- Sparse per-coin overrides. The row IS the badge; author+reason are MANDATORY
-- (a badge without a reason is #894 with better furniture, Dot). Deleting the row
-- returns the coin to the fleet default. DAO rejects keys whose scope != coin-*.
CREATE TABLE IF NOT EXISTS coin_overrides (
    coin_id      INTEGER NOT NULL REFERENCES coins(id) ON DELETE CASCADE,
    key          TEXT NOT NULL REFERENCES param_catalog(key),
    value        TEXT NOT NULL,
    author       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    ts           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (coin_id, key)
);
-- effective(coin,key) = coin_overrides.value ?? fleet_defaults[pinned].value ?? catalog.default_val

-- Only a coin-scoped key may be overridden per coin (scope coin-allowed|coin-only).
-- This is Dot's "CHECK(scope LIKE 'coin-%')" intent; a column CHECK cannot read
-- param_catalog, so it is enforced with triggers instead (INSERT + UPDATE OF key).
-- A non-existent key is already barred by the key REFERENCES param_catalog(key) FK.
CREATE TRIGGER IF NOT EXISTS coin_overrides_scope_ins
BEFORE INSERT ON coin_overrides
FOR EACH ROW
WHEN (SELECT scope FROM param_catalog WHERE key = NEW.key) NOT LIKE 'coin-%'
BEGIN
    SELECT RAISE(ABORT, 'coin_overrides: key is not coin-scoped (scope must be coin-allowed or coin-only)');
END;
CREATE TRIGGER IF NOT EXISTS coin_overrides_scope_upd
BEFORE UPDATE OF key ON coin_overrides
FOR EACH ROW
WHEN (SELECT scope FROM param_catalog WHERE key = NEW.key) NOT LIKE 'coin-%'
BEGIN
    SELECT RAISE(ABORT, 'coin_overrides: key is not coin-scoped (scope must be coin-allowed or coin-only)');
END;

-- Symmetric guard on the other side: a key cannot be DEMOTED out of coin scope
-- (coin-* -> fleet-only) while coin_overrides still hang off it, which would orphan
-- them against the two triggers above. Clearing the overrides first is required --
-- an explicit degraded state, never a silent orphan (same principle as levels_to's
-- ON DELETE SET NULL). Promoting the inverse (fleet-only -> coin-*) is never blocked.
CREATE TRIGGER IF NOT EXISTS param_catalog_scope_demote_guard
BEFORE UPDATE OF scope ON param_catalog
FOR EACH ROW
WHEN NEW.scope NOT LIKE 'coin-%'
     AND EXISTS (SELECT 1 FROM coin_overrides WHERE key = NEW.key)
BEGIN
    SELECT RAISE(ABORT, 'param_catalog: cannot demote a key out of coin scope while coin_overrides reference it (clear the overrides first)');
END;

-- The lot book (round-trips trader_estado.json: tok/cost/price/ts/origin/id + pairing).
-- `cost` = effective USD paid INCLUDING fees -- the no-loss gate reads THIS, not
-- buy_price*tokens (which understates by the fee). `origin` drives pairing
-- eligibility, reserve caps, GRID_IN_RALLY separation, the peel.
CREATE TABLE IF NOT EXISTS lots (
    id           INTEGER PRIMARY KEY,           -- native lot id from the book
    coin_id      INTEGER NOT NULL REFERENCES coins(id) ON DELETE CASCADE,
    tokens       REAL NOT NULL,                 -- 'tok'
    cost         REAL NOT NULL,                 -- effective USD incl. fees (no-loss input)
    buy_price    REAL NOT NULL,                 -- 'price'
    ts           REAL NOT NULL,                 -- epoch seconds (native book dating)
    origin       TEXT NOT NULL,                 -- grid|prebuy|reserve|failed|topup|ladder|...
    levels_to    INTEGER REFERENCES lots(id) ON DELETE SET NULL,  -- pairing link (nullable self-FK)
    group_id     INTEGER                        -- pairing group (nullable)
);
CREATE INDEX IF NOT EXISTS ix_lots_coin ON lots(coin_id);

-- Shared USDC cash: ONE wallet for the whole fleet. On-chain is the DECISION
-- source of truth (#385 free_usdc_fresh); this table MIRRORS it and holds the
-- reservation locks. Cash is spent via reserve -> swap -> commit/release so
-- concurrent per-coin decisions in one tick can never double-spend (Dot invariant).
-- A 'reserve' row LOWERS balance_after (reserved cash is not spendable); 'release'
-- reverses it; 'buy'/'sell'/'deposit'/'withdraw'/'bootstrap' are real moves.
CREATE TABLE IF NOT EXISTS usdc_ledger (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL DEFAULT (datetime('now')),
    delta        REAL NOT NULL,
    balance_after REAL NOT NULL,
    kind         TEXT NOT NULL
                 CHECK (kind IN ('bootstrap','buy','sell','deposit','withdraw','reserve','release')),
    coin_id      INTEGER REFERENCES coins(id),
    txsig        TEXT
);

-- Per-coin deployed-capital attribution (today's capital ledger / #221).
CREATE TABLE IF NOT EXISTS capital (
    coin_id      INTEGER PRIMARY KEY REFERENCES coins(id) ON DELETE CASCADE,
    deployed_usd REAL NOT NULL DEFAULT 0,
    max_usd      REAL,
    ts           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Price history grows unbounded -> own table, rotated by the app. ts is REAL epoch
-- (sub-second) so the 5s rally cadence never collides on the PK.
CREATE TABLE IF NOT EXISTS price_history (
    coin_id      INTEGER NOT NULL REFERENCES coins(id) ON DELETE CASCADE,
    ts           REAL NOT NULL,                 -- epoch seconds, fractional
    price        REAL NOT NULL,
    PRIMARY KEY (coin_id, ts)
);

-- Real fills. Round-trips today's trades.csv EXACTLY. CSV header on export:
--   time,mode,side,signal,price_usd,tokens,usd,cash_usd_after,tokens_held_after,pnl_pct,tx
-- `mode` is THE tax filter (tax_report keeps mode='live'); `pnl_pct` is how realized
-- profit is computed (usd - usd/(1+pct/100)). mode is PER ROW (a coin flips
-- paper->live in place). cash_usd_after / tokens_held_after are derived at export.
CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY,
    coin_id           INTEGER NOT NULL REFERENCES coins(id) ON DELETE CASCADE,
    ts                TEXT NOT NULL,            -- 'time'
    mode              TEXT NOT NULL CHECK (mode IN ('paper','live')),
    side              TEXT NOT NULL CHECK (side IN ('buy','sell')),  -- exported uppercased
    signal            TEXT,
    price             REAL NOT NULL,            -- 'price_usd'
    tokens            REAL NOT NULL,
    usd               REAL NOT NULL,
    pnl_pct           REAL,
    txsig             TEXT                      -- 'tx'
);
CREATE INDEX IF NOT EXISTS ix_trades_coin_ts ON trades(coin_id, ts);
