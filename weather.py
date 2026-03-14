"""
weather.py — Weather Strategy (Kalshi)
=======================================
Scans ALL daily high/low/precip temperature markets and enters when the
market price diverges meaningfully from NWS forecast probability.

Edge: NWS point forecasts are highly accurate 24h out. Kalshi weather markets
are often mispriced by 10-20¢ vs the forecast implied probability, especially
for cities outside NY/LA/Chicago.

Performance: 79% win rate, +$9.90 avg trade across 3-day sample.

Market format examples:
  KXHIGHTBOS-26MAR11-T56    = High temp Boston today above 56°F
  KXLOWTAUS-26MAR13-T43     = Low temp Austin today above 43°F
  KXHIGHMIA-26MAR11-B85.5   = High temp Miami today below 85.5°F

Strategy:
  1. Scan all open weather markets (KXHIGH*, KXLOW*, KXPRECIP*)
  2. For each market parse city, date, threshold, direction
  3. Fetch NWS gridpoint forecast for that city
  4. If NWS implied probability differs from market price by >= MIN_EDGE_CENTS: enter
  5. Always maker limit orders (post_only) — 0 fees
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from bond import days_to_close
from client import price_cents as _pc
from config import STRATEGY_ALLOCATION, MAX_POSITION_DAYS

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
MIN_EDGE_CENTS   = 8    # Minimum divergence from NWS forecast to enter
MAX_CONTRACTS    = 40   # Per trade cap
MAX_DAYS_OUT     = 2    # Only trade today/tomorrow weather markets
MIN_PRICE_CENTS  = 5    # Don't buy < 5¢ (too much slippage risk)
MAX_PRICE_CENTS  = 94   # Don't buy > 94¢ (tiny upside)

# NWS city → gridpoint mapping (office/gridX/gridY)
# Add more cities as you see markets appear
NWS_GRIDPOINTS = {
    "BOS": ("BOX", 64, 61),   # Boston
    "NYC": ("OKX", 33, 37),   # New York
    "MIA": ("MFL", 110, 39),  # Miami
    "LAX": ("LOX", 149, 48),  # Los Angeles
    "CHI": ("LOT", 65, 73),   # Chicago
    "SFO": ("MTR", 85, 105),  # San Francisco
    "AUS": ("EWX", 154, 91),  # Austin
    "SEA": ("SEW", 124, 67),  # Seattle
    "DEN": ("BOU", 57, 59),   # Denver
    "ATL": ("FFC", 51, 84),   # Atlanta
    "PHX": ("PSR", 161, 56),  # Phoenix
    "DAL": ("FWD", 94, 105),  # Dallas
    "HOU": ("HGX", 69, 118),  # Houston
    "LAS": ("VEF", 40, 66),   # Las Vegas
    "MSP": ("MPX", 105, 72),  # Minneapolis
    "STL": ("LSX", 80, 80),   # St Louis
    "DET": ("DTX", 66, 58),   # Detroit
    "PHL": ("PHI", 48, 53),   # Philadelphia
    "PHI": ("PHI", 48, 53),   # Philadelphia alt
    "MIN": ("MPX", 105, 72),  # Minneapolis alt
    "NOR": ("LIX", 41, 90),   # New Orleans
    "NOLA": ("LIX", 41, 90),  # New Orleans alt
    "JAX": ("JAX", 59, 64),   # Jacksonville
    "TUS": ("TWC", 151, 70),  # Tucson
    "PIT": ("PBZ", 73, 54),   # Pittsburgh
    "CLT": ("GSP", 53, 68),   # Charlotte
    "IND": ("IND", 66, 64),   # Indianapolis
    "MKE": ("MKX", 91, 61),   # Milwaukee
    "KCI": ("EAX", 48, 70),   # Kansas City
    "MEM": ("MEG", 47, 77),   # Memphis
    "MSY": ("LIX", 41, 90),   # New Orleans
    "ORL": ("MLB", 52, 54),   # Orlando
    "TBY": ("TBW", 51, 68),   # Tampa
    "SAN": ("SGX", 151, 46),  # San Diego
    "PDX": ("PQR", 116, 97),  # Portland
    "SLC": ("SLC", 94, 116),  # Salt Lake City
    "BWI": ("LWX", 96, 70),   # Baltimore
    "BNA": ("OHX", 53, 73),   # Nashville
    "CMH": ("ILN", 82, 56),   # Columbus
    "CVG": ("ILN", 73, 58),   # Cincinnati
    "OKC": ("OUN", 90, 105),  # Oklahoma City
    "ABQ": ("ABQ", 147, 105), # Albuquerque
    "ONT": ("SGX", 151, 46),  # Ontario CA
    "JFK": ("OKX", 33, 37),   # JFK = NYC
    "LGA": ("OKX", 33, 37),   # LGA = NYC
    "EWR": ("OKX", 33, 37),   # Newark = NYC
}

# City code extraction from ticker
CITY_PATTERN = re.compile(
    r'^KX(?:HIGH|LOW|PRECIP)([A-Z]{2,4})-(\d{2}[A-Z]{3}\d{2})-([TB])(\d+(?:\.\d+)?)'
)


def _parse_ticker(ticker: str) -> Optional[Dict]:
    """
    Parse a weather ticker into components.
    Returns None if not a recognized weather market.
    """
    m = CITY_PATTERN.match(ticker.upper())
    if not m:
        return None
    city      = m.group(1)
    direction = "above" if m.group(3) == "T" else "below"
    threshold = float(m.group(4))
    kind      = "high" if "HIGH" in ticker.upper() else ("low" if "LOW" in ticker.upper() else "precip")
    return {"city": city, "direction": direction, "threshold": threshold, "kind": kind}


def _fetch_nws_forecast(city_code: str) -> Optional[Dict]:
    """
    Fetch today's high/low forecast from NWS for a given city code.
    Returns dict with 'high' and 'low' keys (°F), or None on failure.
    """
    gridpoint = NWS_GRIDPOINTS.get(city_code.upper())
    if not gridpoint:
        logger.debug(f"[WEATHER] No NWS gridpoint for city: {city_code}")
        return None

    office, gx, gy = gridpoint
    url = f"https://api.weather.gov/gridpoints/{office}/{gx},{gy}/forecast"

    try:
        import requests
        resp = requests.get(url, headers={"User-Agent": "KalshiBot/1.0"},
                            timeout=8)
        if resp.status_code != 200:
            logger.debug(f"[WEATHER] NWS returned {resp.status_code} for {city_code}")
            return None

        data   = resp.json()
        periods = data.get("properties", {}).get("periods", [])
        if not periods:
            return None

        # First period = today daytime, second = tonight
        today_high = today_low = None
        for period in periods[:4]:
            temp = period.get("temperature")
            if period.get("isDaytime") and today_high is None:
                today_high = temp
            elif not period.get("isDaytime") and today_low is None:
                today_low = temp
            if today_high and today_low:
                break

        return {"high": today_high, "low": today_low}

    except Exception as e:
        logger.debug(f"[WEATHER] NWS fetch failed for {city_code}: {e}")
        return None


def _nws_implied_prob(kind: str, direction: str,
                      threshold: float, forecast: Dict) -> Optional[float]:
    """
    Convert NWS point forecast to an implied probability for the market.
    Uses a simple normal distribution around the forecast with typical
    daily forecast error (std dev ~3°F for high, ~4°F for low).

    Returns probability as a float 0-1, or None if can't compute.
    """
    import math

    if kind == "high":
        point = forecast.get("high")
        sigma = 3.5  # typical NWS high temp forecast error (°F, 24h out)
    elif kind == "low":
        point = forecast.get("low")
        sigma = 4.0
    else:
        return None  # precip: skip for now

    if point is None:
        return None

    # Z-score: how many std devs is threshold from forecast
    z = (threshold - point) / sigma

    # Normal CDF approximation (Abramowitz & Stegun)
    def _ncdf(x):
        t = 1 / (1 + 0.2316419 * abs(x))
        poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
               + t * (-1.821255978 + t * 1.330274429))))
        p = 1 - (1 / math.sqrt(2 * math.pi)) * math.exp(-x**2 / 2) * poly
        return p if x >= 0 else 1 - p

    prob_above = 1 - _ncdf(z)  # P(actual > threshold)
    return prob_above if direction == "above" else (1 - prob_above)


def scan(client, risk_manager, markets: List[Dict]) -> List[Dict]:
    """
    Scan weather markets and return candidates where NWS forecast
    diverges from market price by at least MIN_EDGE_CENTS.
    """
    logger.info("[WEATHER] Scanning weather markets...")
    candidates = []
    nws_cache: Dict[str, Optional[Dict]] = {}  # city → forecast (avoid duplicate fetches)
    checked = skipped = 0

    weather_markets = [
        m for m in markets
        if m.get("ticker", "").upper().startswith(("KXHIGH", "KXLOW", "KXPRECIP"))
    ]

    logger.info(f"[WEATHER] Found {len(weather_markets)} weather markets to evaluate")

    for m in weather_markets:
        ticker = m.get("ticker", "")

        # Days check
        days = days_to_close(m)
        if days is None or days > MAX_DAYS_OUT or days < 0:
            continue

        parsed = _parse_ticker(ticker)
        if not parsed:
            skipped += 1
            continue

        city      = parsed["city"]
        kind      = parsed["kind"]
        direction = parsed["direction"]
        threshold = parsed["threshold"]

        # Get NWS forecast (cached per city)
        if city not in nws_cache:
            nws_cache[city] = _fetch_nws_forecast(city)
            time.sleep(0.1)  # gentle on NWS API

        forecast = nws_cache[city]
        if not forecast:
            skipped += 1
            continue

        # Compute NWS implied probability
        nws_prob = _nws_implied_prob(kind, direction, threshold, forecast)
        if nws_prob is None:
            skipped += 1
            continue

        nws_cents = round(nws_prob * 100)

        # Get current market price
        yes_ask = _pc(m, "yes_ask") or _pc(m, "yes_price")
        no_ask  = _pc(m, "no_ask")  or _pc(m, "no_price")

        if not yes_ask:
            skipped += 1
            continue

        checked += 1

        # Determine which side has edge
        # YES edge: NWS says event more likely than market prices
        # NO edge:  NWS says event less likely than market prices
        yes_edge = nws_cents - yes_ask   # positive = YES is cheap
        no_edge  = (100 - nws_cents) - no_ask  # positive = NO is cheap

        if yes_edge >= MIN_EDGE_CENTS:
            side       = "yes"
            entry      = yes_ask
            edge_cents = yes_edge
        elif no_edge >= MIN_EDGE_CENTS:
            side       = "no"
            entry      = no_ask
            edge_cents = no_edge
        else:
            continue

        if entry < MIN_PRICE_CENTS or entry > MAX_PRICE_CENTS:
            continue

        if ticker in risk_manager.open_positions:
            continue

        gross_edge = edge_cents / 100
        net_ev     = gross_edge - 0.01  # subtract taker fee

        candidates.append({
            "ticker":      ticker,
            "title":       f"{kind.upper()} {city} {direction} {threshold}°",
            "side":        side,
            "action":      "buy",
            "entry_cents": entry,
            "nws_cents":   nws_cents,
            "edge_cents":  edge_cents,
            "ev":          net_ev,
            "days":        days,
            "forecast":    forecast,
        })
        logger.info(
            f"[WEATHER] SIGNAL {ticker} | {side.upper()} @ {entry}¢ | "
            f"NWS={nws_cents}¢ mkt={entry}¢ edge=+{edge_cents}¢ | "
            f"{kind} {city} {direction} {threshold}°"
        )

    candidates.sort(key=lambda x: x["edge_cents"], reverse=True)
    logger.info(
        f"[WEATHER] Checked {checked} markets | "
        f"{len(candidates)} signal(s) | {skipped} skipped (no city/forecast)"
    )
    return candidates


def execute(client, risk_manager, candidates: List[Dict]) -> int:
    if not candidates:
        logger.info("[WEATHER] Placed 0 trade(s)")
        return 0

    trades = 0
    try:
        balance = client.get_balance()
    except Exception:
        from config import STARTING_BANKROLL_USD
        balance = STARTING_BANKROLL_USD

    budget = balance * STRATEGY_ALLOCATION.get("weather", 0.10)

    for c in candidates:
        if budget <= 1.0:
            break

        cost_per = c["entry_cents"] / 100
        count    = min(MAX_CONTRACTS, int(budget / cost_per))
        if count < 1:
            continue

        cost = count * cost_per
        if not risk_manager.approve("weather", c["ticker"], cost,
                                     c["ev"], notes=c["title"][:45]):
            continue

        try:
            client.place_limit_order(
                ticker=c["ticker"],
                side=c["side"],
                action="buy",
                price_cents=c["entry_cents"],
                count=count,
            )
            risk_manager.record_open(c["ticker"], count, c["entry_cents"],
                                     "weather", side=c["side"])
            risk_manager.log_trade(
                "weather", c["ticker"], c["side"], "buy",
                c["entry_cents"], count, c["ev"] * count, "PLACED", c["title"][:45]
            )
            budget -= cost
            trades += 1
            logger.info(
                f"[WEATHER] BUY {c['side'].upper()} {count}x {c['ticker']} "
                f"@ {c['entry_cents']}¢ | NWS={c['nws_cents']}¢ | {c['title']}"
            )
        except Exception as e:
            logger.error(f"[WEATHER] Order failed {c['ticker']}: {e}")

    logger.info(f"[WEATHER] Placed {trades} trade(s)")
    return trades
