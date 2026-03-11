"""
strategies/fade.py — Confirmed Fade / Overcorrection Reversal (Kalshi)
=======================================================================
Kalshi prices in cents. A 15¢ move = massive overcorrection on a $0.50 market.
Kalshi's regulated, data-backed markets revert faster than Polymarket because
there's less speculative noise and resolution criteria are unambiguous.

30-minute confirmation window eliminates false entries.
"""

import logging
import time
import statistics
from typing import Dict, List, Optional

from config import (
    FADE_CONFIRMATION_HOURS, FADE_MAX_POS_PCT,
    FADE_SPIKE_CENTS, KALSHI_TAKER_FEE_PCT, STRATEGY_ALLOCATION,
)
from bond import days_to_close

logger = logging.getLogger(__name__)

_staged: Dict[str, Dict] = {}   # ticker -> staged fade candidate


def detect_spike(history: List[Dict]) -> Optional[Dict]:
    if len(history) < 6:
        return None
    prices = [int(h.get("yes_price", 0)) for h in history if h.get("yes_price")]
    if len(prices) < 6:
        return None

    recent   = prices[-1]
    lookback = prices[-min(3, len(prices))]
    move     = recent - lookback

    if abs(move) < FADE_SPIKE_CENTS:
        return None

    mean_p = statistics.mean(prices[:-1])
    std_p  = statistics.stdev(prices[:-1]) if len(prices) > 2 else 1
    z      = (recent - mean_p) / max(std_p, 0.1)

    return {
        "current_cents":  recent,
        "lookback_cents": lookback,
        "move_cents":     move,
        "direction":      "UP" if move > 0 else "DOWN",
        "mean_cents":     mean_p,
        "z_score":        z,
    }


def scan(client, risk_manager, markets=None) -> List[Dict]:
    logger.info("[FADE] Scanning for Kalshi overcorrections...")
    candidates = []

    try:
        markets = client.get_all_open_markets()
    except Exception as e:
        logger.error(f"[FADE] Failed: {e}")
        return []

    for m in markets:
        if m.get("status") != "open":
            continue
        if int(m.get("open_interest", 0) or 0) < 200:
            continue

        ticker = m.get("ticker")
        if not ticker:
            continue

        try:
            history = client.get_market_history(ticker)
        except Exception:
            continue

        spike = detect_spike(history)
        if not spike:
            continue

        days = days_to_close(m)
        checks = 0
        reasons = []
        if abs(spike["z_score"]) > 2.0:
            checks += 1; reasons.append(f"z={spike['z_score']:.1f}")
        if days and days > 3:
            checks += 1; reasons.append(f"{days:.0f}d_left")
        recent_vol = int(m.get("volume_24h", m.get("volume", 0) or 0))
        avg_vol    = int(m.get("open_interest", 1)) / 10
        if avg_vol > 0 and recent_vol < avg_vol * 0.6:
            checks += 1; reasons.append("low_vol")

        if checks < 2:
            continue

        fade_side = "no" if spike["direction"] == "UP" else "yes"
        edge_cents = abs(spike["current_cents"] - spike["mean_cents"])
        edge       = edge_cents / 100 - KALSHI_TAKER_FEE_PCT

        if edge < 0.04:
            continue

        if ticker not in _staged:
            _staged[ticker] = {
                "ticker":     ticker,
                "title":      m.get("title", "")[:80],
                "fade_side":  fade_side,
                "entry_cents": spike["current_cents"],
                "target_cents": int(spike["mean_cents"]),
                "edge":        edge,
                "z_score":     spike["z_score"],
                "reasons":     reasons,
                "confidence":  checks / 3,
                "staged_at":   time.time(),
            }
            logger.info(f"[FADE] Staged: {m.get('title','')[:50]} z={spike['z_score']:.1f}")
            continue

        # Already staged — check confirmation
        staged  = _staged[ticker]
        elapsed = (time.time() - staged["staged_at"]) / 3600
        if elapsed < FADE_CONFIRMATION_HOURS:
            continue

        # Re-check: is price still near the spike level?
        try:
            current = client.get_mid_price_cents(ticker)
        except Exception:
            continue
        if current and abs(current - staged["entry_cents"]) > 5:
            # Price moved — not stalling, skip
            del _staged[ticker]
            continue

        candidates.append(staged)
        del _staged[ticker]
        logger.info(f"[FADE] CONFIRMED: {staged['title'][:50]}")

    candidates.sort(key=lambda x: x["edge"] * x["confidence"], reverse=True)
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
                ticker=c["ticker"],
                side=c["fade_side"],
                action="buy",
                price_cents=c["entry_cents"],
                count=count,
            )
            risk_manager.record_open(c["ticker"], count, c["entry_cents"], "fade")
            risk_manager.log_trade(
                strategy="fade", ticker=c["ticker"],
                side=c["fade_side"], action="buy",
                price_cents=c["entry_cents"], count=count,
                expected_pnl=cost * c["edge"],
                notes=f"z={c['z_score']:.1f} {c['reasons']}"
            )
            trades += 1
            strat_budget -= cost
        except Exception as e:
            logger.error(f"[FADE] Order failed {c['ticker']}: {e}")

    return trades
