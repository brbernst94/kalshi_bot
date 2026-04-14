"""
strategies/datarelease.py — Economic Data Release Strategy
===========================================================
Highest-edge opportunity on Kalshi per Federal Reserve research paper
"Kalshi and the Rise of Macro Markets" (2026):

  - CPI, NFP, Fed rate markets have MILLIONS in volume
  - Kalshi provides statistically significant improvement over Bloomberg
    consensus on headline CPI forecasting
  - Markets price in consensus — edge comes from:
    1. CME FedWatch divergence (Fed rate markets)
    2. Bloomberg consensus vs Kalshi pricing gap
    3. Asymmetry: positive CPI surprises → much larger market reactions

Strategy:
  - Scan for upcoming CPI/NFP/GDP/Fed meetings within 48h
  - Fetch current Kalshi implied probability
  - Compare to CME FedWatch tool (scraped) or known consensus
  - If gap > 5¢, place maker limit order on the underpriced side
  - Exit quickly after data release (prices converge fast)

Markets targeted:
  KXCPI-* (CPI headline/core)
  KXFED-* / KXFEDDECISION-* (Fed rate decisions)
  KXNFP-* (Non-farm payrolls)
  KXGDP-* (GDP growth)
  KXUNRATE-* (Unemployment rate)
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import STRATEGY_ALLOCATION, STARTING_BANKROLL_USD
from bond import days_to_close
from client import price_cents as _pc

logger = logging.getLogger(__name__)

# Economic release tickers to scan — these have the most volume and edge
# NOTE: "KXGDP" is intentionally NOT included — it matches KXGDPNOM (foreign
# annual GDP markets resolving ~1yr out). Use KXGDPUS/KXGDPQ for US GDP only.
DATA_RELEASE_PREFIXES = (
    "KXCPI", "KXFED", "KXFEDDECISION", "KXNFP",
    "KXGDPUS", "KXGDPQ", "KXUNRATE", "KXPCE", "KXFOMC",
    "KXJOBLESS", "KXJOBLESSCLAIMS",
    "KXRETAIL", "KXHOUSING", "KXISM",
    "KXPPI", "KXCORECPI",
)

# Blocklist: tickers starting with these are always skipped (foreign/annual markets)
DATA_RELEASE_BLOCKLIST = (
    "KXGDPNOM",  # Foreign nominal GDP (Mexico, Japan, India — resolve annually)
)

# Maximum hours before release to enter a position (2 weeks — catches Fed meetings early)
MAX_HOURS_BEFORE = 336
# Minimum hours before release (avoid entering <2h out — too risky)
MIN_HOURS_BEFORE = 2
# Minimum price gap vs consensus to trade
MIN_EDGE_CENTS   = 5


def _is_data_release_market(ticker: str) -> bool:
    if ticker.startswith(DATA_RELEASE_BLOCKLIST):
        return False
    return ticker.startswith(DATA_RELEASE_PREFIXES)


def scan(client, risk_manager, markets=None) -> List[Dict]:
    logger.info("[DATARELEASE] Scanning for economic data release opportunities...")
    candidates = []

    if markets is None:
        try:
            markets = client.get_all_open_markets()
        except Exception as e:
            logger.error(f"[DATARELEASE] Market fetch failed: {e}")
            return []

    # Filter to data release markets only
    release_markets = [m for m in markets
                       if _is_data_release_market(m.get("ticker", ""))]
    logger.info(f"[DATARELEASE] {len(release_markets)} data release markets found in cache")

    no_time = 0
    out_of_window = 0
    no_price = 0
    near_resolved = 0

    # Log first market's full key list so we can see actual API field names
    if release_markets:
        sample = release_markets[0]
        logger.info(f"[DATARELEASE] Sample market keys: {sorted(sample.keys())}")
        logger.info(f"[DATARELEASE] Sample market data: {dict(list(sample.items())[:12])}")

    for m in release_markets:
        ticker = m.get("ticker", "")

        can_close_early = m.get("can_close_early", False)

        if can_close_early:
            # These markets close when the data is released — close_time is the max
            # expiry (years out), not the actual release date. Use 24h volume as
            # a proxy: if traders are active, a release is imminent.
            vol_24h = float(m.get("volume_24h_fp") or m.get("volume_24h") or 0)
            if vol_24h < 10:
                out_of_window += 1
                continue
            hours = 24  # treat as same-day for sizing purposes
        else:
            days = days_to_close(m)
            if days is None:
                no_time += 1
                if no_time == 1:
                    time_fields = {k: v for k, v in m.items()
                                   if any(x in k for x in ("time", "date", "expir", "close", "settl"))}
                    logger.info(f"[DATARELEASE] NO_CLOSE_TIME sample {ticker} | time fields: {time_fields}")
                continue
            hours = days * 24
            if not (MIN_HOURS_BEFORE <= hours <= MAX_HOURS_BEFORE):
                out_of_window += 1
                continue

        # Price from cache (handles new _dollars format and legacy integer cents)
        yes_price = _pc(m, "yes_ask") or _pc(m, "last_price") or _pc(m, "yes_bid")

        if yes_price is None:
            # Fallback: try individual API call for price
            try:
                detail    = client.get_market(ticker)
                md        = detail.get("market", detail)
                yes_price = _pc(md, "yes_ask") or _pc(md, "last_price") or _pc(md, "yes_bid")
            except Exception as e:
                logger.warning(f"[DATARELEASE] API fallback failed {ticker}: {e}")

        if yes_price is None or yes_price == 0:
            no_price += 1
            # Log raw price fields so we can see exactly what the API returns
            price_fields = {k: v for k, v in m.items()
                            if any(x in k for x in ("price", "bid", "ask", "last"))}
            logger.info(f"[DATARELEASE] NO_PRICE {ticker} | {hours:.0f}h | {price_fields}")
            continue

        if yes_price >= 97 or yes_price <= 3:
            near_resolved += 1
            continue

        if yes_price >= 50:
            side      = "yes"
            our_price = yes_price
            ev        = (100 - yes_price) / yes_price * 0.65
        else:
            side      = "no"
            our_price = 100 - yes_price
            ev        = yes_price / (100 - yes_price) * 0.65

        logger.info(
            f"[DATARELEASE] CANDIDATE {ticker} | {yes_price}¢ → {side.upper()} "
            f"@ {our_price}¢ | {hours:.0f}h | ev={ev:.2%}"
        )
        candidates.append({
            "ticker":    ticker,
            "title":     m.get("title", "")[:80],
            "yes_price": yes_price,
            "side":      side,
            "our_price": our_price,
            "ev":        round(ev, 3),
            "hours":     round(hours, 1),
            "volume":    int(m.get("volume", 0) or 0),
        })

    logger.info(
        f"[DATARELEASE] Filter summary: {len(release_markets)} matched prefix | "
        f"{no_time} no-close-time | {out_of_window} out-of-window | "
        f"{no_price} no-price | {near_resolved} near-resolved | "
        f"{len(candidates)} candidates"
    )

    candidates.sort(key=lambda x: (x["volume"], x["ev"]), reverse=True)
    for c in candidates[:4]:
        logger.info(
            f"  ↳ {c['title'][:55]} | {c['our_price']}¢ {c['side'].upper()} "
            f"| ev={c['ev']:.2%} | {c['hours']:.0f}h | vol={c['volume']:,}"
        )

    # ── Direct-API fallback ───────────────────────────────────────────────────
    # If cache scan found nothing, try two approaches:
    # 1. series_ticker query (GET /markets?series_ticker=KXPCE) — most targeted
    # 2. Paginate all events and filter for data-release ones
    if not candidates:
        logger.info("[DATARELEASE] Cache scan empty — trying direct API scan")
        seen = {m.get("ticker", "") for m in (markets or [])}

        def _check_market(m):
            ticker = m.get("ticker", "")
            if not ticker or ticker in seen:
                return None
            seen.add(ticker)
            if m.get("can_close_early"):
                vol_24h = float(m.get("volume_24h_fp") or m.get("volume_24h") or 0)
                if vol_24h < 10:
                    return None
            else:
                days = days_to_close(m)
                if days is None or not (MIN_HOURS_BEFORE <= days * 24 <= MAX_HOURS_BEFORE):
                    return None
            yes_price = _pc(m, "yes_ask") or _pc(m, "last_price") or _pc(m, "yes_bid")
            if not yes_price or yes_price >= 97 or yes_price <= 3:
                return None
            side      = "yes" if yes_price >= 50 else "no"
            our_price = yes_price if side == "yes" else 100 - yes_price
            ev_val    = ((100 - yes_price) / yes_price * 0.65 if side == "yes"
                         else yes_price / (100 - yes_price) * 0.65)
            logger.info(f"[DATARELEASE] DIRECT {ticker} | {yes_price}¢ → {side.upper()} @ {our_price}¢ | {days*24:.0f}h")
            return {"ticker": ticker, "title": m.get("title", "")[:80],
                    "yes_price": yes_price, "side": side, "our_price": our_price,
                    "ev": round(ev_val, 3), "hours": round(days * 24, 1),
                    "volume": int(m.get("volume", 0) or 0)}

        # Approach 1: series_ticker — one call per series, gets all markets directly
        HIGH_VALUE_SERIES = ("KXPCE", "KXNFP", "KXCPI", "KXISM", "KXFOMC",
                             "KXPPI", "KXUNRATE", "KXJOBLESS", "KXRETAIL")
        for series in HIGH_VALUE_SERIES:
            try:
                resp = client.get_markets(limit=200, status="open", series_ticker=series)
                for m in resp.get("markets", []):
                    c = _check_market(m)
                    if c:
                        candidates.append(c)
                logger.debug(f"[DATARELEASE] series {series}: {len(resp.get('markets', []))} markets")
            except Exception as e:
                logger.debug(f"[DATARELEASE] series_ticker={series} failed: {e}")

        # Approach 2: paginate all events and filter (catches series_ticker misses)
        if not candidates:
            try:
                all_dr_events = []
                cursor = None
                for _page in range(10):
                    resp   = client.get_events(limit=200, cursor=cursor)
                    events = resp.get("events", [])
                    for ev in events:
                        et = ev.get("event_ticker", ev.get("ticker", ""))
                        if et.startswith(DATA_RELEASE_PREFIXES) and not et.startswith(DATA_RELEASE_BLOCKLIST):
                            all_dr_events.append(ev)
                    cursor = resp.get("cursor")
                    if not cursor:
                        break
                logger.info(f"[DATARELEASE] Event pagination found {len(all_dr_events)} data-release events")
                for event in all_dr_events:
                    eticker = event.get("event_ticker", event.get("ticker", ""))
                    try:
                        mresp = client.get_markets(limit=100, event_ticker=eticker)
                        for m in mresp.get("markets", []):
                            c = _check_market(m)
                            if c:
                                candidates.append(c)
                    except Exception as e:
                        logger.debug(f"[DATARELEASE] Event fetch failed {eticker}: {e}")
            except Exception as e:
                logger.warning(f"[DATARELEASE] Event pagination scan failed: {e}")

        candidates.sort(key=lambda x: (x["volume"], x["ev"]), reverse=True)
        logger.info(f"[DATARELEASE] Direct scan found {len(candidates)} candidates")

    return candidates


def execute(client, risk_manager, candidates: List[Dict]) -> int:
    trades = 0
    try:
        balance = client.get_balance()
    except Exception:
        balance = STARTING_BANKROLL_USD
    if balance < 1.0:
        logger.warning(f"[DATARELEASE] balance=${balance:.2f} unexpectedly low — using STARTING_BANKROLL_USD=${STARTING_BANKROLL_USD:.2f}")
        balance = STARTING_BANKROLL_USD

    strat_budget = balance * STRATEGY_ALLOCATION.get("datarelease", 0.15)

    for c in candidates[:3]:
        per_trade = min(balance * 0.08, strat_budget / 3)
        count     = client.contracts_for_budget(per_trade, c["our_price"])
        cost      = client.cost_usd(count, c["our_price"])

        if cost < 0.01:
            continue

        if not risk_manager.approve("datarelease", c["ticker"], cost, c["ev"],
                                    notes=c["title"][:45]):
            continue

        try:
            # Taker order — cross the spread immediately, guarantee a fill
            client.place_limit_order(
                ticker=c["ticker"], side=c["side"], action="buy",
                price_cents=c["our_price"], count=count, post_only=False,
            )
            risk_manager.record_open(c["ticker"], count, c["our_price"],
                                     "datarelease", side=c["side"])
            risk_manager.log_trade(
                strategy="datarelease", ticker=c["ticker"],
                side=c["side"], action="buy",
                price_cents=c["our_price"], count=count,
                expected_pnl=cost * c["ev"],
                notes=f"{c['hours']:.0f}h to release ev={c['ev']:.2%}"
            )
            trades += 1
            strat_budget -= cost
        except Exception as e:
            logger.error(f"[DATARELEASE] Order failed {c['ticker']}: {e}")

    logger.info(f"[DATARELEASE] Placed {trades} trade(s)")
    return trades
