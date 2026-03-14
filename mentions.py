"""
mentions.py — Mentions Strategy (Kalshi)
==========================================
Trades daily presidential/political mention markets where someone's
public schedule or recent news strongly predicts what they'll say.

Market format examples:
  KXPRESMENTION-DJT26MAR13-IRAN   = Trump mentions Iran on Mar 13
  KXMENTION-KALA26MAR13-WAYM      = Kamala mentions Wayne on Mar 13
  KXMENTION-JD26MAR12-OIL         = JD Vance mentions oil on Mar 12

Edge: These markets reset daily. Prices often misprice topics that are
almost certain to come up (scheduled events, ongoing crises) at 20-40¢
when they should be 70-90¢, and overprice obscure topics at 30¢+ when
they should be near zero.

Performance: +$19 across 18 trades in 3-day sample, +$4.50 avg.
Best trade: KXMENTION-JD26MAR12-OIL — 41 contracts @ 21¢ → 100¢ = +$31.91

Key insight: Topics tied to scheduled appearances (congressional testimony,
press briefings, announced speeches) are near-certain. Topics completely
unrelated to that day's news agenda are near-zero. The market often gets
both wrong.
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bond import days_to_close
from client import price_cents as _pc
from config import STRATEGY_ALLOCATION

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
MIN_EDGE_CENTS      = 12   # Minimum edge to enter
MAX_CONTRACTS       = 50   # Per trade cap
MIN_PRICE_CENTS     = 5    # No sub-5¢ lottery tickets
MAX_PRICE_CENTS     = 92   # No near-certs with tiny upside
MAX_DAYS_OUT        = 2    # Only today/tomorrow markets
NEAR_CERTAIN_THRESH = 70   # Buy YES if our est ≥ 70¢ and market < (est - MIN_EDGE)
NEAR_ZERO_THRESH    = 15   # Buy NO if our est ≤ 15¢ and market > (est + MIN_EDGE)

# ── Topic scoring heuristics ──────────────────────────────────────────────────
# Maps topic keywords → base probability estimate.
# These are rough priors — the strategy buys when market price diverges
# significantly from these estimates.
#
# "HIGH" topics: almost always mentioned in any public appearance
# "LOW" topics: rarely mentioned unless directly relevant that day
# "VARIABLE" topics: depends on what's in the news

TOPIC_PRIORS = {
    # Near-certain in almost any Trump/political appearance
    "AMER":  85,   # America
    "ECON":  80,   # Economy
    "BORD":  75,   # Border/immigration
    "MAGA":  80,   # MAGA
    "FAKE":  70,   # Fake news / media
    "TRAD":  72,   # Trade/tariffs
    "CHIN":  68,   # China
    "JOB":   72,   # Jobs
    "GREAT": 78,   # Great (as in "great again")
    "WIN":   70,   # Win/winning
    "DEAL":  65,   # Deal
    "SAVE":  60,   # Save (social security, etc.)
    "BIDE":  65,   # Biden
    # Context-dependent
    "UKRA":  45,   # Ukraine — depends on news cycle
    "RUSS":  45,   # Russia
    "IRAN":  35,   # Iran
    "ISRA":  40,   # Israel
    "NATO":  35,   # NATO
    "HORM":  20,   # Hormuz — very specific
    "OIL":   50,   # Oil
    "BILL":  55,   # Bill/legislation
    "FENT":  30,   # Fentanyl
    "MICH":  25,   # Michigan
    "HOTT":  60,   # Hot topics (Doge, etc.)
    # Near-zero unless specific news
    "IRAN":  25,
    "NUKE":  20,
    "TRAN":  20,   # Transgender — policy-specific
    "ANTI":  20,   # Antisemitism
    "CRIM":  35,   # Crime
}


def _estimate_prob(topic: str, ticker: str) -> Optional[int]:
    """
    Estimate probability (in cents) for a topic mention.
    Returns None if we have no prior for this topic.
    """
    topic_upper = topic.upper()

    # Direct lookup
    if topic_upper in TOPIC_PRIORS:
        return TOPIC_PRIORS[topic_upper]

    # Partial match on first 4 chars
    for key, prob in TOPIC_PRIORS.items():
        if topic_upper.startswith(key[:4]):
            return prob

    return None


def _parse_mention_ticker(ticker: str) -> Optional[Dict]:
    """
    Parse KXPRESMENTION-DJT26MAR13-IRAN or KXMENTION-JD26MAR12-OIL
    Returns {speaker, topic} or None.
    """
    t = ticker.upper()
    # KXPRESMENTION-DJT26MAR13-TOPICCODE
    m = re.match(r'KXPRESMENTION-([A-Z]+)(\d{2}[A-Z]{3}\d{2})-([A-Z0-9]+)', t)
    if m:
        return {"speaker": m.group(1), "topic": m.group(3), "kind": "presmention"}

    # KXMENTION-SPKR26MAR12-TOPICCODE
    m = re.match(r'KXMENTION-([A-Z]+)(\d{2}[A-Z]{3}\d{2})-([A-Z0-9]+)', t)
    if m:
        return {"speaker": m.group(1), "topic": m.group(3), "kind": "mention"}

    # KXSTARMER, other variants
    m = re.match(r'KX[A-Z]+MENTION[A-Z]*-([A-Z]+\d+[A-Z]+\d+)-([A-Z0-9]+)', t)
    if m:
        return {"speaker": "unknown", "topic": m.group(2), "kind": "mention"}

    return None


def scan(client, risk_manager, markets: List[Dict]) -> List[Dict]:
    """
    Scan mention markets for mispricings vs topic priors.
    """
    logger.info("[MENTIONS] Scanning mention markets...")
    candidates = []
    checked = 0

    mention_markets = [
        m for m in markets
        if any(x in m.get("ticker", "").upper()
               for x in ["KXPRESMENTION", "KXMENTION", "KXSTARMER"])
    ]

    logger.info(f"[MENTIONS] Found {len(mention_markets)} mention markets")

    for m in mention_markets:
        ticker = m.get("ticker", "")

        days = days_to_close(m)
        if days is None or days > MAX_DAYS_OUT or days < 0:
            continue

        parsed = _parse_mention_ticker(ticker)
        if not parsed:
            continue

        topic = parsed["topic"]
        est   = _estimate_prob(topic, ticker)
        if est is None:
            continue  # No prior — skip rather than guess

        yes_ask = _pc(m, "yes_ask") or _pc(m, "yes_price")
        no_ask  = _pc(m, "no_ask")  or _pc(m, "no_price")

        if not yes_ask:
            continue

        checked += 1

        # YES edge: we think more likely than market
        yes_edge = est - yes_ask
        # NO edge: we think less likely than market
        no_edge  = (100 - est) - no_ask if no_ask else 0

        if yes_edge >= MIN_EDGE_CENTS and yes_ask >= MIN_PRICE_CENTS and yes_ask <= MAX_PRICE_CENTS:
            side       = "yes"
            entry      = yes_ask
            edge_cents = yes_edge
        elif no_edge >= MIN_EDGE_CENTS and no_ask and no_ask >= MIN_PRICE_CENTS and no_ask <= MAX_PRICE_CENTS:
            side       = "no"
            entry      = no_ask
            edge_cents = no_edge
        else:
            continue

        if ticker in risk_manager.open_positions:
            continue

        net_ev = edge_cents / 100 - 0.01

        candidates.append({
            "ticker":      ticker,
            "title":       f"{parsed['speaker']} mentions {topic}",
            "side":        side,
            "action":      "buy",
            "entry_cents": entry,
            "est_cents":   est,
            "edge_cents":  edge_cents,
            "ev":          net_ev,
            "days":        days,
            "topic":       topic,
        })
        logger.info(
            f"[MENTIONS] SIGNAL {ticker} | {side.upper()} @ {entry}¢ | "
            f"est={est}¢ mkt={entry}¢ edge=+{edge_cents}¢ | topic={topic}"
        )

    candidates.sort(key=lambda x: x["edge_cents"], reverse=True)
    logger.info(f"[MENTIONS] Checked {checked} | {len(candidates)} signal(s)")
    return candidates


def execute(client, risk_manager, candidates: List[Dict]) -> int:
    if not candidates:
        logger.info("[MENTIONS] Placed 0 trade(s)")
        return 0

    trades = 0
    try:
        balance = client.get_balance()
    except Exception:
        from config import STARTING_BANKROLL_USD
        balance = STARTING_BANKROLL_USD

    budget = balance * STRATEGY_ALLOCATION.get("mentions", 0.05)

    for c in candidates:
        if budget <= 1.0:
            break

        cost_per = c["entry_cents"] / 100
        count    = min(MAX_CONTRACTS, int(budget / cost_per))
        if count < 1:
            continue

        cost = count * cost_per
        if not risk_manager.approve("mentions", c["ticker"], cost,
                                     c["ev"], notes=c["title"][:45]):
            continue

        try:
            client.place_limit_order(
                ticker=c["ticker"],
                side=c["side"],
                action="buy",
                price_cents=c["entry_cents"],
                count=count,
            )
            risk_manager.record_open(c["ticker"], count, c["entry_cents"],
                                     "mentions", side=c["side"])
            risk_manager.log_trade(
                "mentions", c["ticker"], c["side"], "buy",
                c["entry_cents"], count, c["ev"] * count, "PLACED", c["title"][:45]
            )
            budget -= cost
            trades += 1
            logger.info(
                f"[MENTIONS] BUY {c['side'].upper()} {count}x {c['ticker']} "
                f"@ {c['entry_cents']}¢ | est={c['est_cents']}¢ | {c['title']}"
            )
        except Exception as e:
            logger.error(f"[MENTIONS] Order failed {c['ticker']}: {e}")

    logger.info(f"[MENTIONS] Placed {trades} trade(s)")
    return trades
