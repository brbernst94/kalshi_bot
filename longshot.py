"""
strategies/longshot.py — Asymmetric Longshot Strategy (Kalshi)
===============================================================
Buy YES contracts priced ≤ 14¢ in high-fertility categories.
Each contract pays $1.00 if correct → minimum 7x return.

Kalshi-specific advantage over Polymarket:
  - Resolution is CFTC-enforced with clear criteria — no oracle ambiguity
  - No wash-trading noise inflating prices
  - Economic/policy markets (CPI, Fed rate, employment) have data-driven edges
    that sophisticated traders haven't fully priced in niche events
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import (
    KALSHI_TAKER_FEE_PCT, LONGSHOT_MAX_POS_PCT,
    LONGSHOT_MAX_PRICE_CENTS, LONGSHOT_MIN_OPEN_INT,
    LONGSHOT_MIN_PRICE_CENTS, STRATEGY_ALLOCATION,
)
from bond import days_to_close

logger = logging.getLogger(__name__)

# Kalshi category keywords → historical longshot fertility score
CATEGORY_SCORES = {
    "election":    0.90,
    "president":   0.85,
    "congress":    0.80,
    "fed":         0.75,   # FOMC rate decisions — experts have edge
    "cpi":         0.78,   # Inflation data — economists have edge
    "employment":  0.72,
    "bitcoin":     0.68,
    "crypto":      0.65,
    "supreme":     0.80,   # Supreme Court — lawyers have edge
    "arrest":      0.95,   # Geopolitical discontinuity
    "resign":      0.78,
    "impeach":     0.82,
}


def _category_score(market: Dict) -> float:
    text = (market.get("title", "") + " " +
            market.get("subtitle", "") + " " +
            str(market.get("tags", []))).lower()
    best = 0.3
    for kw, score in CATEGORY_SCORES.items():
        if kw in text:
            best = max(best, score)
    return best


def _price_momentum(history: List[Dict]) -> float:
    """Positive momentum = price rising = someone accumulating."""
    if len(history) < 4:
        return 0.0
    prices = [int(h.get("yes_price", 50)) for h in history if h.get("yes_price")]
    if len(prices) < 4:
        return 0.0
    mid    = len(prices) // 2
    recent = sum(prices[mid:]) / len(prices[mid:])
    early  = sum(prices[:mid]) / len(prices[:mid])
    if early < 1:
        return 0.0
    change = (recent - early) / early
    return min(max(change, 0) * 3, 1.0)


def scan(client, risk_manager, markets=None) -> List[Dict]:
    logger.info("[LONGSHOT] Scanning Kalshi for asymmetric opportunities...")
    candidates = []

    if markets is None:
        try:
            markets = client.get_all_open_markets()
        except Exception as e:
            logger.error(f"Market fetch failed: {e}")
            return []

    open_markets = [m for m in markets if m.get("status") == "open"]

    # Pass 1: filter by category score using cheap list data (title/tags available)
    cat_filtered = [m for m in open_markets if _category_score(m) >= 0.50]
    logger.info(f"[LONGSHOT] {len(cat_filtered)} markets pass category filter")

    import time as _time
    for m in cat_filtered:
        ticker = m.get("ticker", "")

        # Fetch individual market for price data
        try:
            detail   = client.get_market(ticker)
            md       = detail.get("market", detail)
        except Exception as e:
            logger.debug(f"[LONGSHOT] Detail fetch failed {ticker}: {e}")
            continue

        yes_ask = None
        for field in ("yes_ask", "yes_bid", "last_price"):
            v = md.get(field)
            if v is not None:
                try:
                    yes_ask = int(v)
                    if yes_ask > 0:
                        break
                except Exception:
                    continue

        if yes_ask is None or yes_ask == 0:
            _time.sleep(0.05)
            continue
        if not (LONGSHOT_MIN_PRICE_CENTS <= yes_ask <= LONGSHOT_MAX_PRICE_CENTS):
            _time.sleep(0.05)
            continue

        open_int = int(md.get("open_interest", m.get("open_interest", 0)) or 0)
        if open_int < LONGSHOT_MIN_OPEN_INT:
            _time.sleep(0.05)
            continue

        days = days_to_close(md) or days_to_close(m)
        if days and days < 1:
            _time.sleep(0.05)
            continue

        cat_score = _category_score(md)

        try:
            history  = client.get_market_history(ticker)
            momentum = _price_momentum(history)
        except Exception:
            momentum = 0.0

        cat_premium = (cat_score - 0.5) * 0.3
        our_prob    = min((yes_ask / 100) * (1 + cat_premium) + momentum * 0.05, 0.40)
        payout_mult = 100 / yes_ask
        ev          = our_prob * payout_mult - 1.0 - KALSHI_TAKER_FEE_PCT

        if ev < 0.10:
            _time.sleep(0.05)
            continue

        candidates.append({
            "ticker":        ticker,
            "title":         md.get("title", m.get("title", ""))[:80],
            "yes_price":     yes_ask,
            "payout_mult":   round(payout_mult, 1),
            "our_prob":      round(our_prob, 3),
            "ev":            round(ev, 3),
            "cat_score":     cat_score,
            "momentum":      momentum,
            "open_interest": open_int,
        })
        _time.sleep(0.05)

    candidates.sort(key=lambda x: x["ev"] * x["cat_score"], reverse=True)
    logger.info(f"[LONGSHOT] {len(candidates)} candidates")
    for c in candidates[:5]:
        logger.info(
            f"  ↳ {c['title'][:55]} | {c['yes_price']}¢ "
            f"| {c['payout_mult']}x | ev={c['ev']:.2%} | cat={c['cat_score']:.2f}"
        )
    return candidates


def execute(client, risk_manager, candidates: List[Dict]) -> int:
    trades = 0
    try:
        balance = client.get_balance()
    except Exception:
        from config import STARTING_BANKROLL_USD
        balance = STARTING_BANKROLL_USD

    strat_budget = balance * STRATEGY_ALLOCATION["longshot"]

    for c in candidates[:4]:
        per_trade = min(balance * LONGSHOT_MAX_POS_PCT, strat_budget / 3)
        count     = client.contracts_for_budget(per_trade, c["yes_price"])
        cost      = client.cost_usd(count, c["yes_price"])

        if cost < 1.0:
            continue

        if not risk_manager.approve("longshot", c["ticker"], cost, c["ev"],
                                     notes=c["title"][:45]):
            continue

        try:
            client.place_limit_order(
                ticker=c["ticker"],
                side="yes",
                action="buy",
                price_cents=min(c["yes_price"] + 1, LONGSHOT_MAX_PRICE_CENTS + 2),
                count=count,
            )
            risk_manager.record_open(c["ticker"], count, c["yes_price"], "longshot")
            risk_manager.log_trade(
                strategy="longshot", ticker=c["ticker"],
                side="yes", action="buy",
                price_cents=c["yes_price"], count=count,
                expected_pnl=cost * c["ev"],
                notes=f"{c['payout_mult']}x ev={c['ev']:.2%}"
            )
            trades += 1
            strat_budget -= cost
        except Exception as e:
            logger.error(f"[LONGSHOT] Order failed {c['ticker']}: {e}")

    logger.info(f"[LONGSHOT] Placed {trades} trade(s)")
    return trades
