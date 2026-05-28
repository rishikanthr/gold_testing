"""
detector_core.py — Shared ICT/SMC detection primitives
=======================================================
Pure math — no LLM, no side effects.
All 7 strategies call these functions.
Every result is deterministic: same candles → same output.
"""

from config import (
    EQUAL_LEVEL_TOLERANCE, MIN_SWEEP_DISTANCE, DISPLACEMENT_BODY_PCT,
    MIN_DISPLACEMENT_BODY, MIN_FVG_SIZE, FVG_EXPIRY_CANDLES,
    SWING_LOOKBACK, SL_BUFFER_PIPS, PIP_SIZE, TP1_RR, TP2_RR,
    PSYCH_LEVELS_GOLD, PSYCH_LEVEL_PROXIMITY,
)


# ============================================================
# SWING HIGH / LOW PIVOTS
# ============================================================

def find_swing_highs(candles: list, lookback: int = SWING_LOOKBACK) -> list:
    """Pivot highs — candle high is the highest within `lookback` candles each side."""
    pivots = []
    for i in range(lookback, len(candles) - lookback):
        h = candles[i]["high"]
        if all(candles[i-j]["high"] < h and candles[i+j]["high"] < h
               for j in range(1, lookback + 1)):
            pivots.append((i, h))
    return pivots


def find_swing_lows(candles: list, lookback: int = SWING_LOOKBACK) -> list:
    """Pivot lows — candle low is the lowest within `lookback` candles each side."""
    pivots = []
    for i in range(lookback, len(candles) - lookback):
        lo = candles[i]["low"]
        if all(candles[i-j]["low"] > lo and candles[i+j]["low"] > lo
               for j in range(1, lookback + 1)):
            pivots.append((i, lo))
    return pivots


# ============================================================
# DAILY BIAS  (HH/HL vs LH/LL)
# ============================================================

def get_daily_bias(daily_candles: list) -> tuple:
    """
    Returns (bias: str, reason: str).
    bias ∈ {"BULLISH", "BEARISH", "NEUTRAL"}

    Rules:
    - BULLISH : last 3 swings show HH+HL AND majority of closes up
    - BEARISH : last 3 swings show LH+LL AND majority of closes down
    - NEUTRAL : mixed structure
    """
    if len(daily_candles) < 5:
        return "NEUTRAL", "Insufficient daily candles"

    recent = daily_candles[-5:]
    highs  = [c["high"]  for c in recent]
    lows   = [c["low"]   for c in recent]
    closes = [c["close"] for c in recent]

    hh = highs[-1]  > highs[-2]  > highs[-3]
    hl = lows[-1]   > lows[-2]   > lows[-3]
    lh = highs[-1]  < highs[-2]  < highs[-3]
    ll = lows[-1]   < lows[-2]   < lows[-3]

    bull_closes = sum(closes[i] > closes[i-1] for i in range(1, len(closes)))
    bear_closes = sum(closes[i] < closes[i-1] for i in range(1, len(closes)))

    if (hh or hl) and bull_closes >= 3:
        return "BULLISH", f"HH/HL structure | {bull_closes}/4 bullish closes"
    if (lh or ll) and bear_closes >= 3:
        return "BEARISH", f"LH/LL structure | {bear_closes}/4 bearish closes"
    if bull_closes >= 3:
        return "BULLISH", f"Predominantly bullish closes ({bull_closes}/4)"
    if bear_closes >= 3:
        return "BEARISH", f"Predominantly bearish closes ({bear_closes}/4)"
    return "NEUTRAL", "Mixed structure"


# ============================================================
# WEEKLY BIAS (for Strategy 4)
# ============================================================

def get_weekly_bias(weekly_candles: list) -> tuple:
    """Returns (bias, reason) from weekly candles."""
    if len(weekly_candles) < 3:
        return "NEUTRAL", "Insufficient weekly candles"
    highs = [c["high"]  for c in weekly_candles[-3:]]
    lows  = [c["low"]   for c in weekly_candles[-3:]]
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "BULLISH", "Weekly HH+HL"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "BEARISH", "Weekly LH+LL"
    return "NEUTRAL", "Mixed weekly"


# ============================================================
# HTF PREMIUM / DISCOUNT ZONE
# ============================================================

def get_htf_zone(daily_candles: list, current_price: float) -> str:
    """
    PREMIUM  — above 55% of the 10-day range (sell zone)
    DISCOUNT — below 45% (buy zone)
    EQUILIBRIUM — in between
    """
    if len(daily_candles) < 3:
        return "EQUILIBRIUM"
    recent     = daily_candles[-10:]
    range_high = max(c["high"] for c in recent)
    range_low  = min(c["low"]  for c in recent)
    rng        = range_high - range_low
    if rng == 0:
        return "EQUILIBRIUM"
    pos = (current_price - range_low) / rng
    if pos > 0.55:
        return "PREMIUM"
    if pos < 0.45:
        return "DISCOUNT"
    return "EQUILIBRIUM"


# ============================================================
# EQUAL HIGHS / LOWS  →  LIQUIDITY CLUSTERS
# ============================================================

def find_equal_highs(candles: list, pair: str,
                     max_age: int = 30) -> list:
    """
    BSL pools — two pivot highs within EQUAL_LEVEL_TOLERANCE of each other.
    Returns list of cluster dicts.
    """
    pivots    = find_swing_highs(candles)
    tolerance = EQUAL_LEVEL_TOLERANCE
    clusters  = []
    n         = len(candles)

    for i in range(len(pivots)):
        for j in range(i + 1, len(pivots)):
            idx_i, p_i = pivots[i]
            idx_j, p_j = pivots[j]
            if idx_j < n - max_age:
                continue
            if abs(p_i - p_j) / max(p_i, p_j) <= tolerance:
                level = (p_i + p_j) / 2
                clusters.append({
                    "level":   round(level, 5),
                    "high":    round(max(p_i, p_j), 5),
                    "indices": [idx_i, idx_j],
                    "type":    "BSL",
                    "age":     n - idx_j,
                })

    # deduplicate
    unique = []
    for c in clusters:
        if not any(abs(c["level"] - u["level"]) < 0.0005 for u in unique):
            unique.append(c)
    return unique


def find_equal_lows(candles: list, pair: str,
                    max_age: int = 30) -> list:
    """SSL pools — two pivot lows within tolerance."""
    pivots    = find_swing_lows(candles)
    tolerance = EQUAL_LEVEL_TOLERANCE
    clusters  = []
    n         = len(candles)

    for i in range(len(pivots)):
        for j in range(i + 1, len(pivots)):
            idx_i, p_i = pivots[i]
            idx_j, p_j = pivots[j]
            if idx_j < n - max_age:
                continue
            if abs(p_i - p_j) / max(p_i, p_j) <= tolerance:
                level = (p_i + p_j) / 2
                clusters.append({
                    "level":   round(level, 5),
                    "low":     round(min(p_i, p_j), 5),
                    "indices": [idx_i, idx_j],
                    "type":    "SSL",
                    "age":     n - idx_j,
                })

    unique = []
    for c in clusters:
        if not any(abs(c["level"] - u["level"]) < 0.0005 for u in unique):
            unique.append(c)
    return unique


# ============================================================
# SWEEP DETECTION
# ============================================================

def detect_sweep(candles: list, pair: str,
                 clusters: list, sweep_type: str,
                 lookback: int = 3) -> dict | None:
    """
    Detects a liquidity sweep in the most recent `lookback` candles.

    sweep_type : "BSL" → wick above equal highs, close back below  → SHORT
                 "SSL" → wick below equal lows, close back above   → LONG

    Returns sweep dict or None.
    """
    if not candles or not clusters:
        return None

    min_dist = MIN_SWEEP_DISTANCE.get(pair, 0.0003)
    n        = len(candles)

    for i in range(max(0, n - lookback), n):
        c = candles[i]
        for cluster in clusters:
            level = cluster["level"]

            if sweep_type == "BSL":
                if (c["high"] - level) >= min_dist and c["close"] < level:
                    return {
                        "type":             "BSL",
                        "signal":           "SHORT",
                        "liquidity_level":  level,
                        "sweep_price":      round(c["high"], 5),
                        "sweep_candle_idx": i,
                        "sweep_candle":     c,
                        "sweep_depth_pips": round((c["high"] - level) / PIP_SIZE.get(pair, 0.0001), 1),
                    }

            elif sweep_type == "SSL":
                if (level - c["low"]) >= min_dist and c["close"] > level:
                    return {
                        "type":             "SSL",
                        "signal":           "LONG",
                        "liquidity_level":  level,
                        "sweep_price":      round(c["low"], 5),
                        "sweep_candle_idx": i,
                        "sweep_candle":     c,
                        "sweep_depth_pips": round((level - c["low"]) / PIP_SIZE.get(pair, 0.0001), 1),
                    }
    return None


# ============================================================
# DISPLACEMENT CANDLE
# ============================================================

def find_displacement(candles_15m: list, sweep_time: str,
                      signal: str, pair: str,
                      lookahead: int = 5) -> dict | None:
    """
    Finds the displacement candle AFTER the sweep on the 15M chart.
    Body >= MIN_DISPLACEMENT_BODY AND body >= 60% of range.
    Direction must match signal.
    """
    min_body = MIN_DISPLACEMENT_BODY.get(pair, 0.0025)
    pip      = PIP_SIZE.get(pair, 0.0001)

    # locate candles after sweep_time
    after = []
    for c in candles_15m:
        if c["time"] >= sweep_time:
            after.append(c)
        if len(after) >= lookahead:
            break

    if not after:
        after = candles_15m[-lookahead:]

    for c in after:
        body       = abs(c["close"] - c["open"])
        total_rng  = c["high"] - c["low"]
        if total_rng == 0:
            continue
        body_pct   = body / total_rng
        is_bearish = c["close"] < c["open"]
        is_bullish = c["close"] > c["open"]
        direction_ok = (signal == "SHORT" and is_bearish) or \
                       (signal == "LONG"  and is_bullish)

        if direction_ok and body >= min_body and body_pct >= DISPLACEMENT_BODY_PCT:
            return {
                "candle":    c,
                "body":      round(body, 5),
                "body_pct":  round(body_pct, 3),
                "body_pips": round(body / pip, 1),
            }
    return None


# ============================================================
# FVG DETECTION
# ============================================================

def find_fvg(candles_15m: list, displacement: dict,
             signal: str, pair: str) -> dict | None:
    """
    Finds the Fair Value Gap created by the displacement.

    Bearish FVG: c[i-2].low > c[i].high      → sell zone above
    Bullish FVG: c[i-2].high < c[i].low      → buy zone below

    Returns FVG dict or None.
    """
    min_fvg   = MIN_FVG_SIZE.get(pair, 0.0002)
    disp_time = displacement["candle"]["time"]

    # find displacement index
    disp_idx = None
    for idx, c in enumerate(candles_15m):
        if c["time"] == disp_time:
            disp_idx = idx
            break

    if disp_idx is None:
        disp_idx = len(candles_15m) - 3

    start = max(2, disp_idx - 2)
    end   = min(len(candles_15m) - 1, disp_idx + 4)

    for i in range(start, end + 1):
        if i < 2 or i >= len(candles_15m):
            continue
        c0 = candles_15m[i - 2]
        c2 = candles_15m[i]

        if signal == "SHORT":
            top = c0["low"]
            bot = c2["high"]
            if top > bot and (top - bot) >= min_fvg:
                age = len(candles_15m) - i
                if age <= FVG_EXPIRY_CANDLES:
                    return {
                        "top":      round(top, 5),
                        "bot":      round(bot, 5),
                        "midpoint": round((top + bot) / 2, 5),
                        "age":      age,
                        "type":     "BEARISH",
                        "size_pips": round((top - bot) / PIP_SIZE.get(pair, 0.0001), 1),
                    }
        else:
            bot = c0["high"]
            top = c2["low"]
            if top > bot and (top - bot) >= min_fvg:
                age = len(candles_15m) - i
                if age <= FVG_EXPIRY_CANDLES:
                    return {
                        "top":      round(top, 5),
                        "bot":      round(bot, 5),
                        "midpoint": round((top + bot) / 2, 5),
                        "age":      age,
                        "type":     "BULLISH",
                        "size_pips": round((top - bot) / PIP_SIZE.get(pair, 0.0001), 1),
                    }
    return None


# ============================================================
# FVG SCAN  (for Strategy 5 — multiple FVGs on 4H)
# ============================================================

def scan_all_fvgs(candles: list, signal: str, pair: str,
                  min_body_pips: float = 15.0) -> list:
    """
    Scans all candles for FVGs in the given direction.
    Used by S5 FVG retracement strategy.
    Returns list of FVG dicts sorted by recency (most recent first).
    """
    min_fvg = MIN_FVG_SIZE.get(pair, 0.0002)
    pip     = PIP_SIZE.get(pair, 0.0001)
    fvgs    = []
    n       = len(candles)

    for i in range(2, n):
        c0 = candles[i - 2]
        c1 = candles[i - 1]
        c2 = candles[i]

        body1 = abs(c1["close"] - c1["open"]) / pip
        if body1 < min_body_pips:
            continue

        if signal == "LONG":
            top = c2["low"]
            bot = c0["high"]
            if top > bot and (top - bot) >= min_fvg:
                fvgs.append({
                    "top":      round(top, 5),
                    "bot":      round(bot, 5),
                    "midpoint": round((top + bot) / 2, 5),
                    "age":      n - i,
                    "type":     "BULLISH",
                    "candle_time": c1["time"],
                })
        else:
            top = c0["low"]
            bot = c2["high"]
            if top > bot and (top - bot) >= min_fvg:
                fvgs.append({
                    "top":      round(top, 5),
                    "bot":      round(bot, 5),
                    "midpoint": round((top + bot) / 2, 5),
                    "age":      n - i,
                    "type":     "BEARISH",
                    "candle_time": c1["time"],
                })

    fvgs.sort(key=lambda x: x["age"])
    return fvgs


# ============================================================
# ORDER BLOCK DETECTION  (for Strategy 3)
# ============================================================

def find_order_blocks(candles: list, signal: str, pair: str,
                      max_age: int = 60) -> list:
    """
    Finds Order Blocks — the last candle OPPOSITE to a strong impulse.

    Bearish OB: last BULLISH candle before a 3+ candle bearish impulse
    Bullish OB: last BEARISH candle before a 3+ candle bullish impulse

    Returns list of OB dicts.
    """
    pip  = PIP_SIZE.get(pair, 0.0001)
    obs  = []
    n    = len(candles)

    for i in range(2, n - 3):
        # Check if there's a strong 3-candle impulse starting at i+1
        impulse = candles[i+1 : i+4]
        if len(impulse) < 3:
            continue

        if signal == "SHORT":
            # Need 3 consecutive bearish candles after a bullish candle
            all_bearish = all(c["close"] < c["open"] for c in impulse)
            is_bullish  = candles[i]["close"] > candles[i]["open"]
            if all_bearish and is_bullish:
                age = n - i
                if age <= max_age:
                    ce = (candles[i]["high"] + candles[i]["low"]) / 2
                    obs.append({
                        "high":  candles[i]["high"],
                        "low":   candles[i]["low"],
                        "ce":    round(ce, 5),
                        "age":   age,
                        "type":  "BEARISH_OB",
                        "candle_time": candles[i]["time"],
                    })

        else:  # LONG
            all_bullish = all(c["close"] > c["open"] for c in impulse)
            is_bearish  = candles[i]["close"] < candles[i]["open"]
            if all_bullish and is_bearish:
                age = n - i
                if age <= max_age:
                    ce = (candles[i]["high"] + candles[i]["low"]) / 2
                    obs.append({
                        "high":  candles[i]["high"],
                        "low":   candles[i]["low"],
                        "ce":    round(ce, 5),
                        "age":   age,
                        "type":  "BULLISH_OB",
                        "candle_time": candles[i]["time"],
                    })

    obs.sort(key=lambda x: x["age"])
    return obs


# ============================================================
# NEAREST PSYCHOLOGICAL LEVEL  (gold only)
# ============================================================

def nearest_psych_level(price: float) -> tuple:
    """
    Returns (nearest_level, distance_pts) for gold.
    """
    nearest = min(PSYCH_LEVELS_GOLD, key=lambda l: abs(price - l))
    return nearest, round(abs(price - nearest), 2)


def is_near_psych_level(price: float) -> bool:
    """Returns True if price is within PSYCH_LEVEL_PROXIMITY of a $50 level."""
    _, dist = nearest_psych_level(price)
    return dist <= PSYCH_LEVEL_PROXIMITY


# ============================================================
# CHoCH DETECTION
# ============================================================

def detect_choch(candles: list, signal: str,
                 start_idx: int = None) -> bool:
    """
    Detects Change of Character after a sweep.

    SHORT: price forms LH, then closes below prior swing low  → bearish CHoCH
    LONG:  price forms HL, then closes above prior swing high → bullish CHoCH
    """
    if not candles:
        return False

    post = candles[start_idx:] if start_idx else candles[-10:]
    if len(post) < 3:
        return False

    if signal == "SHORT":
        first_high = post[0]["high"]
        for i in range(1, len(post)):
            if post[i]["high"] < first_high:
                prior_low = min(c["low"] for c in post[:i])
                if any(post[j]["close"] < prior_low for j in range(i, len(post))):
                    return True
        return False
    else:
        first_low = post[0]["low"]
        for i in range(1, len(post)):
            if post[i]["low"] > first_low:
                prior_high = max(c["high"] for c in post[:i])
                if any(post[j]["close"] > prior_high for j in range(i, len(post))):
                    return True
        return False


# ============================================================
# TRADE LEVEL CALCULATION
# ============================================================

def calculate_levels(pair: str, signal: str,
                     entry: float, sweep_price: float) -> dict:
    """
    Computes SL, TP1, TP2 from entry and sweep wick.
    SL = beyond sweep wick + buffer pips
    TP1 = entry ± risk × TP1_RR
    TP2 = entry ± risk × TP2_RR
    """
    pip    = PIP_SIZE.get(pair, 0.0001)
    buffer = SL_BUFFER_PIPS.get(pair, 3) * pip

    if signal == "SHORT":
        sl   = round(sweep_price + buffer, 5)
        risk = sl - entry
        tp1  = round(entry - risk * TP1_RR, 5)
        tp2  = round(entry - risk * TP2_RR, 5)
    else:
        sl   = round(sweep_price - buffer, 5)
        risk = entry - sl
        tp1  = round(entry + risk * TP1_RR, 5)
        tp2  = round(entry + risk * TP2_RR, 5)

    risk_pips = round(abs(risk) / pip, 1)
    return {
        "entry":      entry,
        "stop_loss":  sl,
        "tp1":        tp1,
        "tp2":        tp2,
        "risk_pips":  risk_pips,
        "tp1_pips":   round(abs(entry - tp1) / pip, 1),
        "tp2_pips":   round(abs(entry - tp2) / pip, 1),
        "risk_price": round(abs(risk), 5),
    }


# ============================================================
# ASIAN RANGE HELPER
# ============================================================

def extract_asian_range(candles_h1: list) -> dict | None:
    """
    Finds the most recent completed Asian session range (00:00–07:00 UTC)
    from 1H candles. Returns {high, low, mid, width_pips, drift_pts}.
    """
    from datetime import datetime, timezone

    asian = []
    for c in candles_h1:
        try:
            t = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
            if 0 <= t.hour < 7:
                asian.append(c)
        except Exception:
            continue

    if not asian:
        return None

    hi   = max(c["high"] for c in asian)
    lo   = min(c["low"]  for c in asian)
    mid  = (hi + lo) / 2

    # Flatness: how much did the candle closes drift?
    closes = [c["close"] for c in asian]
    drift  = max(closes) - min(closes)

    return {
        "high":       round(hi, 5),
        "low":        round(lo, 5),
        "mid":        round(mid, 5),
        "width_pips": round((hi - lo) / 0.01, 1),   # gold pips
        "drift_pts":  round(drift, 2),
        "candles":    asian,
    }
