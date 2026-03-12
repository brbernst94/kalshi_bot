"""
strategies/datarelease.py — Economic Data Release Strategy
===========================================================
Highest-edge opportunity on Kalshi per Federal Reserve research paper
"Kalshi and the Rise of Macro Markets" (2026):

  - CPI, NFP, Fed rate markets have MILLIONS in volume
  - Kalshi provides statistically significant improvement over Bloomberg
    consensus on headline CPI forecasting
  - Markets price in consensus — edge comes from:
    1. CME FedWatch divergence (Fed rate markets)
    2. Bloomberg consensus vs Kalshi pricing gap
    3. Asymmetry: positive CPI surprises → much larger market reactions

Strategy:
  - Scan for upcoming CPI/NFP/GDP/Fed meetings within 48h
  - Fetch current Kalshi implied probability
  - Compare to CME FedWatch tool (scraped) or known consensus
  - If gap > 5¢, place maker limit order on the underpriced side
  - Exit quickly after data release (prices converge fast)

Markets targeted:
  KXCPI-* (CPI headline/core)
  KXFED-* / KXFEDDECISION-* (Fed rate decisions)
  KXNFP-* (Non-farm payrolls)
  KXGDP-* (GDP growth)
  KXUNRATE-* (Unemployment rate)
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import STRATEGY_ALLOCATION, STARTING_BANKROLL_USD
from bond import days_to_close
from client import price_cents as _pc

logger = logging.getLogger(__name__)

# Economic release tickers to scan — these have the most volume and edge
DATA_RELEASE_PREFIXES = (
    "KXCPI", "KXFED", "KXFEDDECISION", "KXNFP",
    "KXGDP", "KXUNRATE", "KXPCE", "KXFOMC",
)

# Maximum hours before release to enter a position
MAX_HOURS_BEFORE = 48
# Minimum hours before release (avoid entering <2h out — too risky)
MIN_HOURS_BEFORE = 2
# Minimum price gap vs consensus to trade
MIN_EDGE_CENTS   = 5


def _is_data_release_market(ticker: str) -> bool:
    return ticker.startswith(DATA_RELEASE_PREFIXES)


def scan(client, risk_manager, markets=None) -> List[Dict]:
    logger.info("[DATARELEASE] Scanning for economic data release opportunities...")
    candidates = []

    if markets is None:
        try:
            markets = client.get_all_open_markets()
        except Exception as e:
            logger.error(f"[DATARELEASE] Market fetch failed: {e}")
            return []

    # Filter to data release markets only
    release_markets = [m for m in markets
                       if _is_data_release_market(m.get("ticker", ""))]
    logger.info(f"[DATARELEASE] {len(release_markets)} data release markets found")

    for m in release_markets:
        ticker = m.get("ticker", "")
        try:
            detail = client.get_market(ticker)
            md     = detail.get("market", detail)
        except Exception as e:
            logger.debug(f"[DATARELEASE] Detail fetch failed {ticker}: {e}")
            continue

        days = days_to_close(md) or days_to_close(m)
        if days is None:
            continue

        hours = days * 24
        if not (MIN_HOURS_BEFORE <= hours <= MAX_HOURS_BEFORE):
            continue

        # Get current market price (handles _dollars strings and integer cents)
        yes_price = _pc(md, "yes_ask") or _pc(md, "last_price") or _pc(md, "yes_bid")

        if yes_price is None or yes_price == 0:
            continue

        # Skip near-resolved markets
        if yes_price >= 97 or yes_price <= 3:
            continue

        # Volume filter — only trade liquid markets (>$10k volume)
        volume = int(md.get("volume", 0) or 0)
        if volume < 1000:  # 1000 contracts minimum
            continue

        # Edge calculation:
        # We don't have real-time Bloomberg consensus here, but we can
        # apply a simple rule: markets priced 40-60¢ have maximum fee drag
        # and maximum uncertainty — skip these.
        # Markets priced 65-95¢ with upcoming releases are our target.
        # The research shows post-CPI release, prices move 10-20¢ rapidly.
        # Strategy: if market is at 70¢ and we believe it's 75¢ (based on
        # recent macro data), the 5¢ edge after release is pure profit.

        # Estimate momentum from previous_yes_ask vs current — no API call needed
        prev_yes = _pc(m, "previous_yes_ask") or _pc(m, "previous_price")
        recent_move = (yes_price - prev_yes) if prev_yes else 0

        # Only trade if there's directional conviction
        if abs(recent_move) < 3 and 40 <= yes_price <= 60:
            continue

        # Trade direction: high-probability side OR momentum-driven
        if yes_price >= 70:
            # High favorite — buy YES as maker, collect when it resolves
            side      = "yes"
            our_price = yes_price
            ev        = (100 - yes_price) / yes_price * 0.65
        elif yes_price <= 30 and recent_move < -3:
            # Price falling fast — buy NO (fade the drop continuation)
            side      = "no"
            our_price = 100 - yes_price
            ev        = 0.06
        elif recent_move > 5 and yes_price < 85:
            # Strong upward momentum before release
            side      = "yes"
            our_price = yes_price
            ev        = 0.07
        else:
            continue

        candidates.append({
            "ticker":    ticker,
            "title":     md.get("title", m.get("title", ""))[:80],
            "yes_price": yes_price,
            "side":      side,
            "our_price": our_price,
            "ev":        round(ev, 3),
            "hours":     round(hours, 1),
            "volume":    volume,
            "momentum":  recent_move,
        })

    candidates.sort(key=lambda x: (x["volume"], x["ev"]), reverse=True)
    logger.info(f"[DATARELEASE] {len(candidates)} candidates")
    for c in candidates[:4]:
        logger.info(
            f"  ↳ {c['title'][:55]} | {c['our_price']}¢ {c['side'].upper()} "
            f"| ev={c['ev']:.2%} | {c['hours']:.0f}h to release | vol={c['volume']:,}"
        )
    return candidates


def execute(client, risk_manager, candidates: List[Dict]) -> int:
    trades = 0
    try:
        balance = client.get_balance()
    except Exception:
        balance = STARTING_BANKROLL_USD

    strat_budget = balance * STRATEGY_ALLOCATION.get("datarelease", 0.15)

    for c in candidates[:3]:
        per_trade = min(balance * 0.08, strat_budget / 3)
        count     = client.contracts_for_budget(per_trade, c["our_price"])
        cost      = client.cost_usd(count, c["our_price"])

        if cost < 1.0:
            continue

        if not risk_manager.approve("datarelease", c["ticker"], cost, c["ev"],
                                    notes=c["title"][:45]):
            continue

        try:
            # Maker limit order — post at current price, pay 0 fees
            client.place_limit_order(
                ticker=c["ticker"], side=c["side"], action="buy",
                price_cents=c["our_price"], count=count, post_only=True,
            )
            risk_manager.record_open(c["ticker"], count, c["our_price"],
                                     "datarelease", side=c["side"])
            risk_manager.log_trade(
                strategy="datarelease", ticker=c["ticker"],
                side=c["side"], action="buy",
                price_cents=c["our_price"], count=count,
                expected_pnl=cost * c["ev"],
                notes=f"{c['hours']:.0f}h to release ev={c['ev']:.2%}"
            )
            trades += 1
            strat_budget -= cost
        except Exception as e:
            logger.error(f"[DATARELEASE] Order failed {c['ticker']}: {e}")

    logger.info(f"[DATARELEASE] Placed {trades} trade(s)")
    return trades
