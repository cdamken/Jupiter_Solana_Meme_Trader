"""bootstrap.py — Seed a fresh Jupiter DB with param_catalog and fleet defaults.

Run once after cloning, before starting the scheduler:
    python3 bootstrap.py

Safe to re-run: INSERT OR IGNORE / INSERT OR REPLACE.
"""
import os
import sys

os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

import config as cfg
from store.store import Store

CATALOG = [
    # (key, tier, scope, type, min, max, default_val, recommended, label, help)

    # ── Core grid ────────────────────────────────────────────────────────────
    (
        "BUY_STEP_PCT", "editable", "coin-allowed", "float", "0.5", "30", "4.0", "4.0",
        "Buy step %",
        "The grid buys a lot every time the price drops X% below the last buy reference. "
        "Smaller steps catch more dips but thin the margin over the ~3% round-trip fee -- "
        "at 4% the net per cycle is roughly 1%, at 8% it is ~5%. "
        "Start at 4-5% for high-volatility pump.fun tokens; widen for calmer coins that move slowly.",
    ),
    (
        "SELL_STEP_PCT", "editable", "coin-allowed", "float", "0.5", "50", "4.0", "4.0",
        "Sell step %",
        "Each lot sells when its price rises X% above its own buy price -- per lot, never on an average, "
        "so the hard rule (never sell at a loss) holds exactly. "
        "A wider sell step means fewer sells but each cycle locks more profit. "
        "Asymmetric grids pair a small buy step with a larger sell step (e.g. 5/12) to grab dips often "
        "while extracting a bigger margin on the way out.",
    ),
    (
        "LOT_USD", "editable", "coin-allowed", "float", "1", "500", "25.0", "25.0",
        "Lot size USD",
        "How many USD the grid commits per buy. "
        "Smaller lots let the grid ladder into a dip more finely (more rungs before hitting the capital cap); "
        "larger lots deploy capital faster but exhaust the budget sooner. "
        "Set it so that at least 5-8 steps can be open simultaneously before hitting MAX_CAPITAL_USD.",
    ),
    (
        "MAX_CAPITAL_USD", "editable", "coin-allowed", "float", "10", "5000", "200.0", "200.0",
        "Max capital USD",
        "Hard ceiling on USD deployed into this coin at any time. "
        "The scheduler stops buying once deployed capital would exceed this -- it is not a stop-loss: "
        "lots already open are held until they can sell at a profit. "
        "Set it deliberately per coin; $200 is a reasonable single-coin starting budget, "
        "tune it once you know the coin's liquidity and how deep its drawdowns run.",
    ),
    (
        "SELL_TRAIL", "editable", "coin-allowed", "bool", None, None, "0", "0",
        "Trailing sell",
        "0 = fixed exit: each lot sells the instant it crosses its +SELL_STEP_PCT target. "
        "1 = trailing exit: once a lot reaches its target the bot arms a trail and only sells when "
        "the price retraces SELL_TRAIL_PCT% from the highest peak above the target -- letting winners run "
        "further without ever selling below cost. "
        "SIMD measured +26-32% realized gain vs fixed on 25 days of real minute data. "
        "More effective on volatile coins; has no benefit when the price peaks and drops sharply.",
    ),
    (
        "SELL_TRAIL_PCT", "editable", "coin-allowed", "float", "0.5", "20", "2.0", "2.0",
        "Trail pull-back %",
        "Only active when SELL_TRAIL=1. Sell when the price retraces X% from the peak reached above "
        "the lot's target. Must be > 0 (zero would sell on the first tick). "
        "Sweep on SIMD real data: 2% is optimal for fast pump.fun tokens; slower coins tolerate 1-3 "
        "with little difference. Too tight (0.5%) fires on noise; too wide (10%) gives back too much gain.",
    ),
    (
        "SELL_POLICY", "editable", "coin-allowed", "enum", None, None, "fifo", "fifo",
        "Sell lot order",
        "Which lot to sell first when multiple lots are eligible. "
        "'fifo' = oldest lot first (default). "
        "'lifo' = newest lot first. "
        "'highest' = the lot with the highest buy price (sell the most expensive inventory first). "
        "'cheapest' = the lot with the lowest buy price (sell the easiest win first). "
        "'gain' = the lot with the highest current gain percentage. "
        "FIFO is the default and usually the most tax-efficient; switch to 'gain' to maximize realized P&L.",
    ),
    (
        "REBUY_GAP_PCT", "editable", "coin-allowed", "float", "0", "50", "0", "0",
        "Re-buy gap below last sell %",
        "0 = off (grid may re-buy near or above where it last sold if the price dips from a higher level). "
        ">0 = after a sell, the grid waits until the price drops at least X% below that sell price before "
        "opening the next buy. Turn on when you expect the coin to keep dropping after a sell; "
        "leave at 0 to let the grid re-buy on any qualifying -BUY_STEP_PCT dip.",
    ),
    (
        "CEILING_PERCENTILE", "editable", "coin-allowed", "float", "50", "100", "98.0", "98.0",
        "Ceiling percentile",
        "No buys above the Nth percentile of the coin's own last-14-day price history. "
        "98 means buying is blocked only in the top 2% zone -- the recent local highs. "
        "Prevents the grid from accumulating near peaks where a reversal is most likely. "
        "Lower values are more conservative but can block valid dip-buys during a downtrend. "
        "The ceiling recalculates every tick from the coin's own data so it adapts as the market moves.",
    ),
    (
        "PRICE_GATE_PCT", "editable", "coin-allowed", "float", "5", "100", "30.0", "30.0",
        "Price gate %",
        "If the price ticks more than X% from the previous reading, treat it as bad data and skip the tick. "
        "Protects against DexScreener glitches, stale feeds, and flash-spike decisions "
        "(SIMD incident: a garbage tick at 10x drove a bad trade before this guard existed). "
        "30% is generous for meme tokens that move fast; lower it for calmer assets. "
        "A rejected tick is logged but never triggers a trade.",
    ),

    # ── Volatility-adaptive steps ─────────────────────────────────────────────
    (
        "VOL_ADAPTIVE", "editable", "coin-allowed", "bool", None, None, "0", "0",
        "Volatility-adaptive steps",
        "0 = fixed BUY_STEP_PCT / SELL_STEP_PCT for all market conditions. "
        "1 = the steps are computed each tick as k × realized-vol-1d, clamped to a safety band. "
        "Useful when a coin's daily volatility swings widely (e.g. 9%-21% on SIMD). "
        "Not enough price history (< VOL_MIN_BUCKETS) falls back to the fixed steps (fail-open). "
        "Enable per coin after measuring its volatility range; do not enable fleet-wide by default.",
    ),
    (
        "VOL_K_BUY", "editable", "coin-allowed", "float", "0.05", "2", "0.20", "0.20",
        "Vol-adaptive buy k",
        "Only used when VOL_ADAPTIVE=1. "
        "buy_step = VOL_K_BUY × realized_vol_1d (%), clamped to [BUY_STEP_MIN, BUY_STEP_MAX]. "
        "Calibrated at 0.20 on SIMD data: at 10% daily vol → 2% buy step; at 20% → 4%.",
    ),
    (
        "VOL_K_SELL", "editable", "coin-allowed", "float", "0.1", "5", "1.30", "1.30",
        "Vol-adaptive sell k",
        "Only used when VOL_ADAPTIVE=1. "
        "sell_step = VOL_K_SELL × realized_vol_1d (%), clamped to [SELL_STEP_MIN, SELL_STEP_MAX]. "
        "Calibrated at 1.30 on SIMD data: at 10% daily vol → 13% sell step (wider margin).",
    ),
    (
        "BUY_STEP_MIN", "editable", "coin-allowed", "float", "0.5", "15", "2.0", "2.0",
        "Vol-adaptive buy step floor %",
        "Only used when VOL_ADAPTIVE=1. "
        "Lower clamp for the adaptive buy step -- the grid never buys at a step smaller than this "
        "regardless of how low volatility drops. Must be > 0.",
    ),
    (
        "BUY_STEP_MAX", "editable", "coin-allowed", "float", "1", "30", "10.0", "10.0",
        "Vol-adaptive buy step ceiling %",
        "Only used when VOL_ADAPTIVE=1. "
        "Upper clamp for the adaptive buy step -- prevents the grid from stretching rungs too far apart "
        "in extremely volatile regimes. Must be >= BUY_STEP_MIN.",
    ),
    (
        "SELL_STEP_MIN", "editable", "coin-allowed", "float", "1", "20", "6.0", "6.0",
        "Vol-adaptive sell step floor %",
        "Only used when VOL_ADAPTIVE=1. "
        "Lower clamp for the adaptive sell step. Must be > 0 and <= SELL_STEP_MAX.",
    ),
    (
        "SELL_STEP_MAX", "editable", "coin-allowed", "float", "2", "60", "25.0", "25.0",
        "Vol-adaptive sell step ceiling %",
        "Only used when VOL_ADAPTIVE=1. "
        "Upper clamp for the adaptive sell step. Must be >= SELL_STEP_MIN.",
    ),

    # ── Buy floor (#871) ──────────────────────────────────────────────────────
    (
        "BUY_FLOOR", "editable", "coin-allowed", "bool", None, None, "0", "0",
        "Buy floor gate",
        "0 = off. "
        "1 = the grid will only buy when the wallet USDC balance exceeds BUY_FLOOR_USD. "
        "This is a cash-reserve guard: it ensures the bot always keeps a minimum balance free "
        "for gas refills and other coins, even when deploying capital aggressively. "
        "Applies to grid buys, cadence buys, and prebuy -- any buy that would drop the balance below "
        "the floor is skipped and logged.",
    ),
    (
        "BUY_FLOOR_USD", "editable", "coin-allowed", "float", "0", "1000", "0", "20.0",
        "Buy floor USD",
        "Only used when BUY_FLOOR=1. "
        "Minimum USDC wallet balance required before any buy is allowed. "
        "Set to roughly one lot size so the bot always has a reserve for gas and unexpected fees. "
        "Example: with LOT_USD=25, a floor of $20 means no buy happens if the wallet holds less than $20.",
    ),

    # ── Floor zone / cadence (#185) ────────────────────────────────────────────
    (
        "FLOOR_RELATIVE", "editable", "coin-allowed", "bool", None, None, "0", "0",
        "Floor zone (buy the bottom)",
        "0 = off. "
        "1 = when the current price is in the 'floor zone' (below FLOOR_PCTL percentile of the recent "
        "price window) the floor cadence and reserve marking become active. "
        "The floor zone identifies where the coin is historically cheap; cheap lots bought here are "
        "the inventory that gets sold into the next rally. "
        "The ceiling and the buy-step ladder still apply; this only enables the cadence and zone features.",
    ),
    (
        "FLOOR_PCTL", "editable", "coin-allowed", "float", "0", "50", "25.0", "25.0",
        "Floor zone percentile",
        "Only used when FLOOR_RELATIVE=1 and FLOOR_ZONE_MODE='pctl'. "
        "Price is considered in the floor zone when it is below the Xth percentile of the recent window. "
        "25 = the cheapest quarter. Measured on SIMD: P25 captured all missed dip-buys in a flat market "
        "and still buys real lows (July avg 0.000185 before a rally to 0.000336). "
        "Must be less than the ceiling percentile.",
    ),
    (
        "FLOOR_WINDOW_D", "editable", "coin-allowed", "float", "1", "30", "7.0", "7.0",
        "Floor zone window (days)",
        "Only used when FLOOR_RELATIVE=1 and FLOOR_ZONE_MODE='pctl'. "
        "How many days of price history define the floor zone percentile. "
        "Intentionally SHORT (7d vs ceiling's 14d) so the floor adapts to where the market lives now. "
        "A longer window drags old highs and may never place the price in the floor zone.",
    ),
    (
        "FLOOR_ZONE_MODE", "editable", "coin-allowed", "enum", None, None, "pctl", "min48h",
        "Floor zone definition",
        "How the floor zone boundary is computed. "
        "'pctl' = FLOOR_PCTL percentile of the FLOOR_WINDOW_D window (ages slowly). "
        "'min48h' = min of the last FLOOR_ZONE_WINDOW_H hours × (1 + FLOOR_ZONE_BAND_PCT/100): "
        "a fresh reference that never ages because old lows roll off the short window on their own. "
        "Measured: min48h dominates the percentile on time-to-sell and catches more missed floor entries.",
    ),
    (
        "FLOOR_ZONE_WINDOW_H", "editable", "coin-allowed", "float", "1", "168", "48.0", "48.0",
        "Floor zone window (hours, min48h mode)",
        "Only used when FLOOR_ZONE_MODE='min48h'. "
        "Lookback window in hours whose minimum defines the floor zone anchor. "
        "48h is the measured recommendation: short enough to stay current, long enough to see real lows.",
    ),
    (
        "FLOOR_ZONE_BAND_PCT", "editable", "coin-allowed", "float", "0", "25", "5.0", "5.0",
        "Floor zone band % (min48h mode)",
        "Only used when FLOOR_ZONE_MODE='min48h'. "
        "How far above the recent minimum still counts as the floor zone. "
        "5% means prices up to 5% above the 48h low are treated as cheap. "
        "Wider band = more buys in the floor; narrower = only buys exactly at the minimum.",
    ),
    (
        "FLOOR_CADENCE", "editable", "coin-allowed", "bool", None, None, "0", "0",
        "Floor cadence buy",
        "0 = off. "
        "1 = if the price has been in the floor zone for at least FLOOR_CADENCE_N_H hours and no buy "
        "happened in the last FLOOR_CADENCE_M_H hours, buy 1 lot anyway (up to FLOOR_CADENCE_MAX per episode). "
        "Ensures the bot accumulates cheap inventory even when the price drifts sideways without hitting "
        "a -BUY_STEP_PCT grid step -- the grid stalls in flat markets but the cadence keeps buying the bottom.",
    ),
    (
        "FLOOR_CADENCE_N_H", "editable", "coin-allowed", "float", "0.5", "24", "2.0", "2.0",
        "Floor cadence: hours in floor zone",
        "Only used when FLOOR_CADENCE=1. "
        "How long the price must continuously sit in the floor zone before the cadence buy fires. "
        "2h means the bot waits for a confirmed floor, not just a brief dip touch.",
    ),
    (
        "FLOOR_CADENCE_M_H", "editable", "coin-allowed", "float", "1", "48", "6.0", "6.0",
        "Floor cadence: hours without a buy",
        "Only used when FLOOR_CADENCE=1. "
        "The cadence buy fires only when no buy of any kind happened in the last M hours. "
        "Prevents stacking cadence lots on top of normal grid steps during the same floor episode.",
    ),
    (
        "FLOOR_CADENCE_MAX", "editable", "coin-allowed", "float", "1", "10", "2.0", "2.0",
        "Floor cadence: max lots per episode",
        "Only used when FLOOR_CADENCE=1. "
        "Cap on cadence lots per floor episode (a contiguous period below the zone). "
        "Prevents a long sideways floor from draining the cash reserve lot by lot. "
        "2 = at most 2 cadence buys per episode, then the cadence waits for the price to exit and re-enter.",
    ),
    (
        "FLOOR_RESERVE_MAX_LOTS", "editable", "coin-allowed", "float", "0", "30", "0", "8.0",
        "Floor reserve: max live cadence lots",
        "0 = off (no cap on cadence lots). "
        ">0 = the cadence buy is skipped once this many cadence/reserve lots are already open. "
        "Limits total exposure from the cadence mechanism; never sells lots to make room. "
        "8 is the SIMD-measured recommendation: bounds the tail risk while allowing enough inventory.",
    ),

    # ── Prebuy (#314) ──────────────────────────────────────────────────────────
    (
        "PREBUY", "editable", "coin-allowed", "bool", None, None, "0", "0",
        "Prebuy (quiet-market buy)",
        "0 = off. "
        "1 = when the market is flat (price stayed within PREBUY_BAND_PCT% for PREBUY_WINDOW_H hours), "
        "the bot buys a small prebuy lot to pre-position inventory before the next move. "
        "Prebuy lots are tracked separately with origin='prebuy'; they graduate to grid lots once the "
        "price moves up PREBUY_GRAD_UP_PCT% or are sold at loss if dropped PREBUY_GRAD_DOWN_PCT%. "
        "Useful for tokens that spend long periods in a tight range before pumping.",
    ),
    (
        "PREBUY_WINDOW_H", "editable", "coin-allowed", "float", "1", "72", "12.0", "12.0",
        "Prebuy: flat-market window (hours)",
        "Only used when PREBUY=1. "
        "How many hours of price history to check for the flat-market condition. "
        "If the price stayed within PREBUY_BAND_PCT% over this window, a prebuy is triggered.",
    ),
    (
        "PREBUY_BAND_PCT", "editable", "coin-allowed", "float", "0.5", "20", "4.0", "4.0",
        "Prebuy: flat band %",
        "Only used when PREBUY=1. "
        "The price range (max - min) over the prebuy window must be smaller than X% for the market "
        "to be considered flat. 4% means the coin moved less than 4% in the window.",
    ),
    (
        "PREBUY_MAX_N", "editable", "coin-allowed", "float", "1", "10", "2.0", "2.0",
        "Prebuy: max prebuy lots",
        "Only used when PREBUY=1. "
        "Maximum number of live prebuy lots at any time. "
        "Once this many prebuy lots are open, no new prebuy triggers until some are sold or graduated.",
    ),
    (
        "PREBUY_COOLDOWN_H", "editable", "coin-allowed", "float", "1", "168", "24.0", "24.0",
        "Prebuy: cooldown (hours)",
        "Only used when PREBUY=1. "
        "After a prebuy fires, no new prebuy for at least X hours regardless of market conditions. "
        "Prevents rapid prebuy stacking when the market is stubbornly flat.",
    ),
    (
        "PREBUY_CEIL_TOL", "editable", "coin-allowed", "float", "1.0", "2.0", "1.10", "1.10",
        "Prebuy: ceiling tolerance multiplier",
        "Only used when PREBUY=1. "
        "Prebuys are allowed slightly above the relative ceiling (price < ceiling × tolerance). "
        "1.10 = prebuy is blocked only when price is >10% above the ceiling. "
        "The prebuy runs near the ceiling where the flat market may be, so this loosens the ceiling "
        "gate specifically for prebuy (grid buys are still blocked above the ceiling).",
    ),
    (
        "PREBUY_GRADUATE", "editable", "coin-allowed", "bool", None, None, "0", "0",
        "Prebuy graduation",
        "0 = prebuy lots stay tagged 'prebuy' forever and are managed by prebuy rules. "
        "1 = a prebuy lot is converted ('graduated') to a regular grid lot once the price rises "
        "PREBUY_GRAD_UP_PCT% above its buy price. After graduation it follows standard grid sell rules. "
        "Enable if you want prebuy lots to seamlessly join the grid when the move happens.",
    ),
    (
        "PREBUY_GRAD_UP_PCT", "editable", "coin-allowed", "float", "1", "30", "6.0", "6.0",
        "Prebuy graduation: up threshold %",
        "Only used when PREBUY_GRADUATE=1. "
        "A prebuy lot graduates to a grid lot when its price rises X% above the buy price. "
        "6% is the default: once the price confirms an upward move, the lot joins the grid.",
    ),
    (
        "PREBUY_GRAD_DOWN_PCT", "editable", "coin-allowed", "float", "1", "50", "10.0", "10.0",
        "Prebuy graduation: down cutoff %",
        "Only used when PREBUY_GRADUATE=1. "
        "A prebuy lot is sold at a loss (exception to the hard rule) if the price drops X% below "
        "its buy price. This is the ONLY case where a loss is accepted -- prebuy lots are positioned "
        "speculatively before the hard rule applies. Set generously to avoid triggering on noise.",
    ),

    # ── Starter lot ────────────────────────────────────────────────────────────
    (
        "STARTER_LOT", "editable", "coin-allowed", "bool", None, None, "0", "0",
        "Starter lot (first buy on add)",
        "0 = off. The bot waits for the first -BUY_STEP_PCT dip before buying. "
        "1 = when a coin is first added with an empty lot book and the balance covers the lot, "
        "buy one lot immediately to establish a position (origin='grid'). "
        "Useful if you add a coin mid-dip and don't want to miss the entry.",
    ),
    (
        "STARTER_COOLDOWN_H", "editable", "coin-allowed", "float", "0", "168", "0", "0",
        "Starter lot cooldown (hours)",
        "Only used when STARTER_LOT=1. "
        "Wait X hours after the coin is added before firing the starter lot. "
        "0 = buy immediately. >0 = gives you time to check the entry before the bot commits.",
    ),

    # ── Pairing (#412) ────────────────────────────────────────────────────────
    (
        "PAIRING_GRID", "editable", "coin-allowed", "bool", None, None, "0", "0",
        "Pairing (combined group sale)",
        "0 = off. Each lot sells independently based on its own price target. "
        "1 = when a lot has been stuck (price below its target) for PAIRING_GRID_STUCK_DAYS days, "
        "the bot can link it with cheaper 'leveler' lots so the GROUP sells together at a combined profit. "
        "Frees capital trapped in a single expensive lot by subsidizing it with the gain from cheaper lots. "
        "The group only sells when the combined net exceeds PAIR_PROFIT_PCT%.",
    ),
    (
        "PAIRING_GRID_STUCK_DAYS", "editable", "coin-allowed", "float", "1", "60", "7.0", "7.0",
        "Pairing: stuck threshold (days)",
        "Only used when PAIRING_GRID=1. "
        "A lot is considered stuck once it has been open for X days without selling. "
        "7d is the default: a lot that has not sold after a week becomes eligible for pairing.",
    ),
    (
        "PAIRING_GRID_THRESHOLD_PCT", "editable", "coin-allowed", "float", "1", "100", "20.0", "20.0",
        "Pairing: stuck price threshold %",
        "Only used when PAIRING_GRID=1. "
        "A lot is marked stuck when the current price is more than X% below its sell target. "
        "20% means the lot needs a 20% price recovery before it would sell on its own.",
    ),
    (
        "PAIRING_MAX_LEVELERS", "editable", "coin-allowed", "float", "1", "5", "1.0", "1.0",
        "Pairing: max leveler lots",
        "Only used when PAIRING_GRID=1. "
        "Maximum number of cheap lots that can be linked to a single stuck lot in one pairing group. "
        "1 = one leveler per stuck lot (the simplest and most liquid setup).",
    ),
    (
        "PAIR_PROFIT_PCT", "editable", "coin-allowed", "float", "0.5", "20", "3.0", "3.0",
        "Pairing: minimum combined profit %",
        "Only used when PAIRING_GRID=1. "
        "The paired group only sells when combined proceeds exceed combined cost by at least X%. "
        "3% ensures the pairing always realizes a net positive return, satisfying the hard rule at group level.",
    ),

    # ── Batch sell (#968) ─────────────────────────────────────────────────────
    (
        "BATCH_SELL", "editable", "coin-allowed", "bool", None, None, "0", "0",
        "Batch sell cap",
        "0 = off. All eligible lots sell in a single tick. "
        "1 = cap the sell plan per tick to BATCH_SELL_MAX_LOTS lots and MAX_TRADE_USD total value. "
        "Prevents large simultaneous sells that could move the price against the bot in thin markets. "
        "The bot will sell the remaining lots in subsequent ticks.",
    ),
    (
        "BATCH_SELL_MAX_LOTS", "editable", "coin-allowed", "float", "1", "20", "2.0", "2.0",
        "Batch sell: max lots per tick",
        "Only used when BATCH_SELL=1. "
        "Maximum number of lots the bot will sell in a single tick. "
        "Lots are chosen by the least-gain-first heuristic to preserve upside while clearing the backlog.",
    ),
    (
        "MAX_TRADE_USD", "editable", "coin-allowed", "float", "1", "5000", "50.0", "50.0",
        "Max trade USD per sell",
        "Only used when BATCH_SELL=1. "
        "Hard cap on the total USD value sold per tick. "
        "If the sell plan exceeds this, the bot trims it (smallest-gain first) until it fits. "
        "Set to roughly 2× LOT_USD for normal meme tokens; higher for coins with deep liquidity.",
    ),

    # ── Gas refill ────────────────────────────────────────────────────────────
    (
        "GAS_REFILL", "editable", "coin-allowed", "bool", None, None, "0", "0",
        "Auto gas refill",
        "0 = off. The bot never spends USDC to buy SOL for gas automatically. "
        "1 = after a sell tick in live mode, if the SOL balance (in lamports) drops below "
        "the low-SOL threshold, the bot swaps GAS_REFILL_USDC from the wallet into SOL. "
        "Prevents the bot from stopping because it ran out of gas. "
        "Only applies in live mode -- paper mode has no real gas cost.",
    ),
    (
        "GAS_REFILL_USDC", "editable", "coin-allowed", "float", "1", "100", "10.0", "10.0",
        "Gas refill amount (USDC)",
        "Only used when GAS_REFILL=1. "
        "How much USDC to swap into SOL each time the gas refill fires. "
        "10 USDC buys roughly 0.05-0.1 SOL at current prices -- enough for 50-100 transactions. "
        "Keep it small: the bot refills automatically so a large one-shot top-up is not needed.",
    ),
    (
        "GAS_REFILL_LOW_SOL", "editable", "coin-allowed", "float", "0.001", "1", "0.01", "0.01",
        "Gas refill low-SOL threshold",
        "Only used when GAS_REFILL=1. "
        "The refill fires when the SOL balance drops below X SOL. "
        "0.01 SOL ≈ 10M lamports -- enough for roughly 10 transactions at current fees. "
        "The gas reserve (GAS_RESERVE_LAMPORTS in .env) must always stay above this after the refill.",
    ),

    # ── Topup insurance (#772) ────────────────────────────────────────────────
    (
        "TOPUP_INSURANCE", "editable", "coin-allowed", "bool", None, None, "0", "0",
        "Topup insurance (empty-book buy)",
        "0 = off. "
        "1 = if the lot book is completely empty (no open lots) and no other buy happened this tick, "
        "buy TOPUP_LOT_USD unconditionally -- ignoring the ceiling -- to ensure the bot always holds "
        "at least one lot and is never 100% in cash. "
        "This guarantees position continuity after a full book sellout and protects against missing "
        "a rally that starts while the book is empty.",
    ),
    (
        "TOPUP_LOT_USD", "editable", "coin-allowed", "float", "1", "200", "15.0", "15.0",
        "Topup insurance lot size (USD)",
        "Only used when TOPUP_INSURANCE=1. "
        "How much to buy when the topup insurance fires. "
        "Keep it smaller than LOT_USD (e.g. $15 vs $25 lot) so it is a safety net, not the primary strategy.",
    ),

    # ── Slippage ──────────────────────────────────────────────────────────────
    (
        "MAX_SLIPPAGE_BPS", "editable", "coin-allowed", "float", "10", "1000", "150", "150",
        "Max slippage (basis points)",
        "Maximum slippage tolerance for Jupiter swaps, in basis points (100 bps = 1%). "
        "150 bps = 1.5% is the default for regular buys/sells. "
        "Gas refills use 2× this value because SOL/USDC is more liquid but the swap is smaller. "
        "MONEY-CRITICAL: must be explicitly set for any live coin (fail-closed). "
        "Too tight and swaps fail on volatile tokens; too loose and you lose to MEV.",
    ),

    # ── Fleet-level / system ───────────────────────────────────────────────────
    (
        "QUOTE_MINT", "locked-visible", "fleet-only", "text", None, None,
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", None,
        "Quote mint (USDC address)",
        "The USDC SPL token address on Solana. Always EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v. "
        "Per SIMD incident #356, using any other quote (SOL, etc.) worked silently in paper mode "
        "but spent the wrong currency once LIVE was turned on. "
        "This is locked -- it never changes and cannot be overridden per coin.",
    ),
    (
        "DEFAULT_MODE", "editable", "fleet-only", "enum", None, None, "paper", "paper",
        "Default mode for new coins",
        "Starting mode for new coins added through the dashboard. "
        "'paper' = simulated fills with real prices, no wallet touched. "
        "'live' = real swaps via Jupiter. "
        "A newly added coin always starts in this mode; switch it to live explicitly after "
        "validating behavior in paper first. Never set this to 'live' unless all new coins "
        "should start in live mode immediately.",
    ),
]

FLEET_DEFAULTS = {row[0]: row[6] for row in CATALOG}


def main():
    store = Store(cfg.DB_PATH)
    c = store._c

    print(f"Bootstrapping DB: {cfg.DB_PATH}")

    # param_catalog
    inserted = 0
    for row in CATALOG:
        key, tier, scope, typ, mn, mx, default, rec, label, help_ = row
        cur = c.execute(
            "INSERT OR IGNORE INTO param_catalog"
            " (key, tier, scope, type, min, max, default_val, recommended, label, help)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key, tier, scope, typ, mn, mx, default, rec, label, help_),
        )
        inserted += cur.rowcount
    c.commit()
    print(f"  param_catalog: {inserted} rows inserted ({len(CATALOG)} total)")

    # fleet_versions + fleet_defaults (version 1, pinned)
    existing = c.execute("SELECT version_id FROM fleet_versions WHERE pinned=1").fetchone()
    if existing:
        print(f"  fleet_defaults: version {existing['version_id']} already pinned, skipping")
    else:
        cur = c.execute(
            "INSERT INTO fleet_versions (author, reason, pinned) VALUES (?, ?, 1)",
            ("bootstrap", "initial seed from bootstrap.py"),
        )
        ver_id = cur.lastrowid
        for key, value in FLEET_DEFAULTS.items():
            c.execute(
                "INSERT OR REPLACE INTO fleet_defaults (version_id, key, value) VALUES (?,?,?)",
                (ver_id, key, value),
            )
        c.commit()
        print(f"  fleet_defaults: version {ver_id} created with {len(FLEET_DEFAULTS)} keys (pinned)")

    print("Done. Start the scheduler with: python3 scheduler.py --paper")


if __name__ == "__main__":
    main()
