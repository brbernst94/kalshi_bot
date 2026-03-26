"""
strategies/bond.py — High-Probability Bond Strategy (Kalshi)
=============================================================
Buy YES on markets priced ≥ 90¢ that resolve within 14 days.

Kalshi-specific notes:
  - Prices in cents: 90¢ = yes_price=90
  - Gross return on 90¢ contract = 10/90 = 11.1%
  - After 1% fee: net ≈ 10.1% in ≤14 days → ~263% annualised
  - Maker orders (post_only=True) avoid the taker fee entirely
    → use limit orders slightly below the ask to get maker fills

Kalshi has better bond opportunities than Polymarket because:
  - More policy/economic markets that are near-certain well in advance
  - Tight resolution criteria (official CFTC-approved sources)
  - No oracle ambiguity risk
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import (
    BOND_MAX_DAYS, BOND_MAX_POSITION_PCT, BOND_MIN_PRICE_CENTS,
    KALSHI_TAKER_FEE_PCT, STRATEGY_ALLOCATION,
)
from client import price_cents as _pc

logger = logging.getLogger(__name__)


def days_to_close(market: Dict) -> Optional[float]:
    for field in (
        "close_time", "expiration_time", "end_date",
        "expected_expiration_time", "settlement_time", "resolution_time",
        "close_date", "expiry", "expiry_time",
    ):
        val = market.get(field)
        if val:
            try:
                dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                diff = (dt - datetime.now(timezone.utc)).total_seconds() / 86400
                if diff > 0:
                    return diff
                # diff <= 0 means this timestamp is in the past — try next field
                # (e.g. close_time = trading window end, expiration_time = actual resolve)
            except Exception:
                pass
    return None


def get_yes_price(market: Dict) -> Optional[int]:
    """Return YES price in cents from market dict."""
    # Kalshi returns yes_bid, yes_ask, last_price etc.
    for field in ("yes_ask", "yes_bid", "last_price", "yes_price"):
        val = market.get(field)
        if val is not None:
            try:
                return int(val)
            except Exception:
                continue
    return None


def scan(client, risk_manager, markets=None) -> List[Dict]:
    logger.info("[BOND] Scanning Kalshi for near-certain markets...")
    candidates = []

    if markets is None:
        try:
            markets = client.get_all_open_markets()
        except Exception as e:
            logger.error(f"Market fetch failed: {e}")
            return []

    open_markets = list(markets)  # API already returns open markets only
    # Pre-filter: skip sports — bond strategy targets policy/financial/economic markets
    SPORTS_PREFIXES = (
        "KXNCAAMB", "KXNCAAFB", "KXNCAAWB", "KXUCLGAME",
        "KXWTAMATCH", "KXATPMATCH", "KXWTACHALLENGER", "KXATPCHALLENGER",
        "KXNHLWEST", "KXNHLEAST", "KXNHLGAME",
        "KXNBA", "KXNBAGAME", "KXNFL", "KXNFLGAME",
        "KXMLB", "KXMLS", "KXNCAAMBSPREAD", "KXNCAAMBTOTAL", "KXNCAAMBGAME",
        "KXWBC", "KXWBO", "KXWBA", "KXIBF",   # Boxing titles — illiquid ghost markets
        "KXUFC", "KXPGA", "KXCONCACAF", "KXUCLSPREAD",
    )
    open_markets = [m for m in open_markets
                    if not m.get("ticker", "").startswith(SPORTS_PREFIXES)]
    logger.info(f"[BOND] {len(markets)} total markets, {len(open_markets)} open after sports filter")

    # Pass 1: filter by close time AND require a last_price in the cache
    # (markets with no last_price have never traded — skip them, no liquidity)
    time_filtered = []
    for m in open_markets:
        days = days_to_close(m)
        if days is not None and not (0.1 <= days <= BOND_MAX_DAYS):
            continue
        # Must have a tradeable price — skip ghost markets with no price data
        # price_cents() handles both _dollars strings (new) and integer cents (old)
        lp = _pc(m, "yes_ask") or _pc(m, "last_price") or _pc(m, "yes_bid")
        if not lp:
            continue
        time_filtered.append(m)

    logger.info(f"[BOND] {len(time_filtered)} markets within {BOND_MAX_DAYS}-day window with price data")

    if not time_filtered:
        logger.info("[BOND] 0 candidates")
        return []

    # Pass 2: use last_price from cache — no individual API calls needed
    # For illiquid markets the orderbook is always empty; last_price is the real signal
    for m in time_filtered:
        ticker = m.get("ticker", "")

        days = days_to_close(m)
        if days is None:
            days = 30

        # Use price_cents helper — handles _dollars strings (new) and integer cents (old)
        yes_price_cents = (
            _pc(m, "yes_ask") or _pc(m, "last_price") or
            _pc(m, "yes_bid") or _pc(m, "yes_price")
        )

        if yes_price_cents is None:
            continue

        if yes_price_cents < BOND_MIN_PRICE_CENTS or yes_price_cents >= 99:
            continue

        MAKER_FEE = 0.0
        gross_return = (100 - yes_price_cents) / yes_price_cents
        net_return   = gross_return - MAKER_FEE
        if net_return < 0.005:
            continue

        annualised = ((1 + net_return) ** (365 / max(days, 1))) - 1
        tier = 1 if yes_price_cents >= 90 else 2

        logger.info(
            f"[BOND] CANDIDATE {ticker} | YES={yes_price_cents}¢ | "
            f"days={days:.1f} | net_return={net_return:.1%} | tier={tier}"
        )
        candidates.append({
            "ticker":        ticker,
            "title":         m.get("title", "")[:80],
            "yes_price":     yes_price_cents,
            "gross_return":  gross_return,
            "net_return":    net_return,
            "annualised":    annualised,
            "days":          days,
            "tier":          tier,
        })

    # Sort tier 1 first, then by net return
    candidates.sort(key=lambda x: (-x["tier"], -x["net_return"]))
    logger.info(f"[BOND] {len(candidates)} candidates")
    for c in candidates[:4]:
        logger.info(
            f"  ↳ [{c['tier']}] {c['title'][:50]} | {c['yes_price']}¢ "
            f"| net={c['net_return']:.2%} | ann={c['annualised']:.0%} "
            f"| {c['days']:.1f}d"
        )
    return candidates


def execute(client, risk_manager, candidates: List[Dict]) -> int:
    trades = 0
    try:
        balance = client.get_balance()
    except Exception:
        from config import STARTING_BANKROLL_USD
        balance = STARTING_BANKROLL_USD

    strat_budget = balance * STRATEGY_ALLOCATION["bond"]

    for c in candidates[:3]:
        # Use post_only to get maker fill (avoid 1% taker fee)
        bid_price = max(c["yes_price"] - 1, 1)   # 1¢ below ask for maker fill
        count     = client.contracts_for_budget(
                        min(strat_budget * 0.45, balance * BOND_MAX_POSITION_PCT),
                        bid_price
                    )
        cost      = client.cost_usd(count, bid_price)

        if cost < 1.0:
            continue

        if not risk_manager.approve("bond", c["ticker"], cost, c["gross_return"],
                                     notes=c["title"][:45]):
            continue

        try:
            client.place_limit_order(
                ticker=c["ticker"],
                side="yes",
                action="buy",
                price_cents=bid_price,
                count=count,
                post_only=True,   # Maker = 0% fee
            )
            risk_manager.record_open(c["ticker"], count, bid_price, "bond")
            risk_manager.log_trade(
                strategy="bond", ticker=c["ticker"],
                side="yes", action="buy",
                price_cents=bid_price, count=count,
                expected_pnl=count * (100 - bid_price) / 100,
                notes=c["title"][:60]
            )
            trades += 1
            strat_budget -= cost
        except Exception as e:
            logger.error(f"[BOND] Order failed {c['ticker']}: {e}")

    logger.info(f"[BOND] Placed {trades} trade(s)")
    return trades
