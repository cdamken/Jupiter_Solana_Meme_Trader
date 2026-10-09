#!/usr/bin/env python3
"""vol_engine.py — volatility-adaptive grid steps (issue #250).

PURE logic: no I/O, no network, no global state. Given the recent (timestamp, price)
history it measures realized volatility and maps it to the grid's buy/sell steps, so the
ladder self-adjusts to the regime instead of behaving as a different strategy every week
under fixed steps (the finding in #250, axis A of #238). Same math validated by Dot's
walkforward; extracted here so backtest and production run IDENTICALLY (DRY: one place to
test and audit), exactly like grid_engine / rally_engine.

`trader.py` reads the history (I/O lives there) and applies the result to a COPY of its
config, the same pattern as `apply_relative_ceiling`.

HARD RULE stays intact no matter what: an adaptive SELL_STEP only moves the sell TRIGGER
(`threshold_pct`). The per-lot no-loss floor is enforced separately in `sellable_lots`
(`net price > lot cost`) and is NEVER relaxed here. A smaller adaptive sell_step sells
sooner, never below cost.

Canonical spec (Dot, #250, deterministic re-run of the walkforward):
  - Hourly bucket = int(ts/3600); LAST price wins in each bucket.
  - Window = buckets whose hour is within the last `window_h` hours (5d = 120h) BY
    bucket timestamp, not by count.
  - LOG returns between CONSECUTIVE PRESENT buckets: the present buckets are compacted in
    time order and returns taken between neighbors (gaps are ignored, no interpolation).
  - POPULATION std (divide by n). vol_1d = std * sqrt(24) * 100  (percent per day).
  - Fewer than `min_buckets` (25) present buckets in the window -> None (fail-open: the
    caller keeps the fixed steps). Only SIMD is wired; BOME tied in the sweep and stays fixed.
"""
import math

WINDOW_H_DEFAULT = 120       # 5 days of hourly buckets
MIN_BUCKETS_DEFAULT = 25      # below this the measure is noise -> fail-open to fixed steps
HOURS_PER_DAY = 24


def hourly_buckets(pairs, now, window_h=WINDOW_H_DEFAULT):
    """LAST price per clock-hour bucket (int(ts/3600)) for the pairs whose bucket hour is
    within the last `window_h` hours by bucket timestamp. `pairs` = iterable of
    (ts, price); prices <= 0 are dropped. Returns the compacted series of present-bucket
    prices ordered by bucket hour ascending. Robust to out-of-order rows (keeps the price
    with the largest ts inside each bucket)."""
    current_hour = int(now // 3600)
    cutoff = current_hour - window_h
    last_by_hour = {}   # hour -> (ts, price)
    for ts, p in pairs:
        if p is None or p <= 0:
            continue
        h = int(ts // 3600)
        if h <= cutoff:                 # older than the window
            continue
        prev = last_by_hour.get(h)
        if prev is None or ts >= prev[0]:
            last_by_hour[h] = (ts, p)
    return [last_by_hour[h][1] for h in sorted(last_by_hour)]


def realized_vol(pairs, now, window_h=WINDOW_H_DEFAULT, min_buckets=MIN_BUCKETS_DEFAULT):
    """Realized DAILY volatility in percent from the hourly buckets, or None (fail-open).

    LOG returns between consecutive present buckets, POPULATION std (/n), scaled by
    sqrt(24) to a daily figure and expressed in percent. Returns None with fewer than
    `min_buckets` present buckets in the window (noise floor -> caller keeps fixed steps)."""
    series = hourly_buckets(pairs, now, window_h)
    if len(series) < min_buckets:
        return None
    rets = []
    for i in range(1, len(series)):
        a, b = series[i - 1], series[i]
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if not rets:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / len(rets)   # POPULATION variance (/n)
    return math.sqrt(var) * math.sqrt(HOURS_PER_DAY) * 100.0


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def vol_steps(vol_1d, k_buy, k_sell, buy_min, buy_max, sell_min, sell_max):
    """Maps daily volatility (%) to (buy_step, sell_step) in %, each clamped to its safety
    band. `vol_1d` None -> None (the caller keeps the fixed steps). The clamps are a floor
    /ceiling on the ladder width; they do NOT touch the no-loss rule (see module docstring).
    Calibrated defaults (Dot #250, SIMD): k_buy=0.20, k_sell=1.30, bands 2..10 / 6..25."""
    if vol_1d is None:
        return None
    buy = _clamp(k_buy * vol_1d, buy_min, buy_max)
    sell = _clamp(k_sell * vol_1d, sell_min, sell_max)
    return (buy, sell)


# ---- (#822/#837) adaptive-step saturation ------------------------------------------------
# The #819 failure class: a vol-adaptive step whose K*vol lands OUTSIDE its clamp pins at a
# bound and becomes a STATIC step in disguise (CATE sat at sell_step=25.0 for ~70h). This pure
# test detects it. It is the SINGLE source of that logic: the fleet WATCH (healthcheck #825,
# humans) and the self-protecting FALLBACK (trader #837, the bot itself) both call it, so they
# can never drift apart.
SAT_COVERAGE_H_DEFAULT = 24.0        # a full day of decisions considered
SAT_MIN_SAMPLES_DEFAULT = 5          # decision rows are per-TRADE (~5/day), not per-tick (#825)
SAT_MIN_SPAN_H_DEFAULT = 20.0        # pinned samples must span >= this much of the day
SAT_EPS_DEFAULT = 0.01               # |step - bound| < eps counts as pinned


def adaptive_saturated(samples, bound_lo, bound_hi, now,
                       coverage_h=SAT_COVERAGE_H_DEFAULT, min_samples=SAT_MIN_SAMPLES_DEFAULT,
                       min_span_h=SAT_MIN_SPAN_H_DEFAULT, eps=SAT_EPS_DEFAULT):
    """(#822) PURE saturation test for an adaptive sell step. The #819 failure class: an
    adaptive step whose K*vol lands OUTSIDE its clamps pins at a bound and becomes a STATIC
    step in disguise, with no visibility (CATE sat at sell_step=25.0 for ~70h).

    `samples`: a bot's recent decisions as (ts_epoch, step, adaptive_on, tight) tuples —
      step        = the RAW vol-adaptive base step logged that decision (the K*vol value that
                    pins at the clamp; NOT an effective step already tightened or already
                    swapped for a static fallback, or the pin would be invisible),
      adaptive_on = VOL_ADAPTIVE (or a future REBOUND) was active for it,
      tight       = the cash-band (adaptive_sell_engine) was in TIGHT mode, which legitimately
                    pushes the step DOWN and is NOT saturation, so those are excluded.
    `bound_lo`/`bound_hi`: the EFFECTIVE clamp bounds the runtime used — resolved with the
      same code defaults cfg() applies, not just what config.sh sets (CATE had none of its
      own, so the real bound was the code default).

    Considers only the trailing `coverage_h` window, adaptive-on and non-tight. Returns
    (saturated, which, pinned_hours):
      saturated -> True only when EVERY such decision is pinned (|step - bound| < eps) at the
                   SAME bound, there are >= min_samples of them, AND they SPAN at least
                   `min_span_h` hours (oldest to newest). The span (not "a sample near the 24h
                   edge") is the coverage test on purpose: decision rows are written per TRADE,
                   not per tick (#825, Dot), so a bot logs only ~5/day — an edge-boundary check
                   would demand a decision within a second of the 24h mark and never fire on the
                   real, sparse cadence. FAIL-OPEN: too few samples, or a span shorter than
                   min_span_h (a brief recent burst, or a bot that just stopped) -> not flagged,
                   so a WARN only ever fires once we've truly seen the step pinned across ~a day.
    A fixed step with adaptive OFF is normal and never flagged (those samples are excluded)."""
    cutoff = now - coverage_h * 3600.0
    rel = [(ts, step) for (ts, step, adaptive_on, tight) in samples
           if adaptive_on and not tight and ts is not None and ts >= cutoff
           and step is not None]
    if len(rel) < min_samples:
        return False, None, 0.0
    oldest = min(ts for ts, _ in rel)
    newest = max(ts for ts, _ in rel)
    pinned_hours = (newest - oldest) / 3600.0
    if pinned_hours < min_span_h:                     # hasn't persisted across ~the whole day yet
        return False, None, pinned_hours
    # (#974) check the bound is not None BEFORE the subtraction, so a None bound fails safe (not pinned)
    # instead of raising a TypeError inside abs(step - None). The later guards used to run too late.
    at_hi = bound_hi is not None and all(abs(step - bound_hi) < eps for _, step in rel)
    at_lo = bound_lo is not None and all(abs(step - bound_lo) < eps for _, step in rel)
    if at_hi:
        return True, "max", pinned_hours
    if at_lo:
        return True, "min", pinned_hours
    return False, None, pinned_hours
