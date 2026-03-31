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
import math
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
def kalshi_price_model(btc_move_abs: float, minutes_elapsed: int = 7) -> int:
    """
    Realistic Kalshi YES/NO price given:
      - btc_move_abs    : BTC move from open in decimal form (0.002 = 0.2%)
      - minutes_elapsed : how many minutes into the 15-min cycle

    Two drivers:
      1. Move size — larger move → market reprices higher
      2. Time elapsed — later in cycle → less time uncertainty → price closer to 0/100

    If BTC is up 0.15% with 2 min left, YES is ~83¢ (market knows it's likely done).
    If BTC is up 0.05% with 14 min left, YES is ~53¢ (plenty of time to reverse).
    """
    # Base price from BTC move magnitude
    if btc_move_abs < 0.0005:  base = 52
    elif btc_move_abs < 0.0010: base = 57
    elif btc_move_abs < 0.0015: base = 63
    elif btc_move_abs < 0.0020: base = 68
    elif btc_move_abs < 0.0030: base = 73
    elif btc_move_abs < 0.0050: base = 79
    else:                       base = 85

    # Time-remaining premium: less time left → market prices out uncertainty
    mins_remaining = max(0, 15 - minutes_elapsed)
    if   mins_remaining <= 1:  time_adj = +18
    elif mins_remaining <= 2:  time_adj = +13
    elif mins_remaining <= 3:  time_adj = +9
    elif mins_remaining <= 5:  time_adj = +5
    elif mins_remaining <= 7:  time_adj = +2
    else:                      time_adj = 0

    return min(95, base + time_adj)


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
                trades.append({"entry_px": kalshi_price_model(abs(move), minute), "side": side, "status": "open"})
            elif move <= -entry_pct:
                side = "no"
                trades.append({"entry_px": kalshi_price_model(abs(move), minute), "side": side, "status": "open"})

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


def wilson_ci(wins: int, n: int, z: float = 1.645) -> tuple:
    """Wilson score confidence interval (90% by default, z=1.645).
    Returns (low, high) win rate bounds."""
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z*z/n
    centre = (p + z*z/(2*n)) / denom
    margin = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


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

        wr_lo, wr_hi  = wilson_ci(wins, total)   # 90% CI bounds

        daily         = flat_daily_pnl_pct(win_rate,  avg_entry_px, trades_p_cyc)
        daily_lo      = flat_daily_pnl_pct(wr_lo,     avg_entry_px, trades_p_cyc)  # pessimistic
        daily_hi      = flat_daily_pnl_pct(wr_hi,     avg_entry_px, trades_p_cyc)  # optimistic

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
            "wr_lo":        wr_lo,
            "wr_hi":        wr_hi,
            "total_trades": total,
            "trades_p_cyc": trades_p_cyc,
            "trade_freq":   trade_freq,
            "avg_entry_px": avg_entry_px,
            "arith_ev":     arith_ev,
            "daily_growth": daily,
            "daily_lo":     daily_lo,
            "daily_hi":     daily_hi,
        })

    # ── 3. Rank and print ─────────────────────────────────────────────────────
    # Rank by pessimistic (lower CI) daily P&L — filters out lucky small samples
    results.sort(key=lambda x: x["daily_lo"], reverse=True)

    print(f"\n{'='*90}")
    print("TOP 20 STRATEGIES — ranked by PESSIMISTIC daily P&L (90% CI lower bound)")
    print("Prices time-adjusted: late entries cost more. n=50 markets → wide CI on win rate.")
    print(f"{'='*90}")
    print(f"  {'Entry%':>7} {'MaxMin':>6} {'RevExit':>8} {'ReEnt':>5} | "
          f"{'WinRate':>8} {'90%CI':>12} {'Trades/d':>9} {'AvgPx':>7} | "
          f"{'EV/$':>7} {'Daily(mid)':>11} {'Daily(low)':>11}")
    print(f"  {'-'*7} {'-'*6} {'-'*8} {'-'*5} | "
          f"{'-'*8} {'-'*12} {'-'*9} {'-'*7} | "
          f"{'-'*7} {'-'*11} {'-'*11}")

    for r in results[:20]:
        rev_str = f"{r['rev_pct']*100:.2f}%" if r["rev_pct"] else "  none"
        ci_str  = f"[{r['wr_lo']:.0%},{r['wr_hi']:.0%}]"
        print(
            f"  {r['entry_pct']*100:>6.3f}% {r['max_min']:>6d} {rev_str:>8} "
            f"{'yes' if r['reentry'] else 'no':>5} | "
            f"{r['win_rate']:>8.1%} {ci_str:>12} "
            f"{r['trades_p_cyc']*CYCLES_PER_DAY:>9.1f} "
            f"{r['avg_entry_px']:>7.1f}¢ | "
            f"{r['arith_ev']:>+7.3f} {r['daily_growth']:>+10.1%} {r['daily_lo']:>+10.1%}"
        )

    # ── 4. Best strategy deep-dive ────────────────────────────────────────────
    best = results[0]
    print(f"\n{'='*78}")
    print("BEST STRATEGY (by pessimistic CI) — DEEP DIVE")
    print(f"{'='*78}")
    print(f"  Entry trigger:    BTC moves {best['entry_pct']*100:.3f}% from candle open")
    print(f"  Entry window:     first {best['max_min']} minute(s) of cycle")
    rev_str = f"{best['rev_pct']*100:.2f}%" if best["rev_pct"] else "none — hold to resolution"
    print(f"  Reversal exit:    {rev_str}")
    print(f"  Re-entry:         {'yes' if best['reentry'] else 'no'}")
    print()
    print(f"  Win rate:         {best['win_rate']:.1%}  "
          f"(90% CI: [{best['wr_lo']:.1%}, {best['wr_hi']:.1%}]  n={best['total_trades']} trades)")
    print(f"  Avg Kalshi entry: {best['avg_entry_px']:.1f}¢  (time-adjusted — late entries cost more)")
    print(f"  Trades per day:   {best['trades_p_cyc']*CYCLES_PER_DAY:.1f}  "
          f"(enters {best['trade_freq']:.0%} of cycles)")
    print(f"  Arith EV/trade:   {best['arith_ev']:+.3f} per $1 risked")
    print()
    print(f"  Daily P&L (mid):  {best['daily_growth']:+.1%}   ← point estimate")
    print(f"  Daily P&L (low):  {best['daily_lo']:+.1%}   ← pessimistic 90% CI  (use this)")
    print(f"  Daily P&L (high): {best['daily_hi']:+.1%}   ← optimistic 90% CI")
    print()
    print(f"  ⚠️  n={best['total_trades']} trades is a small sample. The CI range above shows")
    print(f"     how sensitive projections are to win-rate uncertainty.")
    print()

    # Simulate dollar growth using PESSIMISTIC estimate (flat, no reinvestment)
    bankroll = 100.0
    dpnl = best["daily_lo"]   # use conservative estimate
    dpnl_mid = best["daily_growth"]
    print(f"  $100 flat — pessimistic ({dpnl:+.1%}/day)  |  mid ({dpnl_mid:+.1%}/day):")
    print(f"    After  1 day:   ${bankroll + bankroll*dpnl:.2f}  |  ${bankroll + bankroll*dpnl_mid:.2f}")
    print(f"    After  7 days:  ${bankroll + bankroll*dpnl*7:.2f}  |  ${bankroll + bankroll*dpnl_mid*7:.2f}")
    print(f"    After 30 days:  ${bankroll + bankroll*dpnl*30:.2f}  |  ${bankroll + bankroll*dpnl_mid*30:.2f}")
    print()

    # Target check on pessimistic estimate
    target_daily = 0.10
    if best["daily_lo"] >= target_daily:
        print(f"  ✅ Meets 10%/day target even at pessimistic CI")
    elif best["daily_growth"] >= target_daily:
        print(f"  ⚠️  Meets 10%/day at midpoint but NOT at pessimistic bound")
        print(f"     More data (>50 markets) needed to confirm the edge is real")
    else:
        needed_wr = None
        for test_wr in [x/100 for x in range(50, 100)]:
            if flat_daily_pnl_pct(test_wr, best["avg_entry_px"],
                                   best["trades_p_cyc"]) >= target_daily:
                needed_wr = test_wr
                break
        if needed_wr:
            print(f"  ⚠️  Best strategy projects {best['daily_growth']:+.1%}/day (mid)")
            print(f"     10%/day requires {needed_wr:.0%} win rate at these params")
        print()

    # ── 5. Strategies positive at pessimistic CI ───────────────────────────────
    robust = [r for r in results if r["daily_lo"] > 0]
    hits_target = [r for r in results if r["daily_lo"] >= 0.10]

    print(f"{'─'*78}")
    print(f"STRATEGIES POSITIVE AT PESSIMISTIC 90% CI: {len(robust)}")
    if hits_target:
        print(f"STRATEGIES ≥10%/DAY EVEN AT PESSIMISTIC CI: {len(hits_target)}")
    print(f"{'─'*78}")
    show = hits_target[:10] if hits_target else robust[:10]
    for r in show:
        rev_str = f"{r['rev_pct']*100:.2f}%" if r["rev_pct"] else "none"
        ci_str  = f"[{r['wr_lo']:.0%},{r['wr_hi']:.0%}]"
        print(f"  entry={r['entry_pct']*100:.3f}%  "
              f"window={r['max_min']}min  "
              f"exit={rev_str}  "
              f"reentry={'Y' if r['reentry'] else 'N'}  →  "
              f"wr={r['win_rate']:.1%}{ci_str}  "
              f"px={r['avg_entry_px']:.0f}¢  "
              f"daily={r['daily_growth']:+.1%}  low={r['daily_lo']:+.1%}")

    if not robust:
        print("  No strategies show positive EV at the pessimistic bound.")
        print("  Need more data or a genuinely better edge.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BTC 15m strategy optimizer")
    parser.add_argument("--n", type=int, default=50)
    args = parser.parse_args()
    client = KalshiClient()
    run(client, n=args.n)
