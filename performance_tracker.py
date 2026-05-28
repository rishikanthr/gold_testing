"""
performance_tracker.py — Real-time performance analytics
=========================================================
Computes win rate, expectancy, Sharpe ratio, drawdown,
strategy breakdown, and session performance from trade history.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from logger import get_all_trades, get_strategy_stats

logger = logging.getLogger(__name__)


def compute_metrics(trades: list = None) -> dict:
    """
    Computes comprehensive performance metrics from all closed trades.
    Returns a flat dict suitable for display.
    """
    if trades is None:
        trades = get_all_trades()

    closed = [t for t in trades if t.get("status") not in ("OPEN", None)]
    if not closed:
        return _empty_metrics()

    pnls   = [float(t.get("pnl") or 0) for t in closed]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total        = len(closed)
    win_count    = len(wins)
    loss_count   = len(losses)
    win_rate     = win_count / total * 100 if total else 0
    net_pnl      = sum(pnls)
    avg_win      = sum(wins)  / len(wins)  if wins   else 0
    avg_loss     = sum(losses)/ len(losses) if losses else 0
    expectancy   = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")

    # Sharpe ratio (annualised, assuming daily returns)
    if len(pnls) >= 2:
        mean_r = sum(pnls) / len(pnls)
        std_r  = math.sqrt(sum((p - mean_r) ** 2 for p in pnls) / (len(pnls) - 1))
        sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown
    max_dd, peak = _max_drawdown(pnls)

    # TP/SL stats
    tp2_count = sum(1 for t in closed if t.get("tp2_hit"))
    tp1_count = sum(1 for t in closed if t.get("tp1_hit"))
    sl_count  = sum(1 for t in closed if t.get("sl_hit"))

    return {
        "total_trades":   total,
        "win_count":      win_count,
        "loss_count":     loss_count,
        "win_rate":       round(win_rate, 1),
        "net_pnl":        round(net_pnl, 2),
        "avg_win":        round(avg_win, 2),
        "avg_loss":       round(avg_loss, 2),
        "expectancy":     round(expectancy, 2),
        "profit_factor":  round(profit_factor, 2),
        "sharpe_ratio":   round(sharpe, 3),
        "max_drawdown":   round(max_dd, 2),
        "peak_equity":    round(peak, 2),
        "tp1_hit_count":  tp1_count,
        "tp2_hit_count":  tp2_count,
        "sl_hit_count":   sl_count,
    }


def compute_equity_curve(trades: list = None, starting_balance: float = 100_000.0) -> list:
    """
    Returns list of (timestamp, equity) tuples for plotting.
    """
    if trades is None:
        trades = get_all_trades()

    closed = sorted(
        [t for t in trades if t.get("status") not in ("OPEN",) and t.get("pnl") is not None],
        key=lambda x: x.get("close_time") or ""
    )

    equity = starting_balance
    curve  = [{"time": "Start", "equity": equity}]

    for t in closed:
        equity += float(t.get("pnl") or 0)
        curve.append({
            "time":       t.get("close_time", ""),
            "equity":     round(equity, 2),
            "trade_id":   t.get("trade_id"),
            "pair":       t.get("pair"),
            "pnl":        float(t.get("pnl") or 0),
        })

    return curve


def compute_session_stats(trades: list = None) -> dict:
    """
    Breakdown of performance by trading session.
    """
    if trades is None:
        trades = get_all_trades()

    sessions = {"Asian": [], "London": [], "NY": [], "Unknown": []}

    for t in trades:
        s   = t.get("session", "Unknown") or "Unknown"
        pnl = float(t.get("pnl") or 0)
        sessions.setdefault(s, []).append(pnl)

    result = {}
    for session, pnls in sessions.items():
        closed = [p for p in pnls]
        if not closed:
            continue
        wins = [p for p in closed if p > 0]
        result[session] = {
            "trades":   len(closed),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "net_pnl":  round(sum(closed), 2),
        }

    return result


def compute_instrument_stats(trades: list = None) -> dict:
    """PnL and win rate per instrument."""
    if trades is None:
        trades = get_all_trades()

    instruments: dict = {}
    for t in trades:
        pair = t.get("pair", "UNKNOWN")
        pnl  = float(t.get("pnl") or 0)
        instruments.setdefault(pair, []).append(pnl)

    result = {}
    for pair, pnls in instruments.items():
        wins = [p for p in pnls if p > 0]
        result[pair] = {
            "trades":   len(pnls),
            "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
            "net_pnl":  round(sum(pnls), 2),
        }
    return result


def _max_drawdown(pnls: list) -> tuple[float, float]:
    """Returns (max_drawdown, peak) from a list of PnL values."""
    equity  = 100_000.0
    peak    = equity
    max_dd  = 0.0

    for pnl in pnls:
        equity += pnl
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    return max_dd, peak


def _empty_metrics() -> dict:
    return {
        "total_trades": 0, "win_count": 0, "loss_count": 0,
        "win_rate": 0.0, "net_pnl": 0.0, "avg_win": 0.0,
        "avg_loss": 0.0, "expectancy": 0.0, "profit_factor": 0.0,
        "sharpe_ratio": 0.0, "max_drawdown": 0.0, "peak_equity": 100_000.0,
        "tp1_hit_count": 0, "tp2_hit_count": 0, "sl_hit_count": 0,
    }
