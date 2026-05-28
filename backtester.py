"""
backtester.py — Historical backtesting engine
===============================================
Replays historical OANDA candle data through all 7 strategies.

Features:
- Multi-year backtest
- Per-strategy breakdown
- Equity curve
- Drawdown analysis
- Sharpe ratio
- Monte Carlo simulation
- CSV export
"""

import csv
import json
import logging
import math
import os
import random
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from config import (
    OANDA_API_KEY, OANDA_BASE_URL, CANDLE_CONFIG,
    ACCOUNT_BALANCE_DEFAULT, RISK_PERCENT,
    PIP_SIZE, GOLD_PAIR, TP1_RR, TP2_RR, TP1_CLOSE_PCT,
    GOLD_MAX_SL_PIPS, FOREX_MAX_SL_PIPS,
    GOLD_MIN_SL_PIPS, FOREX_MIN_SL_PIPS,
    LOG_DIR,
)
import strategies as _strategies_module
from strategies import analyze_all_strategies

logger = logging.getLogger(__name__)

_HEADERS = {"Authorization": f"Bearer {OANDA_API_KEY}"}


def fetch_historical_candles(
    instrument: str,
    granularity: str,
    from_dt: datetime,
    to_dt: datetime,
    count_per_request: int = 500,
) -> list:
    """
    Fetches full historical candles for a date range in batches of 500.
    OANDA does not allow 'count' + 'from' + 'to' together — we page
    using 'from' + 'count' only, advancing the cursor each batch.
    Returns consolidated list sorted by time.
    """
    url     = f"{OANDA_BASE_URL}/v3/instruments/{instrument}/candles"
    candles = []
    cursor  = from_dt

    to_ts = to_dt.timestamp()

    while cursor.timestamp() < to_ts:
        params = {
            "granularity": granularity,
            "from":        cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count":       count_per_request,
            "price":       "M",
        }

        fetched = 0
        for attempt in range(3):
            try:
                resp = requests.get(url, headers=_HEADERS, params=params, timeout=20)
                if resp.status_code == 200:
                    raw = resp.json().get("candles", [])
                    if not raw:
                        return candles   # no more data
                    for c in raw:
                        if not c.get("complete"):
                            continue
                        mid = c.get("mid", {})
                        c_time = c["time"]
                        # Parse time to check against to_dt
                        c_dt = datetime.strptime(c_time[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)

                        if c_dt.timestamp() > to_ts:
                            return candles

                        candles.append({
                            "time":     c_time,
                            "open":     float(mid.get("o", 0)),
                            "high":     float(mid.get("h", 0)),
                            "low":      float(mid.get("l", 0)),
                            "close":    float(mid.get("c", 0)),
                            "volume":   int(c.get("volume", 0)),
                            "complete": True,
                        })
                        fetched += 1

                    # Advance cursor to just after last candle
                    last_time = raw[-1]["time"][:19]
                    cursor = datetime.strptime(last_time, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)

                    # Small delay to respect rate limits
                    time.sleep(0.25)
                    break

                elif resp.status_code == 429:
                    logger.warning("Rate limited — waiting 5s")
                    time.sleep(5)
                else:
                    logger.error("Historical fetch %d: %s", resp.status_code, resp.text[:120])
                    return candles

            except Exception as exc:
                logger.warning("Fetch attempt %d failed: %s", attempt + 1, exc)
                time.sleep(2)

        if fetched == 0:
            break   # no progress — stop to avoid infinite loop

    logger.info("Fetched %d total %s candles for %s", len(candles), granularity, instrument)
    return candles


def _simulate_trade(
    signal: dict,
    pair: str,
    future_candles: list,
    account_balance: float,
) -> dict:
    """
    Simulates trade outcome against future price action.
    Returns a result dict with status and PnL.
    """
    entry      = float(signal.get("entry", 0))
    sl         = float(signal.get("stop_loss", 0))
    tp1        = float(signal.get("tp1", 0))
    tp2        = float(signal.get("tp2", 0))
    risk_pips  = float(signal.get("risk_pips", 20))
    direction  = signal.get("signal", "LONG")

    if not entry or not sl or risk_pips <= 0:
        return {"status": "INVALID", "pnl": 0, "bars_held": 0}

    pip            = PIP_SIZE.get(pair, 0.0001)
    risk_dollars   = account_balance * (RISK_PERCENT / 100)
    pip_value      = 0.01 if pair == GOLD_PAIR else pip
    units          = risk_dollars / (risk_pips * pip_value)

    tp1_hit  = False
    for i, candle in enumerate(future_candles[:50]):   # max 50 bars forward
        h = candle["high"]
        l = candle["low"]

        if direction == "LONG":
            if l <= sl:
                loss = -risk_dollars
                return {"status": "SL", "pnl": round(loss, 2), "bars_held": i + 1,
                        "tp1_hit": tp1_hit}
            if not tp1_hit and h >= tp1:
                tp1_pnl  = units * (tp1 - entry) * pip_value * TP1_CLOSE_PCT
                units   *= (1 - TP1_CLOSE_PCT)
                tp1_hit  = True
                # Move SL to breakeven
                sl = entry
            if tp1_hit and h >= tp2:
                tp2_pnl = units * (tp2 - entry) * pip_value
                return {
                    "status": "TP2",
                    "pnl":    round(tp1_pnl + tp2_pnl, 2),
                    "bars_held": i + 1,
                    "tp1_hit": True,
                    "tp2_hit": True,
                }
        else:   # SHORT
            if h >= sl:
                loss = -risk_dollars
                return {"status": "SL", "pnl": round(loss, 2), "bars_held": i + 1,
                        "tp1_hit": tp1_hit}
            if not tp1_hit and l <= tp1:
                tp1_pnl  = units * (entry - tp1) * pip_value * TP1_CLOSE_PCT
                units   *= (1 - TP1_CLOSE_PCT)
                tp1_hit  = True
                sl = entry
            if tp1_hit and l <= tp2:
                tp2_pnl = units * (entry - tp2) * pip_value
                return {
                    "status": "TP2",
                    "pnl":    round(tp1_pnl + tp2_pnl, 2),
                    "bars_held": i + 1,
                    "tp1_hit": True,
                    "tp2_hit": True,
                }

    # Expired — close at market (last candle close)
    if future_candles:
        last_close = future_candles[min(49, len(future_candles) - 1)]["close"]
        pips_gained = (last_close - entry) / pip if direction == "LONG" else (entry - last_close) / pip
        pnl = pips_gained * pip_value * units
        return {"status": "EXPIRED", "pnl": round(pnl, 2), "bars_held": 50, "tp1_hit": tp1_hit}

    return {"status": "NO_DATA", "pnl": 0, "bars_held": 0, "tp1_hit": tp1_hit}


def run_backtest(
    instrument: str,
    from_year: int = 2022,
    to_year: int = 2024,
    context: dict = None,
) -> dict:
    """
    Full backtest for one instrument over a multi-year range.
    Uses 15M candles as the primary data series.

    Returns a comprehensive results dict.
    """
    context = context or {}
    logger.info("Starting backtest: %s %d–%d", instrument, from_year, to_year)

    from_dt = datetime(from_year, 1, 1, tzinfo=timezone.utc)
    to_dt   = datetime(to_year, 12, 31, tzinfo=timezone.utc)

    # Fetch 1H candles for structure analysis (proxy for all TFs in backtest)
    candles_1h = fetch_historical_candles(instrument, "H1", from_dt, to_dt)
    if len(candles_1h) < 100:
        logger.error("Insufficient historical data for %s", instrument)
        return {"error": "Insufficient data", "instrument": instrument}

    logger.info("Fetched %d H1 candles for %s", len(candles_1h), instrument)

    trades       = []
    balance      = ACCOUNT_BALANCE_DEFAULT
    equity_curve = [balance]

    # Walk through the data, building MTF views at each point
    window = 100   # candles to use for analysis
    step   = 4     # advance by 4 hours per iteration

    for i in range(window, len(candles_1h) - 50, step):
        segment  = candles_1h[max(0, i - window) : i]
        future   = candles_1h[i : i + 50]

        # Build minimal MTF data from 1H candles
        mtf_data = {
            "Weekly": segment[-10:],
            "Daily":  segment[-20:],
            "4H":     segment[-50:],
            "1H":     segment[-100:],
            "15M":    segment[-150:],
            "5M":     segment[-100:],
        }

        # Inject the candle's timestamp so kill-zone checks use historical time
        try:
            candle_time_str = candles_1h[i]["time"][:19]
            _strategies_module._SIM_TIME = datetime.strptime(
                candle_time_str, "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except Exception:
            _strategies_module._SIM_TIME = None

        try:
            signal = analyze_all_strategies(instrument, mtf_data, context)
        except Exception as exc:
            continue
        finally:
            _strategies_module._SIM_TIME = None   # always reset after each call

        if signal.get("signal") == "NO_TRADE":
            continue

        # Validate SL size
        risk_pips = signal.get("risk_pips", 0)
        is_gold   = instrument == GOLD_PAIR
        if risk_pips > (GOLD_MAX_SL_PIPS if is_gold else FOREX_MAX_SL_PIPS):
            continue
        if risk_pips < (GOLD_MIN_SL_PIPS if is_gold else FOREX_MIN_SL_PIPS):
            continue

        result = _simulate_trade(signal, instrument, future, balance)

        balance += result["pnl"]
        equity_curve.append(round(balance, 2))

        trades.append({
            "time":        candles_1h[i]["time"],
            "strategy_id": signal.get("strategy_id"),
            "direction":   signal.get("signal"),
            "score":       signal.get("score"),
            "status":      result["status"],
            "pnl":         result["pnl"],
            "bars_held":   result.get("bars_held"),
            "tp1_hit":     result.get("tp1_hit", False),
            "tp2_hit":     result.get("tp2_hit", False),
            "balance":     round(balance, 2),
        })

    if not trades:
        return {"error": "No trades generated", "instrument": instrument}

    # Compute final stats
    pnls    = [t["pnl"] for t in trades]
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p < 0]
    total   = len(trades)

    max_dd, peak = _max_drawdown_from_equity(equity_curve)
    sharpe       = _sharpe_ratio(pnls)

    results = {
        "instrument":    instrument,
        "from":          str(from_dt.date()),
        "to":            str(to_dt.date()),
        "total_trades":  total,
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins) / total * 100, 1),
        "net_pnl":       round(sum(pnls), 2),
        "profit_factor": round(abs(sum(wins) / sum(losses)), 2) if losses and sum(losses) != 0 else 0,
        "expectancy":    round(sum(pnls) / total, 2),
        "sharpe_ratio":  round(sharpe, 3),
        "max_drawdown":  round(max_dd, 2),
        "final_balance": round(balance, 2),
        "return_pct":    round((balance - ACCOUNT_BALANCE_DEFAULT) / ACCOUNT_BALANCE_DEFAULT * 100, 2),
        "equity_curve":  equity_curve,
        "trades":        trades,
        "monte_carlo":   run_monte_carlo(pnls),
    }

    logger.info(
        "Backtest complete %s: %d trades, win=%.1f%%, PnL=$%.2f, Sharpe=%.2f, MaxDD=$%.2f",
        instrument, total, results["win_rate"], results["net_pnl"],
        results["sharpe_ratio"], results["max_drawdown"]
    )

    # Export to CSV
    export_path = os.path.join(LOG_DIR, f"backtest_{instrument}_{from_year}_{to_year}.csv")
    os.makedirs(LOG_DIR, exist_ok=True)
    _export_trades_csv(trades, export_path)
    results["csv_path"] = export_path

    return results


def run_monte_carlo(pnls: list, n_simulations: int = 1000, n_trades: int = None) -> dict:
    """
    Monte Carlo simulation: shuffle PnL order N times to estimate
    the distribution of possible equity curves.
    Returns P10/P50/P90 final equity and worst-case drawdown.
    """
    if not pnls:
        return {}

    n_trades    = n_trades or len(pnls)
    final_equities = []
    worst_dds      = []

    base = ACCOUNT_BALANCE_DEFAULT
    for _ in range(n_simulations):
        shuffled = random.sample(pnls, min(n_trades, len(pnls)))
        equity   = base
        peak     = base
        max_dd   = 0.0
        for pnl in shuffled:
            equity += pnl
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
        final_equities.append(round(equity, 2))
        worst_dds.append(round(max_dd, 2))

    final_equities.sort()
    worst_dds.sort(reverse=True)

    p = lambda lst, pct: lst[int(len(lst) * pct / 100)]

    return {
        "simulations":       n_simulations,
        "p10_final_equity":  p(final_equities, 10),
        "p50_final_equity":  p(final_equities, 50),
        "p90_final_equity":  p(final_equities, 90),
        "p10_max_drawdown":  p(worst_dds, 10),
        "p50_max_drawdown":  p(worst_dds, 50),
        "p90_max_drawdown":  p(worst_dds, 90),
    }


def _sharpe_ratio(pnls: list) -> float:
    if len(pnls) < 2:
        return 0.0
    mean = sum(pnls) / len(pnls)
    var  = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
    std  = math.sqrt(var) if var > 0 else 0
    return (mean / std * math.sqrt(252)) if std > 0 else 0.0


def _max_drawdown_from_equity(equity_curve: list) -> tuple[float, float]:
    peak   = equity_curve[0] if equity_curve else 0
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    return max_dd, peak


def _export_trades_csv(trades: list, path: str):
    if not trades:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=trades[0].keys())
        writer.writeheader()
        writer.writerows(trades)
    logger.info("Backtest CSV exported: %s", path)
