"""
Kalshi Trading Bot — Configuration
====================================
Kalshi is CFTC-regulated. Key differences from Polymarket:
  - RSA-PSS request signing (not HMAC)
  - Prices in CENTS (1–99), not dollars (0.01–0.99)
  - Orders sized by CONTRACT COUNT, not USDC amount
  - ~1% taker fee baked into every edge calculation
  - Settled in USD via ACH, not USDC on-chain
  - Demo environment available at demo-api.kalshi.co
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Credentials ───────────────────────────────────────────────────────────────
KALSHI_API_KEY_ID    = os.getenv("KALSHI_API_KEY_ID", "")      # UUID from profile
KALSHI_PRIVATE_KEY   = os.getenv("KALSHI_PRIVATE_KEY", "")     # PEM string or path

# Set USE_DEMO=true to paper trade on demo-api.kalshi.co first
USE_DEMO = os.getenv("USE_DEMO", "false").lower() == "true"

# ── API Endpoints ─────────────────────────────────────────────────────────────
if USE_DEMO:
    BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
else:
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# ── Kalshi-specific market structure ─────────────────────────────────────────
# Prices on Kalshi are in CENTS (integer 1–99)
# 1 contract = $0.01 × price_cents at risk, pays $1.00 if correct
# e.g. buying 10 YES contracts at 45¢ costs $4.50, pays $10.00 if YES resolves
KALSHI_TAKER_FEE_PCT   = 0.01      # ~1% taker fee — must beat this for any trade
KALSHI_MAKER_FEE_PCT   = 0.0       # Maker orders (limit) currently 0% fee
CONTRACT_VALUE_USD     = 1.00      # Each contract pays $1.00 at resolution

# ── Capital & Risk ─────────────────────────────────────────────────────────────
STARTING_BANKROLL_USD    = 500.00
MAX_SINGLE_POSITION_PCT  = 0.25    # Up to 25% on one high-conviction trade
MAX_DAILY_LOSS_PCT       = 0.20    # Hard stop if down 20% in a day
MAX_OPEN_POSITIONS       = 4       # Concentrated book

# Minimum NET edge after fees (need more than 1% just to break even)
MIN_NET_EDGE             = 0.035   # 3.5% net edge minimum

# ── Strategy-specific ─────────────────────────────────────────────────────────

# Bond: near-certain YES contracts
BOND_MIN_PRICE_CENTS  = 90         # Only buy YES ≥ 90¢
BOND_MAX_DAYS         = 14         # Resolve within 14 days
BOND_MAX_POSITION_PCT = 0.25

# Whale following
WHALE_MIN_CONTRACTS      = 200     # Only copy trades of ≥ 200 contracts
WHALE_COPY_DELAY_SECS    = 45
WHALE_MAX_COPY_FRAC      = 0.20    # Copy at 20% of whale's contract count
WHALE_MIN_WIN_RATE       = 0.65
TRACKED_WHALE_MEMBERS    = []      # Kalshi member IDs — populate from leaderboard

# Asymmetric longshot
LONGSHOT_MAX_PRICE_CENTS = 14      # Buy YES ≤ 14¢ (7x+ payout)
LONGSHOT_MIN_PRICE_CENTS = 2
LONGSHOT_MIN_OPEN_INT    = 500     # Needs real open interest
LONGSHOT_MAX_POS_PCT     = 0.08

# Fade / overcorrection
FADE_SPIKE_CENTS         = 15      # Only fade moves ≥ 15¢ in 1 hour
FADE_CONFIRMATION_HOURS  = 0.5
FADE_MAX_POS_PCT         = 0.22

# ── Strategy Allocation ───────────────────────────────────────────────────────
STRATEGY_ALLOCATION = {
    "whale":    0.35,
    "fade":     0.25,
    "bond":     0.20,
    "longshot": 0.20,
}

# ── Scheduling ────────────────────────────────────────────────────────────────
WHALE_SCAN_MINS    = 5
FADE_SCAN_MINS     = 8
BOND_SCAN_MINS     = 30
LONGSHOT_SCAN_MINS = 60
MONITOR_SCAN_MINS  = 10

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL      = "INFO"
LOG_FILE       = "logs/bot.log"
TRADE_LOG_FILE = "logs/trades.csv"

# ── Target ────────────────────────────────────────────────────────────────────
MONTHLY_TARGET_USD = 5_000
