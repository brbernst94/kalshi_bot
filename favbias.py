"""
favbias.py — Favourite Bias Strategy (Kalshi)
===============================================
Detects when high-volume markets are mispriced vs known base rates.
The crowd systematically overbets popular/favourite outcomes — we fade
that bias by buying the underpriced side.

Bidirectional:
  - Crowd overbets YES above base rate  → buy NO
  - Crowd undersells YES below base rate → buy YES

Markets covered: crypto 15m, political mentions, elections, government
shutdown, presidential, Trump, Congress.

Edge: 79% WR on weather (similar base-rate arbitrage). Same framework.
Scans every 5 minutes.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from bond import days_to_close
from client import price_cents as _pc
from config import (
    STRATEGY_ALLOCATION, MAX_POSITION_DAYS,
    STARTING_BANKROLL_USD, MAX_SINGLE_POSITION_PCT,
)

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
MIN_VOLUME_24H   = 5000   # Only trade high-liquidity markets
MIN_EDGE_CENTS   = 8      # Skip if divergence < 8¢ from base rate
MAX_CONTRACTS    = 50     # Per-trade cap
MIN_PRICE_CENTS  = 5
MAX_PRICE_CENTS  = 94
MAX_DAYS_OUT     = 3      # Respect global horizon

# ── Base rates ────────────────────────────────────────────────────────────────
# Empirical base rates for each market prefix.
# YES_base = historical frequency that YES resolves (before crowd bias).
# Crowd systematically pushes popular outcomes above these rates.
BASE_RATES: Dict[str, float] = {
    # Crypto 15-min: direction is near-random, base rate ≈ 50%
    "KXBTC15M":   0.50,
    "KXETH15M":   0.50,
    "KXXRP15M":   0.50,
    "KXSOL15M":   0.50,

    # Presidential/political mentions: incumbents mentioned more → crowds
    # overbet YES (mentioned) — real rate closer to 55%
    "KXPRESMENTION": 0.55,
    "KXMENTION":     0.55,

    # Elections: favourite bias is strong — crowds push front-runners 5-10¢ high
    "KXELECTION":  0.50,   # binary race — true 50/50 before info
    "KXPRES":      0.50,

    # Government shutdown: bias toward NO (gov stays open) — crowds underbet YES
    "KXGOVTSHUT":  0.25,   # shutdowns happen ~25% of deadline events historically

    # Trump/Congress: high name recognition inflates YES prices
    "KXTRUMP":     0.50,
    "KXCONGRESS":  0.50,
}

# ── Bias threshold ────────────────────────────────────────────────────────────
# Crowd must push price this many cents ABOVE base rate to trade
BIAS_THRESHOLD_CENTS = 8   # Same as MIN_EDGE_CENTS


def _get_base_rate(ticker: str) -> Optional[float]:
    """Return base rate for this ticker's prefix, or None if not covered."""
    for prefix, rate in BASE_RATES.items():
        if ticker.startswith(prefix):
            return rate
    return None


def scan(client, risk_manager, markets: List[Dict]) -> List[Dict]:
    """
    Scan open markets for favourite bias mispricing.
    Returns list of candidate trades.
    """
    candidates = []
    balance    = STARTING_BANKROLL_USD
    try:
        balance = client.get_balance()
    except Exception:
        pass

    max_cost = balance * MAX_SINGLE_POSITION_PCT * STRATEGY_ALLOCATION.get("whale", 1.0)

    for m in markets:
        ticker = m.get("ticker", "")
        if not ticker:
            continue

        # Only trade markets in our coverage list
        base_rate = _get_base_rate(ticker)
        if base_rate is None:
            continue

        # Skip if already holding this ticker
        if ticker in risk_manager.open_positions:
            continue

        # Volume filter — only liquid markets
        volume = m.get("volume_24h") or m.get("volume", 0)
        try:
            volume = float(volume)
        except (TypeError, ValueError):
            volume = 0
        if volume < MIN_VOLUME_24H:
            continue

        # Time horizon filter
        days = days_to_close(m)
        if days is None or days > MAX_DAYS_OUT or days < 0:
            continue

        # Get current market price
        try:
            yes_bid, yes_ask = client.get_best_bid_ask(ticker)
        except Exception:
            continue

        if yes_bid is None and yes_ask is None:
            continue

        # Use mid-price (or available side)
        if yes_bid and yes_ask:
            mid = (yes_bid + yes_ask) / 2
        else:
            mid = yes_bid or yes_ask

        if not mid:
            continue

        base_cents = round(base_rate * 100)
        divergence = mid - base_cents   # +ve = crowd overbets YES, -ve = undersells

        # Need at least BIAS_THRESHOLD_CENTS of divergence
        if abs(divergence) < BIAS_THRESHOLD_CENTS:
            continue

        # Determine trade direction
        if divergence > 0:
            # Crowd overbets YES → buy NO (YES overpriced)
            side       = "no"
            entry_px   = 100 - mid   # NO price
            edge_cents = divergence
        else:
            # Crowd undersells YES → buy YES (YES underpriced)
            side       = "yes"
            entry_px   = mid
            edge_cents = abs(divergence)

        entry_px = round(entry_px)
        if not (MIN_PRICE_CENTS <= entry_px <= MAX_PRICE_CENTS):
            continue

        count = min(MAX_CONTRACTS, max(1, int(max_cost * 100 / max(entry_px, 1))))
        if count < 1:
            continue

        candidates.append({
            "ticker":     ticker,
            "side":       side,
            "entry_px":   entry_px,
            "count":      count,
            "base_rate":  base_rate,
            "mid":        mid,
            "edge_cents": edge_cents,
            "days":       days,
            "strategy":   "favbias",
        })

        logger.info(
            f"[FAVBIAS] SIGNAL | {ticker} | {side.upper()} @ {entry_px}¢ | "
            f"mid={mid:.0f}¢ base={base_cents}¢ edge={edge_cents:+.0f}¢ | "
            f"vol={volume:.0f} {days:.1f}d"
        )

    logger.info(f"[FAVBIAS] {len(candidates)} candidate(s) found")
    return candidates


def execute(client, risk_manager, candidates: List[Dict]) -> None:
    """Place limit orders for all candidates."""
    for c in candidates:
        ticker   = c["ticker"]
        side     = c["side"]
        entry_px = c["entry_px"]
        count    = c["count"]

        if ticker in risk_manager.open_positions:
            continue

        if not risk_manager.can_open_position(ticker, entry_px, count):
            logger.debug(f"[FAVBIAS] Risk check failed for {ticker}")
            continue

        try:
            client.place_limit_order(
                ticker=ticker, side=side, action="buy",
                price_cents=entry_px, count=count,
                post_only=True,
            )
            risk_manager.record_open(
                ticker=ticker, strategy="favbias", side=side,
                entry_cents=entry_px, count=count,
            )
            logger.info(
                f"[FAVBIAS] ORDER | {ticker} | {side.upper()} x{count} @ {entry_px}¢ | "
                f"edge={c['edge_cents']:+.0f}¢ base={c['base_rate']:.0%}"
            )
            time.sleep(0.3)
        except Exception as e:
            logger.error(f"[FAVBIAS] Order failed {ticker}: {e}")
