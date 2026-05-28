"""
strategies.py — All 7 Gold Strategies + Forex LQ Sweep
========================================================
Each strategy is a pure function:
    analyze_<name>(pair, mtf_data, context) -> result_dict

result_dict always contains:
    signal         : "LONG" | "SHORT" | "NO_TRADE"
    strategy_id    : str
    score          : int (confluence layers confirmed out of 10)
    entry          : float | None
    stop_loss      : float | None
    tp1            : float | None
    tp2            : float | None
    risk_pips      : float | None
    daily_bias     : str
    htf_zone       : str
    reason         : str
    invalidation   : str
    liquidity_level: float | None
    sweep_price    : float | None
    fvg_top        : float | None
    fvg_bot        : float | None

The master dispatcher calls all enabled strategies and returns
the highest-score valid signal. On equal scores, gold strategies
take priority over forex.
"""

from datetime import datetime, timezone

from config import (
    STRATEGY_ENABLED, STRATEGY_MIN_SCORE,
    ASIAN_RANGE_MAX_PIPS, ASIAN_RANGE_MAX_DRIFT,
    ASIAN_SWEEP_WINDOW_MIN, ASIAN_SWEEP_MIN_PIPS,
    NY_OPEN_SETUP_WINDOW_MIN, NY_MIN_SWEEP_PIPS, NY_MIN_DISPLACEMENT_PIPS,
    PO3_ACCUM_RANGE_MAX, PO3_ACCUM_DRIFT_MAX,
    PO3_JUDAS_TIMING_MIN, PO3_JUDAS_TIMING_MAX,
    SB_WINDOWS, SB_MAX_SETUPS_PER_WINDOW,
    FVG_TREND_MIN_CANDLES, FVG_MIN_SIZE_PIPS_GOLD,
    PSYCH_LEVEL_PROXIMITY, OB_MAX_AGE_CANDLES_4H, OB_MAX_VISITS,
    WEEKLY_ENTRY_DAYS, WEEKLY_SHORT_DAYS,
    GOLD_MAX_SL_PIPS, FOREX_MAX_SL_PIPS,
    GOLD_MIN_SL_PIPS, FOREX_MIN_SL_PIPS,
    PIP_SIZE,
    GOLD_PAIR,
)
from detector_core import (
    get_daily_bias, get_weekly_bias, get_htf_zone,
    find_equal_highs, find_equal_lows,
    detect_sweep, find_displacement, find_fvg,
    detect_choch, calculate_levels,
    extract_asian_range, nearest_psych_level, is_near_psych_level,
    scan_all_fvgs, find_order_blocks,
)


# ============================================================
# SHARED HELPERS
# ============================================================

def _no_trade(strategy_id: str, reason: str,
              daily_bias: str = "UNKNOWN",
              htf_zone: str  = "UNKNOWN") -> dict:
    return {
        "signal": "NO_TRADE", "strategy_id": strategy_id,
        "score": 0, "reason": reason,
        "daily_bias": daily_bias, "htf_zone": htf_zone,
        "entry": None, "stop_loss": None, "tp1": None, "tp2": None,
        "risk_pips": None, "liquidity_level": None, "sweep_price": None,
        "fvg_top": None, "fvg_bot": None, "invalidation": "",
    }


def _validate_sl(pair: str, risk_pips: float) -> str | None:
    """Returns error string if SL is out of bounds, else None."""
    is_gold = pair == GOLD_PAIR
    max_sl  = GOLD_MAX_SL_PIPS  if is_gold else FOREX_MAX_SL_PIPS
    min_sl  = GOLD_MIN_SL_PIPS  if is_gold else FOREX_MIN_SL_PIPS
    if risk_pips > max_sl:
        return f"SL {risk_pips} pips > max {max_sl}"
    if risk_pips < min_sl:
        return f"SL {risk_pips} pips < min {min_sl}"
    return None


# Backtest time injection — set this to a datetime to override "now"
# Leave as None for live trading (uses real clock).
_SIM_TIME: "datetime | None" = None


def _utc_now_hour_min() -> tuple:
    now = _SIM_TIME if _SIM_TIME is not None else datetime.now(timezone.utc)
    return now.hour, now.minute, now.weekday()   # weekday: 0=Mon


def _in_window(start_h: int, end_h: int) -> bool:
    h, m, _ = _utc_now_hour_min()
    curr     = h * 60 + m
    return start_h * 60 <= curr < end_h * 60


def _build_result(strategy_id: str, signal: str, score: int,
                  pair: str, sweep: dict, fvg: dict, levels: dict,
                  daily_bias: str, htf_zone: str,
                  reason: str, invalidation: str) -> dict:
    return {
        "signal":          signal,
        "strategy_id":     strategy_id,
        "score":           score,
        "daily_bias":      daily_bias,
        "htf_zone":        htf_zone,
        "liquidity_level": sweep.get("liquidity_level"),
        "sweep_price":     sweep.get("sweep_price"),
        "fvg_top":         fvg.get("top"),
        "fvg_bot":         fvg.get("bot"),
        "entry":           levels["entry"],
        "stop_loss":       levels["stop_loss"],
        "tp1":             levels["tp1"],
        "tp2":             levels["tp2"],
        "risk_pips":       levels["risk_pips"],
        "tp1_pips":        levels.get("tp1_pips"),
        "tp2_pips":        levels.get("tp2_pips"),
        "reason":          reason,
        "invalidation":    invalidation,
    }


# ============================================================
# STRATEGY 1 — Asian Range Sweep (Bread & Butter)
# ============================================================

def analyze_s1_asian_range_sweep(pair: str, mtf_data: dict,
                                  context: dict) -> dict:
    """
    Gold Strategy 1: Asian range sweeps at London open.

    Score layers (each = +1):
    1. Daily bias confirmed
    2. Asian range tight (≤ ASIAN_RANGE_MAX_PIPS)
    3. Asian range flat (drift ≤ ASIAN_RANGE_MAX_DRIFT)
    4. Price in premium (short) or discount (long)
    5. Sweep wick-only within 90 min of London open
    6. Sweep depth ≥ ASIAN_SWEEP_MIN_PIPS
    7. Displacement ≥ 10 pips, body ratio ≥ 60%
    8. FVG created by displacement
    9. 5M CHoCH confirmed
    10. Inside London open kill zone (07–09 UTC)
    """
    sid = "S1_ASIAN_RANGE_SWEEP"
    if not STRATEGY_ENABLED.get(sid):
        return _no_trade(sid, "Strategy disabled")

    daily   = mtf_data.get("Daily", [])
    h1      = mtf_data.get("1H", [])
    m15     = mtf_data.get("15M", [])
    m5      = mtf_data.get("5M", [])

    if len(daily) < 5 or len(h1) < 20 or len(m15) < 30:
        return _no_trade(sid, "Insufficient candles")

    score = 0

    # Layer 1 — Daily bias
    daily_bias, bias_reason = get_daily_bias(daily)
    if daily_bias == "NEUTRAL":
        return _no_trade(sid, f"Neutral daily bias: {bias_reason}")
    score += 1

    # Layer 2 & 3 — Asian range quality
    asian = extract_asian_range(h1)
    if not asian:
        return _no_trade(sid, "Could not extract Asian session range", daily_bias)

    if asian["width_pips"] > ASIAN_RANGE_MAX_PIPS:
        return _no_trade(sid, f"Asian range too wide: {asian['width_pips']} pips (max {ASIAN_RANGE_MAX_PIPS})", daily_bias)
    score += 1   # tight range

    if asian["drift_pts"] <= ASIAN_RANGE_MAX_DRIFT:
        score += 1   # flat range
    # (not a hard reject — just reduces score)

    # Layer 4 — Premium / discount zone
    current_price = m15[-1]["close"]
    htf_zone      = get_htf_zone(daily, current_price)

    if daily_bias == "BULLISH" and htf_zone == "PREMIUM":
        return _no_trade(sid, "Bullish bias in PREMIUM zone — no buy", daily_bias, htf_zone)
    if daily_bias == "BEARISH" and htf_zone == "DISCOUNT":
        return _no_trade(sid, "Bearish bias in DISCOUNT zone — no sell", daily_bias, htf_zone)
    if htf_zone != "EQUILIBRIUM":
        score += 1

    # Determine signal direction from bias
    signal = "SHORT" if daily_bias == "BEARISH" else "LONG"

    # Layer 5 & 6 — Sweep of Asian range boundary
    # For SHORT: sweep Asian High (BSL)   For LONG: sweep Asian Low (SSL)
    h, m, _  = _utc_now_hour_min()
    in_window = _in_window(7, 9)

    # Build a pseudo-cluster from the Asian range
    if signal == "SHORT":
        pseudo_cluster = [{"level": asian["high"], "type": "BSL"}]
        sweep = detect_sweep(h1, pair, pseudo_cluster, "BSL", lookback=6)
    else:
        pseudo_cluster = [{"level": asian["low"], "type": "SSL"}]
        sweep = detect_sweep(h1, pair, pseudo_cluster, "SSL", lookback=6)

    if not sweep:
        return _no_trade(sid, f"No {signal} sweep of Asian range found", daily_bias, htf_zone)

    if in_window:
        score += 1   # inside kill zone

    if sweep["sweep_depth_pips"] >= ASIAN_SWEEP_MIN_PIPS:
        score += 1   # deep enough sweep

    # Layer 7 — Displacement on 15M
    sweep_time    = sweep["sweep_candle"]["time"]
    displacement  = find_displacement(m15, sweep_time, signal, pair)
    if not displacement:
        return _no_trade(sid, f"No displacement after sweep (need {10}+ pip body)", daily_bias, htf_zone)
    score += 1

    # Layer 8 — FVG
    fvg = find_fvg(m15, displacement, signal, pair)
    if not fvg:
        return _no_trade(sid, "No FVG after displacement", daily_bias, htf_zone)
    score += 1

    # Layer 9 — CHoCH on 5M (or 15M if no 5M data)
    choch_candles = m5 if len(m5) >= 8 else m15
    sweep_idx     = max(0, len(choch_candles) - 12)
    if detect_choch(choch_candles, signal, sweep_idx):
        score += 1

    # Layer 10 — Kill zone bonus
    if in_window:
        score += 1

    # Validate score
    if score < STRATEGY_MIN_SCORE.get(sid, 7):
        return _no_trade(sid, f"Score {score} below minimum {STRATEGY_MIN_SCORE.get(sid, 7)}", daily_bias, htf_zone)

    # Calculate levels
    levels = calculate_levels(pair, signal, fvg["midpoint"], sweep["sweep_price"])
    sl_err = _validate_sl(pair, levels["risk_pips"])
    if sl_err:
        return _no_trade(sid, sl_err, daily_bias, htf_zone)

    reason = (
        f"S1 Asian Sweep | Daily {daily_bias} | {htf_zone} | "
        f"Asian range {asian['width_pips']} pips (drift {asian['drift_pts']} pts) | "
        f"{sweep['type']} swept {sweep['sweep_depth_pips']} pips | "
        f"Disp {displacement['body_pips']} pips | "
        f"FVG {fvg['bot']}–{fvg['top']} | Score {score}/10"
    )
    invalidation = (
        f"Price closes {'above' if signal == 'SHORT' else 'below'} "
        f"sweep wick {sweep['sweep_price']}"
    )

    return _build_result(sid, signal, score, pair, sweep, fvg, levels,
                         daily_bias, htf_zone, reason, invalidation)


# ============================================================
# STRATEGY 2 — NY Open Killshot
# ============================================================

def analyze_s2_ny_open_killshot(pair: str, mtf_data: dict,
                                 context: dict) -> dict:
    """
    Gold Strategy 2: NY open (12:00–14:00 UTC) sweeps London session
    high/low for explosive reversal trades.
    """
    sid = "S2_NY_OPEN_KILLSHOT"
    if not STRATEGY_ENABLED.get(sid):
        return _no_trade(sid, "Strategy disabled")

    if not _in_window(12, 14):
        return _no_trade(sid, "Not in NY open window (12–14 UTC)")

    daily = mtf_data.get("Daily", [])
    h1    = mtf_data.get("1H", [])
    m15   = mtf_data.get("15M", [])

    if len(daily) < 5 or len(h1) < 20 or len(m15) < 30:
        return _no_trade(sid, "Insufficient candles")

    score = 0

    daily_bias, _ = get_daily_bias(daily)
    if daily_bias == "NEUTRAL":
        return _no_trade(sid, "Neutral daily bias")
    score += 1

    current_price = m15[-1]["close"]
    htf_zone      = get_htf_zone(daily, current_price)

    if daily_bias == "BULLISH" and htf_zone == "PREMIUM":
        return _no_trade(sid, "Bullish in premium", daily_bias, htf_zone)
    if daily_bias == "BEARISH" and htf_zone == "DISCOUNT":
        return _no_trade(sid, "Bearish in discount", daily_bias, htf_zone)
    if htf_zone != "EQUILIBRIUM":
        score += 1

    signal = "SHORT" if daily_bias == "BEARISH" else "LONG"

    # Mark London session high/low from 07–12 UTC candles
    london = [c for c in h1 if _is_london_candle(c)]
    if not london:
        return _no_trade(sid, "No London candles found", daily_bias, htf_zone)

    london_high = max(c["high"] for c in london)
    london_low  = min(c["low"]  for c in london)

    # Build pseudo-clusters for London extremes
    if signal == "SHORT":
        cluster = [{"level": london_high, "type": "BSL"}]
        sweep   = detect_sweep(h1, pair, cluster, "BSL", lookback=4)
    else:
        cluster = [{"level": london_low, "type": "SSL"}]
        sweep   = detect_sweep(h1, pair, cluster, "SSL", lookback=4)

    if not sweep:
        return _no_trade(sid, f"No NY sweep of London {'high' if signal == 'SHORT' else 'low'}", daily_bias, htf_zone)

    if sweep["sweep_depth_pips"] >= NY_MIN_SWEEP_PIPS:
        score += 2   # violent NY sweep = strong signal
    elif sweep["sweep_depth_pips"] >= 5:
        score += 1

    sweep_time   = sweep["sweep_candle"]["time"]
    displacement = find_displacement(m15, sweep_time, signal, pair)
    if not displacement:
        return _no_trade(sid, "No displacement after NY sweep", daily_bias, htf_zone)

    if displacement["body_pips"] >= NY_MIN_DISPLACEMENT_PIPS:
        score += 2
    else:
        score += 1

    fvg = find_fvg(m15, displacement, signal, pair)
    if not fvg:
        return _no_trade(sid, "No FVG after NY displacement", daily_bias, htf_zone)
    score += 1

    if detect_choch(m15, signal, max(0, len(m15) - 12)):
        score += 1

    score += 1   # inside NY kill zone (already confirmed above)

    if score < STRATEGY_MIN_SCORE.get(sid, 7):
        return _no_trade(sid, f"Score {score} too low", daily_bias, htf_zone)

    levels = calculate_levels(pair, signal, fvg["midpoint"], sweep["sweep_price"])
    sl_err = _validate_sl(pair, levels["risk_pips"])
    if sl_err:
        return _no_trade(sid, sl_err, daily_bias, htf_zone)

    reason = (
        f"S2 NY Killshot | {daily_bias} | {htf_zone} | "
        f"London {'high' if signal == 'SHORT' else 'low'} swept {sweep['sweep_depth_pips']} pips | "
        f"Disp {displacement['body_pips']} pips | Score {score}/10"
    )
    return _build_result(sid, signal, score, pair, sweep, fvg, levels,
                         daily_bias, htf_zone, reason,
                         f"Close above sweep wick {sweep['sweep_price']}")


def _is_london_candle(c: dict) -> bool:
    try:
        t = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
        return 7 <= t.hour < 12
    except Exception:
        return False


# ============================================================
# STRATEGY 3 — Order Block at Psychological Levels
# ============================================================

def analyze_s3_ob_psychological(pair: str, mtf_data: dict,
                                  context: dict) -> dict:
    """
    Gold Strategy 3: Order blocks at $50 round number clusters.
    Only fires when price is within PSYCH_LEVEL_PROXIMITY of a level.
    """
    sid = "S3_OB_PSYCHOLOGICAL_LEVELS"
    if not STRATEGY_ENABLED.get(sid):
        return _no_trade(sid, "Strategy disabled")

    if pair != GOLD_PAIR:
        return _no_trade(sid, "S3 gold only")

    daily = mtf_data.get("Daily", [])
    h4    = mtf_data.get("4H", [])
    h1    = mtf_data.get("1H", [])
    m15   = mtf_data.get("15M", [])

    if len(daily) < 5 or len(h4) < 20:
        return _no_trade(sid, "Insufficient candles")

    score = 0

    daily_bias, _ = get_daily_bias(daily)
    if daily_bias == "NEUTRAL":
        return _no_trade(sid, "Neutral daily bias")
    score += 1

    current_price = m15[-1]["close"] if m15 else h1[-1]["close"]
    htf_zone      = get_htf_zone(daily, current_price)

    if daily_bias == "BULLISH" and htf_zone == "PREMIUM":
        return _no_trade(sid, "Bullish in premium", daily_bias, htf_zone)
    if daily_bias == "BEARISH" and htf_zone == "DISCOUNT":
        return _no_trade(sid, "Bearish in discount", daily_bias, htf_zone)

    signal = "SHORT" if daily_bias == "BEARISH" else "LONG"

    # Check near psychological level
    psych_lvl, dist = nearest_psych_level(current_price)
    if dist > PSYCH_LEVEL_PROXIMITY:
        return _no_trade(sid, f"Not near round number (${psych_lvl}, dist ${dist:.2f})", daily_bias, htf_zone)
    score += 2   # near round number = strong confluence

    # Find order blocks on 4H near the psych level
    obs = find_order_blocks(h4, signal, pair, max_age=OB_MAX_AGE_CANDLES_4H)
    if not obs:
        return _no_trade(sid, f"No {signal} OB on 4H near ${psych_lvl}", daily_bias, htf_zone)

    # Find best OB closest to psych level and current price
    best_ob = min(obs, key=lambda o: abs(o["ce"] - psych_lvl))
    ob_dist = abs(best_ob["ce"] - psych_lvl)
    if ob_dist > 5.0:   # CE must be within $5 of round number
        return _no_trade(sid, f"Best OB CE ${best_ob['ce']} too far from ${psych_lvl}", daily_bias, htf_zone)
    score += 1

    # Find sweep of OB + round number cluster
    cluster = [{"level": psych_lvl, "type": "BSL" if signal == "SHORT" else "SSL"}]
    sweep   = detect_sweep(m15, pair, cluster,
                           "BSL" if signal == "SHORT" else "SSL",
                           lookback=5)

    if not sweep:
        # also check h1
        sweep = detect_sweep(h1, pair, cluster,
                             "BSL" if signal == "SHORT" else "SSL",
                             lookback=3)
    if not sweep:
        return _no_trade(sid, f"No sweep of ${psych_lvl}", daily_bias, htf_zone)
    score += 2

    sweep_time   = sweep["sweep_candle"]["time"]
    displacement = find_displacement(m15, sweep_time, signal, pair)
    if not displacement:
        return _no_trade(sid, "No displacement", daily_bias, htf_zone)
    score += 1

    fvg = find_fvg(m15, displacement, signal, pair)
    if not fvg:
        return _no_trade(sid, "No FVG", daily_bias, htf_zone)
    score += 1

    if detect_choch(m15, signal, max(0, len(m15) - 12)):
        score += 1

    if score < STRATEGY_MIN_SCORE.get(sid, 7):
        return _no_trade(sid, f"Score {score} too low", daily_bias, htf_zone)

    levels = calculate_levels(pair, signal, fvg["midpoint"], sweep["sweep_price"])
    sl_err = _validate_sl(pair, levels["risk_pips"])
    if sl_err:
        return _no_trade(sid, sl_err, daily_bias, htf_zone)

    reason = (
        f"S3 OB+Psych | {daily_bias} | ${psych_lvl} round number (${dist:.1f} away) | "
        f"4H OB CE {best_ob['ce']} | Sweep {sweep['sweep_depth_pips']} pips | "
        f"Disp {displacement['body_pips']} pips | Score {score}/10"
    )
    return _build_result(sid, signal, score, pair, sweep, fvg, levels,
                         daily_bias, htf_zone, reason,
                         f"Close above OB high {best_ob['high']}")


# ============================================================
# STRATEGY 4 — Weekly Profile (Monday Judas → Tuesday entry)
# ============================================================

def analyze_s4_weekly_profile(pair: str, mtf_data: dict,
                               context: dict) -> dict:
    """
    Gold Strategy 4: Weekly delivery — Monday sweeps prior week
    extreme, then trade Tuesday/Wednesday in the confirmed direction.
    """
    sid = "S4_WEEKLY_PROFILE"
    if not STRATEGY_ENABLED.get(sid):
        return _no_trade(sid, "Strategy disabled")

    if pair != GOLD_PAIR:
        return _no_trade(sid, "S4 gold only")

    _, _, weekday = _utc_now_hour_min()

    # Only enter on Tuesday/Wednesday
    if weekday not in WEEKLY_ENTRY_DAYS:
        return _no_trade(sid, f"Not an entry day (weekday={weekday}, need Tue/Wed)")

    weekly = mtf_data.get("Weekly", [])
    daily  = mtf_data.get("Daily", [])
    h1     = mtf_data.get("1H", [])
    m15    = mtf_data.get("15M", [])

    if len(weekly) < 3 or len(daily) < 5:
        return _no_trade(sid, "Insufficient weekly/daily candles")

    score = 0

    weekly_bias, w_reason = get_weekly_bias(weekly)
    daily_bias,  d_reason = get_daily_bias(daily)

    if weekly_bias == "NEUTRAL":
        return _no_trade(sid, f"Neutral weekly bias: {w_reason}")
    score += 2   # weekly bias is strong confluence

    if daily_bias != "NEUTRAL" and daily_bias == weekly_bias:
        score += 1   # daily aligns with weekly

    signal = "SHORT" if weekly_bias == "BEARISH" else "LONG"

    # Prior week high/low
    pw          = weekly[-2] if len(weekly) >= 2 else weekly[-1]
    pw_high     = pw["high"]
    pw_low      = pw["low"]

    current_price = m15[-1]["close"] if m15 else h1[-1]["close"]
    htf_zone      = get_htf_zone(daily, current_price)

    if signal == "LONG" and htf_zone == "PREMIUM":
        return _no_trade(sid, "Weekly bullish but in premium", weekly_bias, htf_zone)
    if signal == "SHORT" and htf_zone == "DISCOUNT":
        return _no_trade(sid, "Weekly bearish but in discount", weekly_bias, htf_zone)
    if htf_zone != "EQUILIBRIUM":
        score += 1

    # Find sweep of prior week's extreme that occurred on Monday
    # (look back at daily candles for Monday's candle)
    monday_candles = [c for c in daily if _is_weekday(c, 0)]
    if monday_candles:
        mon = monday_candles[-1]
        if signal == "LONG":
            # Monday should have swept the prior week low
            if mon["low"] < pw_low and mon["close"] > pw_low:
                score += 2   # Monday Judas sweep confirmed
        else:
            if mon["high"] > pw_high and mon["close"] < pw_high:
                score += 2   # Monday Judas sweep confirmed

    # Now look for actual entry setup on 1H/15M
    if signal == "SHORT":
        clusters = find_equal_highs(h1, pair)
        sweep    = detect_sweep(h1, pair, clusters, "BSL") if clusters else None
    else:
        clusters = find_equal_lows(h1, pair)
        sweep    = detect_sweep(h1, pair, clusters, "SSL") if clusters else None

    if not sweep:
        return _no_trade(sid, f"No intraday sweep for weekly {signal}", weekly_bias, htf_zone)
    score += 1

    sweep_time   = sweep["sweep_candle"]["time"]
    displacement = find_displacement(m15, sweep_time, signal, pair)
    if not displacement:
        return _no_trade(sid, "No displacement", weekly_bias, htf_zone)
    score += 1

    fvg = find_fvg(m15, displacement, signal, pair)
    if not fvg:
        return _no_trade(sid, "No FVG", weekly_bias, htf_zone)
    score += 1

    if score < STRATEGY_MIN_SCORE.get(sid, 6):
        return _no_trade(sid, f"Score {score} too low", weekly_bias, htf_zone)

    levels = calculate_levels(pair, signal, fvg["midpoint"], sweep["sweep_price"])
    sl_err = _validate_sl(pair, levels["risk_pips"])
    if sl_err:
        return _no_trade(sid, sl_err, weekly_bias, htf_zone)

    reason = (
        f"S4 Weekly Profile | Weekly {weekly_bias} | {daily_bias} daily | "
        f"Tue/Wed entry | Sweep {sweep['sweep_depth_pips']} pips | "
        f"Disp {displacement['body_pips']} pips | Score {score}/10"
    )
    return _build_result(sid, signal, score, pair, sweep, fvg, levels,
                         weekly_bias, htf_zone, reason,
                         f"Prior week extreme {pw_high if signal == 'SHORT' else pw_low} broken")


def _is_weekday(c: dict, target_weekday: int) -> bool:
    try:
        t = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
        return t.weekday() == target_weekday
    except Exception:
        return False


# ============================================================
# STRATEGY 5 — FVG Retracement (Trend Following)
# ============================================================

def analyze_s5_fvg_retracement(pair: str, mtf_data: dict,
                                 context: dict) -> dict:
    """
    Gold Strategy 5: In a strong 4H trend, wait for price to retrace
    into an unfilled FVG, get the CHoCH, and enter with the trend.
    """
    sid = "S5_FVG_RETRACEMENT"
    if not STRATEGY_ENABLED.get(sid):
        return _no_trade(sid, "Strategy disabled")

    daily = mtf_data.get("Daily", [])
    h4    = mtf_data.get("4H", [])
    m15   = mtf_data.get("15M", [])

    if len(daily) < 5 or len(h4) < 20:
        return _no_trade(sid, "Insufficient candles")

    score = 0

    daily_bias, _ = get_daily_bias(daily)
    if daily_bias == "NEUTRAL":
        return _no_trade(sid, "Neutral daily bias")
    score += 1

    signal = "SHORT" if daily_bias == "BEARISH" else "LONG"

    # Check trend strength on 4H — need 5+ candles in one direction
    recent_4h = h4[-8:]
    trend_candles = 0
    for i in range(1, len(recent_4h)):
        if signal == "LONG" and recent_4h[i]["close"] > recent_4h[i-1]["close"]:
            trend_candles += 1
        elif signal == "SHORT" and recent_4h[i]["close"] < recent_4h[i-1]["close"]:
            trend_candles += 1

    if trend_candles < FVG_TREND_MIN_CANDLES:
        return _no_trade(sid, f"Trend not strong enough ({trend_candles}/{FVG_TREND_MIN_CANDLES} candles)", daily_bias)

    score += 1   # strong trend confirmed

    current_price = m15[-1]["close"] if m15 else h4[-1]["close"]
    htf_zone      = get_htf_zone(daily, current_price)

    if signal == "LONG" and htf_zone == "PREMIUM":
        return _no_trade(sid, "Long trend but in premium", daily_bias, htf_zone)
    if signal == "SHORT" and htf_zone == "DISCOUNT":
        return _no_trade(sid, "Short trend but in discount", daily_bias, htf_zone)
    if htf_zone != "EQUILIBRIUM":
        score += 1

    # Find unfilled FVGs on 4H
    fvgs_4h = scan_all_fvgs(h4, signal, pair, min_body_pips=15.0)
    if not fvgs_4h:
        return _no_trade(sid, "No 4H FVGs found", daily_bias, htf_zone)

    # Find FVG that price is currently retracing into
    target_fvg = None
    for fvg in fvgs_4h:
        if signal == "LONG":
            if fvg["bot"] <= current_price <= fvg["top"]:
                target_fvg = fvg
                break
        else:
            if fvg["bot"] <= current_price <= fvg["top"]:
                target_fvg = fvg
                break

    if not target_fvg:
        return _no_trade(sid, "Price not inside a 4H FVG", daily_bias, htf_zone)
    score += 2   # inside FVG retracement zone

    # Look for sweep of small SSL/BSL inside the retracement
    if signal == "LONG":
        clusters = find_equal_lows(m15, pair)
        sweep    = detect_sweep(m15, pair, clusters, "SSL") if clusters else None
    else:
        clusters = find_equal_highs(m15, pair)
        sweep    = detect_sweep(m15, pair, clusters, "BSL") if clusters else None

    if sweep:
        score += 1   # internal sweep inside FVG = extra confluence

    # Find 15M FVG within the 4H FVG for precision entry
    if m15:
        all_15m_fvgs = scan_all_fvgs(m15, signal, pair, min_body_pips=8.0)
        fvg_15m = next(
            (f for f in all_15m_fvgs
             if target_fvg["bot"] <= f["midpoint"] <= target_fvg["top"]),
            None
        )
    else:
        fvg_15m = None

    if fvg_15m:
        score += 1
        entry_fvg = fvg_15m
    else:
        entry_fvg = target_fvg

    if detect_choch(m15 or h4, signal, max(0, len(m15 or h4) - 10)):
        score += 1

    if score < STRATEGY_MIN_SCORE.get(sid, 6):
        return _no_trade(sid, f"Score {score} too low", daily_bias, htf_zone)

    sweep_price = sweep["sweep_price"] if sweep else (
        entry_fvg["bot"] - 5 * PIP_SIZE.get(pair, 0.01) if signal == "LONG"
        else entry_fvg["top"] + 5 * PIP_SIZE.get(pair, 0.01)
    )

    levels = calculate_levels(pair, signal, entry_fvg["midpoint"], sweep_price)
    sl_err = _validate_sl(pair, levels["risk_pips"])
    if sl_err:
        return _no_trade(sid, sl_err, daily_bias, htf_zone)

    pseudo_sweep = {"liquidity_level": sweep_price, "sweep_price": sweep_price}

    reason = (
        f"S5 FVG Retrace | {daily_bias} | 4H FVG {target_fvg['bot']}–{target_fvg['top']} | "
        f"{'15M FVG inside' if fvg_15m else '4H FVG CE'} {entry_fvg['midpoint']} | "
        f"Score {score}/10"
    )
    return _build_result(sid, signal, score, pair, pseudo_sweep, entry_fvg, levels,
                         daily_bias, htf_zone, reason,
                         f"Price closes outside 4H FVG {target_fvg['bot']}")


# ============================================================
# STRATEGY 6 — Power of 3 (Accumulation→Manipulation→Distribution)
# ============================================================

def analyze_s6_power_of_3(pair: str, mtf_data: dict,
                            context: dict) -> dict:
    """
    Gold Strategy 6: PO3 daily model.
    Asia = accumulation, London = Judas swing, NY = distribution.
    Entry is taken when the Judas completes and distribution begins.
    """
    sid = "S6_POWER_OF_3"
    if not STRATEGY_ENABLED.get(sid):
        return _no_trade(sid, "Strategy disabled")

    if pair != GOLD_PAIR:
        return _no_trade(sid, "S6 gold only")

    daily = mtf_data.get("Daily", [])
    h1    = mtf_data.get("1H", [])
    m15   = mtf_data.get("15M", [])
    m5    = mtf_data.get("5M", [])

    if len(daily) < 5 or len(h1) < 20 or len(m15) < 30:
        return _no_trade(sid, "Insufficient candles")

    score = 0
    h, _, _ = _utc_now_hour_min()

    # Must be in distribution window (09:00–16:00 UTC)
    if not (9 <= h < 16):
        return _no_trade(sid, f"Outside PO3 distribution window (current hour {h} UTC)")

    # Layer 1 — Daily bias
    daily_bias, _ = get_daily_bias(daily)
    if daily_bias == "NEUTRAL":
        return _no_trade(sid, "Neutral daily bias")
    score += 1

    current_price = m15[-1]["close"]
    htf_zone      = get_htf_zone(daily, current_price)

    if daily_bias == "BULLISH" and htf_zone == "PREMIUM":
        return _no_trade(sid, "Bullish in premium", daily_bias, htf_zone)
    if daily_bias == "BEARISH" and htf_zone == "DISCOUNT":
        return _no_trade(sid, "Bearish in discount", daily_bias, htf_zone)
    if htf_zone != "EQUILIBRIUM":
        score += 1

    signal = "SHORT" if daily_bias == "BEARISH" else "LONG"

    # Layer 2 — Asian accumulation phase quality
    asian = extract_asian_range(h1)
    if not asian:
        return _no_trade(sid, "Cannot extract Asian range", daily_bias, htf_zone)

    if asian["width_pips"] > PO3_ACCUM_RANGE_MAX:
        return _no_trade(sid, f"Accumulation range too wide {asian['width_pips']} pips", daily_bias, htf_zone)
    score += 1   # valid accumulation range

    if asian["drift_pts"] <= PO3_ACCUM_DRIFT_MAX:
        score += 1   # flat accumulation = denser stop clusters

    # Layer 3 — Judas swing (Phase 2: 07–09 UTC sweep)
    # The Judas sweeps the OPPOSITE side to the distribution direction
    if signal == "LONG":
        # Judas goes DOWN (sweeps Asian Low SSL), then distributes UP
        judas_cluster = [{"level": asian["low"], "type": "SSL"}]
        judas_sweep   = detect_sweep(h1, pair, judas_cluster, "SSL", lookback=8)
    else:
        # Judas goes UP (sweeps Asian High BSL), then distributes DOWN
        judas_cluster = [{"level": asian["high"], "type": "BSL"}]
        judas_sweep   = detect_sweep(h1, pair, judas_cluster, "BSL", lookback=8)

    if not judas_sweep:
        return _no_trade(sid, f"Judas swing not detected — no {signal} sweep of Asian range", daily_bias, htf_zone)
    score += 2   # Judas swing is the core PO3 trigger

    # Verify Judas timing (should be 07–09 UTC)
    try:
        judas_time = datetime.fromisoformat(
            judas_sweep["sweep_candle"]["time"].replace("Z", "+00:00"))
        if not (PO3_JUDAS_TIMING_MIN <= judas_time.hour < PO3_JUDAS_TIMING_MAX):
            score -= 1   # Judas outside ideal window — penalize
    except Exception:
        pass

    # Layer 4 — Distribution displacement on 15M
    sweep_time   = judas_sweep["sweep_candle"]["time"]
    displacement = find_displacement(m15, sweep_time, signal, pair)
    if not displacement:
        return _no_trade(sid, "No distribution displacement candle", daily_bias, htf_zone)
    score += 1

    # Layer 5 — FVG
    fvg = find_fvg(m15, displacement, signal, pair)
    if not fvg:
        return _no_trade(sid, "No FVG after distribution candle", daily_bias, htf_zone)
    score += 1

    # Layer 6 — CHoCH on 5M confirming structure shift
    choch_candles = m5 if len(m5) >= 8 else m15
    if detect_choch(choch_candles, signal, max(0, len(choch_candles) - 10)):
        score += 1

    if score < STRATEGY_MIN_SCORE.get(sid, 7):
        return _no_trade(sid, f"Score {score} below minimum", daily_bias, htf_zone)

    levels = calculate_levels(pair, signal, fvg["midpoint"], judas_sweep["sweep_price"])
    sl_err = _validate_sl(pair, levels["risk_pips"])
    if sl_err:
        return _no_trade(sid, sl_err, daily_bias, htf_zone)

    reason = (
        f"S6 PO3 | Accumulation {asian['width_pips']} pips (drift {asian['drift_pts']}) | "
        f"Judas {judas_sweep['type']} {judas_sweep['sweep_depth_pips']} pips | "
        f"Distribution disp {displacement['body_pips']} pips | "
        f"FVG {fvg['bot']}–{fvg['top']} | Score {score}/10"
    )
    return _build_result(sid, signal, score, pair, judas_sweep, fvg, levels,
                         daily_bias, htf_zone, reason,
                         f"Price closes beyond Judas wick {judas_sweep['sweep_price']}")


# ============================================================
# STRATEGY 7 — Silver Bullet (Time-window entry)
# ============================================================

def analyze_s7_silver_bullet(pair: str, mtf_data: dict,
                               context: dict) -> dict:
    """
    Gold Strategy 7: Silver Bullet — specific 1-hour windows.
    Window 1: 03–04 UTC (Asian SB)
    Window 2: 10–11 UTC (London SB) — BEST
    Window 3: 14–15 UTC (NY SB)

    One trade per window, hard cutoff at window close.
    """
    sid = "S7_SILVER_BULLET"
    if not STRATEGY_ENABLED.get(sid):
        return _no_trade(sid, "Strategy disabled")

    if pair != GOLD_PAIR:
        return _no_trade(sid, "S7 gold only")

    # Find active window
    h, m, _ = _utc_now_hour_min()
    active_window = None
    for w in SB_WINDOWS:
        if w["start_h"] <= h < w["end_h"]:
            active_window = w
            break

    if not active_window:
        return _no_trade(sid, f"Not in any Silver Bullet window (hour={h} UTC)")

    # Check session trade count from context
    sb_trades_this_window = context.get("sb_trades_this_window", {})
    wname = active_window["name"]
    if sb_trades_this_window.get(wname, 0) >= SB_MAX_SETUPS_PER_WINDOW:
        return _no_trade(sid, f"Already traded {wname} this window (1-trade rule)")

    daily = mtf_data.get("Daily", [])
    h1    = mtf_data.get("1H", [])
    m15   = mtf_data.get("15M", [])
    m5    = mtf_data.get("5M", [])

    if len(daily) < 5 or len(m15) < 30:
        return _no_trade(sid, "Insufficient candles")

    score = 0

    daily_bias, _ = get_daily_bias(daily)
    if daily_bias == "NEUTRAL":
        return _no_trade(sid, "Neutral daily bias")
    score += 1

    current_price = m15[-1]["close"]
    htf_zone      = get_htf_zone(daily, current_price)

    if daily_bias == "BULLISH" and htf_zone == "PREMIUM":
        return _no_trade(sid, "Bullish in premium", daily_bias, htf_zone)
    if daily_bias == "BEARISH" and htf_zone == "DISCOUNT":
        return _no_trade(sid, "Bearish in discount", daily_bias, htf_zone)
    if htf_zone != "EQUILIBRIUM":
        score += 1

    signal = "SHORT" if daily_bias == "BEARISH" else "LONG"

    score += 2   # inside Silver Bullet window = strong timing confluence

    # Find equal highs / lows within this window
    if signal == "SHORT":
        clusters = find_equal_highs(m15, pair, max_age=8)   # fresh only
        sweep    = detect_sweep(m15, pair, clusters, "BSL") if clusters else None
        if not sweep:
            clusters = find_equal_highs(h1, pair, max_age=4)
            sweep    = detect_sweep(h1, pair, clusters, "BSL") if clusters else None
    else:
        clusters = find_equal_lows(m15, pair, max_age=8)
        sweep    = detect_sweep(m15, pair, clusters, "SSL") if clusters else None
        if not sweep:
            clusters = find_equal_lows(h1, pair, max_age=4)
            sweep    = detect_sweep(h1, pair, clusters, "SSL") if clusters else None

    if not sweep:
        return _no_trade(sid, f"No sweep in {wname}", daily_bias, htf_zone)
    score += 2

    sweep_time   = sweep["sweep_candle"]["time"]
    displacement = find_displacement(m15, sweep_time, signal, pair)
    if not displacement:
        return _no_trade(sid, "No displacement in SB window", daily_bias, htf_zone)
    score += 1

    fvg = find_fvg(m15, displacement, signal, pair)
    if not fvg:
        return _no_trade(sid, "No FVG in SB window", daily_bias, htf_zone)
    score += 1

    choch_candles = m5 if len(m5) >= 8 else m15
    if detect_choch(choch_candles, signal, max(0, len(choch_candles) - 8)):
        score += 1

    if wname == "London SB":   # best window bonus
        score += 1

    if score < STRATEGY_MIN_SCORE.get(sid, 7):
        return _no_trade(sid, f"Score {score} too low for SB", daily_bias, htf_zone)

    levels = calculate_levels(pair, signal, fvg["midpoint"], sweep["sweep_price"])
    sl_err = _validate_sl(pair, levels["risk_pips"])
    if sl_err:
        return _no_trade(sid, sl_err, daily_bias, htf_zone)

    reason = (
        f"S7 Silver Bullet {wname} | {daily_bias} | {htf_zone} | "
        f"Sweep {sweep['sweep_depth_pips']} pips | "
        f"Disp {displacement['body_pips']} pips | FVG {fvg['bot']}–{fvg['top']} | "
        f"Score {score}/10"
    )
    return _build_result(sid, signal, score, pair, sweep, fvg, levels,
                         daily_bias, htf_zone, reason,
                         f"Window closes at {active_window['end_h']}:00 UTC or SL at {levels['stop_loss']}")


# ============================================================
# ORIGINAL FOREX LQ SWEEP (from existing bot)
# ============================================================

def analyze_forex_lq_sweep(pair: str, mtf_data: dict,
                             context: dict) -> dict:
    """
    Original strategy from the existing bot — works on all 6 forex pairs.
    Daily bias → 1H equal highs/lows → sweep → displacement → FVG → CHoCH.
    """
    sid = "S_FOREX_LQ_SWEEP"
    if not STRATEGY_ENABLED.get(sid):
        return _no_trade(sid, "Strategy disabled")

    if pair == GOLD_PAIR:
        return _no_trade(sid, "Forex strategy — skip gold")

    daily = mtf_data.get("Daily", [])
    h1    = mtf_data.get("1H", [])
    m15   = mtf_data.get("15M", [])

    if len(daily) < 5 or len(h1) < 20 or len(m15) < 30:
        return _no_trade(sid, "Insufficient candles")

    score = 0

    daily_bias, _ = get_daily_bias(daily)
    if daily_bias == "NEUTRAL":
        return _no_trade(sid, "Neutral daily bias")
    score += 1

    current_price = m15[-1]["close"]
    htf_zone      = get_htf_zone(daily, current_price)

    if daily_bias == "BULLISH" and htf_zone == "PREMIUM":
        return _no_trade(sid, "Bullish in premium", daily_bias, htf_zone)
    if daily_bias == "BEARISH" and htf_zone == "DISCOUNT":
        return _no_trade(sid, "Bearish in discount", daily_bias, htf_zone)
    if htf_zone != "EQUILIBRIUM":
        score += 1

    signal = "SHORT" if daily_bias == "BEARISH" else "LONG"

    if signal == "SHORT":
        clusters = find_equal_highs(h1, pair)
        sweep    = detect_sweep(h1, pair, clusters, "BSL") if clusters else None
    else:
        clusters = find_equal_lows(h1, pair)
        sweep    = detect_sweep(h1, pair, clusters, "SSL") if clusters else None

    if not sweep:
        return _no_trade(sid, f"No {signal} sweep on 1H", daily_bias, htf_zone)
    score += 2

    sweep_time   = sweep["sweep_candle"]["time"]
    displacement = find_displacement(m15, sweep_time, signal, pair)
    if not displacement:
        return _no_trade(sid, "No displacement", daily_bias, htf_zone)
    score += 1

    fvg = find_fvg(m15, displacement, signal, pair)
    if not fvg:
        return _no_trade(sid, "No FVG", daily_bias, htf_zone)
    score += 1

    m15_idx = max(0, len(m15) - 12)
    if detect_choch(m15, signal, m15_idx):
        score += 1

    # Kill zone bonus
    if _in_window(7, 10) or _in_window(12, 15):
        score += 1

    if score < STRATEGY_MIN_SCORE.get(sid, 6):
        return _no_trade(sid, f"Score {score} too low", daily_bias, htf_zone)

    levels = calculate_levels(pair, signal, fvg["midpoint"], sweep["sweep_price"])
    sl_err = _validate_sl(pair, levels["risk_pips"])
    if sl_err:
        return _no_trade(sid, sl_err, daily_bias, htf_zone)

    reason = (
        f"Forex LQ Sweep | {daily_bias} | {htf_zone} | "
        f"{sweep['type']} swept {sweep['sweep_depth_pips']} pips | "
        f"Disp {displacement['body_pips']} pips | Score {score}/10"
    )
    return _build_result(sid, signal, score, pair, sweep, fvg, levels,
                         daily_bias, htf_zone, reason,
                         f"Close beyond sweep wick {sweep['sweep_price']}")


# ============================================================
# MASTER DISPATCHER
# ============================================================

def analyze_all_strategies(pair: str, mtf_data: dict,
                             context: dict) -> dict:
    """
    Runs every enabled strategy for the given instrument.
    Returns the single highest-score valid signal.
    If multiple strategies agree on direction, the scores are combined
    (capped at 10) — confluence across strategies is extremely high probability.

    Returns the best result dict, or NO_TRADE if nothing qualifies.
    """
    is_gold     = (pair == GOLD_PAIR)
    all_results = []

    if is_gold:
        runners = [
            analyze_s1_asian_range_sweep,
            analyze_s2_ny_open_killshot,
            analyze_s3_ob_psychological,
            analyze_s4_weekly_profile,
            analyze_s5_fvg_retracement,
            analyze_s6_power_of_3,
            analyze_s7_silver_bullet,
        ]
    else:
        runners = [
            analyze_forex_lq_sweep,
        ]

    for fn in runners:
        try:
            res = fn(pair, mtf_data, context)
            all_results.append(res)
        except Exception as e:
            all_results.append(_no_trade(fn.__name__, f"Error: {str(e)[:80]}"))

    # Filter valid signals
    valid = [r for r in all_results if r["signal"] != "NO_TRADE"]

    if not valid:
        reasons = [r["reason"][:40] for r in all_results[:3]]
        return _no_trade("MASTER", f"No valid signals. Top reasons: {'; '.join(reasons)}")

    # Group by direction — if multiple strategies agree, boost score
    long_results  = [r for r in valid if r["signal"] == "LONG"]
    short_results = [r for r in valid if r["signal"] == "SHORT"]

    best_long  = max(long_results,  key=lambda x: x["score"]) if long_results  else None
    best_short = max(short_results, key=lambda x: x["score"]) if short_results else None

    # Add +1 per additional confirming strategy (agreement bonus)
    if best_long and len(long_results) > 1:
        best_long["score"]  = min(10, best_long["score"]  + len(long_results)  - 1)
        best_long["reason"] += f" [+{len(long_results)-1} strategies agree]"

    if best_short and len(short_results) > 1:
        best_short["score"] = min(10, best_short["score"] + len(short_results) - 1)
        best_short["reason"] += f" [+{len(short_results)-1} strategies agree]"

    # Pick the higher-scoring direction
    candidates = [r for r in [best_long, best_short] if r is not None]
    best       = max(candidates, key=lambda x: x["score"])

    # Final sanity: entry must be within 50 pips of current price
    pip = PIP_SIZE.get(pair, 0.0001)
    if m15 := mtf_data.get("15M"):
        current = m15[-1]["close"]
        if best["entry"] and abs(best["entry"] - current) > 50 * pip:
            return _no_trade("MASTER",
                             f"Entry {best['entry']} too far from current {current:.5f}")

    return best
