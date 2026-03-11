"""
utils/monitor.py — Position Monitor (Kalshi)
All prices in cents. Stops/targets adjusted for cent-based pricing.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Stop loss: cents the position can move against before we cut
STOPS_CENTS = {
    "bond":     -30,   # Cut if YES drops 30¢ (something materially changed)
    "fade":     -25,   # Cut if price continues the spike direction
    "longshot": -999,  # Hold to resolution (lottery ticket model)
    "whale":    -28,
}

# Take profit: cents of profit at which we close early
TAKE_CENTS = {
    "bond":     None,   # Hold to resolution for full $1.00
    "fade":     10,     # Take 10¢+ gain (quick reversion capture)
    "longshot": 40,     # Take profit if longshot runs to 50¢+ (exit early)
    "whale":    12,     # Trail the whale
}


def check_positions(client, risk_manager) -> int:
    closed = 0
    for ticker, pos in dict(risk_manager.open_positions).items():
        strategy    = pos.get("strategy", "unknown")
        entry_cents = pos.get("entry_cents", 50)
        count       = pos.get("count", 1)

        try:
            mid = client.get_mid_price_cents(ticker)
        except Exception:
            continue
        if mid is None:
            continue

        move_cents = mid - entry_cents
        stop       = STOPS_CENTS.get(strategy, -30)
        take       = TAKE_CENTS.get(strategy)
        reason     = None

        if move_cents <= stop:
            reason = f"STOP {move_cents:+d}¢"
        elif strategy == "bond" and mid >= 97:
            reason = f"BOND_MATURE {mid}¢"
        elif take and move_cents >= take:
            reason = f"TAKE_PROFIT +{move_cents}¢"
        elif strategy == "longshot" and mid >= 50:
            reason = f"LONGSHOT_HIT {mid}¢"

        if reason:
            exit_side   = "yes"
            exit_action = "sell"
            try:
                client.place_limit_order(
                    ticker=ticker, side=exit_side, action=exit_action,
                    price_cents=mid, count=count,
                )
                pnl = risk_manager.record_close(ticker, mid)
                risk_manager.log_trade(
                    strategy, ticker, exit_side, exit_action,
                    mid, count, pnl or 0, "CLOSE", reason
                )
                closed += 1
                logger.info(f"[MONITOR] {strategy.upper()} closed {ticker} | {reason}")
            except Exception as e:
                logger.error(f"[MONITOR] Close failed {ticker}: {e}")

    return closed
