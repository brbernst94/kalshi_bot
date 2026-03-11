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
STARTING_BANKROLL_USD    = 170.00
MAX_SINGLE_POSITION_PCT  = 0.25    # Up to 25% on one high-conviction trade
MAX_DAILY_LOSS_PCT       = 0.20    # Hard stop if down 20% in a day
MAX_SINGLE_POSITION_PCT  = 0.20   # Max 20% per position
MAX_DAILY_LOSS_PCT       = 0.50    # Hard stop if down 50% in a day
MAX_OPEN_POSITIONS       = 20      # Allow up to 20 simultaneous positions
# Minimum NET edge after fees (need more than 1% just to break even)
MIN_NET_EDGE             = 0.02   # 3.5% net edge minimum

# ── Strategy-specific ─────────────────────────────────────────────────────────

# Bond: near-certain YES contracts
BOND_MIN_PRICE_CENTS  = 65         # Buy YES ≥ 65¢
BOND_MAX_DAYS         = 90         # Resolve within 90 days
BOND_MAX_POSITION_PCT = 0.25

# Whale following
WHALE_MIN_CONTRACTS      = 50
WHALE_COPY_DELAY_SECS    = 45
WHALE_MAX_COPY_FRAC      = 0.20
WHALE_MIN_WIN_RATE       = 0.65
# ↓ POPULATE THIS — go to kalshi.com/leaderboard, sort by "Profit", copy the
#   username/member-ID from the URL of each top trader's profile page.
#   e.g. if profile URL is kalshi.com/profile/traderguy123, add "traderguy123"
#   Tracked members bypass the sports/category filter and get better sizing.
TRACKED_WHALE_MEMBERS    = [
    # "member_id_1",
    # "member_id_2",
    # "member_id_3",
]

# Asymmetric longshot
LONGSHOT_MAX_PRICE_CENTS = 20      # Buy YES ≤ 20¢ (5x+ payout)
LONGSHOT_MIN_PRICE_CENTS = 2
LONGSHOT_MIN_OPEN_INT    = 0       # Removed — open_interest is always 0 in list data
LONGSHOT_MAX_POS_PCT     = 0.08

# Fade / overcorrection
FADE_SPIKE_CENTS         = 8      # Only fade moves ≥ 15¢ in 1 hour
FADE_CONFIRMATION_HOURS  = 0.5
FADE_MAX_POS_PCT         = 0.22

# ── Strategy Allocation ───────────────────────────────────────────────────────
STRATEGY_ALLOCATION = {
    "whale":       0.30,   # Whale copy — working, non-sports only
    "bond":        0.25,   # Near-certainty favorites — research: 90-99¢ wins > priced
    "datarelease": 0.20,   # CPI/Fed/NFP pre-release — highest edge per Fed research
    "fade":        0.15,   # Overcorrection fades
    "longshot":    0.10,   # Fade overpriced YES longshots (buy NO, maker order)
}

# ── Scheduling ────────────────────────────────────────────────────────────────
WHALE_SCAN_MINS    = 3
FADE_SCAN_MINS     = 3
BOND_SCAN_MINS     = 3
LONGSHOT_SCAN_MINS = 3
MONITOR_SCAN_MINS  = 3

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL      = "DEBUG"
LOG_FILE       = "logs/bot.log"
TRADE_LOG_FILE = "logs/trades.csv"

# ── Target ────────────────────────────────────────────────────────────────────
MONTHLY_TARGET_USD = 5_000
