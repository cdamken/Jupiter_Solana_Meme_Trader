# Jupiter Solana Meme Trader

Multi-coin Solana grid trading bot. A coin is a row, not a directory.

## What this is not

This is not a port of SIMD. It is a redesign that learned from SIMD's operating experience.
The pure engine logic (grid decisions, trailing sell) is reused. Everything else is rebuilt.

## Hard rules (same as SIMD, enforced in code not config)

- Never sell at a loss. Each lot sells only above its effective cost (fees included).
- Quote is always USDC. Gas is always SOL. Never spend all SOL; keep GAS_RESERVE_LAMPORTS.
- Price <= 0 or NaN is not a price. It is a missing feed. Do not trade on it.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  scheduler.py  (one process, ticks all coins)   │
│                                                 │
│  for each coin in status=live|paper:            │
│    price = fetch_price(coin.mint)               │
│    decision = engine.decide(price, book, cfg)   │
│    if decision: execute(decision, coin, cfg)    │
└────────────────────┬────────────────────────────┘
                     │ reads/writes
              ┌──────▼──────┐
              │  store.db   │  SQLite WAL
              │  (one file) │
              └──────┬──────┘
                     │
              ┌──────▼──────┐
              │  panel/     │  Flask, read-only views + config edits
              └─────────────┘
```

One process. One DB. Adding a coin = INSERT into coins + INSERT fleet_defaults override if needed.
Removing a coin = set status='paused'. No directories to create, no crons to add.

## Key design decisions (vs SIMD)

| Concern | SIMD | Jupiter |
|---|---|---|
| State per coin | trader_estado.json in its own dir | lots table in SQLite |
| Config | config.sh (bash, hidden defaults) | fleet_defaults + coin_overrides in DB |
| Adding a coin | new directory + cron + config copy | INSERT coins row |
| Scheduling | one cron job per coin | one Python scheduler loop |
| USDC ledger | usdc_ledger.json (shared file, file locks) | usdc_ledger table (SQLite transactions) |
| Panel | separate Flask process | same process, /panel routes |
| Secrets | config.sh (SMTP, RPC, keypair path) | .env file, never in DB |

## What is reused from SIMD (byte-identical logic)

- `grid_engine.grid_decision()` - pure function, no I/O, already tested
- `effective_price_usd()` - quote validation
- `revalidate_effective_sale()` - hard rule enforcement at worst fill
- `order_avoids_foreign_signer()` - Jupiter route safety
- Price gate logic (reject outlier ticks, re-anchor after N consecutive agreeing ticks)

## What is NOT reused

- config.sh / trader.sh (the bash layer)
- trader.py (too entangled with the single-coin model)
- monitor.sh (replaced by scheduler's own health checks)
- The per-directory state pattern

## Lessons from SIMD bugs applied here

- Jupiter HTTP calls need User-Agent header or Cloudflare blocks (#17 in SIMD)
- A 200 response can contain HTML challenge, not JSON - validate before parsing
- Price <= 0 must never update the reference price (#19)
- USDC ledger: reserve first, swap, then commit/release - never skip the reserve (#502)
- Atomic state writes: write to .tmp, rename, keep .bak (#20)
- Never run tests against the live DB directory (#889)
- Every env var the scheduler reads must be in the .env or fail-closed at startup (#5)

## Store schema

See `store/schema.sql`. Applied at boot; scheduler refuses to start if integrity_check fails.

## Directory layout (planned)

```
Jupiter_Solana_Meme_Trader/
  scheduler.py        # main loop, ticks all active coins
  engine.py           # grid_decision + trailing sell (ported from SIMD, pure)
  execute.py          # LIVE swap path (Jupiter API + Solana signing)
  store/
    schema.sql        # SQLite schema (WAL, FK ON)
    store.py          # DB access layer (read/write helpers)
  panel/
    app.py            # Flask panel
    templates/
  config.py           # loads .env, fail-closed on missing required keys
  .env.example        # template (RPC_URL, KEYPAIR_PATH, SMTP_*)
  tests/
    test_engine.py
    test_store.py
    test_execute.py   # network stubbed, never hits real Jupiter
```

## Setup (paper mode, no real money)

```bash
cp .env.example .env   # fill in RPC_URL and KEYPAIR_PATH
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 scheduler.py --paper   # all coins start in paper mode
```

## Adding a coin

From the panel: fill in slug, label, mint address, initial config. Status defaults to 'paper'.
Flip to 'live' only after validating paper behavior.
