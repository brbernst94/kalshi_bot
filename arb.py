"""
arb.py — Intra-Kalshi Arbitrage
=================================
When YES_ask + NO_ask < 100¢ on the SAME market, buying both legs
guarantees a $1.00 payout regardless of outcome.

Academic basis:
  - IMDEA Networks documented $40M+ extracted via prediction market arb
    (Polymarket alone, Apr 2024–Apr 2025, 86M bets analysed)
  - On Kalshi, maker fee = 0%, so any gap we can post into is pure profit
  - Kalshi $0.005/contract volume incentive runs through Sept 2026

Two modes:
  1. TAKER ARB  — gap ≥ ARB_MIN_TAKER_GAP (8¢): buy both legs immediately,
                  guaranteed profit even after ~7¢ taker fees at mid-range prices
  2. MAKER QUOTE — gap ≥ ARB_MIN_MAKER_GAP (3¢): post YES and NO bids just
                  inside the spread, 0 fees, wait for fills to lock in profit.
                  If only one leg fills → treated as directional position
                  (monitor will exit at target or stop).

Order of execution:
  - TAKER: place YES order → place NO order immediately (both taker, fast)
  - MAKER: place YES bid, then NO bid (both maker, queue in book)

Sizing: up to ARB_MAX_POSITION_PCT of balance per leg.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

from client import price_cents as _pc
from config import (
    MAX_OPEN_POSITIONS,
    MAX_SINGLE_POSITION_PCT,
    STRATEGY_ALLOCATION,
)

logger = logging.getLogger("arb")

# ── Config ────────────────────────────────────────────────────────────────────
ARB_MIN_TAKER_GAP_CENTS = 8     # Need 8¢+ gap for taker arb (covers ~7¢ fees at mid)
ARB_MIN_MAKER_GAP_CENTS = 3     # Post maker quotes when gap ≥ 3¢ (0 fee, pure profit)
ARB_MAX_POSITION_PCT    = 0.15  # Up to 15% of balance per arb leg
ARB_MAX_TRADES_PER_CYCLE = 3    # Don't over-deploy in one cycle
ARB_MIN_CONTRACTS       = 5     # Minimum contracts to bother

def _yes_ask(m: Dict) -> Optional[int]:
    """YES ask price in cents — handles _dollars strings and integer cents."""
    return _pc(m, "yes_ask") or _pc(m, "best_yes_ask_price") or _pc(m, "best_ask")

def _no_ask(m: Dict) -> Optional[int]:
    """NO ask price in cents — handles _dollars strings and integer cents."""
    return _pc(m, "no_ask") or _pc(m, "best_no_ask_price")


def _taker_fee_cents(price_cents: int) -> float:
    """
    Estimate Kalshi taker fee in cents per contract.
    Fee = rate × (100 - price_cents) where rate is tiered:
      ≥ 90¢ → ~1.4%  (0.014)
      ≥ 70¢ → ~3.5%  (0.035)
      ≥ 50¢ → ~7.0%  (0.070)
      <  50¢ → ~7.0%  (0.070)  (conservative — could be higher)
    """
    profit_cents = 100 - price_cents
    if price_cents >= 90:
        rate = 0.014
    elif price_cents >= 70:
        rate = 0.035
    else:
        rate = 0.070
    return rate * profit_cents


def scan(client, risk_manager, markets: List[Dict]) -> List[Dict]:
    """
    Scan the cached market list for arbitrage opportunities.
    Returns a list of opportunity dicts sorted by net profit descending.
    """
    opps: List[Dict] = []
    yes_no_sum_seen = 0

    for m in markets:
        ticker = m.get("ticker", "")
        if not ticker:
            continue

        # Skip markets we already hold both sides of
        if ticker in risk_manager.open_positions:
            continue
        # Skip markets with no yes/no structure
        if m.get("market_type") not in (None, "binary", "yes_no", ""):
            continue

        yes_ask = _yes_ask(m)
        no_ask  = _no_ask(m)

        if yes_ask is None or no_ask is None:
            continue

        total_cost = yes_ask + no_ask
        if total_cost >= 100:
            continue  # No arb gap

        yes_no_sum_seen += 1
        gap = 100 - total_cost

        # Calculate net profit per contract after fees
        yes_fee = _taker_fee_cents(yes_ask)
        no_fee  = _taker_fee_cents(no_ask)
        net_per_contract = gap - yes_fee - no_fee

        # Volume incentive rebate: $0.005 per contract on each leg (through Sept 2026)
        rebate_per_pair = 0.5 + 0.5  # 0.5¢ × 2 legs = 1¢ total
        net_per_contract += rebate_per_pair

        mode = "taker" if gap >= ARB_MIN_TAKER_GAP_CENTS else "maker"

        if gap >= ARB_MIN_MAKER_GAP_CENTS:
            opps.append({
                "ticker":           ticker,
                "yes_ask":          yes_ask,
                "no_ask":           no_ask,
                "gap":              gap,
                "yes_fee":          yes_fee,
                "no_fee":           no_fee,
                "net_per_contract": net_per_contract,
                "mode":             mode,
                "title":            m.get("title", ticker),
            })

    opps.sort(key=lambda x: x["net_per_contract"], reverse=True)

    if opps:
        logger.info(
            f"[ARB] Scanned {len(markets)} markets | "
            f"{yes_no_sum_seen} had YES+NO<100¢ | "
            f"{len(opps)} viable (gap≥{ARB_MIN_MAKER_GAP_CENTS}¢) | "
            f"best net={opps[0]['net_per_contract']:.2f}¢/contract"
        )
        for o in opps[:5]:
            logger.info(
                f"  [{o['mode'].upper()}] {o['ticker']} | "
                f"YES={o['yes_ask']}¢ + NO={o['no_ask']}¢ = {o['yes_ask']+o['no_ask']}¢ | "
                f"gap={o['gap']}¢ | net={o['net_per_contract']:.1f}¢/contract"
            )
    else:
        logger.info(
            f"[ARB] Scanned {len(markets)} markets | "
            f"{yes_no_sum_seen} had YES+NO<100¢ | 0 viable (gap too small or fees negative)"
        )

    return opps


def execute(client, risk_manager, opportunities: List[Dict]) -> int:
    """
    Execute arbitrage trades.
    For TAKER arb: place both legs as takers immediately.
    For MAKER arb: post both legs as makers (limit orders).
    Returns number of pairs traded.
    """
    if not opportunities:
        logger.info("[ARB] Placed 0 arb pair(s)")
        return 0

    balance   = client.get_balance()
    alloc_frac = STRATEGY_ALLOCATION.get("arb", 0.10)
    budget    = balance * alloc_frac
    max_per_leg = balance * ARB_MAX_POSITION_PCT
    placed    = 0

    for opp in opportunities[:ARB_MAX_TRADES_PER_CYCLE]:
        if placed >= ARB_MAX_TRADES_PER_CYCLE:
            break
        if budget <= 1.0:
            logger.info("[ARB] Budget exhausted")
            break
        if len(risk_manager.open_positions) >= MAX_OPEN_POSITIONS - 1:
            logger.info("[ARB] Max open positions reached")
            break

        ticker  = opp["ticker"]
        yes_ask = opp["yes_ask"]
        no_ask  = opp["no_ask"]
        mode    = opp["mode"]
        gap     = opp["gap"]

        # Size: fit within budget and max_per_leg
        # Each pair costs (yes_ask + no_ask) cents = (yes_ask + no_ask)/100 dollars
        pair_cost_usd = (yes_ask + no_ask) / 100.0
        max_from_budget = int(budget / pair_cost_usd)
        max_from_leg    = int(max_per_leg / (yes_ask / 100.0))
        contracts = min(max_from_budget, max_from_leg)
        contracts = max(contracts, ARB_MIN_CONTRACTS)

        if contracts < ARB_MIN_CONTRACTS:
            logger.info(f"[ARB] {ticker} — insufficient budget for min contracts, skip")
            continue

        # Risk approval — check yes leg cost
        cost_yes = contracts * yes_ask / 100.0
        # Gross edge = guaranteed $1 payout / cost (e.g. 100/97 = 3% gross edge)
        gross_edge = (100 - (yes_ask + no_ask)) / 100.0
        approved = risk_manager.approve(
            "arb", ticker, cost_yes, gross_edge,
            notes=f"YES@{yes_ask}¢+NO@{no_ask}¢ gap={gap}¢"
        )
        if not approved:
            logger.info(f"[ARB] {ticker} REJECTED by risk manager")
            continue

        post_only = (mode == "maker")
        ticker_yes = ticker
        ticker_no  = ticker  # Same market, different side

        logger.info(
            f"[ARB] {mode.upper()} {ticker} | "
            f"YES@{yes_ask}¢ + NO@{no_ask}¢ | "
            f"{contracts} contracts | cost=${pair_cost_usd*contracts:.2f} | "
            f"gap={gap}¢ | net≈${opp['net_per_contract']*contracts/100:.2f}"
        )

        yes_order_id = None
        no_order_id  = None

        # ── YES leg ───────────────────────────────────────────────────────────
        try:
            resp_yes = client.place_order(
                ticker    = ticker_yes,
                side      = "yes",
                count     = contracts,
                price     = yes_ask,
                post_only = post_only,
            )
            yes_order_id = (resp_yes or {}).get("order", {}).get("order_id", "?")
            risk_manager.record_open(
                ticker_yes, contracts, yes_ask, "arb", side="yes"
            )
            logger.info(
                f"[ARB] YES leg placed | {contracts}x@{yes_ask}¢ | id={yes_order_id}"
            )
        except Exception as e:
            logger.error(f"[ARB] YES leg failed for {ticker}: {e}")
            continue  # Don't place NO leg without YES

        # ── NO leg ────────────────────────────────────────────────────────────
        # For taker arb: place immediately (race condition minimal at 8¢+ gap)
        # For maker arb: place simultaneously as queued limit orders
        try:
            resp_no = client.place_order(
                ticker    = ticker_no,
                side      = "no",
                count     = contracts,
                price     = no_ask,
                post_only = post_only,
            )
            no_order_id = (resp_no or {}).get("order", {}).get("order_id", "?")
            # We can't track both legs under the same ticker in risk manager
            # Record NO leg under a synthetic key
            risk_manager.record_open(
                ticker_no + "_NO", contracts, no_ask, "arb_no", side="no"
            )
            logger.info(
                f"[ARB] NO  leg placed | {contracts}x@{no_ask}¢ | id={no_order_id}"
            )
        except Exception as e:
            logger.error(
                f"[ARB] NO leg failed for {ticker}: {e} — "
                f"YES leg {yes_order_id} now directional, monitor will manage"
            )

        budget -= pair_cost_usd * contracts
        placed += 1
        time.sleep(0.3)

    logger.info(f"[ARB] Placed {placed} arb pair(s)")
    return placed
