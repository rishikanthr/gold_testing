"""
data_fetcher.py — OANDA v20 candle fetcher
============================================
Fetches multi-timeframe OHLCV candles from OANDA REST API.
Retries on transient failures. Normalises into a standard
{time, open, high, low, close, volume} dict list.
"""

import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from config import (
    OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_BASE_URL,
    OANDA_PRACTICE, CANDLE_CONFIG, ALL_INSTRUMENTS,
    MAX_SPREAD_PIPS, PIP_SIZE,
)

logger = logging.getLogger(__name__)

# OANDA granularity mapping
_GRANULARITY_MAP = {
    "W":   "W",
    "D":   "D",
    "H4":  "H4",
    "H1":  "H1",
    "M15": "M15",
    "M5":  "M5",
}

_SESSION_HEADERS = {
    "Authorization": f"Bearer {OANDA_API_KEY}",
    "Content-Type": "application/json",
}

_MAX_RETRIES = 3
_RETRY_DELAY = 2.0


def _get(url: str, params: dict = None, retries: int = _MAX_RETRIES) -> Optional[dict]:
    """
    GET request with retry logic.
    Returns parsed JSON or None on failure.
    """
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                url,
                headers=_SESSION_HEADERS,
                params=params,
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                logger.warning("Rate limited by OANDA — waiting 5s")
                time.sleep(5)
                continue
            logger.error("OANDA GET %s status %d: %s", url, resp.status_code, resp.text[:200])
            return None
        except requests.exceptions.ConnectionError as exc:
            logger.warning("Connection error (attempt %d/%d): %s", attempt, retries, exc)
            time.sleep(_RETRY_DELAY * attempt)
        except requests.exceptions.Timeout:
            logger.warning("Timeout (attempt %d/%d)", attempt, retries)
            time.sleep(_RETRY_DELAY)
        except Exception as exc:
            logger.error("Unexpected fetch error: %s", exc)
            return None
    return None


def _candle_to_dict(raw: dict) -> dict:
    """Normalises a raw OANDA candle into our standard dict format."""
    mid = raw.get("mid", {})
    return {
        "time":   raw.get("time", ""),
        "open":   float(mid.get("o", 0)),
        "high":   float(mid.get("h", 0)),
        "low":    float(mid.get("l", 0)),
        "close":  float(mid.get("c", 0)),
        "volume": int(raw.get("volume", 0)),
        "complete": raw.get("complete", True),
    }


def fetch_candles(instrument: str, granularity: str, count: int) -> list:
    """
    Fetches `count` candles for `instrument` at the given granularity.
    Returns list of normalised candle dicts (oldest first).
    Only returns completed candles.
    """
    gran = _GRANULARITY_MAP.get(granularity, granularity)
    url  = f"{OANDA_BASE_URL}/v3/instruments/{instrument}/candles"
    params = {
        "granularity": gran,
        "count":       count,
        "price":       "M",   # midpoint only
    }
    data = _get(url, params)
    if not data:
        logger.error("No candle data returned for %s %s", instrument, granularity)
        return []

    candles = [_candle_to_dict(c) for c in data.get("candles", [])]
    # Filter out incomplete (in-progress) candles for strategy logic
    return [c for c in candles if c["complete"]]


def fetch_mtf_data(instrument: str) -> dict:
    """
    Fetches all configured timeframes for one instrument.
    Returns dict keyed by label: {"Weekly": [...], "Daily": [...], ...}
    """
    mtf = {}
    for tf_key, cfg in CANDLE_CONFIG.items():
        label  = cfg["label"]
        count  = cfg["count"]
        candles = fetch_candles(instrument, tf_key, count)
        if candles:
            mtf[label] = candles
            logger.debug("Fetched %d %s candles for %s", len(candles), label, instrument)
        else:
            logger.warning("Empty %s candles for %s — strategies may skip", label, instrument)
            mtf[label] = []
    return mtf


def fetch_account_summary() -> Optional[dict]:
    """
    Fetches account balance, NAV, margin data from OANDA.
    """
    url  = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    data = _get(url)
    if not data:
        return None
    acct = data.get("account", {})
    return {
        "balance":        float(acct.get("balance", 0)),
        "nav":            float(acct.get("NAV", 0)),
        "unrealized_pnl": float(acct.get("unrealizedPL", 0)),
        "margin_used":    float(acct.get("marginUsed", 0)),
        "margin_avail":   float(acct.get("marginAvailable", 0)),
        "open_trade_count": int(acct.get("openTradeCount", 0)),
        "currency":       acct.get("currency", "USD"),
    }


def fetch_open_trades() -> list:
    """
    Fetches all currently open trades from OANDA.
    """
    url  = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    data = _get(url)
    if not data:
        return []
    trades = []
    for t in data.get("trades", []):
        trades.append({
            "id":          t.get("id"),
            "instrument":  t.get("instrument"),
            "units":       float(t.get("currentUnits", t.get("initialUnits", 0))),
            "open_price":  float(t.get("price", 0)),
            "unrealized":  float(t.get("unrealizedPL", 0)),
            "open_time":   t.get("openTime", ""),
            "stop_loss":   float(t["stopLossOrder"]["price"]) if "stopLossOrder" in t else None,
            "take_profit": float(t["takeProfitOrder"]["price"]) if "takeProfitOrder" in t else None,
        })
    return trades


def fetch_current_price(instrument: str) -> Optional[dict]:
    """
    Fetches the latest bid/ask price for an instrument.
    Returns {"bid", "ask", "spread_pips"} or None.
    """
    url = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    data = _get(url, params={"instruments": instrument})
    if not data:
        return None
    prices = data.get("prices", [])
    if not prices:
        return None
    p   = prices[0]
    bid = float(p.get("bids", [{}])[0].get("price", 0))
    ask = float(p.get("asks", [{}])[0].get("price", 0))
    pip = PIP_SIZE.get(instrument, 0.0001)
    spread_pips = round((ask - bid) / pip, 1)
    return {
        "bid":          bid,
        "ask":          ask,
        "mid":          round((bid + ask) / 2, 5),
        "spread_pips":  spread_pips,
        "tradeable":    p.get("tradeable", False),
        "status":       p.get("status", "unknown"),
    }


def is_spread_acceptable(instrument: str) -> tuple[bool, float]:
    """
    Returns (acceptable: bool, spread_pips: float).
    Rejects trades if spread exceeds configured maximum.
    """
    price = fetch_current_price(instrument)
    if not price:
        return False, 999.0
    spread = price["spread_pips"]
    max_spread = MAX_SPREAD_PIPS.get(instrument, 3.0)
    return spread <= max_spread, spread


def fetch_closed_trades(count: int = 50) -> list:
    """
    Fetches the most recent closed trades from OANDA.
    Used for performance tracking.
    """
    url    = f"{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}/trades"
    params = {"state": "CLOSED", "count": count}
    data   = _get(url, params)
    if not data:
        return []
    trades = []
    for t in data.get("trades", []):
        trades.append({
            "id":           t.get("id"),
            "instrument":   t.get("instrument"),
            "units":        float(t.get("initialUnits", 0)),
            "open_price":   float(t.get("price", 0)),
            "close_price":  float(t.get("averageClosePrice", 0)),
            "realized_pnl": float(t.get("realizedPL", 0)),
            "open_time":    t.get("openTime", ""),
            "close_time":   t.get("closeTime", ""),
        })
    return trades
