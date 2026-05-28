"""
config.py — All configuration constants for the ICT/SMC Gold Trading Bot
=========================================================================
Edit this file to tune strategies, risk, and API credentials.
All sensitive values are loaded from a .env file in the same directory.
"""

import os
from dotenv import load_dotenv

# Load .env from the trading-bot directory
_env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(_env_path)

# ============================================================
# OANDA API
# ============================================================
OANDA_API_KEY    = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_PRACTICE   = os.getenv("OANDA_PRACTICE", "true").lower() == "true"

OANDA_BASE_URL = (
    "https://api-fxtrade.oanda.com"
    if not OANDA_PRACTICE
    else "https://api-fxpractice.oanda.com"
)

# ============================================================
# Telegram
# ============================================================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================
# Groq LLM Confirmation
# ============================================================
GROQ_API_KEY          = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL            = "llama-3.3-70b-versatile"
LLM_MIN_CONFIDENCE    = 7       # minimum score (1–10) to approve a trade
USE_LLM_CONFIRMATION  = os.getenv("USE_LLM", "false").lower() == "true"

# ============================================================
# Account & Risk
# ============================================================
ACCOUNT_BALANCE_DEFAULT = 100_000.0   # USD — used when live balance unavailable
RISK_PERCENT            = 1.0         # % of account to risk per trade
MAX_DAILY_LOSS_PCT      = 3.0         # % daily loss triggers circuit breaker
MAX_OPEN_TRADES         = 3           # max simultaneous open positions
MAX_TRADES_PER_DAY      = 5           # hard daily trade limit

# TP/SL management
TP1_RR            = 2.0     # Risk:Reward for TP1 (1:2)
TP2_RR            = 4.0     # Risk:Reward for TP2 (1:4)
TP1_CLOSE_PCT     = 0.50    # % of position closed at TP1 (50%)
MOVE_SL_TO_BE     = True    # Move stop loss to breakeven after TP1

# ============================================================
# Instruments
# ============================================================
GOLD_PAIR = "XAU_USD"

ALL_INSTRUMENTS = [
    "XAU_USD",   # Gold — primary
    "EUR_USD",
    "GBP_USD",
    "USD_JPY",
    "AUD_USD",
    "USD_CAD",
    "NZD_USD",
]

# Pip sizes (1 pip value)
PIP_SIZE = {
    "XAU_USD": 0.01,    # Gold: $0.01 per pip
    "EUR_USD": 0.0001,
    "GBP_USD": 0.0001,
    "USD_JPY": 0.01,
    "AUD_USD": 0.0001,
    "USD_CAD": 0.0001,
    "NZD_USD": 0.0001,
}

# Max allowable spread before skipping a trade
MAX_SPREAD_PIPS = {
    "XAU_USD": 5.0,
    "EUR_USD": 2.0,
    "GBP_USD": 2.5,
    "USD_JPY": 2.0,
    "AUD_USD": 2.5,
    "USD_CAD": 2.5,
    "NZD_USD": 3.0,
}

# SL size limits (pips)
GOLD_MAX_SL_PIPS  = 200.0
GOLD_MIN_SL_PIPS  = 10.0
FOREX_MAX_SL_PIPS = 80.0
FOREX_MIN_SL_PIPS = 5.0

# Gold position sizing safety reduction (OANDA uses oz for XAU_USD)
GOLD_SIZE_REDUCTION = 1.0   # set < 1.0 to conservatively reduce gold lot size

# ============================================================
# Kill Zones (UTC hours) — bot only trades inside these windows
# ============================================================
REQUIRE_KILL_ZONE = True   # Set False to trade 24h

KILL_ZONES = [
    {"name": "London",    "start":  7, "end": 10},
    {"name": "NY Open",   "start": 12, "end": 15},
    {"name": "Asian SB",  "start":  3, "end":  4},
    {"name": "London SB", "start": 10, "end": 11},
    {"name": "NY SB",     "start": 14, "end": 15},
]

# ============================================================
# Strategy toggles & minimum scores
# ============================================================
STRATEGY_ENABLED = {
    "S1_ASIAN_RANGE_SWEEP":       True,
    "S2_NY_OPEN_KILLSHOT":        True,
    "S3_OB_PSYCHOLOGICAL_LEVELS": True,
    "S4_WEEKLY_PROFILE":          True,
    "S5_FVG_RETRACEMENT":         True,
    "S6_POWER_OF_3":              True,
    "S7_SILVER_BULLET":           True,
    "S_FOREX_LQ_SWEEP":           True,
}

STRATEGY_MIN_SCORE = 6   # 0–10 score; signals below this are discarded

# ============================================================
# Strategy-specific parameters
# ============================================================

# S1 — Asian Range Sweep
ASIAN_RANGE_MAX_PIPS   = 150.0    # ignore ranges wider than this
ASIAN_RANGE_MAX_DRIFT  = 0.3      # max body drift ratio in range
ASIAN_SWEEP_WINDOW_MIN = 60       # minutes into London session to look for sweep
ASIAN_SWEEP_MIN_PIPS   = 5.0      # minimum sweep distance in pips

# S2 — NY Open Killshot
NY_OPEN_SETUP_WINDOW_MIN   = 60   # minutes into NY session for setup
NY_MIN_SWEEP_PIPS          = 5.0
NY_MIN_DISPLACEMENT_PIPS   = 8.0

# S3 — OB + Psychological Levels
PSYCH_LEVEL_PROXIMITY = 0.0015    # distance from psych level to qualify (price units)
OB_MAX_AGE_CANDLES_4H = 20        # max age of order block in 4H candles
OB_MAX_VISITS         = 2         # OB invalidated after being touched N times

PSYCH_LEVELS_GOLD = [
    # Major gold psychological levels ($50 increments)
    1800, 1850, 1900, 1950, 2000, 2050, 2100, 2150, 2200, 2250,
    2300, 2350, 2400, 2450, 2500, 2550, 2600, 2650, 2700, 2750,
    2800, 2850, 2900, 2950, 3000, 3050, 3100, 3150, 3200, 3250,
    3300, 3350, 3400, 3450, 3500,
]

# S4 — Weekly Profile
WEEKLY_ENTRY_DAYS = [1, 2]   # Tuesday (1), Wednesday (2) — Mon=0
WEEKLY_SHORT_DAYS = [1, 2]

# S5 — FVG Retracement
FVG_TREND_MIN_CANDLES  = 3    # min trend candles before FVG qualifies
FVG_MIN_SIZE_PIPS_GOLD = 5.0  # min FVG size in pips for gold

# S6 — Power of 3
PO3_ACCUM_RANGE_MAX   = 100.0  # max pips for Asian accumulation range
PO3_ACCUM_DRIFT_MAX   = 0.4    # max drift ratio
PO3_JUDAS_TIMING_MIN  = 7      # UTC hour when Judas swing can start
PO3_JUDAS_TIMING_MAX  = 10     # UTC hour when Judas swing window ends

# S7 — Silver Bullet
SB_WINDOWS = [
    {"name": "Asian SB",   "start_utc": 3,  "end_utc": 4},
    {"name": "London SB",  "start_utc": 10, "end_utc": 11},
    {"name": "NY SB",      "start_utc": 14, "end_utc": 15},
]
SB_MAX_SETUPS_PER_WINDOW = 1   # only 1 SB trade per time window per day

# ============================================================
# ICT/SMC Detection thresholds (used in detector_core.py)
# ============================================================
EQUAL_LEVEL_TOLERANCE   = 0.0003   # price proximity to consider levels "equal"
MIN_SWEEP_DISTANCE      = 0.0002   # minimum sweep beyond level to qualify
DISPLACEMENT_BODY_PCT   = 0.7      # candle body / range ratio to confirm displacement
MIN_DISPLACEMENT_BODY   = 0.0005   # minimum body size for displacement candle
MIN_FVG_SIZE            = 0.0002   # minimum FVG gap size
FVG_EXPIRY_CANDLES      = 50       # FVG expires after N candles
SWING_LOOKBACK          = 5        # pivot high/low lookback window
SL_BUFFER_PIPS          = 3        # extra pip buffer added to stop loss

# ============================================================
# Candle timeframe configuration
# ============================================================
CANDLE_CONFIG = {
    "W":   {"label": "Weekly",  "count": 12},
    "D":   {"label": "Daily",   "count": 30},
    "H4":  {"label": "4H",      "count": 60},
    "H1":  {"label": "1H",      "count": 100},
    "M15": {"label": "15M",     "count": 200},
    "M5":  {"label": "5M",      "count": 100},
}

# ============================================================
# Bot loop timing
# ============================================================
CHECK_INTERVAL_SECONDS = 60   # seconds between each scan cycle

# ============================================================
# File paths
# ============================================================
LOG_DIR         = os.path.join(os.path.dirname(__file__), "data")
DB_FILE         = os.path.join(LOG_DIR, "trades.db")
STATE_FILE      = os.path.join(LOG_DIR, "bot_state.json")
TRADE_LOG_FILE  = os.path.join(LOG_DIR, "trades.csv")
SIGNAL_LOG_FILE = os.path.join(LOG_DIR, "signals.csv")
