"""
btc_optimize.py — Grid search for best BTC 15m trading strategy
================================================================
Tests every combination of entry threshold, timing, exit rules, and
re-entry logic across 50 real markets + 1-minute intra-candle BTC data.
Ranks strategies by projected daily growth toward the 10% target.

Usage:
    python btc_optimize.py          # 50 markets
    python btc_optimize.py --n 100  # more data
"""

import argparse
import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from itertools import product
from typing import Optional

from client import KalshiClient, price_cents as _pc

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
log = logging.getLogger("optimize")

# ── Constants ─────────────────────────────────────────────────────────────────
SETTLEMENT_FEE  = 0.07    # Kalshi takes 7% of winnings at resolution
POSITION_PCT    = 0.80    # fraction of bankroll per trade
CYCLES_PER_DAY  = 96      # 15-min cycles in 24 hours

# Realistic constraints the naive model ignores:
#  - Kalshi reprices within ~5-15s of a BTC move (other bots exist)
#  - Taker spread: ~4-6¢ round-trip on market orders
#  - Not every cycle has a trade (low-volatility windows)
#  - 50-market sample is small — treat win rates with skepticism
TAKER_SPREAD_CENTS = 5    # estimated round-trip cost for market orders

# Kalshi price model: estimated YES price when BTC has moved X% from open
# Based on market behavior — larger BTC moves → higher certainty → higher price
def kalshi_price_model(btc_move_abs: float) -> int:
    """
    Realistic Kalshi YES/NO price given a BTC move in decimal form (0.002 = 0.2%).
    Kalshi reprices within seconds — by the time your order fires, the market
    has already partially moved. These reflect realistic fill prices.
    """
    if btc_move_abs < 0.0005:  return 52   # 0.05% — barely moved
    if btc_move_abs < 0.0010:  return 57   # 0.10% — slight edge
    if btc_move_abs < 0.0015:  return 63   # 0.15% — clear direction
    if btc_move_abs < 0.0020:  return 68   # 0.20% — well priced in
    if btc_move_abs < 0.0030:  return 73   # 0.30% — strong move
    if btc_move_abs < 0.0050:  return 79   # 0.50% — near certain
    return 85                              # 0.50%+ — extreme move


# ── Binance data ──────────────────────────────────────────────────────────────

def fetch_1m_candles(open_ts_ms: int) -> list:
    """15 × 1-minute BTCUSDT klines for one 15-min window."""
    url = (
        "https://api.binance.com/api/v3/klines"
        f"?symbol=BTCUSDT&interval=1m&startTime={open_ts_ms}&limit=15"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "btc-optimize/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return [{"min": i, "open": float(k[1]), "close": float(k[4])} for i, k in enumerate(data)]
    except Exception as e:
        log.warning(f"Binance 1m fetch failed: {e}")
        return []


def fetch_15m_kline(open_ts_ms: int) -> Optional[dict]:
    """Single 15m kline for final BTC direction."""
    url = (
        "https://api.binance.com/api/v3/klines"
        f"?symbol=BTCUSDT&interval=15m&startTime={open_ts_ms}&limit=1"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "btc-optimize/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if not data: return None
        k = data[0]
        return {"open": float(k[1]), "close": float(k[4]),
                "pct": (float(k[4]) - float(k[1])) / float(k[1]) * 100}
    except Exception:
        return None


# ── Strategy simulator ────────────────────────────────────────────────────────

def simulate(candles_1m: list, btc_open: float, yes_won: bool,
             entry_pct: float, max_entry_min: int,
             reversal_pct: Optional[float], allow_reentry: bool) -> list:
    """
    Simulate one strategy on one market. Returns list of trade outcomes.
    Each outcome: {"entry_px": int, "win": bool}

    Parameters
    ----------
    entry_pct      : BTC must move this % from reference to trigger entry
    max_entry_min  : only enter within the first N minutes of the cycle
    reversal_pct   : if set, exit when BTC reverses this % against position
    allow_reentry  : after a reversal exit, reset reference and re-enter
    """
    trades   = []
    ref      = btc_open
    side     = None   # "yes" (long YES) or "no" (long NO)

    for c in candles_1m:
        minute = c["min"]
        price  = c["close"]
        move   = (price - ref) / ref

        if side is None:
            # Only enter within the allowed window
            if minute >= max_entry_min:
                break
            if move >= entry_pct:
                side = "yes"
                trades.append({"entry_px": kalshi_price_model(abs(move)), "side": side, "status": "open"})
            elif move <= -entry_pct:
                side = "no"
                trades.append({"entry_px": kalshi_price_model(abs(move)), "side": side, "status": "open"})

        elif reversal_pct is not None:
            reversed_out = (
                (side == "yes" and move <= -reversal_pct) or
                (side == "no"  and move >=  reversal_pct)
            )
            if reversed_out:
                # Exited mid-candle — estimate Kalshi price at exit
                # BTC has reversed; Kalshi price for our side is now below entry
                entry_px = trades[-1]["entry_px"]
                # Rough model: exited at roughly entry_px - 8¢ (reversed against us)
                exit_px  = max(1, entry_px - 8)
                pnl_cents = exit_px - entry_px   # negative
                trades[-1]["status"] = "reversed"
                trades[-1]["win"]    = False
                trades[-1]["pnl_cents"] = pnl_cents

                if allow_reentry:
                    ref  = price
                    side = None   # look for new signal
                else:
                    break

    # Resolve any still-open position at resolution
    for t in trades:
        if t.get("status") == "open":
            won = (t["side"] == "yes") == yes_won
            t["win"]    = won
            t["status"] = "resolved"
            if won:
                t["pnl_cents"] = int((100 - t["entry_px"]) * (1 - SETTLEMENT_FEE))
            else:
                t["pnl_cents"] = -t["entry_px"]

    return trades


# ── P&L model ─────────────────────────────────────────────────────────────────

def trade_multiplier(trade: dict) -> float:
    """
    Returns the bankroll multiplier for this single trade.
    e.g. 1.37 means bankroll grew 37% on this trade.
    """
    entry_px = trade["entry_px"]
    pnl_c    = trade["pnl_cents"]
    # Cost = POSITION_PCT of bankroll
    # Return on that cost = pnl_c / entry_px
    roi = pnl_c / entry_px
    return 1.0 + POSITION_PCT * roi


def flat_daily_pnl_pct(win_rate: float, avg_entry_px: float,
                       trades_per_cycle: float) -> float:
    """
    Project daily P&L as a fraction of a fixed starting bankroll (no compounding).
    Uses arithmetic expected value × trades per day.
    This avoids the astronomical compounding blowup from high win rates.

    win_pnl / lose_pnl in cents per 100¢ contract (includes spread + fee).
    POSITION_PCT fraction of bankroll is risked per trade.
    """
    win_pnl       = (100 - avg_entry_px) * (1 - SETTLEMENT_FEE) - TAKER_SPREAD_CENTS
    lose_pnl      = -avg_entry_px - TAKER_SPREAD_CENTS
    ev_cents      = win_rate * win_pnl + (1 - win_rate) * lose_pnl
    roi_per_trade = (ev_cents / avg_entry_px) * POSITION_PCT   # fraction of bankroll
    trades_per_day = trades_per_cycle * CYCLES_PER_DAY
    return roi_per_trade * trades_per_day


# Keep old name as alias for the break-even analysis helper below
geometric_daily_growth = flat_daily_pnl_pct


# ── Kalshi market helpers ─────────────────────────────────────────────────────

def parse_close_ts(m: dict) -> Optional[int]:
    for field in ("close_time", "expiration_time"):
        s = m.get(field)
        if s:
            try:
                return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
            except Exception:
                pass
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def run(client: KalshiClient, n: int = 50) -> None:

    # ── 1. Collect market data ────────────────────────────────────────────────
    log.info(f"Fetching {n} closed markets…")
    resp    = client.get_markets(series_ticker="KXBTC15M", status="settled", limit=n)
    markets = resp.get("markets", [])
    if not markets:
        resp    = client.get_markets(series_ticker="KXBTC15M", status="closed", limit=n)
        markets = resp.get("markets", [])
    log.info(f"  Got {len(markets)} markets")

    dataset = []
    log.info("Fetching Binance 1m + 15m data…")
    for m in markets:
        result = m.get("result", "")
        if result not in ("yes", "no"):
            continue
        close_ts = parse_close_ts(m)
        if not close_ts:
            continue
        open_ts_ms = (close_ts - 900) * 1000

        kline    = fetch_15m_kline(open_ts_ms)
        candles  = fetch_1m_candles(open_ts_ms)
        if not kline or not candles:
            continue

        dataset.append({
            "yes_won":   result == "yes",
            "btc_open":  kline["open"],
            "btc_pct":   kline["pct"],
            "candles":   candles,
        })
        time.sleep(0.06)   # rate limit

    log.info(f"  Dataset: {len(dataset)} markets with full data\n")

    # ── 2. Grid search ────────────────────────────────────────────────────────
    entry_pcts      = [0.0005, 0.001, 0.0015, 0.002, 0.003, 0.005]  # 0.05% → 0.5%
    max_entry_mins  = [1, 2, 3, 5, 8, 10, 13]
    reversal_pcts   = [None, 0.001, 0.002, 0.003]  # none, 0.1%, 0.2%, 0.3%
    reentries       = [False, True]

    total_combos = len(entry_pcts) * len(max_entry_mins) * len(reversal_pcts) * len(reentries)
    log.info(f"Grid search: {total_combos} parameter combinations × {len(dataset)} markets…\n")

    results = []

    for entry_pct, max_min, rev_pct, reentry in product(
            entry_pcts, max_entry_mins, reversal_pcts, reentries):

        all_trades      = []
        cycles_traded   = 0
        cycles_no_entry = 0

        for row in dataset:
            trades = simulate(
                row["candles"], row["btc_open"], row["yes_won"],
                entry_pct, max_min, rev_pct, reentry
            )
            if trades:
                cycles_traded += 1
                all_trades.extend(trades)
            else:
                cycles_no_entry += 1

        if not all_trades:
            continue

        wins          = sum(1 for t in all_trades if t.get("win"))
        total         = len(all_trades)
        win_rate      = wins / total
        avg_entry_px  = sum(t["entry_px"] for t in all_trades) / total
        trades_p_cyc  = total / len(dataset)   # avg trades per 15-min cycle
        trade_freq    = cycles_traded / len(dataset)

        daily = flat_daily_pnl_pct(win_rate, avg_entry_px, trades_p_cyc)

        # Arithmetic EV per $1 risked (for comparison)
        win_pay  = (100 - avg_entry_px) * (1 - SETTLEMENT_FEE) / avg_entry_px
        lose_pay = -1.0
        arith_ev = win_rate * win_pay + (1 - win_rate) * lose_pay

        results.append({
            "entry_pct":    entry_pct,
            "max_min":      max_min,
            "rev_pct":      rev_pct,
            "reentry":      reentry,
            "win_rate":     win_rate,
            "total_trades": total,
            "trades_p_cyc": trades_p_cyc,
            "trade_freq":   trade_freq,
            "avg_entry_px": avg_entry_px,
            "arith_ev":     arith_ev,
            "daily_growth": daily,
        })

    # ── 3. Rank and print ─────────────────────────────────────────────────────
    results.sort(key=lambda x: x["daily_growth"], reverse=True)

    print(f"\n{'='*78}")
    print("TOP 20 STRATEGIES — ranked by flat daily P&L (80% size, 5¢ spread, no compounding)")
    print("Daily % = arithmetic EV × trades/day on fixed starting bankroll")
    print(f"{'='*78}")
    print(f"  {'Entry%':>7} {'MaxMin':>7} {'RevExit':>8} {'ReEnt':>6} | "
          f"{'WinRate':>8} {'Trades/d':>9} {'AvgPx':>7} | "
          f"{'ArithEV':>8} {'DailyGrowth':>12}")
    print(f"  {'-'*7} {'-'*7} {'-'*8} {'-'*6} | "
          f"{'-'*8} {'-'*9} {'-'*7} | "
          f"{'-'*8} {'-'*12}")

    for r in results[:20]:
        rev_str = f"{r['rev_pct']*100:.2f}%" if r["rev_pct"] else "  none"
        print(
            f"  {r['entry_pct']*100:>6.3f}% {r['max_min']:>7d} {rev_str:>8} "
            f"{'yes' if r['reentry'] else 'no':>6} | "
            f"{r['win_rate']:>8.1%} {r['trades_p_cyc']*CYCLES_PER_DAY:>9.1f} "
            f"{r['avg_entry_px']:>7.1f}¢ | "
            f"{r['arith_ev']:>+8.3f} {r['daily_growth']:>+11.1%}"
        )

    # ── 4. Best strategy deep-dive ────────────────────────────────────────────
    best = results[0]
    print(f"\n{'='*78}")
    print("BEST STRATEGY — DEEP DIVE")
    print(f"{'='*78}")
    print(f"  Entry trigger:    BTC moves {best['entry_pct']*100:.3f}% from candle open")
    print(f"  Entry window:     first {best['max_min']} minute(s) of cycle")
    rev_str = f"{best['rev_pct']*100:.2f}%" if best["rev_pct"] else "none — hold to resolution"
    print(f"  Reversal exit:    {rev_str}")
    print(f"  Re-entry:         {'yes' if best['reentry'] else 'no'}")
    print()
    print(f"  Win rate:         {best['win_rate']:.1%}")
    print(f"  Avg Kalshi entry: {best['avg_entry_px']:.1f}¢")
    print(f"  Trades per day:   {best['trades_p_cyc']*CYCLES_PER_DAY:.1f}  "
          f"(enters {best['trade_freq']:.0%} of cycles)")
    print(f"  Arith EV/trade:   {best['arith_ev']:+.3f} per $1 risked")
    print(f"  Projected daily:  {best['daily_growth']:+.1%}")
    print()

    # Simulate dollar growth (flat — fixed $100 base, no reinvestment)
    bankroll = 100.0
    dpnl = best["daily_growth"]
    print(f"  $100 flat (no reinvestment):")
    print(f"    After  1 day:   ${bankroll + bankroll*dpnl*1:.2f}  ({dpnl:+.1%}/day)")
    print(f"    After  3 days:  ${bankroll + bankroll*dpnl*3:.2f}")
    print(f"    After  7 days:  ${bankroll + bankroll*dpnl*7:.2f}")
    print(f"    After 30 days:  ${bankroll + bankroll*dpnl*30:.2f}")
    print()

    # Target check
    target_daily = 0.10
    if best["daily_growth"] >= target_daily:
        print(f"  ✅ Meets 10%/day target")
    else:
        needed_wr = None
        # Find what win rate would hit 10%/day with these params
        for test_wr in [x/100 for x in range(50, 100)]:
            if geometric_daily_growth(test_wr, best["avg_entry_px"],
                                      best["trades_p_cyc"]) >= target_daily:
                needed_wr = test_wr
                break
        if needed_wr:
            print(f"  ⚠️  Best strategy projects {best['daily_growth']:+.1%}/day")
            print(f"     10%/day requires {needed_wr:.0%} win rate at these params")
            print(f"     or more trades/cycle")
        print()

    # ── 5. Strategies that hit 10%/day ────────────────────────────────────────
    hits_target = [r for r in results if r["daily_growth"] >= 0.10]
    if hits_target:
        print(f"{'─'*78}")
        print(f"STRATEGIES PROJECTING ≥10%/DAY: {len(hits_target)}")
        print(f"{'─'*78}")
        for r in hits_target[:10]:
            rev_str = f"{r['rev_pct']*100:.2f}%" if r["rev_pct"] else "none"
            print(f"  entry={r['entry_pct']*100:.3f}%  "
                  f"window={r['max_min']}min  "
                  f"exit={rev_str}  "
                  f"reentry={'Y' if r['reentry'] else 'N'}  →  "
                  f"wr={r['win_rate']:.1%}  "
                  f"{r['trades_p_cyc']*CYCLES_PER_DAY:.0f}trades/d  "
                  f"daily={r['daily_growth']:+.1%}")
    else:
        print(f"{'─'*78}")
        print("NO SINGLE STRATEGY PROJECTS ≥10%/DAY with current data.")
        print("Closest strategies and gap to target:")
        gap_needed = 0.10 - best["daily_growth"]
        print(f"  Best daily projection: {best['daily_growth']:+.1%}")
        print(f"  Gap to 10% target:     {gap_needed:+.1%}")
        print()
        print("  Ways to close the gap:")
        # How many trades/day needed at best win rate?
        for tpd in [10, 20, 48, 96]:
            tpc = tpd / CYCLES_PER_DAY
            d = geometric_daily_growth(best["win_rate"], best["avg_entry_px"], tpc)
            print(f"    {tpd:3d} trades/day @ {best['win_rate']:.0%} wr → {d:+.1%}/day")
        print()
        print("  Win rate needed at best trade frequency:")
        tpc = best["trades_p_cyc"]
        for wr in [0.70, 0.75, 0.80, 0.85, 0.90, 0.92, 0.95]:
            d = geometric_daily_growth(wr, best["avg_entry_px"], tpc)
            mark = " ← TARGET" if d >= 0.10 else ""
            print(f"    {wr:.0%} win rate → {d:+.1%}/day{mark}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BTC 15m strategy optimizer")
    parser.add_argument("--n", type=int, default=50)
    args = parser.parse_args()
    client = KalshiClient()
    run(client, n=args.n)
