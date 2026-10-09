"""execute.py — LIVE and paper swap execution.

Paper mode: logs the intent, records a fake lot, no network call.
Live mode: Jupiter swap v2 API -> sign+send -> on-chain confirmation -> commit.

All SIMD lessons applied:
- User-Agent required on Jupiter POST or Cloudflare blocks (403/1010) (#17)
- A 200 from Jupiter can carry HTML challenge, not JSON -- validate before parsing (#17)
- Price <= 0 is not a price, never update ref on garbage feed (#19)
- USDC ledger: reserve first, swap, then commit or release -- never skip (#502)
- GAS_RESERVE_LAMPORTS: always check SOL balance before signing (#hard rule)
- Never expose the private key outside load_keypair()
- Never sell at a loss: revalidate_effective_sale() before every sell
- On-chain confirmation before DB commit (issue #2 fix 1)
- Jupiter swap v2 API (issue #2 fix 2)
- Configurable slippage via MAX_SLIPPAGE_BPS (issue #2 fix 3)
- Transaction signer inspection (issue #2 fix 4)
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

JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL  = "https://api.jup.ag/swap/v1/swap"

DEFAULT_SLIPPAGE_BPS = 150
GAS_REFILL_SLIPPAGE_BPS = 300
TX_CONFIRM_TIMEOUT_S = 60
TX_CONFIRM_POLL_S = 2

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
            return inp / 1e6 / (out / 1e6)
        else:
            return out / 1e6 / (inp / 1e6)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def tx_avoids_foreign_signer(swap_tx_b64: str, wallet_pubkey: str) -> bool:
    """Inspect the serialized transaction for foreign signers.

    Decodes the versioned transaction and checks that every required signer
    is the wallet. This replaces the old no-op check that looked for a
    top-level "signers" key (which v6 never had).
    """
    try:
        from solders.transaction import VersionedTransaction  # type: ignore
        raw = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(raw)
        msg = tx.message
        num_signers = msg.header().num_required_signatures
        account_keys = msg.account_keys()
        for i in range(num_signers):
            key_str = str(account_keys[i])
            if key_str != wallet_pubkey:
                log.warning("Foreign signer at index %d: %s (wallet: %s)",
                            i, key_str, wallet_pubkey)
                return False
        return True
    except ImportError:
        log.warning("solders not installed — cannot verify signers, rejecting tx")
        return False
    except Exception as e:
        log.warning("tx_avoids_foreign_signer: parse error: %s", e)
        return False


def revalidate_effective_sale(lot: dict, price: float, fee: float = 0.015) -> bool:
    """Hard rule: a lot may only be sold if effective price > effective cost (net fee)."""
    cost = lot.get("cost", 0.0)
    tokens = lot.get("tokens", 0.0)
    if tokens <= 0 or cost <= 0:
        return False
    proceeds = price * tokens * (1 - fee)
    return proceeds > cost


# ---- On-chain confirmation ----

def confirm_transaction(rpc_url: str, txsig: str,
                        timeout: float = TX_CONFIRM_TIMEOUT_S) -> bool:
    """Poll RPC until the transaction is confirmed or times out.

    Returns True if confirmed, False if expired/failed/timed out.
    After send, the tx may or may not land — we MUST confirm before
    committing to the DB (SIMD lesson: never assume state after sending).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignatureStatuses",
            "params": [[txsig], {"searchTransactionHistory": True}],
        }
        resp = _post_json(rpc_url, payload)
        if resp:
            statuses = resp.get("result", {}).get("value", [])
            if statuses and statuses[0] is not None:
                status = statuses[0]
                if status.get("err") is not None:
                    log.warning("tx %s failed on-chain: %s", txsig, status["err"])
                    return False
                conf = status.get("confirmationStatus", "")
                if conf in ("confirmed", "finalized"):
                    return True
        time.sleep(TX_CONFIRM_POLL_S)
    log.warning("tx %s: confirmation timed out after %.0fs", txsig, timeout)
    return False


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


def paper_sell(store, coin_id: int, lot: dict, price: float,
               mode: str = "paper", fee: float = 0.015) -> str:
    """Record a paper sell with fee applied (mirrors live accounting)."""
    if not revalidate_effective_sale(lot, price, fee):
        log.warning("[paper] SELL blocked by hard rule: lot=%d price=%.6f", lot["id"], price)
        return ""
    tokens = lot["tokens"]
    proceeds = tokens * price * (1 - fee)
    pnl_pct = (proceeds - lot["cost"]) / lot["cost"] * 100.0
    txsig = f"paper-sell-{lot['id']}"
    store.remove_lot(lot["id"])
    store.usdc_commit_sell(proceeds, coin_id, txsig, price, tokens, pnl_pct, mode)
    log.info("[paper] SELL coin=%d tokens=%.4f price=%.6f proceeds=%.2f pnl=%.1f%%",
             coin_id, tokens, price, proceeds, pnl_pct)
    return txsig


# ---- Live mode ----

def _jupiter_quote(input_mint: str, output_mint: str,
                   amount: int, slippage_bps: int) -> dict | None:
    quote_url = (
        f"{JUPITER_QUOTE_URL}?inputMint={input_mint}&outputMint={output_mint}"
        f"&amount={amount}&slippageBps={slippage_bps}"
    )
    return _get_json(quote_url)


def _jupiter_swap(quote: dict, wallet: str) -> dict | None:
    swap_payload = {
        "quoteResponse": quote,
        "userPublicKey": wallet,
        "wrapAndUnwrapSol": True,
    }
    return _post_json(JUPITER_SWAP_URL, swap_payload)


def _sign_and_send(swap_tx_b64: str, kp, rpc_url: str) -> str | None:
    """Sign a base64-encoded versioned transaction and send it. Returns txsig or None."""
    try:
        from solders.transaction import VersionedTransaction  # type: ignore
        from solana.rpc.api import Client  # type: ignore

        raw = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(raw)
        tx.sign([kp])
        client = Client(rpc_url)
        result = client.send_raw_transaction(bytes(tx))
        return str(result.value)
    except Exception as e:
        log.error("sign_and_send failed: %s", e)
        return None


def live_buy(store, coin_id: int, usd: float, price: float, ts: float,
             rpc_url: str, keypair_path: str, quote_mint: str,
             token_mint: str, gas_reserve_lamports: int,
             token_decimals: int = 6, origin: str = "grid",
             slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> str:
    """Execute a real buy swap via Jupiter swap v2. Returns txsig or '' on failure."""
    kp = load_keypair(keypair_path)
    wallet = str(kp.pubkey())

    lamports = sol_balance_lamports(rpc_url, wallet)
    if lamports is None or lamports < gas_reserve_lamports:
        log.error("Insufficient SOL for gas: %s lamports (need >=%d)", lamports, gas_reserve_lamports)
        return ""

    if not store.usdc_reserve(usd, coin_id):
        log.warning("live_buy: insufficient USDC balance for coin=%d usd=%.2f", coin_id, usd)
        return ""

    in_amount = int(usd * 1e6)

    quote = _jupiter_quote(quote_mint, token_mint, in_amount, slippage_bps)
    if not quote:
        store.usdc_release(usd, coin_id)
        return ""

    eff_price = effective_price_usd(quote, in_amount, input_is_usdc=True)
    if eff_price is None or eff_price <= 0:
        log.warning("live_buy: bad effective price from quote")
        store.usdc_release(usd, coin_id)
        return ""

    swap_resp = _jupiter_swap(quote, wallet)
    if not swap_resp or "swapTransaction" not in swap_resp:
        log.warning("live_buy: no swapTransaction in response")
        store.usdc_release(usd, coin_id)
        return ""

    swap_tx = swap_resp["swapTransaction"]
    if not tx_avoids_foreign_signer(swap_tx, wallet):
        store.usdc_release(usd, coin_id)
        return ""

    txsig = _sign_and_send(swap_tx, kp, rpc_url)
    if not txsig:
        store.usdc_release(usd, coin_id)
        return ""

    if not confirm_transaction(rpc_url, txsig):
        log.error("live_buy: tx %s NOT confirmed — releasing reservation", txsig)
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
              slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> str:
    """Execute a real sell swap via Jupiter swap v2. Returns txsig or '' on failure."""
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

    quote = _jupiter_quote(token_mint, quote_mint, in_amount, slippage_bps)
    if not quote:
        return ""

    eff_price = effective_price_usd(quote, in_amount, input_is_usdc=False)
    if eff_price is None or eff_price <= 0:
        return ""

    if not revalidate_effective_sale(lot, eff_price):
        log.warning("live_sell: worst-fill quote below cost for lot=%d", lot["id"])
        return ""

    swap_resp = _jupiter_swap(quote, wallet)
    if not swap_resp or "swapTransaction" not in swap_resp:
        return ""

    swap_tx = swap_resp["swapTransaction"]
    if not tx_avoids_foreign_signer(swap_tx, wallet):
        return ""

    txsig = _sign_and_send(swap_tx, kp, rpc_url)
    if not txsig:
        return ""

    if not confirm_transaction(rpc_url, txsig):
        log.error("live_sell: tx %s NOT confirmed — lot stays in book", txsig)
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
               quote_mint: str, slippage_bps: int = GAS_REFILL_SLIPPAGE_BPS) -> str:
    """Swap USDC -> SOL to refill gas. LIVE only. Returns txsig or ''."""
    kp = load_keypair(keypair_path)
    wallet = str(kp.pubkey())

    in_amount = int(refill_usdc * 1e6)

    quote = _jupiter_quote(quote_mint, SOL_MINT, in_amount, slippage_bps)
    if not quote:
        log.warning("gas_refill: quote failed")
        return ""

    swap_resp = _jupiter_swap(quote, wallet)
    if not swap_resp or "swapTransaction" not in swap_resp:
        log.warning("gas_refill: no swapTransaction")
        return ""

    swap_tx = swap_resp["swapTransaction"]
    if not tx_avoids_foreign_signer(swap_tx, wallet):
        return ""

    txsig = _sign_and_send(swap_tx, kp, rpc_url)
    if not txsig:
        return ""

    if not confirm_transaction(rpc_url, txsig):
        log.error("gas_refill: tx %s NOT confirmed — no debit", txsig)
        return ""

    store.usdc_deposit(-refill_usdc)

    sol_out = int(quote.get("outAmount", 0)) / 1e9
    log.info("[live] GAS REFILL %.2f USDC -> %.5f SOL txsig=%s",
             refill_usdc, sol_out, txsig)
    return txsig
