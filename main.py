"""
main.py — Kalshi Trading Bot
==============================
CFTC-regulated prediction market trading.
Four strategies, $500 seed, $5k/month target.

Usage:
  python main.py            # Live trading
  python main.py --demo     # Paper trade on demo-api.kalshi.co
  python main.py --dry-run  # Scan only, zero orders placed
"""

import argparse
import logging
import os
import signal
import sys
import time

import schedule

from utils.logger    import setup_logging
from utils.client    import KalshiClient
from utils.risk      import RiskManager
from utils.monitor   import check_positions
from utils.dashboard import print_dashboard, monthly_summary
import strategies.whale    as whale_strat
import strategies.longshot as longshot_strat
import strategies.fade     as fade_strat
import strategies.bond     as bond_strat
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


def run_whale():
    logger.info("━━━ WHALE CYCLE ━━━")
    c = whale_strat.scan(client, risk_manager)
    if not DRY_RUN: whale_strat.execute(client, risk_manager, c)

def run_fade():
    logger.info("━━━ FADE CYCLE ━━━")
    c = fade_strat.scan(client, risk_manager)
    if not DRY_RUN: fade_strat.execute(client, risk_manager, c)

def run_bond():
    logger.info("━━━ BOND CYCLE ━━━")
    c = bond_strat.scan(client, risk_manager)
    if not DRY_RUN: bond_strat.execute(client, risk_manager, c)

def run_longshot():
    logger.info("━━━ LONGSHOT CYCLE ━━━")
    c = longshot_strat.scan(client, risk_manager)
    if not DRY_RUN: longshot_strat.execute(client, risk_manager, c)

def run_monitor():
    global cycle
    cycle += 1
    check_positions(client, risk_manager)
    print_dashboard(risk_manager, cycle)

def shutdown(signum, frame):
    logger.info("Shutdown signal — final report:")
    print_dashboard(risk_manager, cycle)
    monthly_summary()
    sys.exit(0)


def main():
    global client, risk_manager, DRY_RUN

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan only — no orders placed")
    parser.add_argument("--demo", action="store_true",
                        help="Use Kalshi demo environment")
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    if args.demo:
        os.environ["USE_DEMO"] = "true"
        # Reload config to pick up USE_DEMO
        import importlib, config
        importlib.reload(config)

    mode = "DRY-RUN" if DRY_RUN else ("DEMO" if args.demo else "LIVE 🔴")
    logger.info(f"🚀 Kalshi Bot | {mode} | Target: $5,000/month")
    logger.info(f"   Base URL: {__import__('config').BASE_URL}")

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

    schedule.every(WHALE_SCAN_MINS).minutes.do(run_whale)
    schedule.every(FADE_SCAN_MINS).minutes.do(run_fade)
    schedule.every(BOND_SCAN_MINS).minutes.do(run_bond)
    schedule.every(LONGSHOT_SCAN_MINS).minutes.do(run_longshot)
    schedule.every(MONITOR_SCAN_MINS).minutes.do(run_monitor)
    schedule.every().day.at("00:05").do(monthly_summary)

    logger.info("Running initial scan on startup...")
    run_whale()
    run_bond()
    run_longshot()
    run_fade()
    run_monitor()

    logger.info("⏱  Main loop active. CTRL-C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(20)


if __name__ == "__main__":
    main()
