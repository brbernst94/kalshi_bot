"""
strategies/momentum.py — Trend Following on High-Volume Markets
================================================================
Core insight from Kalshi market research:
  - High-volume markets attract INFORMED traders (real money on the line)
  - Price moves in liquid Kalshi markets tend to CONTINUE, not revert
    (opposite of fade — regulated exchange, moves = real information)
  - Sports excluded (mid-game prices reflect real game state, pure noise for us)
  - Crypto/financial/political markets: momentum persists 60-70% of time

Strategy:
  1. Each cycle: scan top markets by 24h volume (liquid = informed)
  2. Compare current price to price seen last cycle (in-memory, 0 extra API calls)
  3. If price moved >= MOMENTUM_MIN_MOVE_CENTS in one direction → enter in that direction
  4. Maker limit order at current price (0 fees)
  5. Monitor handles exits (trailing stop, 97¢ early exit, stop-loss)

Why this beats fade/bond/longshot on Kalshi:
  - Bond: needed 90-99¢ near-certain markets — Kalshi doesn't have enough
  - Longshot: needed 2-20¢ cheap markets — all filtered by sports exclusion
  - Fade: assumed reversion — wrong on regulated, informed markets
  - Momentum: works WITH the market structure instead of against it
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import STRATEGY_ALLOCATION
from bond import days_to_close
from client import price_cents as _pc

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
MOMENTUM_MIN_MOVE_CENTS = 5      # Minimum price move between cycles to trigger
MOMENTUM_MIN_VOLUME     = 2000   # Minimum 24h contracts — ensures real liquidity
MOMENTUM_TOP_N          = 60     # Only scan top N markets by volume each cycle
MOMENTUM_MIN_DAYS       = 2      # Skip markets resolving in < 2 days (too volatile)
MOMENTUM_MAX_DAYS       = 30     # Capped at global MAX_POSITION_DAYS — no long-dated bets
MOMENTUM_MAX_POS_PCT    = 0.07   # Max 7% of balance per trade

# Markets where price moves reflect REAL information (not game-state noise)
# Momentum is strongest in financial, crypto, and political policy markets
MOMENTUM_PRIORITY_PREFIXES = (
    "KXBTC", "KXETH", "KXXRP", "KXSOL", "KXDOGE",  # Crypto — high vol, strong momentum
    "KXFED", "KXFEDDECISION", "KXFOMC",              # Fed rate — policy driven
    "KXCPI", "KXPCE", "KXNFP", "KXGDP", "KXUNRATE", # Economic data
    "KXSPX", "KXNAS", "KXDOW", "KXGOLD", "KXOIL",   # Financial markets
    "KXTRUMP", "KXBIDEN", "KXELECTION",               # Political — high conviction
    "KXDOGE",                                          # DOGE budget cuts — high volume
)

# Always skip these — moves are game-state noise, not tradeable momentum.
# Rule: if you can't model the outcome independently, skip it.
# Data showed: tennis -$48, intl soccer -$61, NHL -$7 on a single day.
# Also skip KXBTCD (daily BTC price contracts): data shows daily contracts
# lose $1.19/trade on avg; KXBTC15M (15-min) makes +$0.63/trade — keep those.
MOMENTUM_SKIP_PREFIXES = (
    # ── US sports ────────────────────────────────────────────────────────────
    "KXNCAAMB", "KXNCAAFB", "KXNCAAWB",       # NCAA (all variants)
    "KXNBA", "KXNBAGAME", "KXNFL", "KXNFLGAME",
    "KXMLB", "KXMLS",
    "KXNHL",                                    # catches KXNHLGAME, KXNHLTOTAL, etc.
    "KXNBA2KCOVER", "KXMLBNL",
    # ── Tennis (all variants) ─────────────────────────────────────────────────
    "KXATP",                                    # catches KXATPMATCH, KXATPCHALLENGERMATCH,
                                                #   KXATPGSPREAD, KXATPGAMETOTAL, etc.
    "KXWTA",                                    # catches KXWTAMATCH, KXWTACHALLENGER, etc.
    "KXITN",
    # ── International soccer ─────────────────────────────────────────────────
    "KXUCL",                                    # catches KXUCLGAME, KXUCLSPREAD, KXUCLTOTAL
    "KXUEL",                                    # UEFA Europa League (KXUELGAME, KXUELTOTAL)
    "KXUECL",                                   # UEFA Europa Conference League
    "KXCONCACAF",
    "KXBRASILEIRO",                             # Brasileirao
    "KXARGPREMDIV",                             # Argentine Primera Division
    "KXEUROLEAGUE",                             # EuroLeague basketball
    "KXFIBACHAMP",                              # FIBA Champions League
    "KXBUNDES", "KXSERIEA", "KXLALIGA", "KXLIGUE",  # European soccer leagues
    "KXEPL", "KXUEFA",
    # ── Other sports ─────────────────────────────────────────────────────────
    "KXWBC", "KXWBO", "KXWBA", "KXIBF",        # Boxing / Baseball Classic
    "KXUFC", "KXPGA",
    "KXKBL", "KXAFC", "KXJBL", "KXCBA",        # Asian/intl leagues (36% win rate)
    "KXKBO", "KXNPB",
    # Esports
    "KXCS2", "KXLOL", "KXVALO", "KXRL", "KXDOTA", "KXESPORT",
    # Other leagues / misc
    "KXEFL", "KXCFL", "KXAFL", "KXSERIEB", "KXLIVTOUR", "KXIWMEN",
    # ── Crypto daily contracts (15-min contracts NOT skipped — they work) ────
    "KXBTCD",                                   # Daily BTC price: -$1.19/trade avg
    "KXETHD", "KXSOLD", "KXXRPD",              # Other daily crypto contracts
)

# ── In-memory state ───────────────────────────────────────────────────────────
_prev_prices:  Dict[str, int]   = {}  # ticker → price seen last cycle
_prev_volumes: Dict[str, int]   = {}  # ticker → volume seen last cycle
_entry_cooldown: Dict[str, float] = {}  # ticker → timestamp of last entry (avoid chasing)

COOLDOWN_SECS = 600  # Don't re-enter the same market within 10 minutes


def _get_volume(m: dict) -> int:
    for field in ("volume_24h", "volume", "open_interest"):
        v = m.get(field)
        if v:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                continue
    return 0


def scan(client, risk_manager, markets: List[Dict]) -> List[Dict]:
    import time
    now = time.time()

    logger.info("[MOMENTUM] Scanning high-volume markets for trend signals...")
    candidates = []

    # Filter out sports
    tradeable = [
        m for m in markets
        if not m.get("ticker", "").startswith(MOMENTUM_SKIP_PREFIXES)
    ]

    # Sort by 24h volume — momentum is strongest where real money is moving
    tradeable.sort(key=_get_volume, reverse=True)
    top_markets = tradeable[:MOMENTUM_TOP_N]

    current_prices:  Dict[str, int] = {}
    current_volumes: Dict[str, int] = {}

    for m in top_markets:
        ticker = m.get("ticker", "")
        if not ticker:
            continue

        current = _pc(m, "yes_ask") or _pc(m, "last_price") or _pc(m, "yes_bid")
        if current is None:
            continue

        vol = _get_volume(m)
        current_prices[ticker]  = current
        current_volumes[ticker] = vol

        # Need a previous observation to measure momentum
        prev = _prev_prices.get(ticker)
        if prev is None:
            continue

        move = current - prev
        if abs(move) < MOMENTUM_MIN_MOVE_CENTS:
            continue

        # Volume and liquidity checks
        if vol < MOMENTUM_MIN_VOLUME:
            continue

        days = days_to_close(m)
        if days is None or days < MOMENTUM_MIN_DAYS or days > MOMENTUM_MAX_DAYS:
            continue

        # Don't re-enter markets we already hold
        if ticker in risk_manager.open_positions:
            continue

        # Cooldown — don't chase a move we already entered
        if ticker in _entry_cooldown and (now - _entry_cooldown[ticker]) < COOLDOWN_SECS:
            continue

        # Direction — trade WITH the momentum
        side = "yes" if move > 0 else "no"
        entry_price = current if side == "yes" else (100 - current)

        # Skip extreme prices — momentum at the tails is resolution risk, not edge
        if entry_price < 5 or entry_price > 92:
            continue

        # Estimate edge: momentum persistence rate (~60%) minus fees
        # Maker order = 0 fees. Edge = P(continuation) * payout - P(reversal) * loss
        p_continue = 0.60
        gross_ev = p_continue * abs(move) / 100
        net_ev   = gross_ev  # maker = 0 fees

        if net_ev < 0.02:
            continue

        # Priority boost for financial/crypto/political markers
        is_priority = ticker.startswith(MOMENTUM_PRIORITY_PREFIXES)
        priority_score = 1.3 if is_priority else 1.0

        candidates.append({
            "ticker":        ticker,
            "title":         m.get("title", "")[:80],
            "side":          side,
            "entry_cents":   entry_price,
            "move_cents":    move,
            "ev":            round(net_ev * priority_score, 4),
            "volume_24h":    vol,
            "days":          round(days, 1),
            "is_priority":   is_priority,
        })

        logger.info(
            f"[MOMENTUM] SIGNAL {ticker} | move={move:+d}¢ → {side.upper()} @ {entry_price}¢ "
            f"| vol={vol:,} | ev={net_ev:.1%} | {days:.0f}d"
        )

    # Update memory
    _prev_prices.update(current_prices)
    _prev_volumes.update(current_volumes)

    # Sort: priority markets first, then by EV
    candidates.sort(key=lambda x: x["ev"], reverse=True)

    logger.info(
        f"[MOMENTUM] Scanned top {len(top_markets)} markets | "
        f"{len(candidates)} signal(s) found"
    )
    return candidates


def execute(client, risk_manager, candidates: List[Dict]) -> int:
    import time

    trades = 0
    try:
        balance = client.get_balance()
    except Exception:
        from config import STARTING_BANKROLL_USD
        balance = STARTING_BANKROLL_USD

    strat_budget = balance * STRATEGY_ALLOCATION.get("momentum", 0.40)
    max_per_trade = min(balance * MOMENTUM_MAX_POS_PCT, strat_budget / max(len(candidates), 1))

    for c in candidates[:4]:  # Max 4 momentum trades per cycle
        count = client.contracts_for_budget(max_per_trade, c["entry_cents"])
        cost  = client.cost_usd(count, c["entry_cents"])

        if cost < 2.0:
            continue

        if not risk_manager.approve(
            "momentum", c["ticker"], cost, c["ev"], notes=c["title"][:45]
        ):
            continue

        try:
            client.place_limit_order(
                ticker=c["ticker"],
                side=c["side"],
                action="buy",
                price_cents=c["entry_cents"],
                count=count,
                post_only=True,  # maker = 0 fees
            )
            risk_manager.record_open(
                c["ticker"], count, c["entry_cents"], "momentum", side=c["side"]
            )
            risk_manager.log_trade(
                strategy="momentum", ticker=c["ticker"],
                side=c["side"], action="buy",
                price_cents=c["entry_cents"], count=count,
                expected_pnl=cost * c["ev"],
                notes=f"move={c['move_cents']:+d}¢ vol={c['volume_24h']:,} ev={c['ev']:.1%}"
            )
            _entry_cooldown[c["ticker"]] = time.time()
            trades += 1
            strat_budget -= cost
            if strat_budget <= 0:
                break
        except Exception as e:
            logger.error(f"[MOMENTUM] Order failed {c['ticker']}: {e}")

    logger.info(f"[MOMENTUM] Placed {trades} trade(s)")
    return trades
