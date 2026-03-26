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
from monitor   import check_positions, cleanup_long_dated_positions, liquidate_all_positions
from dashboard import print_dashboard, monthly_summary
from analyst   import run_daily_analysis
import whale as whale_strat
import momentum as momentum_strat
import datarelease as datarelease_strat
import weather as weather_strat
import mentions as mentions_strat
from config import (
    WHALE_SCAN_MINS, MOMENTUM_SCAN_MINS, MONITOR_SCAN_MINS,
    DATARELEASE_SCAN_MINS, WEATHER_SCAN_MINS, MENTIONS_SCAN_MINS,
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
    markets = get_cached_markets()
    c = whale_strat.scan(client, risk_manager, markets)
    if not DRY_RUN: whale_strat.execute(client, risk_manager, c)

def run_momentum():
    logger.info("━━━ MOMENTUM CYCLE ━━━")
    markets = get_cached_markets()
    c = momentum_strat.scan(client, risk_manager, markets)
    if not DRY_RUN: momentum_strat.execute(client, risk_manager, c)

def run_datarelease():
    logger.info("━━━ DATA RELEASE CYCLE ━━━")
    markets = get_cached_markets()
    c = datarelease_strat.scan(client, risk_manager, markets)
    if not DRY_RUN: datarelease_strat.execute(client, risk_manager, c)

def run_weather():
    logger.info("━━━ WEATHER CYCLE ━━━")
    markets = get_cached_markets()
    c = weather_strat.scan(client, risk_manager, markets)
    if not DRY_RUN: weather_strat.execute(client, risk_manager, c)

def run_mentions():
    logger.info("━━━ MENTIONS CYCLE ━━━")
    markets = get_cached_markets()
    c = mentions_strat.scan(client, risk_manager, markets)
    if not DRY_RUN: mentions_strat.execute(client, risk_manager, c)

def run_monitor():
    global cycle
    cycle += 1
    check_positions(client, risk_manager)
    print_dashboard(risk_manager, cycle)

def run_liquidate_all():
    """One-time full liquidation at startup — sell every open position, start fresh."""
    if DRY_RUN:
        logger.info("[LIQUIDATE] DRY-RUN — skipping full liquidation")
        return
    logger.info("━━━ FULL LIQUIDATION ━━━")
    liquidate_all_positions(client, risk_manager)

def run_cleanup():
    """Exit all portfolio positions resolving beyond MAX_POSITION_DAYS.
    Passes the market cache so cleanup uses real resolution dates, not the
    per-session trading-window close_time that Kalshi puts on position objects."""
    if DRY_RUN:
        logger.info("[CLEANUP] DRY-RUN — skipping long-dated position sweep")
        return
    logger.info("━━━ LONG-DATED CLEANUP ━━━")
    markets = get_cached_markets()
    cleanup_long_dated_positions(client, risk_manager, markets)

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
    schedule.every(MOMENTUM_SCAN_MINS).minutes.do(run_momentum)
    schedule.every(DATARELEASE_SCAN_MINS).minutes.do(run_datarelease)
    schedule.every(WEATHER_SCAN_MINS).minutes.do(run_weather)
    schedule.every(MENTIONS_SCAN_MINS).minutes.do(run_mentions)
    schedule.every(MONITOR_SCAN_MINS).minutes.do(run_monitor)
    schedule.every(4).hours.do(run_cleanup)

    schedule.every().day.at("00:15").do(run_analysis)
    schedule.every().day.at("23:55").do(monthly_summary)

    logger.info("Running initial scan on startup...")
    run_liquidate_all()   # sell everything — start fresh
    run_whale()
    run_momentum()
    run_datarelease()
    run_weather()
    run_mentions()
    run_monitor()

    logger.info("⏱  Main loop active. Daily analysis scheduled for 00:15 UTC.")
    while True:
        schedule.run_pending()
        time.sleep(20)


if __name__ == "__main__":
    main()
