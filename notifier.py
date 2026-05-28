"""
notifier.py — Telegram mobile notification service
====================================================
Sends formatted trade alerts, status updates, and daily
summaries to a configured Telegram chat.

Failures are logged but never raise — notifications must
never crash the main bot loop.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
_TIMEOUT      = 8


def _send(text: str, parse_mode: str = "HTML") -> bool:
    """Core send function. Returns True on success."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — skipping notification")
        return False
    try:
        resp = requests.post(
            _TELEGRAM_URL,
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       text,
                "parse_mode": parse_mode,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return True
        logger.warning("Telegram send failed %d: %s", resp.status_code, resp.text[:100])
        return False
    except Exception as exc:
        logger.warning("Telegram error: %s", exc)
        return False


# ------------------------------------------------------------------
# Public notification methods
# ------------------------------------------------------------------

def notify_startup(version: str = "1.0"):
    """Sent when the bot starts up."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _send(
        f"🤖 <b>ICT/SMC Gold Bot STARTED</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🕐 Time: {now}\n"
        f"📊 Strategies: 7 Gold + Forex LQ\n"
        f"⚙️ Version: {version}\n"
        f"💰 Account: $100,000 | Risk: 1%/trade"
    )


def notify_shutdown(reason: str = "User initiated"):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _send(
        f"🛑 <b>ICT/SMC Bot STOPPED</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⏹ Reason: {reason}\n"
        f"🕐 Time: {now}"
    )


def notify_new_signal(signal: dict, pair: str):
    """Called when a high-quality signal is detected (pre-execution confirmation)."""
    direction_emoji = "🟢" if signal.get("signal") == "LONG" else "🔴"
    score           = signal.get("score", 0)
    strategy        = signal.get("strategy_id", "")
    reason          = signal.get("reason", "")[:120]

    _send(
        f"{direction_emoji} <b>NEW SIGNAL — {pair}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📌 Strategy: {strategy}\n"
        f"📊 Score: {score}/10\n"
        f"💹 Direction: {signal.get('signal')}\n"
        f"🎯 Entry: {signal.get('entry')}\n"
        f"🛡 SL: {signal.get('stop_loss')} ({signal.get('risk_pips')} pips)\n"
        f"✅ TP1: {signal.get('tp1')}\n"
        f"🏆 TP2: {signal.get('tp2')}\n"
        f"🔍 {reason}"
    )


def notify_trade_entry(trade: dict, signal: dict, pair: str, units: int):
    """Called immediately after a trade is opened."""
    direction_emoji = "🟢" if signal.get("signal") == "LONG" else "🔴"
    fill_price      = trade.get("fill_price", 0)
    _send(
        f"{direction_emoji} <b>TRADE OPENED — {pair}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🆔 Trade ID: {trade.get('trade_id')}\n"
        f"💹 Direction: {signal.get('signal')}\n"
        f"📌 Strategy: {signal.get('strategy_id')}\n"
        f"📊 Score: {signal.get('score')}/10\n"
        f"💰 Units: {units:,}\n"
        f"🎯 Fill: {fill_price}\n"
        f"🛡 SL: {signal.get('stop_loss')}\n"
        f"✅ TP1: {signal.get('tp1')}\n"
        f"🏆 TP2: {signal.get('tp2')}\n"
        f"⚖️ RR: 1:{signal.get('tp2', 0) and round(abs(float(signal.get('tp2', 0)) - fill_price) / max(abs(fill_price - float(signal.get('stop_loss', fill_price))), 1e-6), 1)}"
    )


def notify_tp1_hit(trade_id: str, pair: str, pnl: float):
    _send(
        f"✅ <b>TP1 HIT — {pair}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🆔 Trade ID: {trade_id}\n"
        f"💵 Realized PnL: +${pnl:.2f}\n"
        f"🔄 50% position closed\n"
        f"🏃 Runner to TP2 in progress\n"
        f"📏 SL moved to breakeven"
    )


def notify_tp2_hit(trade_id: str, pair: str, pnl: float):
    _send(
        f"🏆 <b>TP2 HIT — {pair}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🆔 Trade ID: {trade_id}\n"
        f"💵 Total PnL: +${pnl:.2f}\n"
        f"🎯 Full target reached!"
    )


def notify_sl_hit(trade_id: str, pair: str, pnl: float):
    _send(
        f"🛑 <b>STOP LOSS HIT — {pair}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🆔 Trade ID: {trade_id}\n"
        f"💸 Loss: -${abs(pnl):.2f}\n"
        f"📐 Risk was managed — onwards"
    )


def notify_circuit_breaker(daily_pnl: float, pct: float):
    _send(
        f"🚨 <b>CIRCUIT BREAKER ACTIVATED</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📉 Daily loss: -${abs(daily_pnl):.2f} ({pct:.1f}%)\n"
        f"⛔ Limit: 3% (-$3,000)\n"
        f"🛑 No more trades today\n"
        f"✅ Resets at midnight UTC"
    )


def notify_daily_summary(summary: dict):
    trades  = summary.get("trades_today", 0)
    pnl     = summary.get("daily_pnl", 0.0)
    wins    = summary.get("wins", 0)
    losses  = summary.get("losses", 0)
    wr      = (wins / max(1, wins + losses)) * 100

    pnl_emoji = "📈" if pnl >= 0 else "📉"
    sign      = "+" if pnl >= 0 else ""

    _send(
        f"{pnl_emoji} <b>DAILY SUMMARY</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📅 Date: {summary.get('date')}\n"
        f"📊 Trades: {trades}\n"
        f"✅ Wins: {wins} | ❌ Losses: {losses}\n"
        f"🎯 Win Rate: {wr:.0f}%\n"
        f"💰 Net PnL: {sign}${pnl:.2f}"
    )


def notify_error(context: str, error: str):
    _send(
        f"⚠️ <b>BOT ERROR</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 Context: {context}\n"
        f"❗ Error: {str(error)[:200]}"
    )
