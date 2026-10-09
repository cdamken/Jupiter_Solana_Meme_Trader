#!/usr/bin/env python3
"""(#1030) Shared price-feed SOURCE selection, used by BOTH trader.py (imports it) and monitor.sh
(runs it as `python3 price_feed.py`), so the two can never diverge on WHERE a coin's price comes
from. Two sources:

  - UNPINNED (PRICE_PAIR_ADDRESS empty): DexScreener /tokens/<token>, most-liquid SOL pair whose
    base is our token (#283/#288). Today's behavior.
  - PINNED (PRICE_PAIR_ADDRESS set): DexScreener /pairs/solana/<addr>, ONLY that pool. Fail-closed:
    if the pinned pair is absent / malformed / non-positive the result is None (a no-data tick per
    #19) and the caller falls back to GeckoTerminal, exactly as today -- it NEVER falls back to the
    max-liquidity pick, because that is the pool-switch glitch #1006 was curing.

Pure (no network here): the caller does the HTTP and hands the parsed JSON in. Stdlib only."""
import sys


def dex_endpoint(token, pair_address=None):
    """The DexScreener URL to price `token`. Pinned -> the single-pool pairs endpoint; otherwise
    the tokens endpoint (max-liquidity pick). PURE."""
    addr = (pair_address or "").strip()
    if addr:
        return f"https://api.dexscreener.com/latest/dex/pairs/solana/{addr}"
    return f"https://api.dexscreener.com/latest/dex/tokens/{token}"


def pos_float(x):
    """A POSITIVE float, or None when the value is a non-numeric string, None, NaN or <= 0 -- all of
    which are "no data" (#19), never fed into arithmetic downstream. PURE."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def pick_dex_pair(pairs, token):
    """(#283) PURE: the most-liquid SOL pair whose baseToken is `token`. DexScreener's `pairs`
    ordering is NOT stable, so trusting pairs[0] can serve a different pool (even a different token
    sharing a name). Keep only pairs where OUR token is the base, prefer SOL-quoted, pick the most
    liquid. Returns the pair dict or None. (Lives here so trader.py and monitor.sh share one copy.)"""
    allp = pairs or []
    def _addr(p):
        return ((p.get("baseToken") or {}).get("address") or "")
    mine = [p for p in allp if _addr(p).lower() == (token or "").lower()]
    cand = mine or allp   # fall back to all only if none matched (keeps old behavior for odd feeds)
    if not cand:
        return None
    sol = [p for p in cand if (p.get("quoteToken") or {}).get("symbol") == "SOL"]
    pool = sol or cand
    def _liq(p):
        try:
            return float((p.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            return 0.0
    return max(pool, key=_liq)


def pinned_pair(pairs, pair_address):
    """The pair whose pairAddress == `pair_address` (case-insensitive), or None. FAIL-CLOSED: no
    exact match -> None, never a fallback to another pool (the whole point of pinning). PURE."""
    addr = (pair_address or "").strip().lower()
    if not addr:
        return None
    for p in (pairs or []):
        if (p.get("pairAddress") or "").lower() == addr:
            return p
    return None


def select_pair(payload, token, pair_address=None):
    """The pair dict to price from, given a DexScreener JSON payload. PINNED -> pinned_pair
    (fail-closed); otherwise -> pick_dex_pair. The /pairs endpoint returns the pool under "pairs"
    (and some responses under "pair"); accept either. PURE."""
    data = payload or {}
    pairs = data.get("pairs")
    if not pairs and data.get("pair"):
        pairs = [data["pair"]]
    if (pair_address or "").strip():
        return pinned_pair(pairs, pair_address)
    return pick_dex_pair(pairs, token)


def price_usd(payload, token, pair_address=None):
    """Positive priceUsd for the selected pair, or None (no-data). PURE."""
    p = select_pair(payload, token, pair_address)
    return pos_float(p.get("priceUsd")) if p else None


# ---- tiny CLI for monitor.sh (never diverge from trader on source) ----
# Usage:
#   python3 price_feed.py url   <token> [pair_address]     -> prints the DexScreener URL
#   python3 price_feed.py price <token> [pair_address]     -> reads the JSON on stdin, prints
#                                                             "<priceUsd> <marketCap>" or "" (no-data)
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "price"
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    pair = sys.argv[3] if len(sys.argv) > 3 else ""
    if mode == "url":
        print(dex_endpoint(token, pair))
        sys.exit(0)
    import json
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print("")
        sys.exit(0)
    p = select_pair(payload, token, pair)
    pr = pos_float(p.get("priceUsd")) if p else None
    if pr is None:
        print("")
        sys.exit(0)
    mc = (p.get("marketCap") or p.get("fdv") or "") if p else ""
    print(f"{pr} {mc}")
