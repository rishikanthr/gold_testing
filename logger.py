"""
logger.py — Structured logging + SQLite trade database
========================================================
Sets up:
1. Python logging with colour-coded console + rotating file handler
2. SQLite database for persistent trade/signal storage
3. CSV export helpers
"""

import csv
import json
import logging
import logging.handlers
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config import LOG_DIR, DB_FILE, TRADE_LOG_FILE, SIGNAL_LOG_FILE


# ------------------------------------------------------------------
# Python logger setup
# ------------------------------------------------------------------

def setup_logging(level: str = "INFO") -> logging.Logger:
    """
    Configures root logger with:
    - Coloured console output
    - Rotating file handler (10 MB, 5 backups)
    Returns the root logger.
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    log_level = getattr(logging, level.upper(), logging.INFO)

    # Root logger
    root = logging.getLogger()
    root.setLevel(log_level)

    # Remove any existing handlers (prevents duplicate lines on reload)
    root.handlers.clear()

    # Console handler with colour
    console = logging.StreamHandler()
    console.setLevel(log_level)
    try:
        import colorlog
        fmt = colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s %(levelname)-8s%(reset)s %(name)s — %(message)s",
            datefmt="%H:%M:%S",
            log_colors={
                "DEBUG":    "cyan",
                "INFO":     "green",
                "WARNING":  "yellow",
                "ERROR":    "red",
                "CRITICAL": "bold_red",
            },
        )
    except ImportError:
        fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s",
                                datefmt="%H:%M:%S")
    console.setFormatter(fmt)
    root.addHandler(console)

    # Rotating file handler
    log_file = os.path.join(LOG_DIR, "bot.log")
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(log_level)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    return root


# ------------------------------------------------------------------
# SQLite database
# ------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    os.makedirs(LOG_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Creates tables if they don't exist."""
    conn = _get_conn()
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id        TEXT,
                pair            TEXT,
                signal          TEXT,
                strategy_id     TEXT,
                score           INTEGER,
                entry_price     REAL,
                stop_loss       REAL,
                tp1             REAL,
                tp2             REAL,
                risk_pips       REAL,
                units           INTEGER,
                open_time       TEXT,
                close_time      TEXT,
                close_price     REAL,
                pnl             REAL,
                status          TEXT DEFAULT 'OPEN',
                daily_bias      TEXT,
                htf_zone        TEXT,
                reason          TEXT,
                session         TEXT,
                risk_dollars    REAL,
                tp1_hit         INTEGER DEFAULT 0,
                tp2_hit         INTEGER DEFAULT 0,
                sl_hit          INTEGER DEFAULT 0,
                be_moved        INTEGER DEFAULT 0,
                market_cond     TEXT,
                screenshots_path TEXT,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                pair            TEXT,
                signal          TEXT,
                strategy_id     TEXT,
                score           INTEGER,
                entry           REAL,
                stop_loss       REAL,
                tp1             REAL,
                tp2             REAL,
                risk_pips       REAL,
                daily_bias      TEXT,
                htf_zone        TEXT,
                reason          TEXT,
                taken           INTEGER DEFAULT 0,
                skip_reason     TEXT,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT,
                payload     TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
    conn.close()


def log_trade_open(
    trade_id: str, pair: str, signal_dict: dict,
    units: int, open_time: str, session: str,
    risk_dollars: float,
):
    """Inserts a new open trade record."""
    conn = _get_conn()
    with conn:
        conn.execute("""
            INSERT INTO trades
            (trade_id, pair, signal, strategy_id, score, entry_price,
             stop_loss, tp1, tp2, risk_pips, units, open_time,
             daily_bias, htf_zone, reason, session, risk_dollars, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade_id,
            pair,
            signal_dict.get("signal"),
            signal_dict.get("strategy_id"),
            signal_dict.get("score"),
            signal_dict.get("entry"),
            signal_dict.get("stop_loss"),
            signal_dict.get("tp1"),
            signal_dict.get("tp2"),
            signal_dict.get("risk_pips"),
            units,
            open_time,
            signal_dict.get("daily_bias"),
            signal_dict.get("htf_zone"),
            signal_dict.get("reason", "")[:500],
            session,
            risk_dollars,
            "OPEN",
        ))
    conn.close()


def log_trade_close(
    trade_id: str, close_price: float, pnl: float,
    close_time: str, status: str,
    tp1_hit: bool = False, tp2_hit: bool = False,
    sl_hit: bool = False, be_moved: bool = False,
):
    """Updates a trade record on close."""
    conn = _get_conn()
    with conn:
        conn.execute("""
            UPDATE trades SET
                close_time   = ?,
                close_price  = ?,
                pnl          = ?,
                status       = ?,
                tp1_hit      = ?,
                tp2_hit      = ?,
                sl_hit       = ?,
                be_moved     = ?
            WHERE trade_id = ?
        """, (
            close_time, close_price, pnl, status,
            int(tp1_hit), int(tp2_hit), int(sl_hit), int(be_moved),
            trade_id,
        ))
    conn.close()


def log_signal(signal: dict, pair: str, taken: bool, skip_reason: str = ""):
    """Logs every signal (taken or skipped)."""
    conn = _get_conn()
    with conn:
        conn.execute("""
            INSERT INTO signals
            (pair, signal, strategy_id, score, entry, stop_loss, tp1, tp2,
             risk_pips, daily_bias, htf_zone, reason, taken, skip_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            pair,
            signal.get("signal"),
            signal.get("strategy_id"),
            signal.get("score"),
            signal.get("entry"),
            signal.get("stop_loss"),
            signal.get("tp1"),
            signal.get("tp2"),
            signal.get("risk_pips"),
            signal.get("daily_bias"),
            signal.get("htf_zone"),
            signal.get("reason", "")[:500],
            int(taken),
            skip_reason,
        ))
    conn.close()


def log_event(event_type: str, payload: dict):
    """Logs arbitrary bot events (startup, error, circuit breaker, etc.)."""
    conn = _get_conn()
    with conn:
        conn.execute(
            "INSERT INTO bot_events (event_type, payload) VALUES (?,?)",
            (event_type, json.dumps(payload))
        )
    conn.close()


def get_recent_trades(limit: int = 50) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_open_db_trades() -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY open_time DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_trades() -> list:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM trades ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_strategy_stats() -> list:
    """Returns win rate and PnL per strategy."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT
            strategy_id,
            COUNT(*) AS total,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses,
            ROUND(SUM(pnl), 2) AS net_pnl,
            ROUND(AVG(pnl), 2) AS avg_pnl,
            ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_rate
        FROM trades
        WHERE status != 'OPEN'
        GROUP BY strategy_id
        ORDER BY net_pnl DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def export_trades_csv(filepath: str = None):
    """Exports all trades to a CSV file."""
    path  = filepath or TRADE_LOG_FILE
    trades = get_all_trades()
    if not trades:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=trades[0].keys())
        writer.writeheader()
        writer.writerows(trades)
