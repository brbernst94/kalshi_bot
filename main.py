"""
main.py — Kalshi Trading Bot
==============================
CFTC-regulated prediction market trading.
Four strategies, $500 seed, $5k/month target.
Daily analyst runs at 00:15 UTC — scores strategies, rewrites config if needed.

Usage:
  python main.py            # Live trading
  python main.py --demo     # Paper trade on demo-api.kalshi.co
  python main.py --dry-run  # Scan only, zero orders placed
  python main.py --analyze  # Run daily analysis immediately and exit
"""

import argparse
import logging
import os
import signal
import sys
import time

import schedule

from logger    import setup_logging
from client    import KalshiClient
from risk      import RiskManager
from monitor   import check_positions
from dashboard import print_dashboard, monthly_summary
from analyst   import run_daily_analysis
import whale as whale_strat
import longshot as longshot_strat
import fade as fade_strat
import bond as bond_strat
from config import (
    WHALE_SCAN_MINS, FADE_SCAN_MINS,
    BOND_SCAN_MINS, LONGSHOT_SCAN_MINS, MONITOR_SCAN_MINS
)

setup_logging()
logger = logging.getLogger("main")

client       = None
risk_manager = None
DRY_RUN      = False
cycle        = 0

# Shared market cache — fetched once per minute, shared across all strategies
_market_cache      = []
_market_cache_time = 0

def get_cached_markets():
    global _market_cache, _market_cache_time
    import time
    now = time.time()
    if now - _market_cache_time > 300:   # refresh every 5 minutes
        try:
            _market_cache      = client.get_all_open_markets()
            _market_cache_time = now
            logger.info(f"[CACHE] Refreshed {len(_market_cache)} markets")
        except Exception as e:
            logger.error(f"[CACHE] Market fetch failed: {e}")
    return _market_cache


def run_whale():
    logger.info("━━━ WHALE CYCLE ━━━")
    c = whale_strat.scan(client, risk_manager)
    if not DRY_RUN: whale_strat.execute(client, risk_manager, c)

def run_fade():
    logger.info("━━━ FADE CYCLE ━━━")
    markets = get_cached_markets()
    c = fade_strat.scan(client, risk_manager, markets)
    if not DRY_RUN: fade_strat.execute(client, risk_manager, c)

def run_bond():
    logger.info("━━━ BOND CYCLE ━━━")
    markets = get_cached_markets()
    c = bond_strat.scan(client, risk_manager, markets)
    if not DRY_RUN: bond_strat.execute(client, risk_manager, c)

def run_longshot():
    logger.info("━━━ LONGSHOT CYCLE ━━━")
    markets = get_cached_markets()
    c = longshot_strat.scan(client, risk_manager, markets)
    if not DRY_RUN: longshot_strat.execute(client, risk_manager, c)

def run_monitor():
    global cycle
    cycle += 1
    check_positions(client, risk_manager)
    print_dashboard(risk_manager, cycle)

def run_analysis():
    """Daily analyst — scores strategies and rebalances config if needed."""
    logger.info("━━━ DAILY ANALYSIS ━━━")
    try:
        scores, new_alloc, changed = run_daily_analysis()
        if changed:
            logger.info(
                "⚠️  Config rebalanced. New allocation: " +
                " | ".join(f"{k}={v:.0%}" for k, v in new_alloc.items())
            )
            logger.info("   Strategies will use new weights on next scan cycle.")
        else:
            logger.info("✅ All strategies performing — no rebalancing needed.")
    except Exception as e:
        logger.error(f"[ANALYST] Analysis failed: {e}", exc_info=True)

def shutdown(signum, frame):
    logger.info("Shutdown signal — final report:")
    print_dashboard(risk_manager, cycle)
    monthly_summary()
    sys.exit(0)


def main():
    global client, risk_manager, DRY_RUN

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",  action="store_true",
                        help="Scan only — no orders placed")
    parser.add_argument("--demo",     action="store_true",
                        help="Use Kalshi demo environment")
    parser.add_argument("--analyze",  action="store_true",
                        help="Run daily analysis immediately and exit")
    args    = parser.parse_args()
    DRY_RUN = args.dry_run

    if args.demo:
        os.environ["USE_DEMO"] = "true"
        import importlib, config
        importlib.reload(config)

    mode = "DRY-RUN" if DRY_RUN else ("DEMO" if args.demo else "LIVE 🔴")
    logger.info(f"🚀 Kalshi Bot | {mode} | Target: $5,000/month")
    logger.info(f"   Base URL: {__import__('config').BASE_URL}")

    # Analysis-only mode (useful for manual inspection)
    if args.analyze:
        run_analysis()
        sys.exit(0)

    client       = KalshiClient()
    risk_manager = RiskManager(client)

    if not DRY_RUN:
        try:
            balance = client.get_balance()
            logger.info(f"✅ Connected | Balance: ${balance:.2f} USD")
            if balance < 10:
                logger.warning("⚠️  Low balance — deposit funds before live trading")
        except Exception as e:
            logger.error(
                f"❌ Connection failed: {e}\n"
                "Check KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY in your .env"
            )
            sys.exit(1)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Schedules ─────────────────────────────────────────────────────────────
    schedule.every(WHALE_SCAN_MINS).minutes.do(run_whale)
    schedule.every(FADE_SCAN_MINS).minutes.do(run_fade)
    schedule.every(BOND_SCAN_MINS).minutes.do(run_bond)
    schedule.every(LONGSHOT_SCAN_MINS).minutes.do(run_longshot)
    schedule.every(MONITOR_SCAN_MINS).minutes.do(run_monitor)

    # Daily analysis at 00:15 UTC — after midnight reset, before first trades
    schedule.every().day.at("00:15").do(run_analysis)
    # Monthly summary at end of day
    schedule.every().day.at("23:55").do(monthly_summary)

    logger.info("Running initial scan on startup...")
    run_whale()
    run_bond()
    run_longshot()
    run_fade()
    run_monitor()

    logger.info("⏱  Main loop active. Daily analysis scheduled for 00:15 UTC.")
    while True:
        schedule.run_pending()
        time.sleep(20)


if __name__ == "__main__":
    main()
