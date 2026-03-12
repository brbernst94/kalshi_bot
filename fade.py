"""
strategies/fade.py — Confirmed Fade / Overcorrection Reversal
==============================================================
Detects irrational price spikes in political/macro markets and fades them.

KEY ARCHITECTURAL CHANGE (March 2026):
  OLD: called get_market_history(ticker) for every market -> 500+ API calls/cycle
  NEW: tracks prices in-memory between cycles -> ZERO extra API calls

  We already have last_price from the market cache. Store it each cycle.
  On next cycle, compare new price to stored price. Spike = big move.

API migration (March 12 2026):
  integer cents fields (yes_ask, last_price) -> _dollars string fields
  Use client.price_cents(market, field) helper throughout.
"""

import logging
import time
from typing import Dict, List, Optional

from config import (
    FADE_CONFIRMATION_HOURS, FADE_MAX_POS_PCT,
    FADE_SPIKE_CENTS, KALSHI_TAKER_FEE_PCT, STRATEGY_ALLOCATION,
)
from bond import days_to_close
from client import price_cents

logger = logging.getLogger(__name__)

_prev_prices: Dict[str, int] = {}   # ticker -> last seen price in cents
_staged: Dict[str, Dict]     = {}   # ticker -> staged candidate awaiting confirmation

FADE_SKIP_PREFIXES = (
    "KXNCAAMB", "KXNCAAFB", "KXNCAAWB", "KXNBA", "KXNBAGAME",
    "KXNFL", "KXNFLGAME", "KXMLB", "KXMLS", "KXNHL",
    "KXWBC", "KXWBO", "KXWBA", "KXIBF", "KXUFC",
    "KXPGA", "KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGER",
    "KXUCLGAME", "KXCONCACAF", "KXNBA2KCOVER", "KXMLBNL",
)


def _get_market_price(m: dict) -> Optional[int]:
    for field in ("yes_ask", "last_price", "yes_bid", "previous_yes_ask"):
        v = price_cents(m, field)
        if v is not None:
            return v
    return None


def scan(client, risk_manager, markets=None) -> List[Dict]:
    global _prev_prices
    logger.info("[FADE] Scanning for overcorrections (in-memory, 0 API calls)...")
    candidates = []

    if markets is None:
        try:
            markets = client.get_all_open_markets()
        except Exception as e:
            logger.error(f"[FADE] Market fetch failed: {e}")
            return []

    tradeable = [m for m in markets
                 if not m.get("ticker", "").startswith(FADE_SKIP_PREFIXES)]
    logger.info(f"[FADE] {len(tradeable)} non-sports markets to scan")

    current_prices: Dict[str, int] = {}
    spike_markets = []

    for m in tradeable:
        ticker = m.get("ticker", "")
        if not ticker:
            continue
        current = _get_market_price(m)
        if current is None:
            continue
        current_prices[ticker] = current
        prev = _prev_prices.get(ticker)
        if prev is None:
            continue

        move = current - prev
        if abs(move) < FADE_SPIKE_CENTS:
            continue

        days = days_to_close(m)
        if days is None or days < 3:
            continue
        volume = int(m.get("volume", 0) or 0)
        if volume < 500:
            continue

        fade_side = "no" if move > 0 else "yes"
        edge = abs(move) / 100 - KALSHI_TAKER_FEE_PCT
        if edge < 0.04:
            continue

        spike_markets.append({
            "ticker":       ticker,
            "title":        m.get("title", "")[:80],
            "fade_side":    fade_side,
            "entry_cents":  current,
            "target_cents": prev,
            "move_cents":   move,
            "edge":         edge,
            "days":         days,
            "volume":       volume,
        })

    _prev_prices.update(current_prices)

    for spike in spike_markets:
        ticker = spike["ticker"]
        if ticker not in _staged:
            _staged[ticker] = {**spike, "staged_at": time.time()}
            logger.info(f"[FADE] Staged: {spike['title'][:50]} | move={spike['move_cents']:+d}c edge={spike['edge']:.1%}")
            continue

        staged  = _staged[ticker]
        elapsed = (time.time() - staged["staged_at"]) / 3600
        if elapsed < FADE_CONFIRMATION_HOURS:
            continue

        current_now = current_prices.get(ticker)
        if current_now is None or abs(current_now - staged["entry_cents"]) > 5:
            del _staged[ticker]
            continue

        staged["entry_cents"] = current_now
        candidates.append(staged)
        del _staged[ticker]
        logger.info(f"[FADE] CONFIRMED: {staged['title'][:50]}")

    stale = [t for t in list(_staged) if t not in current_prices]
    for t in stale:
        del _staged[t]

    candidates.sort(key=lambda x: x["edge"], reverse=True)
    logger.info(f"[FADE] {len(candidates)} confirmed fade(s) ready")
    return candidates


def execute(client, risk_manager, candidates: List[Dict]) -> int:
    trades = 0
    try:
        balance = client.get_balance()
    except Exception:
        from config import STARTING_BANKROLL_USD
        balance = STARTING_BANKROLL_USD

    strat_budget = balance * STRATEGY_ALLOCATION["fade"]

    for c in candidates[:2]:
        count = client.contracts_for_budget(
            min(balance * FADE_MAX_POS_PCT, strat_budget * 0.55),
            c["entry_cents"]
        )
        cost = client.cost_usd(count, c["entry_cents"])
        if cost < 2.0:
            continue
        if not risk_manager.approve("fade", c["ticker"], cost, c["edge"],
                                    notes=c["title"][:45]):
            continue
        try:
            client.place_limit_order(
                ticker=c["ticker"], side=c["fade_side"], action="buy",
                price_cents=c["entry_cents"], count=count, post_only=False,
            )
            risk_manager.record_open(c["ticker"], count, c["entry_cents"], "fade",
                                     side=c["fade_side"])
            risk_manager.log_trade(
                strategy="fade", ticker=c["ticker"],
                side=c["fade_side"], action="buy",
                price_cents=c["entry_cents"], count=count,
                expected_pnl=cost * c["edge"],
                notes=f"move={c['move_cents']:+d}c edge={c['edge']:.1%}"
            )
            trades += 1
            strat_budget -= cost
        except Exception as e:
            logger.error(f"[FADE] Order failed {c['ticker']}: {e}")

    logger.info(f"[FADE] Placed {trades} trade(s)")
    return trades
