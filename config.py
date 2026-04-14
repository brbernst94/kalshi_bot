"""
Kalshi Trading Bot — Configuration
====================================
Kalshi is CFTC-regulated. Key differences from Polymarket:
  - RSA-PSS request signing (not HMAC)
  - Prices in CENTS (1–99), not dollars (0.01–0.99)
  - Orders sized by CONTRACT COUNT, not USDC amount
  - ~1% taker fee baked into every edge calculation
  - Settled in USD via ACH, not USDC on-chain
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Credentials ───────────────────────────────────────────────────────────────
KALSHI_API_KEY_ID    = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY   = os.getenv("KALSHI_PRIVATE_KEY", "")

USE_DEMO = os.getenv("USE_DEMO", "false").lower() == "true"

# ── API Endpoints ─────────────────────────────────────────────────────────────
if USE_DEMO:
    BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
else:
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# ── Kalshi market structure ───────────────────────────────────────────────────
KALSHI_TAKER_FEE_PCT   = 0.01
KALSHI_MAKER_FEE_PCT   = 0.0
CONTRACT_VALUE_USD     = 1.00

# ── Capital & Risk ────────────────────────────────────────────────────────────
STARTING_BANKROLL_USD    = 1000.00
MAX_SINGLE_POSITION_PCT  = 0.20
MAX_DAILY_LOSS_PCT       = 0.10
MAX_OPEN_POSITIONS       = 20
MIN_NET_EDGE             = 0.02

# ── Stop / Take profit (global, used by monitor.py) ──────────────────────────
STOP_LOSS_PCT   = 0.10   # Exit if price drops 10% from entry
TAKE_PROFIT_PCT = 0.70   # Exit if price gains 70% from entry

# ── Time horizon ──────────────────────────────────────────────────────────────
MAX_POSITION_DAYS = 3    # No position beyond 3 days; cleanup exits any older ones

# ── Whale following ───────────────────────────────────────────────────────────
WHALE_MIN_CONTRACTS      = 50
WHALE_COPY_DELAY_SECS    = 45
WHALE_MAX_COPY_FRAC      = 0.20
WHALE_MIN_WIN_RATE       = 0.65
TRACKED_WHALE_MEMBERS    = [
    # "member_id_1",
]

# ── Strategy Allocation ───────────────────────────────────────────────────────
STRATEGY_ALLOCATION = {
    "datarelease": 0.30,
    "weather":     0.30,
    "whale":       0.15,
    "momentum":    0.10,
    "favbias":     0.15,
}

# ── Scheduling ────────────────────────────────────────────────────────────────
MONITOR_SCAN_MINS     = 3
DATARELEASE_SCAN_MINS = 5
WEATHER_SCAN_MINS     = 10
WHALE_SCAN_MINS       = 5
MOMENTUM_SCAN_MINS    = 5
FAVBIAS_SCAN_MINS     = 5

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL      = "INFO"
LOG_FILE       = "logs/bot.log"
TRADE_LOG_FILE = "logs/trades.csv"

# ── Target ────────────────────────────────────────────────────────────────────
MONTHLY_TARGET_USD = 5_000
