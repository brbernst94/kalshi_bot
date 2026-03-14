"""
sell_long_positions.py — Emergency one-shot liquidation of long-dated positions
================================================================================
Run this ONCE from your local machine to immediately exit all portfolio
positions resolving more than MAX_DAYS days from now.

Usage:
    python sell_long_positions.py           # dry-run preview, no orders placed
    python sell_long_positions.py --live    # actually place the sell orders

This script is self-contained. It reads your existing KALSHI_API_KEY_ID and
KALSHI_PRIVATE_KEY env vars (same ones Railway uses).
"""

import argparse
import os
import sys
import time
import re
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
MAX_DAYS = 30          # Sell anything resolving further out than this
SLEEP_BETWEEN = 0.5   # Seconds between orders (stay under rate limit)


# ── Minimal standalone client (no dependency on client.py) ────────────────────
import hmac, hashlib, base64, json
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _load_key(pem_str: str):
    if not HAS_CRYPTO:
        raise RuntimeError("pip install cryptography")
    return serialization.load_pem_private_key(pem_str.encode(), password=None)


def _sign(key_id: str, private_key, method: str, path: str) -> dict:
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    msg = f"{ts}{method.upper()}{path}"
    sig = private_key.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY":       key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type":            "application/json",
    }


def api_get(key_id, private_key, path, params=None):
    if not HAS_REQUESTS:
        raise RuntimeError("pip install requests")
    headers = _sign(key_id, private_key, "GET", f"/trade-api/v2{path}")
    r = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def api_post(key_id, private_key, path, body):
    if not HAS_REQUESTS:
        raise RuntimeError("pip install requests")
    headers = _sign(key_id, private_key, "POST", f"/trade-api/v2{path}")
    r = requests.post(f"{BASE_URL}{path}", headers=headers,
                      data=json.dumps(body), timeout=10)
    r.raise_for_status()
    return r.json()


# ── Helpers ───────────────────────────────────────────────────────────────────

def days_until(iso_str: str) -> float:
    """Return days from now until an ISO timestamp string."""
    if not iso_str:
        return 9999
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        return 9999


def price_cents(obj: dict, field: str) -> int:
    """Read a price field that may be int cents or '$0.XX' dollars string."""
    val = obj.get(field)
    if val is None:
        return 0
    s = str(val).strip()
    if s.startswith("$"):
        try:
            return round(float(s[1:]) * 100)
        except ValueError:
            return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def get_current_price(key_id, private_key, ticker: str, side: str) -> int:
    """Get the current mid price for a ticker."""
    try:
        data = api_get(key_id, private_key, f"/markets/{ticker}")
        mkt = data.get("market", data)
        if side == "yes":
            # Best bid for YES (what we can sell at)
            bid = price_cents(mkt, "yes_bid") or price_cents(mkt, "yes_price")
            return max(bid, 1)
        else:
            bid = price_cents(mkt, "no_bid") or price_cents(mkt, "no_price")
            return max(bid, 1)
    except Exception as e:
        print(f"    ⚠ Price fetch failed for {ticker}: {e} — using 50¢ fallback")
        return 50


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="Actually place orders (default is dry-run)")
    parser.add_argument("--max-days", type=int, default=MAX_DAYS,
                        help=f"Sell positions resolving beyond this many days (default {MAX_DAYS})")
    args = parser.parse_args()
    live = args.live
    max_days = args.max_days

    # ── Auth ──────────────────────────────────────────────────────────────────
    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    pem    = os.environ.get("KALSHI_PRIVATE_KEY", "")

    # Also try loading from .env file if env vars not set
    if not key_id or not pem:
        for env_file in [".env", "../.env"]:
            if os.path.exists(env_file):
                for line in open(env_file):
                    line = line.strip()
                    if line.startswith("KALSHI_API_KEY_ID="):
                        key_id = line.split("=",1)[1].strip().strip('"').strip("'")
                    elif line.startswith("KALSHI_PRIVATE_KEY="):
                        pem = line.split("=",1)[1].strip().strip('"').strip("'")
                        pem = pem.replace("\\n", "\n")

    if not key_id or not pem:
        print("❌ Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY env vars (or put them in .env)")
        sys.exit(1)

    if not HAS_CRYPTO:
        print("❌ Run: pip install cryptography requests")
        sys.exit(1)

    private_key = _load_key(pem)

    mode = "🔴 LIVE" if live else "🟡 DRY-RUN (add --live to place orders)"
    print(f"\n{'='*65}")
    print(f"  Kalshi Long-Dated Position Liquidator  |  {mode}")
    print(f"  Selling all positions resolving > {max_days} days from now")
    print(f"{'='*65}\n")

    # ── Fetch all portfolio positions ─────────────────────────────────────────
    try:
        data = api_get(key_id, private_key, "/portfolio/positions")
        positions = data.get("market_positions", [])
    except Exception as e:
        print(f"❌ Failed to fetch positions: {e}")
        sys.exit(1)

    print(f"Fetched {len(positions)} portfolio positions\n")

    to_sell = []
    skipped = 0

    for pos in positions:
        ticker = pos.get("ticker") or pos.get("market_ticker", "")
        net    = int(pos.get("net_position", pos.get("position", 0)) or 0)
        if net == 0 or not ticker:
            continue

        side = "yes" if net > 0 else "no"
        qty  = abs(net)

        # Get close_time from the position object directly (faster than extra API call)
        close_time = (pos.get("close_time") or
                      pos.get("expiration_time") or
                      pos.get("market_close_time") or "")

        # If not in position object, fetch market data
        if not close_time:
            try:
                mdata = api_get(key_id, private_key, f"/markets/{ticker}")
                mkt = mdata.get("market", mdata)
                close_time = (mkt.get("close_time") or
                              mkt.get("expiration_time") or "")
                time.sleep(0.1)  # gentle rate limit
            except Exception:
                close_time = ""

        days = days_until(close_time)

        if days <= max_days:
            skipped += 1
            continue

        avg_cents = price_cents(pos, "average_price") or price_cents(pos, "yes_price") or 50

        to_sell.append({
            "ticker":     ticker,
            "side":       side,
            "qty":        qty,
            "avg_cents":  avg_cents,
            "days":       days,
            "close_time": close_time[:10] if close_time else "unknown",
        })

    if not to_sell:
        print(f"✅ Nothing to sell — all positions resolve within {max_days} days.")
        print(f"   ({skipped} positions are within the horizon)")
        return

    # ── Preview ───────────────────────────────────────────────────────────────
    print(f"Found {len(to_sell)} positions to liquidate:\n")
    print(f"  {'Ticker':<45} {'Side':<4} {'Qty':>4}  {'Avg':>5}  {'Days':>6}  {'Resolves'}")
    print(f"  {'-'*45} {'-'*4} {'-'*4}  {'-'*5}  {'-'*6}  {'-'*10}")
    for p in sorted(to_sell, key=lambda x: -x['days']):
        print(f"  {p['ticker']:<45} {p['side']:<4} {p['qty']:>4}  {p['avg_cents']:>4}¢  {p['days']:>5.0f}d  {p['close_time']}")

    total_exposure = sum(x['qty'] * x['avg_cents'] / 100 for x in to_sell)
    print(f"\n  Total capital to recover: ~${total_exposure:.2f}")
    print()

    if not live:
        print("🟡 DRY-RUN complete. Run with --live to place the sell orders.\n")
        return

    # ── Place orders ──────────────────────────────────────────────────────────
    print(f"Placing {len(to_sell)} sell orders...\n")
    sold = 0
    failed = 0

    for pos in to_sell:
        ticker = pos["ticker"]
        side   = pos["side"]
        qty    = pos["qty"]

        # Get current bid to price the limit sell
        current = get_current_price(key_id, private_key, ticker, side)
        # Price 2¢ below current bid to ensure fill; minimum 1¢
        exit_price = max(current - 2, 1)

        print(f"  SELL {side.upper()} x{qty} @ {exit_price}¢  ({ticker})", end="  ")

        try:
            api_post(key_id, private_key, "/portfolio/orders", {
                "ticker":    ticker,
                "action":    "sell",
                "side":      side,
                "type":      "limit",
                "count":     qty,
                "yes_price": exit_price if side == "yes" else (100 - exit_price),
                "no_price":  exit_price if side == "no"  else (100 - exit_price),
            })
            est_recovery = qty * exit_price / 100
            print(f"✅  est. recovery ~${est_recovery:.2f}")
            sold += 1
        except Exception as e:
            print(f"❌  {e}")
            # Retry at a more aggressive price (5¢ lower)
            fallback = max(exit_price - 5, 1)
            try:
                api_post(key_id, private_key, "/portfolio/orders", {
                    "ticker":    ticker,
                    "action":    "sell",
                    "side":      side,
                    "type":      "limit",
                    "count":     qty,
                    "yes_price": fallback if side == "yes" else (100 - fallback),
                    "no_price":  fallback if side == "no"  else (100 - fallback),
                })
                print(f"  ↳ retry @ {fallback}¢ ✅")
                sold += 1
            except Exception as e2:
                print(f"  ↳ retry failed: {e2}")
                failed += 1

        time.sleep(SLEEP_BETWEEN)

    print(f"\n{'='*65}")
    print(f"  Done: {sold} orders placed, {failed} failed")
    if sold > 0:
        print(f"  ✅ Capital is being freed up — check your portfolio in ~1 min")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
