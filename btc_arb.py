"""
btc_arb.py — BTC/Kalshi 15m latency arbitrage
===============================================
Run:       python btc_arb.py
Dry-run:   python btc_arb.py --dry-run

Strategy (derived from 50-market backtests — 92% accuracy):
  • At each 15-min cycle open, Binance kline gives the BTC reference price
  • When BTC moves ≥ BTC_ENTRY_PCT from that reference → buy corresponding
    Kalshi side (YES if BTC↑, NO if BTC↓)
  • Hold to resolution — do NOT exit early unless BTC fully reverses
  • EXIT SIGNAL: if BTC crosses back through zero against our position
    (e.g. entered YES on +0.15%, BTC drops to −0.1%) → sell immediately
  • One trade per 15-min cycle, no re-entry

Why this works:
  BTC price moves first on Binance.  Kalshi price lags by seconds.
  Entering Kalshi early (while YES is still ~50¢) captures the full EV.
  At 92% win rate + 7% settlement fee, EV ≈ +39% per trade at 50¢ entry.
"""

import argparse
import asyncio
import json
import logging
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import websockets

from client import KalshiClient, price_cents as _pc
from config import STARTING_BANKROLL_USD

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("btcarb")

# ── Parameters ────────────────────────────────────────────────────────────────
BTC_ENTRY_PCT     = 0.0010  # 0.10% BTC move from candle open triggers entry
STOP_LOSS_PCT     = 0.50    # exit if Kalshi contract loses 50% of entry price
                             # e.g. entered YES at 70¢ → sell if YES drops to 35¢
NO_ENTRY_FINAL_S  = 300     # only enter in first 10 min (stop with 5 min left)
POSITION_PCT      = 0.21    # ~Quarter Kelly — survives variance without blowing up
MIN_TRADE_USD     = 2.00    # skip if cost is below this
SETTLEMENT_FEE    = 0.07    # 7% of winnings taken by Kalshi at resolution
MIN_EV            = 0.05    # skip trade if EV < 5¢ per dollar risked

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


# ── Kalshi market discovery ───────────────────────────────────────────────────

_MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}

def _parse_close_time(m: dict) -> Optional[datetime]:
    for field in ("close_time", "expiration_time", "close_date"):
        s = m.get(field)
        if s:
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
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


def find_market(client: KalshiClient) -> Optional[Tuple[str, datetime]]:
    """Return (ticker, close_time) for the soonest open KXBTC15M market."""
    try:
        data    = client.get_markets(limit=10, series_ticker="KXBTC15M", status="open")
        markets = data.get("markets", [])
        logger.info(f"[KALSHI] series_ticker query returned {len(markets)} market(s)")
    except Exception as e:
        logger.error(f"[KALSHI] Market fetch failed: {e}")
        return None

    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=20)
    valid  = []
    for m in markets:
        ct = _parse_close_time(m)
        if ct and now < ct <= cutoff:
            valid.append((m["ticker"], ct))

    if not valid:
        return None
    valid.sort(key=lambda x: x[1])
    return valid[0]


# ── Binance kline WebSocket ───────────────────────────────────────────────────

class BinanceFeed:
    """
    Subscribes to Binance btcusdt@kline_15m.
    Each message contains:
      k.o  — candle open price  (resets at each :00/:15/:30/:45 UTC)
      k.c  — latest trade price
      k.x  — True when the candle is closed (final)
    """

    def __init__(self):
        self._lock   = threading.Lock()
        self._open   = None   # float BTC candle open
        self._price  = None   # float BTC current price
        self._closed = False  # has this candle closed?
        self._thread = None
        self._stop   = threading.Event()

    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait up to 15s for first data
        for _ in range(150):
            if self._price is not None:
                return True
            time.sleep(0.1)
        return False

    def stop(self):
        self._stop.set()

    def get_state(self) -> Tuple[Optional[float], Optional[float], bool]:
        """Returns (candle_open, current_price, candle_is_closed)."""
        with self._lock:
            return self._open, self._price, self._closed

    def _run(self):
        asyncio.run(self._connect())

    async def _connect(self):
        url = "wss://stream.binance.com:9443/ws/btcusdt@kline_15m"
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("[BINANCE] WebSocket connected")
                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        try:
                            data = json.loads(raw)
                            k    = data.get("k", {})
                            with self._lock:
                                self._open   = float(k["o"])
                                self._price  = float(k["c"])
                                self._closed = bool(k.get("x", False))
                        except Exception:
                            pass
            except Exception as e:
                if not self._stop.is_set():
                    logger.warning(f"[BINANCE] WS error: {e} — reconnecting in 2s")
                    await asyncio.sleep(2)


# ── Order helpers ─────────────────────────────────────────────────────────────

def _market_buy(client: KalshiClient, ticker: str, side: str, count: int) -> bool:
    try:
        client.place_limit_order(ticker=ticker, side=side, action="buy",
                                  price_cents=99, count=count, post_only=False)
        return True
    except Exception as e:
        logger.error(f"Buy failed: {e}")
        if hasattr(e, "response") and e.response is not None:
            try:
                logger.error(f"Buy error body: {e.response.text}")
            except Exception:
                pass
        return False


def _market_sell(client: KalshiClient, ticker: str, side: str, count: int) -> bool:
    try:
        client.place_limit_order(ticker=ticker, side=side, action="sell",
                                  price_cents=1, count=count, post_only=False)
        return True
    except Exception as e:
        logger.error(f"Sell failed: {e}")
        return False


def _get_kalshi_price(client: KalshiClient, ticker: str) -> Optional[int]:
    """Best mid-price for YES side in cents."""
    try:
        bid, ask = client.get_best_bid_ask(ticker)
        if bid and ask:
            return (bid + ask) // 2
        return bid or ask
    except Exception:
        return None


# ── Per-cycle logic ───────────────────────────────────────────────────────────

def trade_cycle(client: KalshiClient, feed: BinanceFeed,
                ticker: str, close_time: datetime,
                dry_run: bool = False) -> None:
    """
    Watches one 15-min cycle.  Enters on BTC_ENTRY_PCT move, then either:
      - Holds to resolution if BTC direction holds
      - Exits early if Kalshi contract drops 50% from entry price (STOP_LOSS_PCT)
    """
    no_entry_after = close_time - timedelta(seconds=NO_ENTRY_FINAL_S)
    secs_total     = (close_time - datetime.now(timezone.utc)).total_seconds()
    exit_str       = f"stop={STOP_LOSS_PCT:.0%}loss"
    logger.info(
        f"━━━ CYCLE  {ticker}  closes {close_time.strftime('%H:%M:%S')} UTC  "
        f"({secs_total:.0f}s)  entry≥{BTC_ENTRY_PCT*100:.2f}%  {exit_str}"
    )

    holding    = False
    pos_side   = ""
    pos_count  = 0
    pos_entry_btc  = 0.0
    pos_kalshi_px  = 0
    last_stop_check = 0.0
    exited_early    = False

    while not _stop_flag.is_set():
        now = datetime.now(timezone.utc)
        if now >= close_time:
            break

        btc_open, btc_now, _ = feed.get_state()
        if btc_open is None or btc_now is None:
            time.sleep(0.01)
            continue

        move_pct = (btc_now - btc_open) / btc_open   # +ve = BTC up

        if holding:
            secs = (close_time - now).total_seconds()

            # ── BTC backup stop: BTC reversed past entry threshold (other side)
            # e.g. entered YES at +0.15% → stop if BTC drops to -0.15%
            # This approximates a ~50% Kalshi loss without needing an API call.
            btc_reversed = (
                (pos_side == "yes" and move_pct <= -BTC_ENTRY_PCT) or
                (pos_side == "no"  and move_pct >=  BTC_ENTRY_PCT)
            )
            if btc_reversed:
                logger.info(
                    f"🛑 BTC STOP  {pos_side.upper()}  btc_move={move_pct*100:+.3f}%  "
                    f"({secs:.0f}s left) — BTC reversed past -{BTC_ENTRY_PCT*100:.2f}%"
                )
                if not dry_run:
                    _market_sell(client, ticker, pos_side, pos_count)
                exited_early = True
                break

            # ── Kalshi price stop: exit if contract loses 50% of entry price ──
            if now.timestamp() - last_stop_check >= 5.0:
                last_stop_check = now.timestamp()
                cur_px = _get_kalshi_price(client, ticker)
                if cur_px is not None:
                    stop_px = pos_kalshi_px * STOP_LOSS_PCT
                    if cur_px <= stop_px:
                        logger.info(
                            f"🛑 KALSHI STOP  {pos_side.upper()}  "
                            f"entry={pos_kalshi_px}¢  now={cur_px}¢  "
                            f"stop={stop_px:.0f}¢  ({secs:.0f}s left) — exiting"
                        )
                        if not dry_run:
                            _market_sell(client, ticker, pos_side, pos_count)
                        exited_early = True
                        break

        else:
            # Don't enter in the final 2 minutes
            if now >= no_entry_after:
                logger.info("⏸  Entry window closed — holding for resolution or next cycle")
                break

            if abs(move_pct) >= BTC_ENTRY_PCT:
                side = "yes" if move_pct > 0 else "no"
                secs = (close_time - now).total_seconds()

                # Get current Kalshi price for sizing and logging
                kalshi_px = _get_kalshi_price(client, ticker) or 50

                try:
                    balance = client.get_cash()
                except Exception:
                    balance = STARTING_BANKROLL_USD

                # Size by worst-case reserve price (99¢) not mid-price.
                # Kalshi holds 99¢ × count until the order fills.
                count    = max(1, int(balance * POSITION_PCT * 100 / 99))
                cost_usd = count * kalshi_px / 100   # estimated fill cost

                # EV check — skip if Kalshi has already priced out the edge
                win_pct  = 0.92   # observed from backtests
                win_pay  = (100 - kalshi_px) / 100 * (1 - SETTLEMENT_FEE)
                lose_pay = kalshi_px / 100
                ev_per_dollar = win_pct * win_pay - (1 - win_pct) * lose_pay

                if ev_per_dollar < MIN_EV:
                    logger.info(
                        f"⏭  LOW EV  {side.upper()}  kalshi={kalshi_px}¢  "
                        f"EV={ev_per_dollar:+.2f}/$ < {MIN_EV:.2f} threshold — skipping"
                    )
                    break

                logger.info(
                    f"🚀 BTC SIGNAL  {side.upper()}  "
                    f"btc_move={move_pct*100:+.3f}%  "
                    f"kalshi≈{kalshi_px}¢  "
                    f"EV≈{ev_per_dollar:+.2f}/$ "
                    f"({secs:.0f}s left)"
                )

                if cost_usd < MIN_TRADE_USD:
                    logger.warning(f"Trade too small (${cost_usd:.2f}) — skipping")
                    break

                if not dry_run:
                    ok = _market_buy(client, ticker, side, count)
                else:
                    ok = True
                    logger.info(
                        f"[DRY-RUN] Would buy {side.upper()} x{count} "
                        f"@ ~{kalshi_px}¢  ${cost_usd:.2f}"
                    )

                if ok:
                    holding       = True
                    pos_side      = side
                    pos_count     = count
                    pos_entry_btc = move_pct
                    pos_kalshi_px = kalshi_px
                    if not dry_run:
                        logger.info(
                            f"✅ Entered {side.upper()} x{count} "
                            f"@ ~{kalshi_px}¢  ${cost_usd:.2f}  "
                            f"hold to {close_time.strftime('%H:%M:%S')} UTC"
                        )
                else:
                    # Buy failed — stop attempting this cycle to avoid order spam
                    logger.warning("Order failed — skipping remainder of cycle")
                    break

        time.sleep(0.01)   # 100 Hz — WS push is near-instant

    # ── Cycle end ─────────────────────────────────────────────────────────────
    btc_open, btc_final, _ = feed.get_state()
    final_move = ((btc_final - btc_open) / btc_open * 100) if btc_open and btc_final else 0.0

    if holding and not exited_early:
        logger.info(
            f"━━━ HOLDING to resolution  {pos_side.upper()} x{pos_count} "
            f"@ ~{pos_kalshi_px}¢  |  final BTC move={final_move:+.3f}%"
        )
    else:
        logger.info(
            f"━━━ CYCLE DONE  no position  |  final BTC move={final_move:+.3f}%"
        )


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(client: KalshiClient, dry_run: bool = False) -> None:
    bal = client.get_cash()
    logger.info(f"BTC Arb | balance=${bal:.2f} | {'DRY-RUN' if dry_run else 'LIVE 🔴'}")
    logger.info(
        f"entry≥{BTC_ENTRY_PCT*100:.2f}% BTC move  "
        f"stop={STOP_LOSS_PCT:.0%}loss  "
        f"window=first {(900-NO_ENTRY_FINAL_S)//60}min  "
        f"size={POSITION_PCT:.0%}  fee={SETTLEMENT_FEE:.0%}"
    )

    # Start Binance feed
    feed = BinanceFeed()
    logger.info("[BINANCE] Connecting to kline_15m stream…")
    if feed.start():
        btc_open, btc_now, _ = feed.get_state()
        logger.info(f"[BINANCE] ⚡ Live  BTC=${btc_now:,.2f}  candle_open=${btc_open:,.2f}")
    else:
        logger.error("[BINANCE] Feed failed to connect — cannot trade without BTC signal")
        return

    while not _stop_flag.is_set():
        close_time   = _next_close()
        cycle_open   = close_time - timedelta(minutes=15)
        now          = datetime.now(timezone.utc)
        sleep_secs   = (cycle_open - now).total_seconds()

        close_utc = close_time.strftime("%H:%M UTC")
        close_mdt = (close_time - timedelta(hours=6)).strftime("%H:%M MDT")

        if sleep_secs > 2:
            logger.info(
                f"Next cycle: {cycle_open.strftime('%H:%M')}–{close_time.strftime('%H:%M')} UTC  "
                f"({close_utc} / {close_mdt})  sleeping {sleep_secs:.0f}s"
            )
            _sleep_until(cycle_open)

        if _stop_flag.is_set():
            break

        # Find the market — retry up to 12x with 5s gaps (60s total).
        # Kalshi sometimes takes 15-30s to open the next market after close.
        result = None
        for attempt in range(12):
            result = find_market(client)
            if result:
                break
            if attempt < 11:
                logger.warning(f"No KXBTC15M market found (attempt {attempt+1}/12) — retrying in 5s")
                time.sleep(5)

        if not result:
            logger.warning("Skipping cycle — no open market found")
            _sleep_until(close_time + timedelta(seconds=5))
            continue

        ticker, market_close = result
        logger.info(f"Market: {ticker}  closes {market_close.strftime('%H:%M:%S')} UTC")

        trade_cycle(client, feed, ticker, market_close, dry_run=dry_run)

        # Sleep until after this cycle closes before looking for next
        _sleep_until(close_time + timedelta(seconds=5))

    feed.stop()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="BTC/Kalshi 15m latency arbitrage — Binance signal, hold to resolution"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Log signals without placing orders")
    args = parser.parse_args()

    _client = KalshiClient()

    def _shutdown(sig, frame):
        logger.info("Shutting down…")
        _stop_flag.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    run(_client, dry_run=args.dry_run)
