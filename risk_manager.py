"""
risk_manager.py — Circuit breaker & risk validation
=====================================================
Enforces all risk rules:
- 1% risk per trade
- Max 3% daily loss (circuit breaker)
- Max 3 open trades
- Max 5 trades per day
- No duplicate instrument trades
- No revenge trading
- SL size validation
"""

import json
import logging
import os
from datetime import datetime, timezone, date
from typing import Optional

from config import (
    RISK_PERCENT, MAX_DAILY_LOSS_PCT, MAX_OPEN_TRADES,
    MAX_TRADES_PER_DAY, ACCOUNT_BALANCE_DEFAULT,
    STATE_FILE, LOG_DIR,
    GOLD_MAX_SL_PIPS, FOREX_MAX_SL_PIPS,
    GOLD_MIN_SL_PIPS, FOREX_MIN_SL_PIPS,
    GOLD_PAIR, MAX_SPREAD_PIPS, PIP_SIZE,
)

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Stateful risk controller.
    Persists daily counters to STATE_FILE so bot restarts
    don't reset the daily trade count mid-session.
    """

    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.state = self._load_state()
        self._ensure_daily_reset()

    # ----------------------------------------------------------
    # State persistence
    # ----------------------------------------------------------

    def _load_state(self) -> dict:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("Could not load state: %s — starting fresh", exc)
        return {}

    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as exc:
            logger.error("Failed to save state: %s", exc)

    def _ensure_daily_reset(self):
        today = str(date.today())
        if self.state.get("date") != today:
            logger.info("New trading day — resetting daily counters (was %s)", self.state.get("date"))
            self.state = {
                "date":               today,
                "trades_today":       0,
                "daily_pnl":          0.0,
                "circuit_breaker":    False,
                "traded_instruments": [],
                "sb_trades_window":   {},
                "last_loss_time":     None,
            }
            self._save_state()

    # ----------------------------------------------------------
    # Core validation
    # ----------------------------------------------------------

    def validate_trade(
        self,
        signal: dict,
        open_trades: list,
        account_balance: float,
        spread_pips: float,
    ) -> tuple[bool, str]:
        """
        Full pre-trade risk check. Returns (allowed: bool, reason: str).
        Checks every rule in order — first failure wins.
        """
        self._ensure_daily_reset()

        pair  = signal.get("pair", "")
        score = signal.get("score", 0)

        # 1. Circuit breaker — daily loss limit
        if self.state.get("circuit_breaker"):
            loss_pct = abs(self.state["daily_pnl"]) / account_balance * 100
            return False, f"Circuit breaker ACTIVE — daily loss {loss_pct:.1f}% exceeds {MAX_DAILY_LOSS_PCT}%"

        # 2. Max open trades
        if len(open_trades) >= MAX_OPEN_TRADES:
            return False, f"Max open trades ({MAX_OPEN_TRADES}) reached"

        # 3. Max daily trades
        if self.state["trades_today"] >= MAX_TRADES_PER_DAY:
            return False, f"Max daily trades ({MAX_TRADES_PER_DAY}) reached"

        # 4. Duplicate instrument
        already_open = {t["instrument"] for t in open_trades}
        if pair in already_open:
            return False, f"Already have open trade on {pair} — no duplicates"

        # 5. Spread filter
        max_spread = MAX_SPREAD_PIPS.get(pair, 3.0)
        if spread_pips > max_spread:
            return False, f"Spread {spread_pips} pips > max {max_spread} pips for {pair}"

        # 6. SL size validation
        risk_pips = signal.get("risk_pips", 0)
        is_gold   = pair == GOLD_PAIR
        max_sl    = GOLD_MAX_SL_PIPS if is_gold else FOREX_MAX_SL_PIPS
        min_sl    = GOLD_MIN_SL_PIPS  if is_gold else FOREX_MIN_SL_PIPS

        if risk_pips > max_sl:
            return False, f"SL {risk_pips} pips > max {max_sl} for {pair}"
        if risk_pips < min_sl:
            return False, f"SL {risk_pips} pips < min {min_sl} for {pair}"

        # 7. Score minimum (already pre-filtered by strategies, but double-check)
        if score < 6:
            return False, f"Score {score}/10 too low (minimum 6)"

        # 8. Entry price sanity — entry must be present
        if not signal.get("entry") or not signal.get("stop_loss") or not signal.get("tp1"):
            return False, "Incomplete signal — missing entry/SL/TP"

        return True, "OK"

    def record_trade_open(self, pair: str):
        """Call after a trade is successfully opened."""
        self._ensure_daily_reset()
        self.state["trades_today"]       += 1
        self.state["traded_instruments"].append(pair)
        self._save_state()
        logger.info("Trade opened. Trades today: %d / %d",
                    self.state["trades_today"], MAX_TRADES_PER_DAY)

    def record_trade_close(self, pnl: float, account_balance: float):
        """Call after a trade closes. Updates daily PnL and checks circuit breaker."""
        self._ensure_daily_reset()
        self.state["daily_pnl"] += pnl

        if pnl < 0:
            self.state["last_loss_time"] = datetime.now(timezone.utc).isoformat()

        daily_loss_pct = abs(self.state["daily_pnl"]) / account_balance * 100
        if self.state["daily_pnl"] < 0 and daily_loss_pct >= MAX_DAILY_LOSS_PCT:
            self.state["circuit_breaker"] = True
            logger.critical(
                "CIRCUIT BREAKER TRIGGERED — daily loss %.2f (%.1f%%)",
                self.state["daily_pnl"], daily_loss_pct
            )

        self._save_state()

    def get_daily_summary(self) -> dict:
        """Returns a dict of current daily risk metrics."""
        return {
            "date":            self.state.get("date"),
            "trades_today":    self.state.get("trades_today", 0),
            "daily_pnl":       self.state.get("daily_pnl", 0.0),
            "circuit_breaker": self.state.get("circuit_breaker", False),
        }

    def update_sb_window(self, window_name: str):
        """Records that a Silver Bullet trade was taken in a given window."""
        self._ensure_daily_reset()
        w = self.state.setdefault("sb_trades_window", {})
        w[window_name] = w.get(window_name, 0) + 1
        self._save_state()

    def get_sb_context(self) -> dict:
        """Returns context dict for Silver Bullet window tracking."""
        return {"sb_trades_this_window": self.state.get("sb_trades_window", {})}

    def calculate_risk_dollars(self, account_balance: float) -> float:
        return account_balance * (RISK_PERCENT / 100.0)

    def is_circuit_breaker_active(self) -> bool:
        self._ensure_daily_reset()
        return self.state.get("circuit_breaker", False)
