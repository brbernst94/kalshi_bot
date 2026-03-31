"""
btc_15m_scalp.py — BTC 15-minute final-5-minute breakout scalper
=================================================================
Run standalone:  python btc_15m_scalp.py
Dry-run mode:    python btc_15m_scalp.py --dry-run

SPEED:
  Primary:   Kalshi WebSocket feed — price updates pushed in real-time
             (<50ms latency, no polling overhead)
  Fallback:  REST polling with NO sleep — limited only by HTTP round-trip
             (~100-300ms / call, ~3-8 Hz)

Strategy:
  • BTC 15-min markets (KXBTC15M-*) close at :00, :15, :30, :45 UTC
  • 5 minutes before each close: hot loop starts
  • ENTRY: YES or NO crosses above 75¢ FROM BELOW (never fires if price opens ≥ 75¢)
  • STOP GAIN: 95¢ — sell, window done
  • STOP LOSS: 65¢ — sell, 30s cooldown, re-entry allowed
  • WINDOW CLOSE with position: hold to resolution ($1/contract payout)
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from client import KalshiClient, price_cents as _pc
from config import STARTING_BANKROLL_USD

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("btc15m")

# ── Tunable parameters ────────────────────────────────────────────────────────
ENTRY_MOVE_CENTS   = 5     # Enter when price moves ≥5¢ from cycle-open reference
STOP_LOSS_PCT      = 0.10  # Exit if position loses 10% of invested capital
STOP_GAIN_PCT      = 0.10  # Exit if position gains 10% of invested capital
WATCH_MINUTES      = 10    # Watch the first N minutes of each 15-min cycle
EXIT_BEFORE_CLOSE  = 5     # Sell out this many minutes before cycle close
POSITION_PCT       = 0.80  # 80% of available cash per trade
MIN_TRADE_USD      = 2.00  # Skip if trade cost is below this
REENTRY_COOLDOWN_S = 30    # Wait after stop loss before re-entry

# REST fallback: no artificial sleep — purely HTTP-latency limited
REST_POLL_SLEEP    = 0.0

BTC15M_PREFIXES = ("KXBTC15M",)  # confirmed: series_ticker=KXBTC15M, format KXBTC15M-YYMMMDDHHММ

_stop_flag = threading.Event()


# ── Timing ────────────────────────────────────────────────────────────────────

def _next_close() -> datetime:
    now     = datetime.now(timezone.utc)
    next_15 = ((now.minute // 15) + 1) * 15
    if next_15 < 60:
        return now.replace(minute=next_15, second=0, microsecond=0)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _sleep_until(target: datetime) -> None:
    while not _stop_flag.is_set():
        secs = (target - datetime.now(timezone.utc)).total_seconds()
        if secs <= 0:
            return
        time.sleep(min(1.0, secs))


# ── Market discovery ──────────────────────────────────────────────────────────

_MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}

def _parse_close_time(m: dict) -> Optional[datetime]:
    """Parse close time from market dict fields, then fall back to ticker name."""
    # Try standard API fields first
    for field in ("close_time", "expiration_time", "close_date"):
        s = m.get(field)
        if s:
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

    # Fall back: parse from ticker name.
    # Confirmed formats:
    #   KXBTC15M-26MAR30T0545   → 2026-03-30 05:45 UTC
    #   KXBTC15M-26MAR30-T0545  → 2026-03-30 05:45 UTC
    #   KXBTC15M-26jan060745    → 2026-01-06 07:45 UTC
    import re
    ticker = m.get("ticker", "")
    suffix = re.sub(r"^KXBTC15M-?", "", ticker, flags=re.IGNORECASE)
    pat = re.match(r"(\d{2})([A-Za-z]{3})(\d{2})(?:-T|-|T)?(\d{4})", suffix)
    if pat:
        yy, mon, dd, hhmm = pat.groups()
        month = _MONTH_MAP.get(mon.lower())
        if month:
            try:
                return datetime(2000+int(yy), month, int(dd),
                                int(hhmm[:2]), int(hhmm[2:]),
                                tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def find_btc15m_market(client: KalshiClient) -> Optional[Tuple[str, datetime]]:
    """
    Find the active BTC 15-min market closing soonest in the next 20 minutes.
    Uses series_ticker=KXBTC15M (confirmed correct API param).
    Close time is parsed from the API response or inferred from the ticker name.
    """
    now        = datetime.now(timezone.utc)
    horizon    = now + timedelta(minutes=20)

    best_ticker = None
    best_close: Optional[datetime] = None
    btc_seen   = []

    def _consider(m: dict):
        nonlocal best_ticker, best_close
        ticker = m.get("ticker", "")
        if not ticker.upper().startswith("KXBTC15M"):
            return

        close_dt = _parse_close_time(m)
        if not close_dt:
            logger.debug(f"[BTC15M] Could not parse close time for {ticker}")
            return

        btc_seen.append((ticker, close_dt))

        if close_dt <= now or close_dt > horizon:
            return

        if best_close is None or close_dt < best_close:
            best_ticker, best_close = ticker, close_dt

    # series_ticker=KXBTC15M — returns only BTC 15-min markets
    try:
        data = client.get_markets(limit=50, series_ticker="KXBTC15M", status="open")
        markets = data.get("markets", [])
        logger.info(f"[BTC15M] series_ticker query returned {len(markets)} market(s)")
        for m in markets:
            _consider(m)
        if best_ticker:
            return (best_ticker, best_close)
    except Exception as e:
        logger.warning(f"[BTC15M] series_ticker query failed: {e}")

    # Fallback: full paginated scan
    try:
        cursor = None
        for _ in range(10):
            data   = client.get_markets(limit=200, cursor=cursor, status="open")
            for m in data.get("markets", []):
                _consider(m)
            cursor = data.get("cursor")
            if not cursor or best_ticker:
                break
    except Exception as e:
        logger.error(f"[BTC15M] Full scan failed: {e}")

    if btc_seen:
        btc_seen.sort(key=lambda x: x[1])
        logger.info(f"[BTC15M] KXBTC15M markets found:")
        for t, dt in btc_seen[:10]:
            logger.info(f"  {t}  closes {dt.strftime('%H:%M:%S')} UTC")
    else:
        logger.warning("[BTC15M] No BTC markets found at all in open markets")

    return (best_ticker, best_close) if best_ticker else None


# ── WebSocket price feed ──────────────────────────────────────────────────────

class WsPriceFeed:
    """
    Subscribes to Kalshi's WebSocket ticker channel for one market.

    Price updates are pushed by Kalshi the instant the order book changes —
    no polling, no sleep. Latency is purely network (typically < 50ms).

    Thread-safe: get_price() can be called from any thread.
    Auto-reconnects on dropped connections.
    Falls back gracefully if websockets library is not installed.
    """

    def __init__(self, client: KalshiClient, ticker: str):
        self.client  = client
        self.ticker  = ticker
        self._price  = None           # latest YES price in cents
        self._lock   = threading.Lock()
        self._ready  = threading.Event()   # set once first price arrives
        self._stop   = threading.Event()
        self._thread = None
        self._ok     = False          # False if WS unavailable

    def start(self, timeout: float = 5.0) -> bool:
        """
        Launch WebSocket thread and wait up to `timeout` seconds for first price.
        Returns True if WebSocket connected successfully, False to use REST fallback.
        """
        try:
            import websockets   # noqa: F401 — check availability
        except ImportError:
            logger.warning("[WS] websockets not installed — using REST fallback")
            return False

        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="btc15m-ws"
        )
        self._thread.start()

        # Wait for first price tick so the hot loop starts with real data
        self._ready.wait(timeout=timeout)
        self._ok = self._ready.is_set()
        if self._ok:
            logger.info(f"[WS] Feed live — first price: {self._price}¢")
        else:
            logger.warning("[WS] Timed out waiting for first price — using REST fallback")
        return self._ok

    def stop(self) -> None:
        self._stop.set()

    def get_price(self) -> Optional[int]:
        with self._lock:
            return self._price

    def _set_price(self, yes_cents: int) -> None:
        with self._lock:
            self._price = yes_cents
        self._ready.set()

    def _thread_main(self) -> None:
        asyncio.run(self._ws_loop())

    async def _ws_loop(self) -> None:
        import websockets

        # Derive WS URL from REST base URL
        # e.g. https://api.elections.kalshi.com/trade-api/v2
        #   →  wss://api.elections.kalshi.com/trade-api/ws/v2
        ws_url = (
            self.client.base_url
            .replace("https://", "wss://")
            .replace("/trade-api/v2", "/trade-api/ws/v2")
        )

        while not self._stop.is_set():
            try:
                # Auth headers — same RSA-PSS signing, path = /trade-api/ws/v2
                headers = self.client._sign("GET", "/trade-api/ws/v2")

                async with websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    # Subscribe to ticker channel for this market
                    await ws.send(json.dumps({
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["ticker"],
                            "market_tickers": [self.ticker],
                        },
                    }))
                    logger.debug(f"[WS] Subscribed to ticker:{self.ticker}")

                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        self._handle_msg(raw)

            except Exception as e:
                if not self._stop.is_set():
                    logger.debug(f"[WS] Connection dropped ({e}) — reconnecting in 1s")
                    await asyncio.sleep(1)

    def _handle_msg(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        msg_type = data.get("type", "")
        msg      = data.get("msg", {})

        if msg_type not in ("ticker", "subscribed"):
            return

        # Handle both new _dollars format and legacy integer cents
        yes_px = None

        # New format: yes_ask_dollars = "0.7600"
        v = msg.get("yes_ask_dollars")
        if v is not None:
            try:
                c = int(round(float(v) * 100))
                if 1 <= c <= 99:
                    yes_px = c
            except (TypeError, ValueError):
                pass

        # Legacy: yes_ask = 76 (integer cents)
        if yes_px is None:
            v = msg.get("yes_ask")
            if v is not None:
                try:
                    c = int(v)
                    if 1 <= c <= 99:
                        yes_px = c
                except (TypeError, ValueError):
                    pass

        # Last resort: last_price
        if yes_px is None:
            for field in ("last_price_dollars", "last_price"):
                v = msg.get(field)
                if v is None:
                    continue
                try:
                    if "dollars" in field:
                        c = int(round(float(v) * 100))
                    else:
                        c = int(v)
                    if 1 <= c <= 99:
                        yes_px = c
                        break
                except (TypeError, ValueError):
                    pass

        if yes_px is not None:
            self._set_price(yes_px)


# ── REST price (fallback) ─────────────────────────────────────────────────────

def get_yes_price_rest(client: KalshiClient, ticker: str) -> Optional[int]:
    """Direct REST fetch. No sleep — caller loops as fast as HTTP allows."""
    try:
        data = client._get(f"/markets/{ticker}")
        m    = data.get("market", data)

        # Try all known price fields
        price = (
            _pc(m, "yes_ask")   or
            _pc(m, "last_price") or
            _pc(m, "yes_bid")   or
            _pc(m, "yes_price") or
            _pc(m, "no_ask")    # last resort: derive YES from NO ask
        )

        if price is None:
            # Log the actual fields returned so we can diagnose
            keys = [k for k in m.keys() if "price" in k.lower()
                    or "ask" in k.lower() or "bid" in k.lower()]
            logger.warning(f"[REST] No price in response for {ticker}. "
                           f"Price-related keys: {keys} | "
                           f"Sample: { {k: m[k] for k in keys[:6]} }")

        return price
    except Exception as e:
        logger.warning(f"[REST] Price fetch failed for {ticker}: {e}")
        return None


# ── Orders ────────────────────────────────────────────────────────────────────

def _market_buy(client: KalshiClient, ticker: str, side: str, count: int) -> bool:
    """Market buy — limit at 99¢ sweeps the entire ask side instantly."""
    try:
        client.place_limit_order(
            ticker=ticker, side=side, action="buy",
            price_cents=99, count=count,
            post_only=False,
        )
        return True
    except Exception as e:
        logger.error(f"Buy failed: {e}")
        return False


def _market_sell(client: KalshiClient, ticker: str, side: str, count: int) -> bool:
    """Market sell — limit at 1¢ hits the entire bid side instantly."""
    try:
        client.place_limit_order(
            ticker=ticker, side=side, action="sell",
            price_cents=1, count=count,
            post_only=False,
        )
        return True
    except Exception as e:
        logger.error(f"Sell failed: {e}")
        return False


# ── Core hot loop ─────────────────────────────────────────────────────────────

def scalp_window(client: KalshiClient, ticker: str, close_time: datetime,
                 window_end: datetime, dry_run: bool = False) -> None:
    """
    Hot loop: watches the first WATCH_MINUTES of the cycle, exits EXIT_BEFORE_CLOSE
    minutes before close.

    Price source (in priority order):
      1. WebSocket feed — real-time push, < 50ms latency
      2. REST polling   — no sleep, ~100-300ms per call

    State machine per window:
      WATCHING → enters on ≥5¢ move from cycle-open reference price
      HOLDING  → exits on +10% gain, -10% loss, or time (5 min before close)
    """
    remaining = (close_time - datetime.now(timezone.utc)).total_seconds()
    logger.info(
        f"━━━ WINDOW OPEN ━━━  {ticker}  |  {remaining:.0f}s  |  "
        f"move≥{ENTRY_MOVE_CENTS}¢  SL=-{STOP_LOSS_PCT:.0%}  SG=+{STOP_GAIN_PCT:.0%}  "
        f"exit@{EXIT_BEFORE_CLOSE}m-left"
    )

    # Start WebSocket feed — falls back to REST automatically if unavailable
    ws = WsPriceFeed(client, ticker)
    use_ws = ws.start(timeout=10.0)
    if use_ws:
        logger.info("[WS] ⚡ Real-time price feed active")
    else:
        logger.info("[REST] Polling mode — no artificial sleep")

    def _get_price() -> Optional[int]:
        if use_ws:
            return ws.get_price()
        return get_yes_price_rest(client, ticker)

    # Capture reference price at cycle open — entry fires on ≥5¢ move from here
    ref_price: Optional[int] = None

    # Position state
    holding    = False
    pos_side   = ""
    pos_count  = 0
    pos_entry  = 0
    last_sl_ts = 0.0
    done       = False

    ticks   = 0
    t_start = time.time()

    while not _stop_flag.is_set() and not done:
        now = datetime.now(timezone.utc)
        if now >= close_time:
            break

        yes_px = _get_price()
        if yes_px is None:
            time.sleep(0.05)
            continue

        ticks += 1
        no_px = 100 - yes_px

        # Latch reference price on first valid tick
        if ref_price is None:
            ref_price = yes_px
            logger.info(f"📍 Reference price: YES={ref_price}¢  NO={100 - ref_price}¢")

        if holding:
            pos_px  = yes_px if pos_side == "yes" else no_px
            secs    = (close_time - now).total_seconds()
            pnl_pct = (pos_px - pos_entry) / pos_entry

            # Time exit — 5 minutes left in cycle
            if now >= window_end:
                logger.info(
                    f"⏰ TIME EXIT  {pos_side.upper()} x{pos_count} "
                    f"@ {pos_px}¢  ({pnl_pct:+.1%})  ({secs:.0f}s left)"
                )
                if not dry_run:
                    _market_sell(client, ticker, pos_side, pos_count)
                    pnl = (pos_px - pos_entry) * pos_count / 100
                    logger.info(f"   PnL ≈ ${pnl:+.2f}")
                done = True

            elif pnl_pct >= STOP_GAIN_PCT:
                logger.info(
                    f"🎯 STOP GAIN  {pos_side.upper()} x{pos_count} "
                    f"@ {pos_px}¢  ({pnl_pct:+.1%})  ({secs:.0f}s left)"
                )
                if not dry_run:
                    _market_sell(client, ticker, pos_side, pos_count)
                    pnl = (pos_px - pos_entry) * pos_count / 100
                    logger.info(f"   PnL ≈ ${pnl:+.2f}")
                done = True

            elif pnl_pct <= -STOP_LOSS_PCT:
                logger.info(
                    f"🛑 STOP LOSS  {pos_side.upper()} x{pos_count} "
                    f"@ {pos_px}¢  ({pnl_pct:+.1%})  ({secs:.0f}s left) — watching for re-entry"
                )
                if not dry_run:
                    _market_sell(client, ticker, pos_side, pos_count)
                    pnl = (pos_px - pos_entry) * pos_count / 100
                    logger.info(f"   PnL ≈ ${pnl:+.2f}")
                holding    = False
                last_sl_ts = time.time()
                ref_price  = yes_px  # reset reference after stop loss

        else:
            # Don't enter new positions inside the exit window
            if now >= window_end:
                break

            in_cooldown = (time.time() - last_sl_ts) < REENTRY_COOLDOWN_S

            if not in_cooldown and ref_price is not None:
                move       = yes_px - ref_price
                entry_side  = ""
                entry_price = 0

                if move >= ENTRY_MOVE_CENTS:
                    entry_side, entry_price = "yes", yes_px
                elif move <= -ENTRY_MOVE_CENTS:
                    entry_side, entry_price = "no", no_px

                if entry_side:
                    secs = (close_time - now).total_seconds()
                    logger.info(
                        f"🔼 MOVE  {entry_side.upper()} @ {entry_price}¢  "
                        f"(ref={ref_price}¢ move={move:+d}¢  {secs:.0f}s left)"
                    )

                    try:
                        balance = client.get_cash()
                    except Exception:
                        balance = STARTING_BANKROLL_USD

                    count    = max(1, int(balance * POSITION_PCT * 100
                                         / max(entry_price, 1)))
                    cost_usd = count * entry_price / 100

                    if cost_usd >= MIN_TRADE_USD:
                        if not dry_run:
                            ok = _market_buy(client, ticker, entry_side, count)
                        else:
                            ok = True
                            logger.info(
                                f"[DRY-RUN] Would buy {entry_side.upper()} "
                                f"x{count} @ {entry_price}¢  ${cost_usd:.2f}"
                            )

                        if ok:
                            holding   = True
                            pos_side  = entry_side
                            pos_count = count
                            pos_entry = entry_price
                            if not dry_run:
                                logger.info(
                                    f"✅ Entered {entry_side.upper()} "
                                    f"x{count} @ {entry_price}¢  ${cost_usd:.2f}"
                                )
                    else:
                        logger.debug(f"Trade too small (${cost_usd:.2f} < ${MIN_TRADE_USD})")

        # WebSocket mode: tiny sleep keeps CPU sane while waiting for next push
        # REST mode: no sleep — loop re-fires immediately after HTTP response
        if use_ws:
            time.sleep(0.01)   # 100 Hz check of in-memory value

    # ── Window closed ─────────────────────────────────────────────────────────
    elapsed = time.time() - t_start
    hz      = ticks / elapsed if elapsed > 0 else 0
    ws.stop()

    if holding:
        # Exited the watch window still holding — shouldn't happen, but sell cleanly
        pos_px = None
        try:
            pos_px = get_yes_price_rest(client, ticker)
        except Exception:
            pass
        px_str = f"@ {pos_px}¢" if pos_px is not None else "(price unknown)"
        logger.info(
            f"━━━ WINDOW CLOSED ━━━  Still holding {pos_side.upper()} x{pos_count} "
            f"{px_str} — selling now  [{ticks} ticks @ {hz:.0f} Hz]"
        )
        if not dry_run:
            _market_sell(client, ticker, pos_side, pos_count)
    elif done:
        logger.info(
            f"━━━ WINDOW CLOSED ━━━  Exited ✓  [{ticks} ticks @ {hz:.0f} Hz]"
        )
    else:
        logger.info(
            f"━━━ WINDOW CLOSED ━━━  No position  [{ticks} ticks @ {hz:.0f} Hz]"
        )


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(client: KalshiClient, dry_run: bool = False) -> None:
    bal = client.get_balance()
    logger.info(
        f"BTC 15m Scalper | balance=${bal:.2f} | "
        f"{'DRY-RUN' if dry_run else 'LIVE 🔴'}"
    )
    logger.info(
        f"move≥{ENTRY_MOVE_CENTS}¢  SL=-{STOP_LOSS_PCT:.0%}  SG=+{STOP_GAIN_PCT:.0%}  "
        f"watch={WATCH_MINUTES}m  exit@{EXIT_BEFORE_CLOSE}m-left  "
        f"size={POSITION_PCT:.0%}  min=${MIN_TRADE_USD}"
    )

    while not _stop_flag.is_set():
        close_time   = _next_close()
        # Watch starts at cycle open (15 min before close)
        window_start = close_time - timedelta(minutes=15)
        # Sell out EXIT_BEFORE_CLOSE minutes before close
        window_end   = close_time - timedelta(minutes=EXIT_BEFORE_CLOSE)
        now          = datetime.now(timezone.utc)

        # If we're already past the exit point, skip to next cycle
        if now >= window_end:
            time.sleep(5)
            continue

        sleep_secs = (window_start - now).total_seconds()

        close_utc = close_time.strftime("%H:%M UTC")
        close_mst = (close_time - timedelta(hours=7)).strftime("%H:%M MST")
        close_mdt = (close_time - timedelta(hours=6)).strftime("%H:%M MDT")

        if sleep_secs > 2:
            logger.info(
                f"Next window: {window_start.strftime('%H:%M')}–{window_end.strftime('%H:%M')} UTC  "
                f"(closes {close_utc} / {close_mst} / {close_mdt})  "
                f"sleeping {sleep_secs:.0f}s"
            )
            _sleep_until(window_start)

        if _stop_flag.is_set():
            break

        result = find_btc15m_market(client)
        if not result:
            logger.warning("No active BTC 15-min market found — retrying next cycle")
            time.sleep(60)
            continue

        ticker, market_close = result
        logger.info(
            f"Market: {ticker}  closes {market_close.strftime('%H:%M:%S')} UTC"
        )

        scalp_window(client, ticker, market_close, window_end, dry_run=dry_run)
        time.sleep(5)   # avoid clock-edge race before next_close() recalculates


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="BTC 15-minute breakout scalper — WebSocket feed, sub-50ms latency"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Log signals without placing orders")
    args = parser.parse_args()

    _client = KalshiClient()

    def _shutdown(sig, frame):
        logger.info("Shutting down…")
        _stop_flag.set()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    run(_client, dry_run=args.dry_run)
