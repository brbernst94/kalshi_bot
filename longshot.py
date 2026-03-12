"""
strategies/longshot.py — Longshot Fade Strategy (Kalshi)
=========================================================
STRATEGY PIVOT based on academic research (Bürgi et al. 2025, 300k contracts):

  ORIGINAL PLAN: Buy YES contracts priced 2-20¢ (longshots)
  RESEARCH FINDING: These contracts WIN FAR LESS than price implies.
    Average taker loss on longshots: ~32%. Structural and persistent.

  NEW PLAN: FADE the longshot bias by buying NO on overpriced cheap YES markets.
    We post as MAKER → zero fees.
    Statistical edge from bias working FOR us.

  TARGET: Markets priced 3-20¢ YES in entertainment/tech/culture categories
    where longshot bias is strongest.
  METHOD: Buy NO (limit order, post_only) at implied no_price → 0 fee.
  EDGE: Research shows YES longshots win ~35% less than priced → NO wins more.
"""

import logging
import random
from typing import Dict, List

from config import (
    LONGSHOT_MAX_PRICE_CENTS, LONGSHOT_MIN_PRICE_CENTS,
    LONGSHOT_MAX_POS_PCT, STRATEGY_ALLOCATION,
)
from bond import days_to_close
from client import price_cents as _pc

logger = logging.getLogger(__name__)

# Categories with strongest longshot overpricing (entertainment, celeb, tech milestones)
STRONG_BIAS_CATEGORIES = {
    "entertainment": 0.90, "award": 0.88, "oscar": 0.88, "grammy": 0.85,
    "celebrity":     0.80, "viral": 0.78, "pop culture": 0.75,
    "billboard":     0.80, "streaming": 0.72, "chart": 0.75,
    "spacex":        0.75, "launch": 0.68, "ipo": 0.72,
    "acquisition":   0.70, "merger": 0.68, "tech": 0.65,
    "model":         0.70, "top ":  0.65,
}


def _fade_score(market: Dict) -> float:
    text = (market.get("title", "") + " " +
            market.get("subtitle", "") + " " +
            str(market.get("tags", []))).lower()
    best = 0.3
    for kw, score in STRONG_BIAS_CATEGORIES.items():
        if kw in text:
            best = max(best, score)
    return best


def scan(client, risk_manager, markets=None) -> List[Dict]:
    logger.info("[LONGSHOT] Scanning for overpriced YES longshots to fade...")
    candidates = []

    if markets is None:
        try:
            markets = client.get_all_open_markets()
        except Exception as e:
            logger.error(f"Market fetch failed: {e}")
            return []

    open_markets = list(markets)
    SKIP_PREFIXES = ("KXMVE", "KXNCAAMB", "KXUCLGAME", "KXNCAAFB",
                     "KXNBA", "KXNFL", "KXMLB", "KXNHL", "KXMLS")
    open_markets = [m for m in open_markets
                    if not m.get("ticker", "").startswith(SKIP_PREFIXES)]

    bias_filtered = [m for m in open_markets if _fade_score(m) >= 0.4]
    logger.info(f"[LONGSHOT] {len(bias_filtered)} markets in longshot-bias categories")

    if len(bias_filtered) > 40:
        random.shuffle(bias_filtered)
        bias_filtered = bias_filtered[:40]

    for m in bias_filtered:
        ticker = m.get("ticker", "")

        # Use price_cents helper — handles _dollars strings (new) and integer cents (old)
        yes_ask = _pc(m, "yes_ask") or _pc(m, "last_price") or _pc(m, "yes_bid")

        if yes_ask is None or yes_ask == 0:
            continue
        if not (LONGSHOT_MIN_PRICE_CENTS <= yes_ask <= LONGSHOT_MAX_PRICE_CENTS):
            continue

        days = days_to_close(m)
        if days is None or days < 0.5 or days > 60:
            continue

        fade_score   = _fade_score(m)
        no_price     = 100 - yes_ask
        true_yes_prob = (yes_ask / 100) * 0.65  # 35% discount for longshot bias
        our_edge      = (1 - true_yes_prob) - (no_price / 100)
        ev = our_edge

        if ev < 0.02:
            continue

        candidates.append({
            "ticker":    ticker,
            "title":     m.get("title", "")[:80],
            "yes_price": yes_ask,
            "no_price":  no_price,
            "fade_score": fade_score,
            "ev":        round(ev, 3),
            "days":      days,
        })

    candidates.sort(key=lambda x: x["ev"] * x["fade_score"], reverse=True)
    logger.info(f"[LONGSHOT] {len(candidates)} fade candidates")
    for c in candidates[:5]:
        logger.info(
            f"  ↳ FADE {c['title'][:50]} | YES@{c['yes_price']}¢ "
            f"→ BUY NO@{c['no_price']}¢ | ev={c['ev']:.2%} | {c['days']:.1f}d"
        )
    return candidates


def execute(client, risk_manager, candidates: List[Dict]) -> int:
    trades = 0
    try:
        balance = client.get_balance()
    except Exception:
        from config import STARTING_BANKROLL_USD
        balance = STARTING_BANKROLL_USD

    strat_budget = balance * STRATEGY_ALLOCATION.get("longshot", 0.15)

    for c in candidates[:4]:
        per_trade = min(balance * LONGSHOT_MAX_POS_PCT, strat_budget / 3)
        count     = client.contracts_for_budget(per_trade, c["no_price"])
        cost      = client.cost_usd(count, c["no_price"])

        if cost < 1.0:
            continue

        if not risk_manager.approve("longshot", c["ticker"], cost, c["ev"],
                                    notes=c["title"][:45]):
            continue

        try:
            client.place_limit_order(
                ticker=c["ticker"], side="no", action="buy",
                price_cents=c["no_price"], count=count, post_only=True,
            )
            risk_manager.record_open(c["ticker"], count, c["no_price"], "longshot", side="no")
            risk_manager.log_trade(
                strategy="longshot", ticker=c["ticker"],
                side="no", action="buy",
                price_cents=c["no_price"], count=count,
                expected_pnl=cost * c["ev"],
                notes=f"fade YES@{c['yes_price']}¢ ev={c['ev']:.2%}"
            )
            trades += 1
            strat_budget -= cost
        except Exception as e:
            logger.error(f"[LONGSHOT] Order failed {c['ticker']}: {e}")

    logger.info(f"[LONGSHOT] Placed {trades} fade trade(s)")
    return trades
