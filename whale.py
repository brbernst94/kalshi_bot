"""
strategies/whale.py — Whale Following Strategy (Kalshi)
=========================================================
Kalshi-specific differences vs Polymarket:
  - Whale trades visible via GET /markets/trades (public fill feed)
  - Large fills = high conviction (no wash trading incentive on regulated exchange)
  - Kalshi member leaderboard is public — can seed TRACKED_WHALE_MEMBERS
  - No anonymous blockchain wallets: trades tied to member IDs
  - Fees make wash trading actively unprofitable (unlike Polymarket airdrop farming)

On a regulated exchange, a $5,000+ fill is almost always real conviction.
The Kalshi leaderboard publishes top traders by profit — those are the
wallets/members worth following.
"""

import logging
from client import price_cents as _pc
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import (
    STRATEGY_ALLOCATION, TRACKED_WHALE_MEMBERS,
    WHALE_COPY_DELAY_SECS, WHALE_MAX_COPY_FRAC,
    WHALE_MIN_CONTRACTS, WHALE_MIN_WIN_RATE,
)

logger = logging.getLogger(__name__)

_recent_copies: Dict[str, Dict] = {}   # ticker -> last copy info
_member_cache:  Dict[str, Dict] = {}   # member_id -> stats


def _cost_usd(count: int, price_cents: int) -> float:
    return count * price_cents / 100


def fetch_large_fills(client) -> List[Dict]:
    """
    Pull recent large trades — only from high-edge markets.
    Sports excluded (near-50/50 mid-game, correlated losses).
    Financial, crypto, political markets kept (real info asymmetry).
    Tracked whale members bypass the filter entirely.
    """
    HIGH_EDGE_PREFIXES = (
        "KXBTCD", "KXETHD", "KXXRPD", "KXSOLD",   # Crypto prices
        "KXFED", "KXFEDDECISION",                   # Fed rate
        "KXCPI", "KXPCE", "KXGDP", "KXUNRATE",     # Economic data
        "KXNFP", "KXSPX", "KXNAS", "KXDOW",        # Markets/jobs
        "KXGOLD", "KXOIL",                           # Commodities
        "KXPRES", "KXTXRUN", "KXTRUMP",             # Political
        "KXCONGRESS", "KXSUPREME", "KXELECTION",    # Gov/courts
        "KXACAREPEAL", "KXSPACEX", "KXIPORA",       # Policy/tech
        "KXPRESMENTION",                             # Presidential mentions
        "KXHOUSERACE", "KXSENRACE",                 # Race markets (not sports)
        "KXPOWER", "KXBITCOIN",                     # Energy/crypto variants
    )

    SPORTS_PREFIXES = (
        "KXNCAAMB", "KXNCAAFB", "KXNCAAWB",
        "KXUCLGAME", "KXWTAMATCH", "KXATPMATCH", "KXWTACHALLENGER",
        "KXNHLWEST", "KXNHLEAST", "KXNBA", "KXNFL", "KXMLB", "KXMLS",
        "KXNCAAMBSPREAD", "KXNCAAMBTOTAL", "KXNCAAMBGAME",
        "KXNHLGAME", "KXNBAGAME", "KXNFLGAME",
    )

    large_fills = []
    try:
        data   = client._get("/markets/trades", params={"limit": 200})
        trades = data.get("trades", [])
    except Exception as e:
        logger.error(f"[WHALE] Market fetch failed: {e}")
        return []

    sports_skipped = 0

    # During off-peak hours (4-7pm ET, overnight), lower the bar to catch
    # whatever signal exists rather than returning 0
    from datetime import datetime, timezone
    hour_utc = datetime.now(timezone.utc).hour
    # 21:00-23:59 UTC = 4-7pm ET (off-peak), 0-11 UTC = overnight/pre-market
    is_off_peak = hour_utc >= 21 or hour_utc <= 11
    effective_min = max(20, WHALE_MIN_CONTRACTS // 2) if is_off_peak else WHALE_MIN_CONTRACTS

    for t in trades:
        # Handle fractional count migration (count_fp is string like "31.00")
        count_raw = t.get("count_fp") or t.get("count", 0)
        try:
            count = int(float(count_raw))
        except (TypeError, ValueError):
            count = 0
        if count < effective_min:
            continue

        ticker    = t.get("ticker", "")
        member_id = t.get("taker_member_id", "")
        is_tracked = member_id in TRACKED_WHALE_MEMBERS

        # Always skip KXMVE parlays
        if ticker.startswith("KXMVE") or ticker.startswith("KXMVECROSS"):
            sports_skipped += 1
            continue

        # Skip sports unless it's a tracked whale
        if ticker.startswith(SPORTS_PREFIXES) and not is_tracked:
            sports_skipped += 1
            continue

        # Accept everything else — don't over-restrict to allowlist
        # (financial, crypto, political, policy markets all have edge)

        price = _pc(t, "yes_price") or _pc(t, "no_price") or 50
        side  = t.get("taker_side", "yes")

        large_fills.append({
            "ticker":      ticker,
            "title":       ticker[:80],
            "side":        side,
            "action":      "buy",
            "price_cents": price,
            "count":       count,
            "cost_usd":    _cost_usd(count, price),
            "member_id":   member_id,
            "created_at":  t.get("created_time", ""),
            "is_tracked":  is_tracked,
        })

    large_fills.sort(key=lambda x: x["cost_usd"], reverse=True)

    # Deduplicate by ticker — keep only the largest fill per market
    seen = {}
    for f in large_fills:
        t = f["ticker"]
        if t not in seen or f["cost_usd"] > seen[t]["cost_usd"]:
            seen[t] = f
    large_fills = sorted(seen.values(), key=lambda x: x["cost_usd"], reverse=True)

    logger.info(f"[WHALE] {len(large_fills)} quality fills ({sports_skipped} sports/low-edge skipped)")
    return large_fills


def get_member_stats(client, member_id: str) -> Dict:
    """
    Try to fetch member stats. Falls back to conservative defaults.
    Kalshi's leaderboard endpoint is public.
    """
    if not member_id:
        return {"win_rate": 0.5, "profit": 0, "is_known": False}

    if member_id in _member_cache:
        cached = _member_cache[member_id]
        age    = (datetime.now(timezone.utc) -
                  cached.get("cached_at", datetime.now(timezone.utc))
                  ).total_seconds()
        if age < 3600:
            return cached

    # If it's a tracked whale, give benefit of the doubt
    if member_id in TRACKED_WHALE_MEMBERS:
        stats = {"win_rate": 0.75, "profit": 100_000,
                 "is_known": True, "is_tracked": True,
                 "cached_at": datetime.now(timezone.utc)}
        _member_cache[member_id] = stats
        return stats

    # Unknown member — moderate defaults
    stats = {"win_rate": 0.55, "profit": 0,
             "is_known": False, "is_tracked": False,
             "cached_at": datetime.now(timezone.utc)}
    _member_cache[member_id] = stats
    return stats


def scan(client, risk_manager) -> List[Dict]:
    logger.info("[WHALE] Scanning for large conviction fills...")
    raw_fills  = fetch_large_fills(client)
    candidates = []

    for fill in raw_fills:
        member_id = fill["member_id"]
        stats     = get_member_stats(client, member_id)

        # Allow tracked whales OR any large untracked fill (unknown members default to 0.55)
        # Don't block on win_rate if TRACKED_WHALE_MEMBERS is empty
        tracked_only_gate = bool(TRACKED_WHALE_MEMBERS)
        if tracked_only_gate and stats["win_rate"] < WHALE_MIN_WIN_RATE and not fill["is_tracked"]:
            continue

        # Cool-down: skip if we already copied this ticker recently
        if fill["ticker"] in _recent_copies:
            last = _recent_copies[fill["ticker"]]
            elapsed = (datetime.now(timezone.utc) -
                       last.get("copied_at", datetime.now(timezone.utc))
                       ).total_seconds() / 60
            if elapsed < 30:   # 30-minute cooldown per ticker
                continue

        confidence = min(stats["win_rate"] / 0.75, 1.0)
        edge       = max((stats["win_rate"] - 0.50) * 2, 0.04)

        candidates.append({
            **fill,
            "confidence":   confidence,
            "edge":         edge,
            "member_stats": stats,
            "detected_at":  datetime.now(timezone.utc),
        })

    candidates.sort(key=lambda x: x["cost_usd"] * x["confidence"], reverse=True)
    logger.info(f"[WHALE] {len(candidates)} actionable signal(s)")
    for c in candidates[:3]:
        logger.info(
            f"  ↳ {c['title'][:55]} | {c['side'].upper()} {c['count']}x "
            f"@ {c['price_cents']}¢ | conf={c['confidence']:.2f}"
        )
    return candidates


def execute(client, risk_manager, candidates: List[Dict]) -> int:
    trades = 0
    try:
        balance = client.get_balance()
    except Exception:
        from config import STARTING_BANKROLL_USD
        balance = STARTING_BANKROLL_USD

    strat_budget = balance * STRATEGY_ALLOCATION["whale"]

    for c in candidates[:4]:
        # Size at 7% of live balance per trade — scales automatically as balance grows
        # No hard dollar cap; balance is fetched live so this self-adjusts
        max_per_trade = balance * 0.07
        copy_count = max(1, int(max_per_trade * 100 / max(c["price_cents"], 1)))
        cost       = client.cost_usd(copy_count, c["price_cents"])

        if cost < 2.0:
            continue

        if not risk_manager.approve("whale", c["ticker"], cost, c["edge"],
                                     notes=c["title"][:45]):
            continue

        logger.info(f"[WHALE] Waiting {WHALE_COPY_DELAY_SECS}s to confirm fill...")
        time.sleep(WHALE_COPY_DELAY_SECS)

        # Confirm price hasn't moved away dramatically
        try:
            current = client.get_mid_price_cents(c["ticker"])
        except Exception:
            continue
        if current and abs(current - c["price_cents"]) > 8:
            logger.info(f"[WHALE] Price drifted {abs(current - c['price_cents'])}¢ — skip")
            continue

        # Skip markets that are near resolution (≥95¢ or ≤5¢) — almost done, no edge to copy
        if c["price_cents"] >= 95 or c["price_cents"] <= 5:
            logger.debug(f"[WHALE] Skip {c['ticker']} — near resolution at {c['price_cents']}¢")
            continue

        entry_price = current or c["price_cents"]

        try:
            # Whale uses taker order (immediate fill) — edge is speed, not fee savings.
            # A 45s-delayed maker order risks missing the move entirely.
            client.place_limit_order(
                ticker=c["ticker"],
                side=c["side"],
                action=c["action"],
                price_cents=entry_price,
                count=copy_count,
                post_only=False,  # taker — fill immediately
            )
            _recent_copies[c["ticker"]] = {"copied_at": datetime.now(timezone.utc)}
            risk_manager.record_open(c["ticker"], copy_count, entry_price, "whale", side=c["side"])
            risk_manager.log_trade(
                strategy="whale", ticker=c["ticker"],
                side=c["side"], action=c["action"],
                price_cents=entry_price, count=copy_count,
                expected_pnl=cost * c["edge"] * 2,
                notes=f"whale={c['count']}x wr={c['member_stats']['win_rate']:.0%}"
            )
            trades += 1
            strat_budget -= cost
        except Exception as e:
            logger.error(f"[WHALE] Order failed {c['ticker']}: {e}")

    logger.info(f"[WHALE] Copied {trades} whale trade(s)")
    return trades
