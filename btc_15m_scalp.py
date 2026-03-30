"""
btc_15m_scalp.py — BTC 15-minute final-5-minute breakout scalper
=================================================================
Run standalone:  python btc_15m_scalp.py
Dry-run mode:    python btc_15m_scalp.py --dry-run

Strategy:
  • BTC 15-min markets (KXBTC15M-*) close at :00, :15, :30, :45 UTC every hour
  • 5 minutes before each close, this script wakes up and polls the price at 5 Hz
  • ENTRY: when YES or NO price crosses above 75¢ from below
            - If price is already ≥ 75¢ when the window opens → no entry (must cross)
  • STOP GAIN: exit at 95¢ — done for this window
  • STOP LOSS: exit at 65¢ — 30s cooldown, then watch for another crossover
  • At market close: hold any open position to resolution ($1/contract payout)

Tunable constants are at the top of this file.
"""

import argparse
import logging
import os
import sys
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

# ── Use the project's Kalshi client (handles RSA-PSS auth) ───────────────────
from client import KalshiClient, price_cents as _pc
from config import STARTING_BANKROLL_USD

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("btc15m")

# ── Tunable parameters ────────────────────────────────────────────────────────
ENTRY_CENTS        = 75    # Enter when side crosses above this FROM BELOW
STOP_LOSS_CENTS    = 65    # Exit if position price drops to this
STOP_GAIN_CENTS    = 95    # Take profit when position price reaches this
POLL_INTERVAL_S    = 0.2   # 5 Hz price polling (stays within Kalshi rate limits)
WINDOW_MINUTES     = 5     # Minutes before market close to start watching
POSITION_PCT       = 0.07  # 7% of account balance per trade
MIN_TRADE_USD      = 2.00  # Skip trades smaller than this
REENTRY_COOLDOWN_S = 30    # Wait this long after a stop loss before re-entering

BTC15M_PREFIXES = ("KXBTC15M", "KXBTC-15M", "KXBTC15MIN")

# ── State ─────────────────────────────────────────────────────────────────────
_stop_flag = threading.Event()


# ── Timing ────────────────────────────────────────────────────────────────────

def _next_close() -> datetime:
    """
    Returns the UTC time of the NEXT BTC 15-min market close.
    Closes occur at :00, :15, :30, :45 of every UTC hour.
    """
    now     = datetime.now(timezone.utc)
    next_15 = ((now.minute // 15) + 1) * 15
    if next_15 < 60:
        return now.replace(minute=next_15, second=0, microsecond=0)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _sleep_until(target: datetime) -> None:
    """Sleep until target UTC time, checking _stop_flag every second."""
    while not _stop_flag.is_set():
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


# ── Market discovery ──────────────────────────────────────────────────────────

def _parse_close_time(m: dict) -> Optional[datetime]:
    for field in ("close_time", "expiration_time", "close_date"):
        s = m.get(field)
        if s:
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
    return None


def find_btc15m_market(client: KalshiClient) -> Optional[Tuple[str, datetime]]:
    """
    Find the active BTC 15-min market with the nearest future close time.
    Returns (ticker, close_time_utc) or None.

    Fast path: filtered query with event_ticker=KXBTC15M
    Slow path: paginate all open markets and scan for KXBTC15M* prefix
    """
    now         = datetime.now(timezone.utc)
    best_ticker = None
    best_close:  Optional[datetime] = None

    def _consider(m: dict):
        nonlocal best_ticker, best_close
        t = m.get("ticker", "")
        if not any(t.startswith(p) for p in BTC15M_PREFIXES):
            return
        close_dt = _parse_close_time(m)
        if not close_dt or close_dt <= now:
            return
        if best_close is None or close_dt < best_close:
            best_ticker = t
            best_close  = close_dt

    # Fast path
    try:
        data = client.get_markets(limit=20, event_ticker="KXBTC15M", status="open")
        for m in data.get("markets", []):
            _consider(m)
        if best_ticker:
            return (best_ticker, best_close)
    except Exception:
        pass

    # Slow path: paginate
    try:
        cursor = None
        for _ in range(5):
            data   = client.get_markets(limit=200, cursor=cursor, status="open")
            for m in data.get("markets", []):
                _consider(m)
            cursor = data.get("cursor")
            if not cursor or best_ticker:
                break
    except Exception as e:
        logger.error(f"Market lookup failed: {e}")

    return (best_ticker, best_close) if best_ticker else None


# ── Price feed ────────────────────────────────────────────────────────────────

def get_yes_price(client: KalshiClient, ticker: str) -> Optional[int]:
    """
    Fetch current YES price in cents (1–99) via direct market endpoint.
    Returns None on any error (caller should retry on next tick).
    """
    try:
        data = client._get(f"/markets/{ticker}")
        m    = data.get("market", data)
        # Prefer yes_ask (what we'd pay as taker), fall back to last trade or bid
        return _pc(m, "yes_ask") or _pc(m, "last_price") or _pc(m, "yes_bid")
    except Exception:
        return None


# ── Order helpers ─────────────────────────────────────────────────────────────

def place_taker_buy(client: KalshiClient, ticker: str, side: str,
                    price_cents: int, count: int) -> bool:
    """Taker buy order. Returns True on success."""
    try:
        client.place_limit_order(
            ticker=ticker,
            side=side,
            action="buy",
            price_cents=price_cents,
            count=count,
            post_only=False,   # taker — fill immediately at market
        )
        return True
    except Exception as e:
        logger.error(f"Buy order failed: {e}")
        return False


def place_taker_sell(client: KalshiClient, ticker: str, side: str,
                     price_cents: int, count: int) -> bool:
    """
    Taker sell order. Undercuts by 2¢ to guarantee fill.
    Returns True on success.
    """
    sell_price = max(1, price_cents - 2)
    try:
        client.place_limit_order(
            ticker=ticker,
            side=side,
            action="sell",
            price_cents=sell_price,
            count=count,
            post_only=False,
        )
        return True
    except Exception as e:
        logger.error(f"Sell order failed: {e}")
        return False


# ── Core 5-minute loop ────────────────────────────────────────────────────────

def scalp_window(client: KalshiClient, ticker: str, close_time: datetime,
                 dry_run: bool = False) -> None:
    """
    The hot loop. Runs from window_start until close_time.

    State machine:
      WATCHING  →  entry fires when a crossover is detected
      HOLDING   →  monitors for stop gain (95¢) or stop loss (65¢)
      DONE      →  stop gain hit, no more entries this window
    """
    remaining = (close_time - datetime.now(timezone.utc)).total_seconds()
    logger.info(
        f"━━━ WINDOW OPEN ━━━  {ticker}  |  {remaining:.0f}s  |  "
        f"ENTRY≥{ENTRY_CENTS}¢  SL={STOP_LOSS_CENTS}¢  SG={STOP_GAIN_CENTS}¢"
    )

    # Crossover tracking — must see price BELOW threshold before it can trigger
    seen_yes_below = False   # YES has been < ENTRY_CENTS
    seen_no_below  = False   # NO  has been < ENTRY_CENTS  (i.e. YES > 100 - ENTRY_CENTS)

    # Position tracking
    holding     = False
    pos_side    = ""     # "yes" | "no"
    pos_count   = 0
    pos_entry   = 0      # cents
    last_sl_ts  = 0.0    # time.time() of last stop loss
    done        = False  # window finished (stop gain hit)

    while not _stop_flag.is_set() and not done:
        if datetime.now(timezone.utc) >= close_time:
            break

        yes_px = get_yes_price(client, ticker)
        if yes_px is None:
            time.sleep(POLL_INTERVAL_S)
            continue

        no_px = 100 - yes_px

        if holding:
            # ── Manage the open position ──────────────────────────────────
            pos_px = yes_px if pos_side == "yes" else no_px
            secs   = (close_time - datetime.now(timezone.utc)).total_seconds()

            if pos_px >= STOP_GAIN_CENTS:
                logger.info(
                    f"🎯 STOP GAIN  {pos_side.upper()} x{pos_count} "
                    f"@ {pos_px}¢  ({secs:.0f}s left)"
                )
                if not dry_run:
                    place_taker_sell(client, ticker, pos_side, pos_px, pos_count)
                    pnl = (pos_px - 2 - pos_entry) * pos_count / 100
                    logger.info(f"   PnL ≈ ${pnl:+.2f}")
                done = True

            elif pos_px <= STOP_LOSS_CENTS:
                logger.info(
                    f"🛑 STOP LOSS  {pos_side.upper()} x{pos_count} "
                    f"@ {pos_px}¢  ({secs:.0f}s left) — will re-watch"
                )
                if not dry_run:
                    place_taker_sell(client, ticker, pos_side, pos_px, pos_count)
                    pnl = (pos_px - 2 - pos_entry) * pos_count / 100
                    logger.info(f"   PnL ≈ ${pnl:+.2f}")
                holding        = False
                last_sl_ts     = time.time()
                # Require a FRESH crossover before re-entering
                seen_yes_below = False
                seen_no_below  = False

        else:
            # ── Watch for a crossover ─────────────────────────────────────

            if yes_px < ENTRY_CENTS:
                seen_yes_below = True
            if no_px < ENTRY_CENTS:       # no_px < 75  ↔  yes_px > 25
                seen_no_below = True

            cooldown_active = (time.time() - last_sl_ts) < REENTRY_COOLDOWN_S

            if not cooldown_active:
                entry_side  = ""
                entry_price = 0

                if seen_yes_below and yes_px >= ENTRY_CENTS:
                    entry_side  = "yes"
                    entry_price = yes_px
                elif seen_no_below and no_px >= ENTRY_CENTS:
                    entry_side  = "no"
                    entry_price = no_px

                if entry_side:
                    secs = (close_time - datetime.now(timezone.utc)).total_seconds()
                    logger.info(
                        f"🔼 BREAKOUT  {entry_side.upper()} @ {entry_price}¢  "
                        f"({secs:.0f}s left)"
                    )

                    if not dry_run:
                        try:
                            balance = client.get_balance()
                        except Exception:
                            balance = STARTING_BANKROLL_USD

                        count    = max(1, int(balance * POSITION_PCT * 100
                                              / max(entry_price, 1)))
                        cost_usd = count * entry_price / 100

                        if cost_usd >= MIN_TRADE_USD:
                            ok = place_taker_buy(client, ticker,
                                                 entry_side, entry_price, count)
                            if ok:
                                holding    = True
                                pos_side   = entry_side
                                pos_count  = count
                                pos_entry  = entry_price
                                logger.info(
                                    f"✅ Entered {entry_side.upper()} "
                                    f"x{count} @ {entry_price}¢  "
                                    f"cost=${cost_usd:.2f}"
                                )
                                # Reset so we don't re-fire on the same candle
                                seen_yes_below = False
                                seen_no_below  = False
                        else:
                            logger.debug(f"Trade too small (${cost_usd:.2f})")
                    else:
                        # Dry-run: simulate entry
                        try:
                            balance = client.get_balance()
                        except Exception:
                            balance = STARTING_BANKROLL_USD
                        count = max(1, int(balance * POSITION_PCT * 100
                                          / max(entry_price, 1)))
                        holding    = True
                        pos_side   = entry_side
                        pos_count  = count
                        pos_entry  = entry_price
                        logger.info(
                            f"[DRY-RUN] Would enter {entry_side.upper()} "
                            f"x{count} @ {entry_price}¢"
                        )
                        seen_yes_below = False
                        seen_no_below  = False

        time.sleep(POLL_INTERVAL_S)

    # ── Window closed ─────────────────────────────────────────────────────────
    if holding:
        secs = max(0, (close_time - datetime.now(timezone.utc)).total_seconds())
        logger.info(
            f"━━━ WINDOW CLOSED ━━━  Holding {pos_side.upper()} x{pos_count} "
            f"@ {pos_entry}¢  →  resolves in ~{secs:.0f}s"
        )
    elif done:
        logger.info("━━━ WINDOW CLOSED ━━━  Stop gain taken ✓")
    else:
        logger.info("━━━ WINDOW CLOSED ━━━  No position")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(client: KalshiClient, dry_run: bool = False) -> None:
    """
    Main perpetual loop. Runs forever until KeyboardInterrupt or _stop_flag.

      1. Compute next window start (5 min before close)
      2. Sleep until then
      3. Find the active BTC 15-min market
      4. Run the hot-loop scalper
      5. Repeat
    """
    bal = client.get_balance()
    logger.info(
        f"BTC 15m Scalper ready | balance=${bal:.2f} | "
        f"{'DRY-RUN' if dry_run else 'LIVE 🔴'}"
    )
    logger.info(
        f"Parameters: entry≥{ENTRY_CENTS}¢  SL={STOP_LOSS_CENTS}¢  "
        f"SG={STOP_GAIN_CENTS}¢  size={POSITION_PCT:.0%}  poll={POLL_INTERVAL_S}s"
    )

    while not _stop_flag.is_set():
        close_time   = _next_close()
        window_start = close_time - timedelta(minutes=WINDOW_MINUTES)
        now          = datetime.now(timezone.utc)
        sleep_secs   = (window_start - now).total_seconds()

        # Show next window in both UTC and MST (UTC-7)
        close_utc = close_time.strftime("%H:%M UTC")
        close_mst = (close_time - timedelta(hours=7)).strftime("%H:%M MST")
        close_mdt = (close_time - timedelta(hours=6)).strftime("%H:%M MDT")

        if sleep_secs > 2:
            logger.info(
                f"Waiting {sleep_secs:.0f}s → next window closes "
                f"{close_utc}  /  {close_mst}  /  {close_mdt}"
            )
            _sleep_until(window_start)

        if _stop_flag.is_set():
            break

        # Resolve market ticker right before the window
        result = find_btc15m_market(client)
        if not result:
            logger.warning(
                "No active BTC 15-min market found — "
                "will retry next cycle"
            )
            time.sleep(60)
            continue

        ticker, market_close = result
        logger.info(
            f"Market locked: {ticker}  "
            f"closes {market_close.strftime('%H:%M:%S')} UTC"
        )

        scalp_window(client, ticker, market_close, dry_run=dry_run)

        # Short pause to avoid a tight loop at the boundary
        time.sleep(5)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import signal

    parser = argparse.ArgumentParser(
        description="BTC 15-minute breakout scalper for Kalshi"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Watch and log signals but place no real orders"
    )
    args = argparse.Namespace(**vars(parser.parse_args()))

    client = KalshiClient()

    def _shutdown(sig, frame):
        logger.info("Shutting down…")
        _stop_flag.set()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    run(client, dry_run=args.dry_run)
