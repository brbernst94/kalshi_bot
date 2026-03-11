"""
monitor.py — Position Monitor (Kalshi)
========================================
Runs every 1 minute. Checks every open position against 6 exit signals:

  1. STOP LOSS       — price moved against us past threshold
  2. TAKE PROFIT     — hit target gain, lock it in
  3. TRAILING STOP   — price ran our way then reversed — protect gains
  4. TIME STOP       — held too long with no resolution, cut and redeploy
  5. RESOLUTION      — market resolved (price at 99¢ or 1¢), exit immediately
  6. FADE REVERSAL   — fade position but momentum still going wrong way

All prices in cents (1–99). Each contract pays $1.00 at resolution.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ── Per-strategy stop loss ─────────────────────────────────────────────────────
STOP_LOSS_CENTS = {
    "bond":     -12,   # Near-certain market — if drops 12¢ something changed
    "fade":     -8,    # Short-duration — cut quickly if wrong
    "longshot": -999,  # Lottery ticket — hold to resolution
    "whale":    -10,   # Tightened from -15: sports games move fast
}

# ── Take profit ────────────────────────────────────────────────────────────────
TAKE_PROFIT_CENTS = {
    "bond":     None,  # Hold to resolution for full $1.00 payout
    "fade":     8,     # Reversion captured
    "longshot": 35,    # Longshot ran — take profit before reversal
    "whale":    10,    # Trail the whale
}

# ── Trailing stop ─────────────────────────────────────────────────────────────
# ACTIVATE: cents of gain before trailing stop kicks in
# TRAIL:    how many cents below peak we allow before exiting
TRAIL_ACTIVATE = {"fade": 5,  "longshot": 20, "whale": 6}
TRAIL_DISTANCE = {"fade": 4,  "longshot": 15, "whale": 5}

# ── Time stop (hours) ─────────────────────────────────────────────────────────
TIME_STOP_HOURS = {
    "bond":     72,    # 3 days
    "fade":     6,     # 6 hours — wrong if no reversion by then
    "longshot": 240,   # 10 days
    "whale":    24,    # 1 day
}

# Resolution thresholds
RESOLUTION_YES = 98
RESOLUTION_NO  = 2

# Fade momentum bail — if price moves another X¢ wrong since last check, exit
FADE_MOMENTUM_BAIL = 5


def _check_trailing_stop(strategy: str, pos: Dict, mid: int) -> Optional[str]:
    activate = TRAIL_ACTIVATE.get(strategy)
    trail    = TRAIL_DISTANCE.get(strategy)
    if not activate or not trail:
        return None
    entry    = pos.get("entry_cents", 50)
    peak     = pos.get("peak_cents", entry)
    if (peak - entry) < activate:
        return None
    trail_level = peak - trail
    if mid <= trail_level:
        return f"TRAIL_STOP peak={peak}¢ now={mid}¢ floor={trail_level}¢"
    return None


def _check_time_stop(strategy: str, pos: Dict) -> Optional[str]:
    max_h     = TIME_STOP_HOURS.get(strategy)
    opened_at = pos.get("opened_at")
    if not max_h or not opened_at:
        return None
    age_h = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600
    if age_h >= max_h:
        return f"TIME_STOP age={age_h:.1f}h max={max_h}h"
    return None


def _check_fade_reversal(pos: Dict, mid: int) -> Optional[str]:
    entry     = pos.get("entry_cents", 50)
    last_seen = pos.get("last_seen_cents", entry)
    momentum  = mid - last_seen  # negative = still falling against us
    move      = mid - entry
    if move < 0 and momentum <= -FADE_MOMENTUM_BAIL:
        return f"FADE_REVERSAL entry={entry}¢ now={mid}¢ momentum={momentum:+d}¢"
    return None


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

        # Update peak for trailing stop
        pos["peak_cents"] = max(pos.get("peak_cents", entry_cents), mid)
        last_seen = pos.get("last_seen_cents", entry_cents)
        pos["last_seen_cents"] = mid

        # ── 6 exit checks (priority order) ────────────────────────────────
        reason = None

        # 1. Resolution
        if mid >= RESOLUTION_YES:
            reason = f"RESOLVED_YES {mid}¢"
        elif mid <= RESOLUTION_NO:
            reason = f"RESOLVED_NO {mid}¢"

        # 2. Take profit
        take = TAKE_PROFIT_CENTS.get(strategy)
        if not reason and take and move >= take:
            reason = f"TAKE_PROFIT +{move}¢"

        # 3. Trailing stop
        if not reason:
            reason = _check_trailing_stop(strategy, pos, mid)

        # 4. Fade reversal
        if not reason and strategy == "fade":
            reason = _check_fade_reversal(pos, mid)

        # 5. Stop loss
        stop = STOP_LOSS_CENTS.get(strategy, -20)
        if not reason and move <= stop:
            reason = f"STOP_LOSS {move:+d}¢"

        # 6. Time stop
        if not reason:
            reason = _check_time_stop(strategy, pos)

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
