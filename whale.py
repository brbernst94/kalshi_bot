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
    Pull recent large trades — allowlist only.
    Everything NOT on the allowlist is blocked automatically.
    No more unknown sports leagues bleeding through.
    """
    # Only these prefixes are allowed. Everything else is blocked.
    WHALE_ALLOWLIST = (
        # Crypto 15m
        "KXBTC15M",
        # Crypto daily
        "KXBTCD", "KXETHD", "KXXRPD", "KXSOLD",
        # Weather
        "KXHIGH", "KXLOW", "KXPRECIP",
        # Macro data releases
        "KXCPI", "KXPCE", "KXNFP", "KXGDP", "KXUNRATE",
        "KXFED", "KXFEDDECISION", "KXFOMC",
        "KXSPX", "KXNAS", "KXDOW", "KXGOLD", "KXOIL",
        # Mentions
        "KXPRESMENTION", "KXMENTION",
        # Political / government
        "KXPRES", "KXTRUMP", "KXELECTION",
        "KXCONGRESS", "KXSUPREME", "KXGOVTSHUT",
        "KXHOUSERACE", "KXSENRACE",
    )

    large_fills = []
    try:
        data   = client._get("/markets/trades", params={"limit": 200})
        trades = data.get("trades", [])
    except Exception as e:
        logger.error(f"[WHALE] Market fetch failed: {e}")
        return []

    skipped = 0

    from datetime import datetime, timezone
    hour_utc = datetime.now(timezone.utc).hour
    is_off_peak = hour_utc >= 21 or hour_utc <= 11
    effective_min = max(20, WHALE_MIN_CONTRACTS // 2) if is_off_peak else WHALE_MIN_CONTRACTS

    for t in trades:
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

        # Allowlist filter — block anything not explicitly permitted
        if not is_tracked and not any(ticker.startswith(p) for p in WHALE_ALLOWLIST):
            skipped += 1
            continue

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

    logger.info(f"[WHALE] {len(large_fills)} quality fills ({skipped} non-allowlist skipped)")
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


def scan(client, risk_manager, markets: List[Dict] = None) -> List[Dict]:
    logger.info("[WHALE] Scanning for large conviction fills...")
    raw_fills  = fetch_large_fills(client)

    # Build ticker→market lookup from cache for days check
    from bond import days_to_close
    WHALE_MAX_DAYS = 30  # whale can follow longer-dated conviction fills
    cache = {m.get("ticker", ""): m for m in (markets or [])}

    # Build set of event_tickers already held — one position per event max
    held_events: set = set()
    for held_ticker in risk_manager.open_positions:
        mkt = cache.get(held_ticker)
        if mkt:
            et = mkt.get("event_ticker", "")
            if et:
                held_events.add(et)

    candidates = []
    skip_days = skip_event = skip_cooldown = 0

    for fill in raw_fills:
        ticker    = fill["ticker"]
        member_id = fill["member_id"]
        stats     = get_member_stats(client, member_id)

        # Allow tracked whales OR any large untracked fill (unknown members default to 0.55)
        # Don't block on win_rate if TRACKED_WHALE_MEMBERS is empty
        tracked_only_gate = bool(TRACKED_WHALE_MEMBERS)
        if tracked_only_gate and stats["win_rate"] < WHALE_MIN_WIN_RATE and not fill["is_tracked"]:
            continue

        # ── Days check — skip expired/unknown OR long-dated markets ──────
        mkt  = cache.get(ticker)
        days = days_to_close(mkt) if mkt else None
        if days is None or days > WHALE_MAX_DAYS:
            skip_days += 1
            logger.info(
                f"[WHALE] SKIP_DAYS {ticker} | "
                f"{'not in cache' if not mkt else ('expired' if days is None else f'{days:.1f}d out')}"
            )
            continue

        # ── Event dedup — one position per underlying event ───────────────
        event_ticker = mkt.get("event_ticker", "") if mkt else ""
        if event_ticker and event_ticker in held_events:
            skip_event += 1
            logger.debug(f"[WHALE] SKIP {ticker} — already hold a position in event {event_ticker}")
            continue

        # Cool-down: skip if we already copied this ticker recently
        if ticker in _recent_copies:
            last = _recent_copies[ticker]
            elapsed = (datetime.now(timezone.utc) -
                       last.get("copied_at", datetime.now(timezone.utc))
                       ).total_seconds() / 60
            if elapsed < 30:   # 30-minute cooldown per ticker
                skip_cooldown += 1
                continue

        confidence = min(stats["win_rate"] / 0.75, 1.0)
        edge       = max((stats["win_rate"] - 0.50) * 2, 0.04)

        candidates.append({
            **fill,
            "event_ticker": event_ticker,
            "confidence":   confidence,
            "edge":         edge,
            "member_stats": stats,
            "detected_at":  datetime.now(timezone.utc),
        })
        # Reserve this event so later fills in the same cycle don't double-up
        if event_ticker:
            held_events.add(event_ticker)

    candidates.sort(key=lambda x: x["cost_usd"] * x["confidence"], reverse=True)
    logger.info(
        f"[WHALE] {len(candidates)} actionable | "
        f"filtered: {skip_days} days, {skip_event} event-dup, {skip_cooldown} cooldown"
    )
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

    # Fresh position sync before the loop — prevents placing opposing orders on
    # markets we already hold (YES + NO on same ticker = guaranteed fee loss).
    risk_manager.sync_positions_from_api()

    for c in candidates[:4]:
        # Hard guard: skip if we hold any position on this ticker (any side)
        if c["ticker"] in risk_manager.open_positions:
            held = risk_manager.open_positions[c["ticker"]]
            logger.info(
                f"[WHALE] Skip {c['ticker']} — already hold "
                f"{held.get('side','?').upper()} {held.get('count','?')}x "
                f"(would create competing position)"
            )
            continue
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
