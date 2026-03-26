"""
diagnose.py — Kalshi Bot Diagnostic Script
Runs real API calls, logs everything, finds the root cause of zero trades.
Does NOT place any orders.
"""

import logging
import sys
import os

# Set up logging first
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("diagnose")

logger.info("=== KALSHI BOT DIAGNOSTIC ===")

# Step 1: Check env vars
api_key = os.getenv("KALSHI_API_KEY_ID", "")
priv_key = os.getenv("KALSHI_PRIVATE_KEY", "")
logger.info(f"KALSHI_API_KEY_ID present: {bool(api_key)} (len={len(api_key)})")
logger.info(f"KALSHI_PRIVATE_KEY present: {bool(priv_key)} (len={len(priv_key)}, starts_with={priv_key[:30] if priv_key else 'EMPTY'})")

try:
    from client import KalshiClient
    from risk import RiskManager
    import bond
    import datarelease

    # Step 2: Initialize client
    logger.info("\n--- Initializing KalshiClient ---")
    client = KalshiClient()

    # Step 3: Raw balance API response
    logger.info("\n--- RAW Balance API Response ---")
    try:
        raw_balance = client._get("/portfolio/balance")
        logger.info(f"Balance API keys: {list(raw_balance.keys())}")
        logger.info(f"Balance API full response: {raw_balance}")
    except Exception as e:
        logger.error(f"Balance API FAILED: {e}")
        raw_balance = {}

    # Step 4: Parsed balance
    logger.info("\n--- Parsed Balance ---")
    try:
        balance = client.get_balance()
        logger.info(f"get_balance() = ${balance:.2f}")
    except Exception as e:
        logger.error(f"get_balance() FAILED: {e}")
        balance = 0.0

    # Step 5: Fetch all open markets
    logger.info("\n--- Fetching All Open Markets ---")
    try:
        markets = client.get_all_open_markets()
        logger.info(f"Total markets returned: {len(markets)}")
    except Exception as e:
        logger.error(f"get_all_open_markets() FAILED: {e}")
        markets = []

    if markets:
        # Count price data availability
        has_yes_ask = sum(1 for m in markets if m.get("yes_ask") or m.get("yes_ask_dollars"))
        has_last_price = sum(1 for m in markets if m.get("last_price") or m.get("last_price_dollars"))
        logger.info(f"Markets with yes_ask>0: {has_yes_ask}")
        logger.info(f"Markets with last_price>0: {has_last_price}")

        # Count by strategy prefixes
        dr_prefixes = (
            "KXCPI", "KXFED", "KXFEDDECISION", "KXNFP",
            "KXGDPUS", "KXGDPQ", "KXUNRATE", "KXPCE", "KXFOMC",
            "KXJOBLESS", "KXJOBLESSCLAIMS",
            "KXRETAIL", "KXHOUSING", "KXISM",
            "KXPPI", "KXCORECPI",
        )
        weather_prefixes = ("KXHIGH", "KXLOW", "KXPRECIP")

        dr_count = sum(1 for m in markets if m.get("ticker", "").startswith(dr_prefixes))
        weather_count = sum(1 for m in markets if m.get("ticker", "").startswith(weather_prefixes))
        logger.info(f"DATA_RELEASE_PREFIXES matches: {dr_count}")
        logger.info(f"Weather (KXHIGH/KXLOW/KXPRECIP) matches: {weather_count}")

        # Show first 5 markets from DR prefixes
        dr_markets = [m for m in markets if m.get("ticker", "").startswith(dr_prefixes)]
        logger.info(f"\n--- First 5 Data Release Markets (full dict) ---")
        for i, m in enumerate(dr_markets[:5]):
            logger.info(f"DR[{i}]: {m}")

        # Show sample market keys to understand API structure
        logger.info(f"\n--- Sample Market Keys ---")
        sample = markets[0] if markets else {}
        logger.info(f"Sample market keys: {sorted(sample.keys())}")
        logger.info(f"Sample market full dict: {sample}")

        # Show first 5 weather markets
        weather_markets = [m for m in markets if m.get("ticker", "").startswith(weather_prefixes)]
        logger.info(f"\n--- First 5 Weather Markets (full dict) ---")
        for i, m in enumerate(weather_markets[:5]):
            logger.info(f"WEATHER[{i}]: {m}")

    # Step 6: Run bond.scan()
    logger.info("\n--- Running bond.scan() ---")
    risk_manager = RiskManager(client)
    try:
        bond_candidates = bond.scan(client, risk_manager, markets)
        logger.info(f"bond.scan() returned {len(bond_candidates)} candidates")
        for c in bond_candidates[:5]:
            logger.info(f"  BOND: {c}")
    except Exception as e:
        logger.error(f"bond.scan() FAILED: {e}", exc_info=True)
        bond_candidates = []

    # Step 7: Run datarelease.scan()
    logger.info("\n--- Running datarelease.scan() ---")
    try:
        dr_candidates = datarelease.scan(client, risk_manager, markets)
        logger.info(f"datarelease.scan() returned {len(dr_candidates)} candidates")
        for c in dr_candidates[:5]:
            logger.info(f"  DR: {c}")
    except Exception as e:
        logger.error(f"datarelease.scan() FAILED: {e}", exc_info=True)
        dr_candidates = []

    # Step 8: Risk manager approval test for candidates
    all_candidates = bond_candidates + dr_candidates
    logger.info(f"\n--- Risk Manager Approval Test ({len(all_candidates)} total candidates) ---")
    for c in all_candidates[:5]:
        ticker = c.get("ticker", "UNKNOWN")
        cost = c.get("our_price", c.get("yes_price", 50)) / 100  # rough 1-contract cost
        edge = c.get("ev", c.get("gross_return", 0.05))
        would_approve = risk_manager.approve(
            "test", ticker, cost, edge, notes="diagnostic"
        )
        logger.info(f"  approve({ticker}, cost=${cost:.2f}, edge={edge:.3f}) = {would_approve}")
        logger.info(f"    balance={balance:.2f}, positions={len(risk_manager.open_positions)}, daily_pnl={risk_manager.daily_pnl:.2f}")

    # Step 9: Diagnose the root cause
    logger.info("\n=== ROOT CAUSE DIAGNOSIS ===")
    if balance == 0:
        logger.error("ROOT CAUSE A: balance=0 — forces cost > budget, skips all trades")
        logger.error(f"  Balance API raw: {raw_balance}")
    elif not markets:
        logger.error("ROOT CAUSE: No markets returned from API")
    elif dr_count == 0 and weather_count == 0:
        logger.error("ROOT CAUSE E: No data release or weather markets in cache")
    elif not bond_candidates and not dr_candidates:
        logger.error("ROOT CAUSE: bond.scan and datarelease.scan both returned 0 candidates")
        logger.error("Check the filter output above for no_time/no_price/out_of_window counts")
    else:
        logger.info(f"Candidates found: {len(all_candidates)} — checking if risk manager blocks them")

    # Extra: show days_to_close for sample markets
    logger.info("\n--- days_to_close sample (first 10 non-sports markets) ---")
    from bond import days_to_close
    sports_px = ("KXNCAAMB", "KXNCAAFB", "KXNHL", "KXNBA", "KXNFL", "KXMLB", "KXMLS", "KXMVE")
    non_sports = [m for m in markets if not m.get("ticker", "").startswith(sports_px)][:10]
    for m in non_sports:
        ticker = m.get("ticker", "?")
        days = days_to_close(m)
        # Show all time-related fields
        time_fields = {k: v for k, v in m.items() if any(x in k for x in ("time", "date", "expir", "close", "settl"))}
        logger.info(f"  {ticker}: days={days} | time_fields={time_fields}")

    # Extra: check price fields
    logger.info("\n--- Price field check (first 10 non-sports markets) ---")
    from client import price_cents as _pc
    for m in non_sports:
        ticker = m.get("ticker", "?")
        ya = _pc(m, "yes_ask")
        lp = _pc(m, "last_price")
        yb = _pc(m, "yes_bid")
        price_fields = {k: v for k, v in m.items() if any(x in k for x in ("price", "bid", "ask", "last"))}
        logger.info(f"  {ticker}: yes_ask={ya} last_price={lp} yes_bid={yb} | raw_price_fields={price_fields}")

    logger.info("\n=== DIAGNOSTIC COMPLETE ===")

except Exception as e:
    logger.error(f"DIAGNOSTIC FAILED: {e}", exc_info=True)
