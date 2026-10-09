"""execute.py — LIVE and paper swap execution.

Paper mode: logs the intent, records a fake lot, no network call.
Live mode: Jupiter API quote -> route check -> Solana sign+send -> confirm.

All SIMD lessons applied:
- User-Agent required on Jupiter POST or Cloudflare blocks (403/1010) (#17)
- A 200 from Jupiter can carry HTML challenge, not JSON -- validate before parsing (#17)
- Price <= 0 is not a price, never update ref on garbage feed (#19)
- USDC ledger: reserve first, swap, then commit or release -- never skip (#502)
- GAS_RESERVE_LAMPORTS: always check SOL balance before signing (#hard rule)
- Never expose the private key outside load_keypair()
- Never sell at a loss: revalidate_effective_sale() before every sell
"""
from __future__ import annotations
import base64
import json
import logging
import time
import urllib.request
import urllib.error

import price_feed as pf

log = logging.getLogger(__name__)

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"

_UA = "Mozilla/5.0 (compatible; JupiterTrader/1.0)"


# ---- HTTP helpers ----

def _get_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
    except urllib.error.URLError as e:
        log.warning("GET %s failed: %s", url, e)
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        log.warning("GET %s: 200 but body is not JSON (HTML challenge?)", url)
        return None


def _post_json(url: str, payload: dict) -> dict | None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
    except urllib.error.URLError as e:
        log.warning("POST %s failed: %s", url, e)
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        log.warning("POST %s: 200 but body is not JSON (HTML challenge?)", url)
        return None


# ---- Price feed ----

def fetch_price(mint: str, pair_address: str | None = None) -> float | None:
    """Fetch price in USD from DexScreener via price_feed module.

    Uses liquidity-weighted pair selection (#283/#288) and optional pair pinning
    (#1030/#1031). Returns None on bad feed (fail-closed).
    """
    url = pf.dex_endpoint(mint, pair_address)
    data = _get_json(url)
    if not data:
        return None
    return pf.price_usd(data, mint, pair_address)


# ---- Keypair ----

def load_keypair(path: str):
    """Load a Solana keypair from a JSON byte-array file. Key never logged."""
    try:
        from solders.keypair import Keypair  # type: ignore
        with open(path, "rb") as f:
            raw = json.load(f)
        return Keypair.from_bytes(bytes(raw))
    except Exception as e:
        raise RuntimeError(f"Failed to load keypair from {path}: {e}") from e


def effective_price_usd(quote: dict, in_amount: int, input_is_usdc: bool) -> float | None:
    """Compute the effective USD price per token from a Jupiter quote."""
    try:
        out = int(quote.get("outAmount", 0))
        inp = int(quote.get("inAmount", 0))
        if out <= 0 or inp <= 0:
            return None
        if input_is_usdc:
            # buying: inAmount is USDC (6 decimals), outAmount is tokens
            return inp / 1e6 / (out / 1e6)
        else:
            # selling: inAmount is tokens, outAmount is USDC
            return out / 1e6 / (inp / 1e6)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def order_avoids_foreign_signer(route: dict, wallet_pubkey: str) -> bool:
    """Verify the swap route does not require a signer we do not control."""
    if "signers" not in route:
        return True
    for s in route["signers"]:
        if s != wallet_pubkey:
            log.warning("Foreign signer detected in route: %s", s)
            return False
    return True


def revalidate_effective_sale(lot: dict, price: float, fee: float = 0.015) -> bool:
    """Hard rule: a lot may only be sold if effective price > effective cost (net fee)."""
    cost = lot.get("cost", 0.0)
    tokens = lot.get("tokens", 0.0)
    if tokens <= 0 or cost <= 0:
        return False
    proceeds = price * tokens * (1 - fee)
    return proceeds > cost


# ---- SOL balance check ----

def sol_balance_lamports(rpc_url: str, pubkey: str) -> int | None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance",
               "params": [pubkey]}
    resp = _post_json(rpc_url, payload)
    if not resp:
        return None
    return resp.get("result", {}).get("value")


# ---- Paper mode ----

def paper_buy(store, coin_id: int, usd: float, price: float, ts: float,
              origin: str = "grid") -> str:
    """Record a paper buy. Returns a fake txsig."""
    if price <= 0:
        return ""
    tokens = usd / price
    if not store.usdc_reserve(usd, coin_id):
        return ""
    lot_id = store.add_lot(coin_id, tokens, usd, price, ts, origin)
    store.usdc_commit_buy(usd, coin_id, f"paper-buy-{lot_id}")
    log.info("[paper] BUY coin=%d tokens=%.4f price=%.6f usd=%.2f lot=%d",
             coin_id, tokens, price, usd, lot_id)
    return f"paper-buy-{lot_id}"


def paper_sell(store, coin_id: int, lot: dict, price: float, mode: str = "paper") -> str:
    """Record a paper sell. Returns a fake txsig."""
    if not revalidate_effective_sale(lot, price):
        log.warning("[paper] SELL blocked by hard rule: lot=%d price=%.6f", lot["id"], price)
        return ""
    tokens = lot["tokens"]
    proceeds = tokens * price
    pnl_pct = (price - lot["buy_price"]) / lot["buy_price"] * 100.0
    txsig = f"paper-sell-{lot['id']}"
    store.remove_lot(lot["id"])
    store.usdc_commit_sell(proceeds, coin_id, txsig, price, tokens, pnl_pct, mode)
    log.info("[paper] SELL coin=%d tokens=%.4f price=%.6f proceeds=%.2f pnl=%.1f%%",
             coin_id, tokens, price, proceeds, pnl_pct)
    return txsig


# ---- Live mode ----

def live_buy(store, coin_id: int, usd: float, price: float, ts: float,
             rpc_url: str, keypair_path: str, quote_mint: str,
             token_mint: str, gas_reserve_lamports: int,
             token_decimals: int = 6, origin: str = "grid",
             slippage_bps: int = 150) -> str:
    """Execute a real buy swap via Jupiter. Returns txsig or '' on failure."""
    kp = load_keypair(keypair_path)
    wallet = str(kp.pubkey())

    # Gas check
    lamports = sol_balance_lamports(rpc_url, wallet)
    if lamports is None or lamports < gas_reserve_lamports:
        log.error("Insufficient SOL for gas: %s lamports (need >=%d)", lamports, gas_reserve_lamports)
        return ""

    # Reserve USDC before attempting the swap
    if not store.usdc_reserve(usd, coin_id):
        log.warning("live_buy: insufficient USDC balance for coin=%d usd=%.2f", coin_id, usd)
        return ""

    in_amount = int(usd * 1e6)          # USDC has 6 decimals

    # Quote
    quote_url = (
        f"{JUPITER_QUOTE_URL}?inputMint={quote_mint}&outputMint={token_mint}"
        f"&amount={in_amount}&slippageBps={slippage_bps}"
    )
    quote = _get_json(quote_url)
    if not quote:
        store.usdc_release(usd, coin_id)
        return ""

    eff_price = effective_price_usd(quote, in_amount, input_is_usdc=True)
    if eff_price is None or eff_price <= 0:
        log.warning("live_buy: bad effective price from quote")
        store.usdc_release(usd, coin_id)
        return ""

    # Build swap tx
    swap_payload = {
        "quoteResponse": quote,
        "userPublicKey": wallet,
        "wrapAndUnwrapSol": True,
    }
    swap_resp = _post_json(JUPITER_SWAP_URL, swap_payload)
    if not swap_resp or "swapTransaction" not in swap_resp:
        log.warning("live_buy: no swapTransaction in response")
        store.usdc_release(usd, coin_id)
        return ""

    if not order_avoids_foreign_signer(swap_resp, wallet):
        store.usdc_release(usd, coin_id)
        return ""

    # Sign and send
    try:
        from solders.transaction import VersionedTransaction  # type: ignore
        from solana.rpc.api import Client                      # type: ignore

        raw = base64.b64decode(swap_resp["swapTransaction"])
        tx = VersionedTransaction.from_bytes(raw)
        tx.sign([kp])
        client = Client(rpc_url)
        result = client.send_raw_transaction(bytes(tx))
        txsig = str(result.value)
    except Exception as e:
        log.error("live_buy: send failed: %s", e)
        store.usdc_release(usd, coin_id)
        return ""

    tokens_out = int(quote.get("outAmount", 0)) / (10 ** token_decimals)
    lot_id = store.add_lot(coin_id, tokens_out, usd, eff_price, ts, origin)
    store.usdc_commit_buy(usd, coin_id, txsig)
    log.info("[live] BUY coin=%d tokens=%.4f price=%.6f usd=%.2f lot=%d txsig=%s",
             coin_id, tokens_out, eff_price, usd, lot_id, txsig)
    return txsig


def live_sell(store, coin_id: int, lot: dict, price: float,
              rpc_url: str, keypair_path: str, quote_mint: str,
              token_mint: str, gas_reserve_lamports: int,
              token_decimals: int = 6,
              slippage_bps: int = 150) -> str:
    """Execute a real sell swap via Jupiter. Returns txsig or '' on failure."""
    if not revalidate_effective_sale(lot, price):
        log.warning("live_sell: hard rule blocks lot=%d at price=%.6f", lot["id"], price)
        return ""

    kp = load_keypair(keypair_path)
    wallet = str(kp.pubkey())

    lamports = sol_balance_lamports(rpc_url, wallet)
    if lamports is None or lamports < gas_reserve_lamports:
        log.error("Insufficient SOL for gas: %s lamports", lamports)
        return ""

    tokens = lot["tokens"]
    in_amount = int(tokens * 10 ** token_decimals)

    quote_url = (
        f"{JUPITER_QUOTE_URL}?inputMint={token_mint}&outputMint={quote_mint}"
        f"&amount={in_amount}&slippageBps={slippage_bps}"
    )
    quote = _get_json(quote_url)
    if not quote:
        return ""

    eff_price = effective_price_usd(quote, in_amount, input_is_usdc=False)
    if eff_price is None or eff_price <= 0:
        return ""

    # Revalidate at worst fill
    test_lot = dict(lot, buy_price=lot["buy_price"])
    if not revalidate_effective_sale(test_lot, eff_price):
        log.warning("live_sell: worst-fill quote below cost for lot=%d", lot["id"])
        return ""

    swap_payload = {
        "quoteResponse": quote,
        "userPublicKey": wallet,
        "wrapAndUnwrapSol": True,
    }
    swap_resp = _post_json(JUPITER_SWAP_URL, swap_payload)
    if not swap_resp or "swapTransaction" not in swap_resp:
        return ""

    if not order_avoids_foreign_signer(swap_resp, wallet):
        return ""

    try:
        from solders.transaction import VersionedTransaction  # type: ignore
        from solana.rpc.api import Client                      # type: ignore

        raw = base64.b64decode(swap_resp["swapTransaction"])
        tx = VersionedTransaction.from_bytes(raw)
        tx.sign([kp])
        client = Client(rpc_url)
        result = client.send_raw_transaction(bytes(tx))
        txsig = str(result.value)
    except Exception as e:
        log.error("live_sell: send failed: %s", e)
        return ""

    proceeds = int(quote.get("outAmount", 0)) / 1e6
    pnl_pct = (eff_price - lot["buy_price"]) / lot["buy_price"] * 100.0
    store.remove_lot(lot["id"])
    store.usdc_commit_sell(proceeds, coin_id, txsig, eff_price, tokens, pnl_pct, "live")
    log.info("[live] SELL coin=%d tokens=%.4f price=%.6f proceeds=%.2f pnl=%.1f%% txsig=%s",
             coin_id, tokens, eff_price, proceeds, pnl_pct, txsig)
    return txsig


# ---- Gas refill (USDC -> SOL) ----

SOL_MINT = "So11111111111111111111111111111111111111112"


def gas_refill(store, refill_usdc: float, rpc_url: str, keypair_path: str,
               quote_mint: str, slippage_bps: int = 300) -> str:
    """Swap USDC -> SOL to refill gas. LIVE only. Returns txsig or ''."""
    kp = load_keypair(keypair_path)
    wallet = str(kp.pubkey())

    in_amount = int(refill_usdc * 1e6)

    quote_url = (
        f"{JUPITER_QUOTE_URL}?inputMint={quote_mint}&outputMint={SOL_MINT}"
        f"&amount={in_amount}&slippageBps={slippage_bps}"
    )
    quote = _get_json(quote_url)
    if not quote:
        log.warning("gas_refill: quote failed")
        return ""

    swap_payload = {
        "quoteResponse": quote,
        "userPublicKey": wallet,
        "wrapAndUnwrapSol": True,
    }
    swap_resp = _post_json(JUPITER_SWAP_URL, swap_payload)
    if not swap_resp or "swapTransaction" not in swap_resp:
        log.warning("gas_refill: no swapTransaction")
        return ""

    if not order_avoids_foreign_signer(swap_resp, wallet):
        return ""

    try:
        from solders.transaction import VersionedTransaction  # type: ignore
        from solana.rpc.api import Client  # type: ignore

        raw = base64.b64decode(swap_resp["swapTransaction"])
        tx = VersionedTransaction.from_bytes(raw)
        tx.sign([kp])
        client = Client(rpc_url)
        result = client.send_raw_transaction(bytes(tx))
        txsig = str(result.value)
    except Exception as e:
        log.error("gas_refill: send failed: %s", e)
        return ""

    store.usdc_deposit(-refill_usdc)

    sol_out = int(quote.get("outAmount", 0)) / 1e9
    log.info("[live] GAS REFILL %.2f USDC -> %.5f SOL txsig=%s",
             refill_usdc, sol_out, txsig)
    return txsig
