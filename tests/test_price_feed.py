"""test_price_feed.py -- Price feed source selection (issue #4).

Tests the pure price_feed module (verbatim from SIMD #1030/#1031) and
Jupiter's fetch_price integration. Stdlib only, no network.
"""
import sys
import os

os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import price_feed as pf
import execute

FAILURES = []


def chk(name, cond):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f" FAIL {name}")
        FAILURES.append(name)


TOK = "EMeugag3yfyvKqNKknGDWAudNALafZjbv9ByzCE8pump"
ADDR = "PairAddr111111111111111111111111111111111111"

# payload with two pools of our token + one wrong-token pool
PAIRS = [
    {"pairAddress": "AAA", "baseToken": {"address": TOK},
     "quoteToken": {"symbol": "SOL"},
     "liquidity": {"usd": 1000}, "priceUsd": "0.0004"},
    {"pairAddress": ADDR, "baseToken": {"address": TOK},
     "quoteToken": {"symbol": "SOL"},
     "liquidity": {"usd": 50}, "priceUsd": "0.0006"},
    {"pairAddress": "WRONG", "baseToken": {"address": "OtherTokenMint"},
     "quoteToken": {"symbol": "SOL"},
     "liquidity": {"usd": 9999}, "priceUsd": "9.99"},
]
PAYLOAD = {"pairs": PAIRS}

# ---- (a) pure helpers ----
chk("dex_endpoint unpinned = tokens endpoint",
    pf.dex_endpoint(TOK) == f"https://api.dexscreener.com/latest/dex/tokens/{TOK}")
chk("dex_endpoint pinned = pairs endpoint",
    pf.dex_endpoint(TOK, ADDR) == f"https://api.dexscreener.com/latest/dex/pairs/solana/{ADDR}")
chk("dex_endpoint empty pin -> tokens endpoint",
    pf.dex_endpoint(TOK, "  ") == pf.dex_endpoint(TOK))

chk("pos_float positive ok", pf.pos_float("1.5") == 1.5)
chk("pos_float bad values -> None",
    all(pf.pos_float(x) is None for x in ("abc", None, "0", "-1", float("nan"))))

# ---- (b) pick_dex_pair: most-liquid pool of OUR token ----
chk("pick_dex_pair: most-liquid of our token (0.0004), not the wrong-token pool",
    pf.pick_dex_pair(PAIRS, TOK)["priceUsd"] == "0.0004")

chk("pick_dex_pair: empty list -> None",
    pf.pick_dex_pair([], TOK) is None)

chk("pick_dex_pair: None list -> None",
    pf.pick_dex_pair(None, TOK) is None)

# ---- (c) pinned_pair: exact match, fail-closed ----
chk("pinned_pair: matches by address (case-insensitive)",
    pf.pinned_pair(PAIRS, ADDR.upper())["priceUsd"] == "0.0006")

chk("pinned_pair: no match -> None (fail-closed)",
    pf.pinned_pair(PAIRS, "NOPE") is None)

chk("pinned_pair: empty address -> None",
    pf.pinned_pair(PAIRS, "") is None)

# ---- (d) select_pair: pinned vs unpinned ----
chk("select_pair pinned -> pinned pool (0.0006)",
    pf.select_pair(PAYLOAD, TOK, ADDR)["priceUsd"] == "0.0006")

chk("select_pair pinned-absent -> None (fail-closed, NOT the max-liquidity pick)",
    pf.select_pair(PAYLOAD, TOK, "GHOST") is None)

chk("select_pair unpinned -> most-liquid (0.0004)",
    pf.select_pair(PAYLOAD, TOK)["priceUsd"] == "0.0004")

chk("select_pair: single 'pair' payload shape accepted",
    pf.select_pair({"pair": PAIRS[1]}, TOK, ADDR)["priceUsd"] == "0.0006")

# ---- (e) price_usd: the top-level API ----
chk("price_usd pinned = 0.0006",
    pf.price_usd(PAYLOAD, TOK, ADDR) == 0.0006)

chk("price_usd pinned-absent = None",
    pf.price_usd(PAYLOAD, TOK, "GHOST") is None)

chk("price_usd unpinned = 0.0004 (most-liquid)",
    pf.price_usd(PAYLOAD, TOK) == 0.0004)

chk("price_usd bad price string -> None",
    pf.price_usd({"pairs": [{"pairAddress": "X", "baseToken": {"address": TOK},
                              "quoteToken": {"symbol": "SOL"},
                              "liquidity": {"usd": 100}, "priceUsd": "garbage"}]}, TOK) is None)

chk("price_usd zero price -> None",
    pf.price_usd({"pairs": [{"pairAddress": "X", "baseToken": {"address": TOK},
                              "quoteToken": {"symbol": "SOL"},
                              "liquidity": {"usd": 100}, "priceUsd": "0"}]}, TOK) is None)

# ---- (f) fetch_price integration (stub HTTP) ----
_real_get = execute._get_json


def _stub_get(url):
    if "/pairs/solana/" in url:
        addr = url.rsplit("/", 1)[-1]
        matched = [p for p in PAIRS if p["pairAddress"].lower() == addr.lower()]
        return {"pairs": matched} if matched else {"pairs": []}
    return PAYLOAD


execute._get_json = _stub_get
try:
    chk("fetch_price unpinned uses liquidity pick (0.0004)",
        abs(execute.fetch_price(TOK) - 0.0004) < 1e-12)

    chk("fetch_price pinned returns pinned pool (0.0006)",
        abs(execute.fetch_price(TOK, pair_address=ADDR) - 0.0006) < 1e-12)

    chk("fetch_price pinned-absent -> None (fail-closed)",
        execute.fetch_price(TOK, pair_address="GHOST") is None)
finally:
    execute._get_json = _real_get

# ---- (g) edge: empty payload ----
chk("price_usd empty payload -> None",
    pf.price_usd({}, TOK) is None)

chk("price_usd no pairs key -> None",
    pf.price_usd({"something": "else"}, TOK) is None)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)}")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL OK")
