"""utils/dashboard.py — Kalshi bot terminal dashboard"""
import csv, os, logging
from datetime import datetime, timezone
from config import STARTING_BANKROLL_USD, MONTHLY_TARGET_USD, TRADE_LOG_FILE

logger = logging.getLogger(__name__)
G="\033[92m"; R="\033[91m"; C="\033[96m"; B="\033[1m"; X="\033[0m"

def _c(v): return G if v >= 0 else R


def print_dashboard(risk_manager, cycle: int):
    s       = risk_manager.status()
    balance = s["balance"]
    daily   = s["daily_pnl"]
    total   = balance - STARTING_BANKROLL_USD
    prog    = (total / MONTHLY_TARGET_USD) * 100
    filled  = int(30 * min(max(prog, 0), 100) / 100)
    bar     = "█" * filled + "░" * (30 - filled)

    print(f"\n{B}{C}{'═'*64}{X}")
    print(f"{B}  🎯  Kalshi Bot  |  Cycle #{cycle}  |  "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}{X}")
    print(f"{C}{'═'*64}{X}")
    print(f"  Balance     : {B}${balance:>10.2f} USD{X}")
    print(f"  Total PnL   : {_c(total)}{B}${total:>+10.2f}{X}")
    print(f"  Today PnL   : {_c(daily)}{daily:>+10.2f}{X}")
    print(f"  Open Pos    : {s['open_positions']:>3} / 4")
    print(f"  Progress    : [{C}{bar}{X}] {prog:.1f}% of ${MONTHLY_TARGET_USD:,}/mo")
    print(f"  Demo Mode   : {'YES ⚠️' if 'demo' in __import__('config').BASE_URL else 'NO — LIVE 🔴'}")
    from config import STRATEGY_ALLOCATION
    print(f"\n  {B}Budget split (${balance:.0f}):{X}")
    for k, v in STRATEGY_ALLOCATION.items():
        print(f"    {k.upper():8}  ${balance * v:>7.2f}")
    if s["positions"]:
        print(f"\n  {B}Open Positions:{X}")
        for ticker, p in s["positions"].items():
            held = (datetime.now(timezone.utc) -
                    p["opened_at"]).total_seconds() / 3600
            print(f"    {p['strategy'].upper():8} | {ticker:28} | "
                  f"{p['count']}x @ {p['entry_cents']}¢ | {held:.1f}h")
    print(f"{C}{'═'*64}{X}\n")


def monthly_summary():
    s = {"total_trades": 0, "total_pnl": 0.0, "by_strategy": {}, "win_rate": 0.0}
    if not os.path.exists(TRADE_LOG_FILE):
        return s
    wins = 0
    with open(TRADE_LOG_FILE, newline="") as f:
        for row in csv.DictReader(f):
            try:
                pnl   = float(row.get("expected_pnl_usd", 0))
                strat = row.get("strategy", "?")
                s["total_trades"] += 1
                s["total_pnl"]    += pnl
                s["by_strategy"].setdefault(strat, 0.0)
                s["by_strategy"][strat] += pnl
                if pnl > 0: wins += 1
            except Exception:
                continue
    if s["total_trades"] > 0:
        s["win_rate"] = wins / s["total_trades"]
    logger.info(f"📊 {s['total_trades']} trades | ${s['total_pnl']:+.2f} PnL "
                f"| {s['win_rate']:.1%} wr | {s['by_strategy']}")
    return s
