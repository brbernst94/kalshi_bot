"""
monitor.py — Position Monitor (Kalshi)
========================================
Runs every 3 minutes. Checks every open position against 4 exit signals:

  1. RESOLUTION      — market resolved (price at 99¢ or 1¢), exit immediately
  2. NEAR_CERTAIN    — price ≥97¢, redeploy capital rather than wait
  3. TAKE PROFIT     — price gained 70% from entry (e.g. 50¢ → 85¢)
  4. STOP LOSS       — price dropped 10% from entry (e.g. 50¢ → 45¢)

Also provides cleanup_long_dated_positions() which sweeps ALL portfolio
positions (including manually placed ones) and exits anything resolving
more than MAX_POSITION_DAYS out. Runs at startup then every 4 hours.

All prices in cents (1–99). Each contract pays $1.00 at resolution.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bond import days_to_close
from config import MAX_POSITION_DAYS

logger = logging.getLogger(__name__)

# ── Global stop loss / take profit (percentage of entry price) ────────────────
STOP_LOSS_PCT   = 0.10   # Exit if price drops 10% from entry (e.g. 50¢ → 45¢)
TAKE_PROFIT_PCT = 0.70   # Exit if price gains 70% from entry (e.g. 50¢ → 85¢)

# Resolution thresholds
RESOLUTION_YES = 98
RESOLUTION_NO  = 2


def check_positions(client, risk_manager) -> int:
    """
    Check all open positions against all exit signals.
    Returns number of positions closed this cycle.
    """
    closed = 0

    for ticker, pos in dict(risk_manager.open_positions).items():
        strategy    = pos.get("strategy", "unknown")
        entry_cents = pos.get("entry_cents", 50)
        count       = pos.get("count", 1)

        try:
            mid = client.get_mid_price_cents(ticker)
        except Exception as e:
            logger.debug(f"[MONITOR] Price fetch failed {ticker}: {e}")
            continue
        if mid is None:
            continue

        move = mid - entry_cents

        # Compute percentage-based thresholds for this position
        stop_loss_price   = round(entry_cents * (1 - STOP_LOSS_PCT))
        take_profit_price = min(round(entry_cents * (1 + TAKE_PROFIT_PCT)), 97)

        # ── 4 exit checks (priority order) ────────────────────────────────
        reason = None

        # 1. Resolution
        if mid >= RESOLUTION_YES:
            reason = f"RESOLVED_YES {mid}¢"
        elif mid <= RESOLUTION_NO:
            reason = f"RESOLVED_NO {mid}¢"

        # 2. Near-certain — free up capital rather than sit on <$0.50 upside
        if not reason:
            is_no_leg = ticker.endswith("_NO") or pos.get("side") == "no"
            winning_price = mid if not is_no_leg else (100 - mid)
            if winning_price >= 97:
                remaining_upside_cents = 100 - winning_price
                reason = f"NEAR_CERTAIN {winning_price}¢ (${remaining_upside_cents*count/100:.2f} left — redeploying)"

        # 3. Take profit — 70% gain on entry
        if not reason and mid >= take_profit_price:
            reason = f"TAKE_PROFIT {mid}¢ (entry={entry_cents}¢ target={take_profit_price}¢)"

        # 4. Stop loss — 10% drop from entry
        if not reason and mid <= stop_loss_price:
            reason = f"STOP_LOSS {mid}¢ (entry={entry_cents}¢ floor={stop_loss_price}¢)"

        # ── Execute exit ───────────────────────────────────────────────────
        if reason:
            if "RESOLVED_YES" in reason:
                exit_price = min(mid, 98)
            elif "RESOLVED_NO" in reason:
                exit_price = max(mid, 2)
            else:
                exit_price = mid

            try:
                # Determine correct exit side:
                # net_position > 0 = we hold YES contracts → sell YES
                # net_position < 0 = we hold NO contracts → sell NO
                net = pos.get("net_position", pos.get("count", 1))
                if "side" in pos:
                    exit_side = pos["side"]
                elif int(net) < 0:
                    exit_side = "no"
                else:
                    exit_side = "yes"
                try:
                    client.place_limit_order(
                        ticker=ticker, side=exit_side, action="sell",
                        price_cents=exit_price, count=count,
                    )
                except Exception as e:
                    if "400" in str(e):
                        # Try opposite side — position may have been entered differently
                        flip_side = "no" if exit_side == "yes" else "yes"
                        try:
                            client.place_limit_order(
                                ticker=ticker, side=flip_side, action="sell",
                                price_cents=exit_price, count=count,
                            )
                            exit_side = flip_side
                        except Exception:
                            # Last resort: 5¢ lower on original side
                            fallback_price = max(exit_price - 5, 1)
                            client.place_limit_order(
                                ticker=ticker, side=exit_side, action="sell",
                                price_cents=fallback_price, count=count,
                            )
                            exit_price = fallback_price
                    else:
                        raise
                pnl = risk_manager.record_close(ticker, exit_price)
                risk_manager.log_trade(
                    strategy, ticker, "yes", "sell",
                    exit_price, count, pnl or 0, "CLOSE", reason
                )
                closed += 1
                logger.info(
                    f"[MONITOR] EXIT | {strategy.upper()} | {ticker} | "
                    f"{reason} | entry={entry_cents}¢ → exit={exit_price}¢ | "
                    f"pnl=${pnl:+.2f}"
                )
            except Exception as e:
                logger.error(f"[MONITOR] Exit failed {ticker}: {e}")

    if closed:
        logger.info(f"[MONITOR] {closed} position(s) closed this cycle")

    return closed


def cleanup_long_dated_positions(client, risk_manager,
                                  markets: Optional[List[Dict]] = None) -> int:
    """
    Sweep ALL portfolio positions and exit any resolving beyond MAX_POSITION_DAYS.

    Root cause of previous failures: Kalshi's /portfolio/positions API returns
    a close_time field that represents the *trading-window* close (often today),
    NOT the settlement/resolution date. Reading it made every position look
    short-dated, so cleanup always exited 0.

    Fix: resolve dates from the market cache (passed in from main.py), which
    contains the real expiry dates fetched from the markets API. Zero extra
    API calls — the cache is already loaded every 5 minutes anyway.

    For tickers not in the cache (manually placed in categories we don't scan),
    falls back to a direct GET /markets/{ticker}.
    """
    exited  = 0
    no_date = 0
    from client import price_cents as _pc

    # Build a ticker → market dict from the cache for O(1) lookups
    cache: Dict[str, Dict] = {}
    if markets:
        for m in markets:
            t = m.get("ticker", "")
            if t:
                cache[t] = m

    logger.info(f"[CLEANUP] Market cache has {len(cache)} entries")

    try:
        all_positions: List[Dict] = client.get_positions()
    except Exception as e:
        logger.error(f"[CLEANUP] Failed to fetch portfolio positions: {e}")
        return 0

    logger.info(f"[CLEANUP] Scanning {len(all_positions)} positions "
                f"(>{MAX_POSITION_DAYS}d = exit)...")

    for pos in all_positions:
        ticker = pos.get("ticker") or pos.get("market_ticker", "")
        if not ticker:
            continue

        net = int(pos.get("net_position", pos.get("position", 0)) or 0)
        if net == 0:
            continue

        side = "yes" if net > 0 else "no"
        qty  = abs(net)

        # ── Get resolution date from MARKET data, not position object ────────
        # The position object's close_time = trading window close (often today).
        # The market object's close_time = actual resolution date. Use that.
        mkt_data = cache.get(ticker)

        if not mkt_data:
            # Not in cache (manually placed, or category we don't scan) — fetch
            try:
                raw      = client.get_market(ticker)
                mkt_data = raw.get("market", raw)
                time.sleep(0.15)
            except Exception as e:
                logger.debug(f"[CLEANUP] Can't fetch market {ticker}: {e}")
                no_date += 1
                continue

        days = days_to_close(mkt_data)

        if days is None:
            # Already resolved or date unparseable
            logger.debug(f"[CLEANUP] No resolution date for {ticker} — skipping")
            no_date += 1
            continue

        if days <= MAX_POSITION_DAYS:
            logger.debug(f"[CLEANUP] {ticker} {days:.0f}d — keeping")
            continue

        # ── This position is too far out — sell it ────────────────────────────
        logger.info(f"[CLEANUP] SELLING {ticker} | {side.upper()} x{qty} | {days:.0f}d out")

        try:
            mid = client.get_mid_price_cents(ticker)
        except Exception:
            mid = None

        if not mid:
            mid = (_pc(pos, "yes_price") if side == "yes" else _pc(pos, "no_price")) or 50

        exit_price = max(mid - 2, 1)

        try:
            client.place_limit_order(
                ticker=ticker, side=side, action="sell",
                price_cents=exit_price, count=qty,
            )

            if ticker in risk_manager.open_positions:
                pnl = risk_manager.record_close(ticker, exit_price)
            else:
                avg_cents = _pc(pos, "average_price") or 50
                pnl = (exit_price - avg_cents) * qty / 100

            risk_manager.log_trade(
                strategy="cleanup", ticker=ticker, side=side, action="sell",
                price_cents=exit_price, count=qty, expected_pnl=pnl,
                status="CLOSE",
                notes=f"LONG_DATED {days:.0f}d > {MAX_POSITION_DAYS}d limit"
            )
            exited += 1
            logger.info(
                f"[CLEANUP] EXIT {ticker} | {side.upper()} x{qty} @ {exit_price}¢ | "
                f"{days:.0f}d | est. pnl=${pnl:+.2f}"
            )
            time.sleep(0.4)

        except Exception as e:
            logger.error(f"[CLEANUP] Exit order failed for {ticker}: {e}")

    logger.info(f"[CLEANUP] Done — {exited} exited, {no_date} skipped (no date)")
    return exited
