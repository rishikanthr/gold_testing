"""
trade_manager.py — Active trade lifecycle management
======================================================
Monitors open trades and:
- Moves SL to breakeven when TP1 is hit
- Partially closes at TP1
- Closes runner at TP2
- Detects SL hits from OANDA
- Tracks trade state in memory + DB
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from config import MOVE_SL_TO_BE, TP1_CLOSE_PCT, GOLD_PAIR, PIP_SIZE
from data_fetcher import fetch_open_trades, fetch_current_price
from trade_executor import modify_stop_loss, partial_close_at_tp1, close_trade
from logger import log_trade_close, log_event
from notifier import notify_tp1_hit, notify_tp2_hit, notify_sl_hit

logger = logging.getLogger(__name__)


class TradeManager:
    """
    Manages the lifecycle of all active bot trades.
    Maintains a local state dict of open trades keyed by trade_id.
    """

    def __init__(self, risk_manager):
        self.risk_manager   = risk_manager
        self._active_trades: dict = {}   # trade_id → state dict

    def register_trade(
        self,
        trade_id: str,
        pair: str,
        signal: dict,
        fill_price: float,
        units: int,
    ):
        """
        Called when a new trade is opened.
        Stores all metadata needed to manage its lifecycle.
        """
        self._active_trades[trade_id] = {
            "trade_id":    trade_id,
            "pair":        pair,
            "signal":      signal.get("signal"),
            "strategy_id": signal.get("strategy_id"),
            "entry":       fill_price,
            "stop_loss":   signal.get("stop_loss"),
            "tp1":         signal.get("tp1"),
            "tp2":         signal.get("tp2"),
            "risk_pips":   signal.get("risk_pips"),
            "units":       units,
            "tp1_hit":     False,
            "be_moved":    False,
            "open_time":   datetime.now(timezone.utc).isoformat(),
        }
        logger.info("Trade registered: %s %s entry=%.5f", trade_id, pair, fill_price)

    def manage_all(self) -> list:
        """
        Main management loop — call this every cycle.
        Checks all registered trades against current prices.
        Returns list of closed trade_ids.
        """
        if not self._active_trades:
            return []

        closed = []

        # Sync with OANDA to detect any SL/TP hits we missed
        oanda_open = {t["id"]: t for t in fetch_open_trades()}

        for trade_id, state in list(self._active_trades.items()):

            # Check if trade was closed by OANDA (SL/TP hit)
            if trade_id not in oanda_open:
                self._handle_closed(trade_id, state)
                closed.append(trade_id)
                del self._active_trades[trade_id]
                continue

            pair   = state["pair"]
            price  = fetch_current_price(pair)
            if not price:
                continue

            mid    = price["mid"]
            sig    = state["signal"]
            tp1    = state["tp1"]
            tp2    = state["tp2"]
            entry  = state["entry"]
            sl     = state["stop_loss"]

            # Check TP1 hit (if not already processed)
            if not state["tp1_hit"]:
                tp1_hit = (sig == "LONG" and mid >= tp1) or (sig == "SHORT" and mid <= tp1)
                if tp1_hit:
                    self._handle_tp1(trade_id, state, mid)

            # Check TP2 hit
            tp2_hit = (sig == "LONG" and mid >= tp2) or (sig == "SHORT" and mid <= tp2)
            if tp2_hit:
                self._handle_tp2(trade_id, state, mid)
                closed.append(trade_id)
                del self._active_trades[trade_id]
                continue

            # Check SL hit (shouldn't normally happen — OANDA handles it)
            sl_hit = (sig == "LONG" and mid <= sl) or (sig == "SHORT" and mid >= sl)
            if sl_hit:
                result = close_trade(trade_id)
                if result:
                    pnl = result.get("pnl", 0)
                    self._on_trade_close(trade_id, state, result.get("close_price", mid),
                                         pnl, "SL_HIT", sl_hit=True)
                    notify_sl_hit(trade_id, pair, pnl)
                    closed.append(trade_id)
                    del self._active_trades[trade_id]

        return closed

    def _handle_tp1(self, trade_id: str, state: dict, current_price: float):
        """Partial close at TP1 and move SL to breakeven."""
        pair    = state["pair"]
        entry   = state["entry"]
        units   = state["units"]
        tp1_pnl = self._estimate_pnl(state, current_price, int(units * TP1_CLOSE_PCT))

        logger.info("TP1 reached for trade %s at %.5f — partial closing", trade_id, current_price)

        # Partial close
        partial_close_at_tp1(trade_id, units)
        state["tp1_hit"] = True
        state["units"]   = int(units * (1 - TP1_CLOSE_PCT))

        # Move SL to breakeven
        if MOVE_SL_TO_BE:
            pip    = PIP_SIZE.get(pair, 0.0001)
            buffer = 2 * pip   # 2 pip buffer from exact entry
            new_sl = (entry + buffer) if state["signal"] == "LONG" else (entry - buffer)
            if modify_stop_loss(trade_id, new_sl):
                state["stop_loss"] = new_sl
                state["be_moved"]  = True
                logger.info("SL moved to BE for trade %s: %.5f", trade_id, new_sl)

        notify_tp1_hit(trade_id, pair, tp1_pnl)
        log_event("TP1_HIT", {
            "trade_id":    trade_id,
            "pair":        pair,
            "price":       current_price,
            "tp1_pnl":     tp1_pnl,
        })

    def _handle_tp2(self, trade_id: str, state: dict, current_price: float):
        """Close remaining runner at TP2."""
        pair    = state["pair"]
        units   = state["units"]
        result  = close_trade(trade_id)
        pnl     = result.get("pnl", 0) if result else 0
        price   = result.get("close_price", current_price) if result else current_price

        logger.info("TP2 reached for trade %s at %.5f — full close PnL=%.2f",
                    trade_id, price, pnl)

        self._on_trade_close(trade_id, state, price, pnl, "TP2_HIT",
                             tp1_hit=state["tp1_hit"], tp2_hit=True)
        notify_tp2_hit(trade_id, pair, pnl)

    def _handle_closed(self, trade_id: str, state: dict):
        """
        Trade was closed externally (OANDA SL/TP execution).
        We don't have the exact close price from OANDA here,
        so we estimate from the SL/TP levels.
        """
        sig  = state["signal"]
        tp1  = state["tp1"]
        sl   = state["stop_loss"]

        if state["tp1_hit"]:
            status = "TP2_HIT"
            price  = state["tp2"]
        else:
            # Could be SL or TP1 — check which is more likely by comparing levels
            status = "CLOSED_EXTERNAL"
            price  = sl

        pnl = self._estimate_pnl(state, price, state["units"])
        sl_hit = "SL" in status or pnl < 0

        self._on_trade_close(trade_id, state, price, pnl, status,
                             tp1_hit=state["tp1_hit"],
                             tp2_hit=(status == "TP2_HIT"),
                             sl_hit=sl_hit)

        if sl_hit:
            notify_sl_hit(trade_id, state["pair"], pnl)
        elif status == "TP2_HIT":
            notify_tp2_hit(trade_id, state["pair"], pnl)

    def _on_trade_close(
        self, trade_id: str, state: dict,
        close_price: float, pnl: float, status: str,
        tp1_hit: bool = False, tp2_hit: bool = False,
        sl_hit: bool = False,
    ):
        close_time = datetime.now(timezone.utc).isoformat()
        log_trade_close(
            trade_id=trade_id,
            close_price=close_price,
            pnl=pnl,
            close_time=close_time,
            status=status,
            tp1_hit=tp1_hit,
            tp2_hit=tp2_hit,
            sl_hit=sl_hit,
            be_moved=state.get("be_moved", False),
        )
        self.risk_manager.record_trade_close(pnl, 100_000.0)
        logger.info("Trade closed %s status=%s pnl=%.2f", trade_id, status, pnl)

    def _estimate_pnl(self, state: dict, close_price: float, units: int) -> float:
        entry = state["entry"]
        sig   = state["signal"]
        pair  = state["pair"]
        pip   = PIP_SIZE.get(pair, 0.0001)

        if sig == "LONG":
            pips = (close_price - entry) / pip
        else:
            pips = (entry - close_price) / pip

        pip_value = 0.01 if pair == GOLD_PAIR else pip
        return round(pips * pip_value * units, 2)

    def get_active_count(self) -> int:
        return len(self._active_trades)

    def get_active_trades(self) -> list:
        return list(self._active_trades.values())
