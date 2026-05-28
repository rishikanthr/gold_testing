"""
trade_executor.py — OANDA order execution
==========================================
Handles:
- Market order entry with SL/TP
- Trade modification (SL update, partial close)
- Trade closure
- Position sizing from risk %
"""

import logging
import json
from typing import Optional

import requests

from config import (
    OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_BASE_URL,
    RISK_PERCENT, ACCOUNT_BALANCE_DEFAULT,
    PIP_SIZE, GOLD_PAIR, GOLD_SIZE_REDUCTION,
    TP1_RR, TP2_RR, TP1_CLOSE_PCT,
)

logger = logging.getLogger(__name__)

_HEADERS = {
    "Authorization":  f"Bearer {OANDA_API_KEY}",
    "Content-Type":   "application/json",
    "Accept-Datetime-Format": "RFC3339",
}

_MAX_RETRIES = 3


def _post(url: str, body: dict, retries: int = _MAX_RETRIES) -> Optional[dict]:
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, headers=_HEADERS, json=body, timeout=10)
            if resp.status_code in (200, 201):
                return resp.json()
            logger.error("POST %s status %d: %s", url, resp.status_code, resp.text[:300])
            return None
        except requests.exceptions.ConnectionError as exc:
            logger.warning("Connection error (attempt %d): %s", attempt, exc)
            import time; time.sleep(2 * attempt)
        except Exception as exc:
            logger.error("Unexpected error: %s", exc)
            return None
    return None


def _put(url: str, body: dict) -> Optional[dict]:
    try:
        resp = requests.put(url, headers=_HEADERS, json=body, timeout=10)
        if resp.status_code in (200, 201):
            return resp.json()
        logger.error("PUT %s status %d: %s", url, resp.status_code, resp.text[:300])
        return None
    except Exception as exc:
        logger.error("PUT error: %s", exc)
        return None


def calculate_position_size(
    pair: str,
    risk_pips: float,
    account_balance: float = ACCOUNT_BALANCE_DEFAULT,
) -> int:
    """
    Calculate units to risk exactly RISK_PERCENT of account balance.

    For XAU_USD:  1 unit = 1 troy oz. Pip value = $0.01/unit/pip.
        units = risk_dollars / (risk_pips * 0.01)
    For JPY pairs: pip = 0.01, pip_value ≈ (0.01 / price) * units_per_lot
    For all others: pip = 0.0001, pip_value ≈ $10 per lot per pip
        units = risk_dollars / (risk_pips * pip_size * units_per_pip_value)
    """
    if risk_pips <= 0:
        logger.warning("risk_pips <= 0 (%s) — cannot size position", risk_pips)
        return 0

    risk_dollars = account_balance * (RISK_PERCENT / 100.0)
    pip          = PIP_SIZE.get(pair, 0.0001)

    if pair == GOLD_PAIR:
        # XAU_USD: pip_value_per_unit = $0.01 (1 pip = $0.01/oz)
        pip_value_per_unit = 0.01
        units = risk_dollars / (risk_pips * pip_value_per_unit)
        units = int(units * GOLD_SIZE_REDUCTION)
    elif "JPY" in pair:
        # Approximate: 1 pip (0.01) on USD/JPY ≈ $0.01 / price * units
        # Use conservative approximation: pip_value ≈ $0.10 per unit at 150 JPY
        pip_value_per_unit = 0.01 / 150.0
        units = int(risk_dollars / (risk_pips * pip_value_per_unit))
    else:
        # Standard forex: 1 standard lot = 100,000 units, pip = $10/lot
        # pip_value_per_unit = 0.0001 * 1 = $0.0001 for non-quoted currencies
        # Simplified: units = risk_$ / (risk_pips * pip)
        units = int(risk_dollars / (risk_pips * pip))

    # Sanity bounds
    if pair == GOLD_PAIR:
        units = max(1, min(units, 50_000))    # 1 oz to 50,000 oz max
    else:
        units = max(1000, min(units, 10_000_000))   # 1K to 10M units

    logger.info(
        "Position size %s: risk=$%.2f pips=%.1f → %d units",
        pair, risk_dollars, risk_pips, units
    )
    return units


def execute_market_order(
    pair: str,
    signal: str,          # "LONG" or "SHORT"
    units: int,
    stop_loss: float,
    take_profit: float,
    comment: str = "",
) -> Optional[dict]:
    """
    Submits a market order with SL and TP attached.
    Returns trade dict with id, entry price, etc.
    """
    signed_units = units if signal == "LONG" else -units
    url  = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    body = {
        "order": {
            "type":          "MARKET",
            "instrument":    pair,
            "units":         str(signed_units),
            "timeInForce":   "FOK",
            "positionFill":  "DEFAULT",
            "stopLossOnFill": {
                "price": str(round(stop_loss, 5)),
                "timeInForce": "GTC",
            },
            "takeProfitOnFill": {
                "price": str(round(take_profit, 5)),
                "timeInForce": "GTC",
            },
            "clientExtensions": {
                "comment": comment[:128],
            },
        }
    }

    logger.info(
        "Placing %s %s %d units | SL=%.5f TP=%.5f",
        signal, pair, abs(signed_units), stop_loss, take_profit
    )

    data = _post(url, body)
    if not data:
        return None

    tf = data.get("orderFillTransaction", {})
    if not tf:
        logger.error("Order not filled immediately: %s", data)
        return None

    trade_id    = tf.get("tradeOpened", {}).get("tradeID", "")
    fill_price  = float(tf.get("price", 0))
    actual_units = abs(float(tf.get("units", signed_units)))

    logger.info("Order filled: trade_id=%s price=%.5f", trade_id, fill_price)
    return {
        "trade_id":    trade_id,
        "fill_price":  fill_price,
        "units":       actual_units,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "signal":      signal,
        "pair":        pair,
    }


def modify_stop_loss(trade_id: str, new_sl: float) -> bool:
    """Updates the stop loss on an open trade (e.g., move to breakeven)."""
    url  = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders"
    body = {
        "stopLoss": {
            "price":       str(round(new_sl, 5)),
            "timeInForce": "GTC",
        }
    }
    result = _put(url, body)
    if result:
        logger.info("SL updated for trade %s → %.5f", trade_id, new_sl)
        return True
    logger.error("Failed to update SL for trade %s", trade_id)
    return False


def close_trade(trade_id: str, units: Optional[int] = None) -> Optional[dict]:
    """
    Closes a trade fully or partially.
    If units is None, closes the full position.
    """
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/close"
    body = {}
    if units:
        body["units"] = str(units)

    try:
        resp = requests.put(url, headers=_HEADERS, json=body, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            tx   = data.get("orderFillTransaction", {})
            pnl  = float(tx.get("pl", 0))
            price = float(tx.get("price", 0))
            logger.info("Closed trade %s at %.5f PnL=%.2f", trade_id, price, pnl)
            return {"close_price": price, "pnl": pnl}
        logger.error("Close failed trade %s: %s", trade_id, resp.text[:200])
        return None
    except Exception as exc:
        logger.error("Close error for trade %s: %s", trade_id, exc)
        return None


def partial_close_at_tp1(trade_id: str, total_units: int) -> bool:
    """
    Closes TP1_CLOSE_PCT of the position at TP1 and returns True on success.
    """
    close_units = int(total_units * TP1_CLOSE_PCT)
    if close_units <= 0:
        return False
    result = close_trade(trade_id, close_units)
    return result is not None
