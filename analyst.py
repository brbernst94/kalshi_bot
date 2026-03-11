"""
utils/analyst.py — Daily Strategy Analyst & Auto-Rebalancer
=============================================================
Runs every day at 00:15 UTC. Reads trades.csv, scores every strategy
across five dimensions, decides whether to rebalance, and rewrites
config.py if it does.

Scoring dimensions (each 0–1):
  1. Win Rate       — % of CLOSE trades that were profitable
  2. PnL/Trade      — average realised PnL per closed position
  3. Trade Velocity — is the strategy actually firing? (low trades = low signal)
  4. Edge Decay     — are recent trades weaker than older ones?
  5. Drawdown       — did this strategy hit stop-losses repeatedly?

Rebalancing rules:
  - If a strategy scores < 0.35 for 2+ consecutive days → cut allocation by 30%,
    redistribute to the top scorer
  - If a strategy scores > 0.75 AND is under-allocated → bump it up
  - Hard floor: no strategy drops below 5% allocation
  - Hard ceiling: no strategy exceeds 50% allocation
  - STRATEGY_ALLOCATION always sums to 1.00
  - Writes a human-readable daily report to logs/analysis_YYYY-MM-DD.txt
  - Only touches STRATEGY_ALLOCATION and per-strategy thresholds in config.py
    — credentials and API endpoints are never modified
"""

import csv
import json
import logging
import os
import re
import textwrap
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TRADE_LOG      = "logs/trades.csv"
ANALYSIS_DIR   = "logs"
CONFIG_FILE    = "config.py"
HISTORY_FILE   = "logs/score_history.json"

STRATEGIES     = ["whale", "fade", "bond", "longshot"]
MIN_ALLOC      = 0.05
MAX_ALLOC      = 0.50
LOW_SCORE_THRESHOLD  = 0.35
HIGH_SCORE_THRESHOLD = 0.75
CUT_FACTOR     = 0.30    # reduce losing strategy by 30% of its current allocation
BOOST_FACTOR   = 0.15    # increase winning strategy by 15%


# ── Data loading ──────────────────────────────────────────────────────────────

def load_trades(days_back: int = 30) -> List[Dict]:
    """Load all trades from CSV, optionally filtered to recent N days."""
    if not os.path.exists(TRADE_LOG):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    trades = []
    with open(TRADE_LOG, newline="") as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if ts >= cutoff:
                    trades.append({**row, "_dt": ts})
            except Exception:
                continue
    return trades


def split_by_strategy(trades: List[Dict]) -> Dict[str, List[Dict]]:
    by_strat = defaultdict(list)
    for t in trades:
        by_strat[t.get("strategy", "unknown")].append(t)
    return dict(by_strat)


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_strategy(name: str, trades: List[Dict],
                   all_trades: List[Dict]) -> Dict:
    """
    Score a single strategy. Returns a dict with individual dimension scores
    and a composite score (0–1).
    """
    if not trades:
        return {
            "composite": 0.10,   # penalise silence — no trades = no signal
            "win_rate": 0, "pnl_per_trade": 0, "velocity": 0,
            "edge_decay": 0.5, "drawdown": 1.0,
            "n_trades": 0, "total_pnl": 0, "note": "no trades"
        }

    closed   = [t for t in trades if t.get("status") == "CLOSE"]
    placed   = [t for t in trades if t.get("status") == "PLACED"]
    n_closed = len(closed)
    n_placed = len(placed)

    # 1. Win rate (closed trades)
    wins     = [t for t in closed if float(t.get("expected_pnl_usd", 0)) > 0]
    win_rate = len(wins) / n_closed if n_closed > 0 else 0.0

    # 2. PnL per trade (normalised to $10 = 1.0)
    total_pnl     = sum(float(t.get("expected_pnl_usd", 0)) for t in closed)
    pnl_per_trade = (total_pnl / n_closed) if n_closed > 0 else 0.0
    pnl_score     = min(max(pnl_per_trade / 10.0, -1.0), 1.0)   # clamp -1 to 1
    pnl_score     = (pnl_score + 1) / 2   # rescale to 0–1

    # 3. Trade velocity (are we firing enough? low = under-signalling)
    # Expected: at least 1 trade per 2 days for active strategies
    days_active = max(
        (max(t["_dt"] for t in trades) -
         min(t["_dt"] for t in trades)).days + 1,
        1
    )
    trades_per_day = (n_placed + n_closed) / days_active
    velocity_score = min(trades_per_day / 1.0, 1.0)   # 1 trade/day = 1.0

    # 4. Edge decay (are recent trades worse than older ones?)
    # Compare PnL of last-half vs first-half of closed trades
    if n_closed >= 4:
        mid         = n_closed // 2
        sorted_cl   = sorted(closed, key=lambda t: t["_dt"])
        early_pnl   = sum(float(t.get("expected_pnl_usd", 0)) for t in sorted_cl[:mid])
        recent_pnl  = sum(float(t.get("expected_pnl_usd", 0)) for t in sorted_cl[mid:])
        if early_pnl != 0:
            decay = (recent_pnl - early_pnl) / abs(early_pnl)
            edge_decay_score = min(max((decay + 1) / 2, 0), 1)
        else:
            edge_decay_score = 0.5
    else:
        edge_decay_score = 0.5   # neutral when not enough data

    # 5. Drawdown (stops hit = bad; 0 stops = 1.0)
    stops    = [t for t in closed if "STOP" in t.get("notes", "")]
    stop_rate = len(stops) / max(n_closed, 1)
    drawdown_score = 1.0 - stop_rate

    # Composite — weighted average
    composite = (
        win_rate          * 0.30 +
        pnl_score         * 0.30 +
        velocity_score    * 0.15 +
        edge_decay_score  * 0.15 +
        drawdown_score    * 0.10
    )

    return {
        "composite":      round(composite, 3),
        "win_rate":       round(win_rate, 3),
        "pnl_per_trade":  round(pnl_per_trade, 2),
        "velocity":       round(velocity_score, 3),
        "edge_decay":     round(edge_decay_score, 3),
        "drawdown":       round(drawdown_score, 3),
        "n_trades":       n_placed + n_closed,
        "n_closed":       n_closed,
        "total_pnl":      round(total_pnl, 2),
        "stop_hits":      len(stops),
        "note":           "",
    }


# ── Rebalancing logic ─────────────────────────────────────────────────────────

def load_score_history() -> Dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_score_history(history: Dict):
    os.makedirs(ANALYSIS_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def compute_new_allocation(scores: Dict[str, Dict],
                            current_alloc: Dict[str, float],
                            history: Dict) -> Tuple[Dict[str, float], List[str]]:
    """
    Compute new STRATEGY_ALLOCATION based on scores + history.
    Returns (new_allocation, list_of_change_reasons).
    """
    today     = str(date.today())
    reasons   = []
    new_alloc = dict(current_alloc)

    # Record today's scores in history
    for name, s in scores.items():
        if name not in history:
            history[name] = []
        history[name].append({"date": today, "score": s["composite"]})
        # Keep last 14 days
        history[name] = history[name][-14:]

    # Check for consecutive low-score days (trigger cut)
    for name in STRATEGIES:
        hist = history.get(name, [])
        if len(hist) >= 2:
            last_two = [h["score"] for h in hist[-2:]]
            if all(s < LOW_SCORE_THRESHOLD for s in last_two):
                cut = new_alloc.get(name, 0.20) * CUT_FACTOR
                new_alloc[name] = max(new_alloc.get(name, 0.20) - cut, MIN_ALLOC)
                reasons.append(
                    f"✂️  {name.upper()} cut by {CUT_FACTOR:.0%} "
                    f"(scored {last_two[0]:.2f}, {last_two[1]:.2f} — 2 low days)"
                )

    # Boost top performer if under-allocated
    best_name  = max(scores, key=lambda n: scores[n]["composite"])
    best_score = scores[best_name]["composite"]
    if best_score > HIGH_SCORE_THRESHOLD:
        current = new_alloc.get(best_name, 0.20)
        boost   = min(BOOST_FACTOR, MAX_ALLOC - current)
        if boost > 0.01:
            new_alloc[best_name] = current + boost
            reasons.append(
                f"🚀 {best_name.upper()} boosted +{boost:.0%} "
                f"(scored {best_score:.2f} — top performer)"
            )

    # Normalise to sum = 1.00 (redistribute proportionally)
    total = sum(new_alloc.values())
    if total > 0:
        new_alloc = {k: round(v / total, 4) for k, v in new_alloc.items()}

    # Final clamp pass
    for name in STRATEGIES:
        if name not in new_alloc:
            new_alloc[name] = MIN_ALLOC
        new_alloc[name] = round(max(MIN_ALLOC, min(MAX_ALLOC, new_alloc[name])), 4)

    # Re-normalise after clamping
    total = sum(new_alloc[n] for n in STRATEGIES)
    new_alloc = {n: round(new_alloc[n] / total, 4) for n in STRATEGIES}
    # Ensure exact sum due to rounding
    diff = round(1.0 - sum(new_alloc.values()), 4)
    if diff:
        top = max(new_alloc, key=new_alloc.get)
        new_alloc[top] = round(new_alloc[top] + diff, 4)

    return new_alloc, reasons


# ── Config rewriter ───────────────────────────────────────────────────────────

def rewrite_config(new_alloc: Dict[str, float],
                   scores: Dict[str, Dict]) -> bool:
    """
    Surgically rewrite only the STRATEGY_ALLOCATION dict in config.py.
    Returns True if the file was changed.
    """
    if not os.path.exists(CONFIG_FILE):
        logger.error(f"[ANALYST] {CONFIG_FILE} not found — cannot rewrite")
        return False

    with open(CONFIG_FILE, "r") as f:
        original = f.read()

    # Build new allocation block
    lines = ["STRATEGY_ALLOCATION = {\n"]
    for name in STRATEGIES:
        pct = new_alloc.get(name, 0.20)
        score = scores.get(name, {}).get("composite", 0)
        lines.append(f'    "{name}":    {pct:.4f},   # score={score:.3f}\n')
    lines.append("}\n")
    new_block = "".join(lines)

    # Replace existing STRATEGY_ALLOCATION block using regex
    pattern = r"STRATEGY_ALLOCATION\s*=\s*\{[^}]+\}"
    if not re.search(pattern, original, re.DOTALL):
        logger.warning("[ANALYST] Could not locate STRATEGY_ALLOCATION in config.py")
        return False

    new_content = re.sub(pattern, new_block.rstrip(), original, flags=re.DOTALL)

    if new_content == original:
        return False   # No change needed

    with open(CONFIG_FILE, "w") as f:
        f.write(new_content)

    logger.info("[ANALYST] config.py rewritten with new STRATEGY_ALLOCATION")
    return True


# ── Report writer ─────────────────────────────────────────────────────────────

def write_report(scores: Dict[str, Dict],
                 old_alloc: Dict[str, float],
                 new_alloc: Dict[str, float],
                 reasons: List[str],
                 changed: bool,
                 overall_pnl: float,
                 n_total_trades: int):
    today     = str(date.today())
    filepath  = os.path.join(ANALYSIS_DIR, f"analysis_{today}.txt")
    bar       = "═" * 60

    lines = [
        bar,
        f"  📊 KALSHI BOT — DAILY ANALYSIS  |  {today}",
        bar,
        f"  Total trades (last 30d): {n_total_trades}",
        f"  Total realised PnL:      ${overall_pnl:+.2f}",
        "",
        "  STRATEGY SCORECARDS",
        "  " + "─" * 56,
    ]

    for name in STRATEGIES:
        s   = scores.get(name, {})
        old = old_alloc.get(name, 0)
        new = new_alloc.get(name, 0)
        arrow = "▲" if new > old else ("▼" if new < old else "─")
        lines += [
            f"  {name.upper():10} score={s.get('composite',0):.3f}  "
            f"alloc: {old:.0%} {arrow} {new:.0%}",
            f"    win_rate={s.get('win_rate',0):.1%}  "
            f"pnl/trade=${s.get('pnl_per_trade',0):+.2f}  "
            f"trades={s.get('n_trades',0)}  "
            f"stops={s.get('stop_hits',0)}",
            f"    velocity={s.get('velocity',0):.2f}  "
            f"edge_decay={s.get('edge_decay',0):.2f}  "
            f"drawdown={s.get('drawdown',0):.2f}",
            "",
        ]

    lines += ["  REBALANCING DECISIONS", "  " + "─" * 56]
    if reasons:
        for r in reasons:
            lines.append(f"  {r}")
    else:
        lines.append("  ✅ No rebalancing needed — allocations unchanged")

    lines += [
        "",
        "  NEW ALLOCATION",
        "  " + "─" * 56,
    ]
    for name in STRATEGIES:
        bar_len = int(new_alloc.get(name, 0) * 30)
        bar_str = "█" * bar_len + "░" * (30 - bar_len)
        lines.append(f"  {name.upper():10} [{bar_str}] {new_alloc.get(name,0):.1%}")

    lines += ["", bar, ""]

    os.makedirs(ANALYSIS_DIR, exist_ok=True)
    with open(filepath, "w") as f:
        f.write("\n".join(lines))

    # Also print to stdout so it appears in Railway logs
    print("\n" + "\n".join(lines))
    logger.info(f"[ANALYST] Report written to {filepath}")


# ── Main entry point ──────────────────────────────────────────────────────────

def run_daily_analysis():
    """
    Called by main.py scheduler at 00:15 UTC every day.
    Full pipeline: load → score → decide → rewrite → report.
    """
    logger.info("[ANALYST] ═══ Daily analysis starting ═══")

    # Load trades (last 30 days for scoring, all time for context)
    trades_30d  = load_trades(days_back=30)
    by_strategy = split_by_strategy(trades_30d)

    # Current allocation from config
    try:
        from config import STRATEGY_ALLOCATION
        current_alloc = dict(STRATEGY_ALLOCATION)
    except Exception:
        current_alloc = {n: 0.25 for n in STRATEGIES}

    # Score each strategy
    scores = {}
    for name in STRATEGIES:
        strat_trades   = by_strategy.get(name, [])
        scores[name]   = score_strategy(name, strat_trades, trades_30d)
        logger.info(
            f"[ANALYST] {name.upper():10} "
            f"score={scores[name]['composite']:.3f}  "
            f"wr={scores[name]['win_rate']:.1%}  "
            f"pnl=${scores[name]['total_pnl']:+.2f}  "
            f"n={scores[name]['n_trades']}"
        )

    # Load score history and compute new allocation
    history                = load_score_history()
    new_alloc, reasons     = compute_new_allocation(scores, current_alloc, history)
    save_score_history(history)

    # Check if anything actually changed
    changed = any(
        abs(new_alloc.get(n, 0) - current_alloc.get(n, 0)) > 0.005
        for n in STRATEGIES
    )

    # Rewrite config if needed
    if changed:
        rewrite_config(new_alloc, scores)
        logger.info(f"[ANALYST] Allocation changed: {current_alloc} → {new_alloc}")
    else:
        logger.info("[ANALYST] Allocation unchanged")

    # Overall stats
    overall_pnl   = sum(s["total_pnl"] for s in scores.values())
    n_total       = sum(s["n_trades"] for s in scores.values())

    # Write report
    write_report(scores, current_alloc, new_alloc, reasons,
                 changed, overall_pnl, n_total)

    logger.info("[ANALYST] ═══ Daily analysis complete ═══")
    return scores, new_alloc, changed
