"""
btc_research.py — BTC 15m Kalshi vs Binance pattern research
=============================================================
Fetches the last N closed KXBTC15M markets, pulls matching Binance 15m
candles, and runs statistical analysis to discover profitable entry conditions.

Usage:
    python btc_research.py          # analyse last 50 settled markets
    python btc_research.py --n 100  # analyse last 100
"""

import argparse
import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from client import KalshiClient, price_cents as _pc

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
log = logging.getLogger("research")

# ── Kalshi fee model ──────────────────────────────────────────────────────────
# Taker round-trip: buy at ask, sell at bid before resolution
#   Effective spread cost ≈ 4–6¢ per contract (market-dependent)
# Settlement fee (if held to resolution): 7% of net winnings
SETTLEMENT_FEE_PCT = 0.07   # 7% of profit at resolution
TAKER_SPREAD_EST   = 5      # estimated round-trip spread cost in cents


# ── Binance ───────────────────────────────────────────────────────────────────

def binance_1m_candles(open_ts_ms: int) -> list:
    """
    Fetch 15 x 1-minute BTCUSDT klines covering the full 15-min window.
    Returns list of {open, close, ts_ms} dicts, or [] on error.
    """
    url = (
        "https://api.binance.com/api/v3/klines"
        f"?symbol=BTCUSDT&interval=1m&startTime={open_ts_ms}&limit=15"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "btc-research/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return [{"ts_ms": int(k[0]), "open": float(k[1]), "close": float(k[4])} for k in data]
    except Exception as e:
        log.warning(f"Binance 1m fetch failed: {e}")
        return []


def simulate_single_trade(candles: list, candle_open: float,
                           entry_pct: float, exit_pct: float,
                           resolves_yes: bool) -> dict:
    """
    Strategy A: tied to candle open, one trade per cycle.
    Enter when BTC moves ±entry_pct from open. Hold to resolution unless
    BTC reverses past -exit_pct (against position). Returns trade result.
    """
    ref   = candle_open
    side  = None   # "yes" or "no"

    for c in candles:
        price = c["close"]
        move  = (price - ref) / ref

        if side is None:
            if move >= entry_pct:
                side = "yes"
            elif move <= -entry_pct:
                side = "no"
        else:
            # Check reversal exit
            if side == "yes" and move <= -exit_pct:
                # Exited early — BTC reversed against us
                # Kalshi price has also reversed — rough loss
                win = False
                return {"trades": 1, "result": "reversed_exit", "win": win,
                        "btc_final": (candles[-1]["close"] - candle_open) / candle_open}
            elif side == "no" and move >= exit_pct:
                win = False
                return {"trades": 1, "result": "reversed_exit", "win": win,
                        "btc_final": (candles[-1]["close"] - candle_open) / candle_open}

    if side is None:
        return {"trades": 0, "result": "no_entry", "win": None,
                "btc_final": (candles[-1]["close"] - candle_open) / candle_open}

    # Held to resolution
    win = (side == "yes") == resolves_yes
    return {"trades": 1, "result": "resolution", "win": win,
            "btc_final": (candles[-1]["close"] - candle_open) / candle_open}


def simulate_reentry(candles: list, candle_open: float,
                     entry_pct: float, exit_pct: float,
                     resolves_yes: bool) -> dict:
    """
    Strategy B: re-entry after reversal, both directions.
    After a reversal exit, reset reference to current price and watch for
    a fresh ±entry_pct move in either direction.
    """
    ref     = candle_open
    side    = None
    trades  = 0
    wins    = 0
    losses  = 0
    results = []

    for c in candles:
        price = c["close"]
        move  = (price - ref) / ref

        if side is None:
            if move >= entry_pct:
                side = "yes"
                trades += 1
            elif move <= -entry_pct:
                side = "no"
                trades += 1
        else:
            reversed_out = (
                (side == "yes" and move <= -exit_pct) or
                (side == "no"  and move >= exit_pct)
            )
            if reversed_out:
                losses += 1
                results.append(f"{side}_loss")
                # Reset reference to current price, watch for new signal
                ref  = price
                side = None

    # Resolve any open position
    if side is not None:
        win = (side == "yes") == resolves_yes
        if win:
            wins += 1
            results.append(f"{side}_win")
        else:
            losses += 1
            results.append(f"{side}_loss")

    total = wins + losses
    return {
        "trades":  trades,
        "wins":    wins,
        "losses":  losses,
        "win_pct": wins / total if total > 0 else None,
        "results": results,
        "btc_final": (candles[-1]["close"] - candle_open) / candle_open,
    }


def binance_kline(open_ts_ms: int) -> Optional[dict]:
    """
    Fetch a single 15m BTCUSDT kline from Binance public API.
    open_ts_ms: UTC epoch milliseconds for the candle open.
    Returns dict with open/high/low/close/volume or None on error.
    """
    url = (
        "https://api.binance.com/api/v3/klines"
        f"?symbol=BTCUSDT&interval=15m&startTime={open_ts_ms}&limit=1"
    )
    try:
        req  = urllib.request.Request(url, headers={"User-Agent": "btc-research/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if not data:
            return None
        k = data[0]
        return {
            "open_ts":  int(k[0]),
            "close_ts": int(k[6]),
            "open":     float(k[1]),
            "high":     float(k[2]),
            "low":      float(k[3]),
            "close":    float(k[4]),
            "volume":   float(k[5]),
            "pct":      (float(k[4]) - float(k[1])) / float(k[1]) * 100,
        }
    except Exception as e:
        log.warning(f"Binance fetch failed: {e}")
        return None


# ── Kalshi helpers ────────────────────────────────────────────────────────────

def parse_close_ts(m: dict) -> Optional[int]:
    """Return close time as UTC epoch seconds, or None."""
    for field in ("close_time", "expiration_time"):
        s = m.get(field)
        if s:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                return int(dt.timestamp())
            except (ValueError, AttributeError):
                pass
    return None


def get_open_price(client: KalshiClient, ticker: str, open_ts: int) -> Optional[int]:
    """
    Fetch the YES price at the very start of the 15-min window.
    Uses market history; returns the first tick at or after open_ts.
    """
    try:
        history = client.get_market_history(ticker, start_ts=open_ts)
        if not history:
            return None
        # history entries: {yes_price, no_price, ts}
        for entry in history:
            px = _pc(entry, "yes_price") or _pc(entry, "yes_ask") or _pc(entry, "last_price")
            if px:
                return px
        return None
    except Exception as e:
        log.debug(f"History fetch failed {ticker}: {e}")
        return None


# ── EV model ──────────────────────────────────────────────────────────────────

def ev_intraday(entry_cents: int, exit_cents: int, win: bool) -> float:
    """P&L for a round-trip intraday trade (no settlement fee)."""
    return (exit_cents - entry_cents) / 100


def ev_to_resolution(entry_cents: int, resolves_yes: bool, bought_yes: bool) -> float:
    """P&L for a position held to resolution, after 7% settlement fee."""
    if bought_yes:
        if resolves_yes:
            profit = (100 - entry_cents) / 100
            return profit * (1 - SETTLEMENT_FEE_PCT)
        return -entry_cents / 100
    else:  # bought NO
        if not resolves_yes:
            profit = (100 - entry_cents) / 100
            return profit * (1 - SETTLEMENT_FEE_PCT)
        return -entry_cents / 100


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyse(client: KalshiClient, n: int = 50) -> None:

    # 1. Fetch closed markets
    log.info(f"Fetching last {n} closed KXBTC15M markets…")
    resp    = client.get_markets(series_ticker="KXBTC15M", status="settled", limit=n)
    markets = resp.get("markets", [])
    if not markets:
        # try status=closed
        resp    = client.get_markets(series_ticker="KXBTC15M", status="closed", limit=n)
        markets = resp.get("markets", [])
    log.info(f"  Got {len(markets)} markets")

    # 2. Build dataset
    records = []
    for m in markets:
        ticker = m.get("ticker", "")
        result = m.get("result", "")          # "yes" or "no"
        if result not in ("yes", "no"):
            continue

        close_ts = parse_close_ts(m)
        if not close_ts:
            continue

        open_ts     = close_ts - 15 * 60       # 15-min window open
        open_ts_ms  = open_ts * 1000

        # Binance candle for this window
        kline = binance_kline(open_ts_ms)
        if kline is None:
            continue

        # Kalshi YES price at window open (first available tick)
        kalshi_open_px = get_open_price(client, ticker, open_ts)

        # Also try the market-level last_price as a proxy
        if kalshi_open_px is None:
            kalshi_open_px = _pc(m, "last_price") or _pc(m, "yes_ask") or _pc(m, "yes_bid")

        btc_up  = kline["pct"] > 0
        yes_won = (result == "yes")

        # Fetch 1-minute intra-candle data for simulation
        candles_1m = binance_1m_candles(open_ts_ms)

        records.append({
            "ticker":          ticker,
            "close_ts":        close_ts,
            "result":          result,
            "yes_won":         yes_won,
            "btc_pct":         kline["pct"],
            "btc_up":          btc_up,
            "btc_open":        kline["open"],
            "btc_close":       kline["close"],
            "btc_vol":         kline["volume"],
            "kalshi_open_px":  kalshi_open_px,
            "candles_1m":      candles_1m,
        })
        time.sleep(0.05)   # be gentle with Binance rate limit

    if not records:
        log.error("No usable records — check market status field")
        return

    log.info(f"\n{'='*60}")
    log.info(f"  ANALYSIS: {len(records)} markets")
    log.info(f"{'='*60}\n")

    n_total = len(records)
    n_yes   = sum(1 for r in records if r["yes_won"])
    n_no    = n_total - n_yes

    # ── Section 1: Base rates ─────────────────────────────────────────────────
    print(f"{'─'*60}")
    print("1. BASE RATES")
    print(f"{'─'*60}")
    print(f"   YES resolved:  {n_yes}/{n_total}  ({100*n_yes/n_total:.1f}%)")
    print(f"   NO  resolved:  {n_no}/{n_total}  ({100*n_no/n_total:.1f}%)")
    btc_up_count = sum(1 for r in records if r["btc_up"])
    print(f"   BTC up 15m:    {btc_up_count}/{n_total}  ({100*btc_up_count/n_total:.1f}%)")
    print()

    # ── Section 2: BTC direction vs Kalshi result ─────────────────────────────
    print(f"{'─'*60}")
    print("2. BTC DIRECTION vs KALSHI RESULT")
    print(f"{'─'*60}")
    align = sum(1 for r in records if r["btc_up"] == r["yes_won"])
    print(f"   BTC direction predicts YES/NO: {align}/{n_total}  ({100*align/n_total:.1f}%)")

    # When BTC is up, does YES win?
    btc_up_recs = [r for r in records if r["btc_up"]]
    btc_dn_recs = [r for r in records if not r["btc_up"]]
    if btc_up_recs:
        yes_when_up = sum(1 for r in btc_up_recs if r["yes_won"])
        print(f"   P(YES | BTC↑): {yes_when_up}/{len(btc_up_recs)}  ({100*yes_when_up/len(btc_up_recs):.1f}%)")
    if btc_dn_recs:
        no_when_dn = sum(1 for r in btc_dn_recs if not r["yes_won"])
        print(f"   P(NO  | BTC↓): {no_when_dn}/{len(btc_dn_recs)}  ({100*no_when_dn/len(btc_dn_recs):.1f}%)")
    print()

    # ── Section 3: BTC move magnitude ────────────────────────────────────────
    print(f"{'─'*60}")
    print("3. BTC MOVE MAGNITUDE vs WIN RATE")
    print(f"{'─'*60}")
    thresholds = [0.0, 0.1, 0.2, 0.3, 0.5, 1.0]
    for lo, hi in zip(thresholds, thresholds[1:] + [999]):
        bucket = [r for r in records if lo <= abs(r["btc_pct"]) < hi]
        if not bucket:
            continue
        # When BTC moves this much, and we trade in BTC's direction
        wins = sum(1 for r in bucket if r["btc_up"] == r["yes_won"])
        avg_move = sum(abs(r["btc_pct"]) for r in bucket) / len(bucket)
        print(f"   |BTC| {lo:.1f}–{hi:.1f}%  →  n={len(bucket):3d}  "
              f"win%={100*wins/len(bucket):5.1f}%  avg_move={avg_move:.2f}%")
    print()

    # ── Section 4: Kalshi open price calibration ──────────────────────────────
    print(f"{'─'*60}")
    print("4. KALSHI OPEN PRICE CALIBRATION (implied vs actual)")
    print(f"{'─'*60}")
    priced = [r for r in records if r["kalshi_open_px"] is not None]
    if priced:
        buckets = [(20,35), (35,45), (45,55), (55,65), (65,80)]
        for lo, hi in buckets:
            b = [r for r in priced if lo <= r["kalshi_open_px"] < hi]
            if not b:
                continue
            actual_yes = sum(1 for r in b if r["yes_won"]) / len(b)
            implied    = sum(r["kalshi_open_px"] for r in b) / len(b) / 100
            edge       = actual_yes - implied
            print(f"   YES open {lo:2d}–{hi:2d}¢  →  n={len(b):3d}  "
                  f"implied={100*implied:.0f}%  actual={100*actual_yes:.0f}%  "
                  f"edge={edge:+.2f}")
    else:
        print("   (No open price data available)")
    print()

    # ── Section 5: Fee-adjusted EV at various entry prices ───────────────────
    print(f"{'─'*60}")
    print("5. FEE-ADJUSTED EV — HOLD TO RESOLUTION")
    print(f"{'─'*60}")
    print("   Assumes you can correctly predict direction 60% of the time")
    print("   (replace with your actual edge from section 2/3)")
    print()
    p_win = align / n_total  # observed directional accuracy
    print(f"   Observed directional accuracy: {100*p_win:.1f}%")
    print()
    print(f"   {'Entry':>6}  {'EV(win)':>8}  {'EV(lose)':>9}  {'Net EV':>8}  {'Break-even P':>13}")
    for entry in [35, 40, 45, 50, 55, 60, 65]:
        ev_w = ev_to_resolution(entry, True, True)    # bought YES, YES wins
        ev_l = ev_to_resolution(entry, False, True)   # bought YES, NO wins (same formula)
        net  = p_win * ev_w + (1 - p_win) * ev_l
        # Break-even: P × (1-entry)*0.93 = (1-P) × entry  →  solve for P
        win_payout = (100 - entry) / 100 * (1 - SETTLEMENT_FEE_PCT)
        lose_cost  = entry / 100
        breakeven  = lose_cost / (win_payout + lose_cost)
        print(f"   {entry:>5}¢  {ev_w:>+8.3f}  {ev_l:>+9.3f}  {net:>+8.3f}  {100*breakeven:>12.1f}%")
    print()

    # ── Section 6: BTC volatility signal ─────────────────────────────────────
    print(f"{'─'*60}")
    print("6. BTC VOLUME vs OUTCOME PREDICTABILITY")
    print(f"{'─'*60}")
    med_vol = sorted(r["btc_vol"] for r in records)[len(records)//2]
    high_vol = [r for r in records if r["btc_vol"] >= med_vol]
    low_vol  = [r for r in records if r["btc_vol"] <  med_vol]
    if high_vol:
        h_align = sum(1 for r in high_vol if r["btc_up"] == r["yes_won"])
        print(f"   High volume (≥median):  {h_align}/{len(high_vol)}  "
              f"directional accuracy={100*h_align/len(high_vol):.1f}%")
    if low_vol:
        l_align = sum(1 for r in low_vol if r["btc_up"] == r["yes_won"])
        print(f"   Low  volume (<median):  {l_align}/{len(low_vol)}  "
              f"directional accuracy={100*l_align/len(low_vol):.1f}%")
    print()

    # ── Section 7: Strategy recommendation ───────────────────────────────────
    print(f"{'='*60}")
    print("7. STRATEGY RECOMMENDATION")
    print(f"{'='*60}")

    best_bucket = None
    best_winpct = 0.0
    for lo, hi in zip(thresholds, thresholds[1:] + [999]):
        bucket = [r for r in records if lo <= abs(r["btc_pct"]) < hi and len(records) >= 5]
        if len(bucket) < 5:
            continue
        wins   = sum(1 for r in bucket if r["btc_up"] == r["yes_won"])
        winpct = wins / len(bucket)
        if winpct > best_winpct:
            best_winpct  = winpct
            best_bucket  = (lo, hi, len(bucket))

    print()
    if best_bucket:
        lo, hi, cnt = best_bucket
        print(f"   Strongest signal: BTC move magnitude {lo:.1f}–{hi:.1f}%")
        print(f"   Win rate in this bucket: {100*best_winpct:.1f}%  (n={cnt})")
        entry_mid = 50
        win_payout = (100 - entry_mid) / 100 * (1 - SETTLEMENT_FEE_PCT)
        lose_cost  = entry_mid / 100
        net_ev = best_winpct * win_payout - (1 - best_winpct) * lose_cost
        print(f"   Net EV at 50¢ entry: ${net_ev:+.3f} per $1 risked")
    print()
    print("   CURRENT BOT WEAKNESSES (from log data):")
    print("   ✗ Enters AFTER 15% move — move is often exhausted by then")
    print("   ✗ No BTC price feed — reacting to Kalshi price, not root cause")
    print("   ✗ Taker orders cost 4–6¢ spread each way on a 5–10¢ expected move")
    print()
    print("   RECOMMENDED IMPROVEMENTS:")
    print("   1. Add Binance WebSocket — trade Kalshi based on BTC momentum")
    print("      directly, not lagging Kalshi price (BTC moves first, Kalshi follows)")
    print("   2. Target mid-range entries (40–58¢) where the 7% settlement")
    print("      fee is lowest as a % of potential gain")
    print("   3. Hold to resolution when confident — avoids round-trip spread")
    print("      cost (saves ~5¢ vs intraday exit on a 10¢ move)")
    print("   4. Size larger when BTC volume is high (more predictable outcome)")
    print()

    # ── Section 8: Strategy A — tied to open, 1 trade per cycle ─────────────
    print(f"{'─'*60}")
    print("8. SIMULATION A — TIED TO OPEN, 1 TRADE PER CYCLE")
    print(f"{'─'*60}")
    sim_records = [r for r in records if r["candles_1m"]]
    print(f"   Markets with 1m data: {len(sim_records)}")

    ENTRY_PCT = 0.001   # 0.1% trigger
    EXIT_PCT  = 0.001   # -0.1% reversal exit

    a_trades = a_wins = a_losses = a_no_entry = a_reversed = 0
    for r in sim_records:
        res = simulate_single_trade(
            r["candles_1m"], r["btc_open"], ENTRY_PCT, EXIT_PCT, r["yes_won"]
        )
        if res["trades"] == 0:
            a_no_entry += 1
        elif res["result"] == "reversed_exit":
            a_reversed += 1
            a_losses   += 1
            a_trades   += 1
        else:
            a_trades += 1
            if res["win"]:
                a_wins += 1
            else:
                a_losses += 1

    a_total = a_wins + a_losses
    print(f"   Total entries:    {a_trades}")
    print(f"   No-entry cycles:  {a_no_entry}  (BTC never moved 0.1%)")
    print(f"   Reversed exits:   {a_reversed}")
    print(f"   Win rate:         {a_wins}/{a_total}  ({100*a_wins/a_total:.1f}%)" if a_total else "   No trades")

    if a_total:
        entry_px = 50
        ev_w = (100 - entry_px) / 100 * (1 - SETTLEMENT_FEE_PCT)
        ev_l = -entry_px / 100
        wr   = a_wins / a_total
        net  = wr * ev_w + (1 - wr) * ev_l
        print(f"   EV at 50¢ entry:  {net:+.3f} per $1 risked")
    print()

    # ── Section 9: Strategy B — re-entry after reversal, both directions ─────
    print(f"{'─'*60}")
    print("9. SIMULATION B — RE-ENTRY AFTER REVERSAL, BOTH DIRECTIONS")
    print(f"{'─'*60}")

    b_total_trades = b_total_wins = b_total_losses = 0
    b_cycles_traded = 0
    b_trades_per_cycle = []

    for r in sim_records:
        res = simulate_reentry(
            r["candles_1m"], r["btc_open"], ENTRY_PCT, EXIT_PCT, r["yes_won"]
        )
        if res["trades"] > 0:
            b_cycles_traded    += 1
            b_total_trades     += res["trades"]
            b_total_wins       += res["wins"]
            b_total_losses     += res["losses"]
            b_trades_per_cycle.append(res["trades"])

    b_total = b_total_wins + b_total_losses
    avg_trades = sum(b_trades_per_cycle) / len(b_trades_per_cycle) if b_trades_per_cycle else 0
    print(f"   Cycles traded:     {b_cycles_traded}")
    print(f"   Total trades:      {b_total_trades}  (avg {avg_trades:.1f}/cycle)")
    print(f"   Win rate:          {b_total_wins}/{b_total}  ({100*b_total_wins/b_total:.1f}%)" if b_total else "   No trades")

    if b_total:
        wr  = b_total_wins / b_total
        net = wr * ev_w + (1 - wr) * ev_l
        print(f"   EV at 50¢ entry:   {net:+.3f} per $1 risked")
        print()
        print(f"   vs Strategy A:     {b_total_trades - a_trades:+d} extra trades, "
              f"win rate {100*b_total_wins/b_total:.1f}% vs {100*a_wins/a_total:.1f}%")
    print()

    # ── Raw data summary ──────────────────────────────────────────────────────
    print(f"{'─'*60}")
    print("RAW DATA (last 10 markets)")
    print(f"{'─'*60}")
    print(f"  {'Ticker':<32} {'BTC%':>6}  {'Result':<6}  {'Kalshi':>6}")
    for r in records[-10:]:
        px = f"{r['kalshi_open_px']}¢" if r["kalshi_open_px"] else "  n/a"
        print(f"  {r['ticker']:<32} {r['btc_pct']:>+6.2f}%  {r['result']:<6}  {px:>6}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BTC 15m Kalshi vs Binance research")
    parser.add_argument("--n", type=int, default=50, help="Number of closed markets to analyse")
    args = parser.parse_args()

    client = KalshiClient()
    analyse(client, n=args.n)
