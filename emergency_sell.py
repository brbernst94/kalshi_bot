"""
emergency_sell.py  —  Sell all portfolio positions resolving > MAX_DAYS out.
=============================================================================
Self-contained. No dependencies on any other bot files.

Usage (from your bot folder):
    python emergency_sell.py              # preview — shows what would be sold
    python emergency_sell.py --live       # actually places the sell orders
    python emergency_sell.py --live --max-days 90   # wider net if needed

Requirements:  pip install requests cryptography
Credentials:   reads KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY from env or .env
"""

import argparse, base64, json, os, sys, time
from datetime import datetime, timezone

MAX_DAYS_DEFAULT = 30

# ─── Auth ────────────────────────────────────────────────────────────────────
def load_creds():
    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    pem    = os.environ.get("KALSHI_PRIVATE_KEY", "")
    if not key_id or not pem:
        for path in [".env", "../.env"]:
            if os.path.exists(path):
                for line in open(path):
                    k, _, v = line.strip().partition("=")
                    v = v.strip().strip('"').strip("'")
                    if k == "KALSHI_API_KEY_ID":   key_id = v
                    if k == "KALSHI_PRIVATE_KEY":  pem = v.replace("\\n", "\n")
    if not key_id or not pem:
        sys.exit("❌  Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY (env vars or .env file)")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        return key_id, key
    except ImportError:
        sys.exit("❌  Run: pip install cryptography requests")

def _headers(key_id, private_key, method, path):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ts  = int(datetime.now(timezone.utc).timestamp() * 1000)
    sig = private_key.sign(f"{ts}{method}{path}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY":       key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type":            "application/json",
    }

BASE = "https://api.elections.kalshi.com/trade-api/v2"

def get(key_id, key, path, params=None):
    import requests
    h = _headers(key_id, key, "GET", f"/trade-api/v2{path}")
    r = requests.get(f"{BASE}{path}", headers=h, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def post(key_id, key, path, body):
    import requests
    h = _headers(key_id, key, "POST", f"/trade-api/v2{path}")
    r = requests.post(f"{BASE}{path}", headers=h, data=json.dumps(body), timeout=15)
    r.raise_for_status()
    return r.json()

# ─── Date parsing ─────────────────────────────────────────────────────────────
def days_until(val) -> float:
    """Parse any date/timestamp value and return days from now. 9999 if unparseable."""
    if not val:
        return 9999
    s = str(val).strip()
    # Try ISO format first
    for fmt in [None, "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
        try:
            if fmt is None:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            else:
                dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
        except Exception:
            continue
    # Try unix timestamp (milliseconds or seconds)
    try:
        n = float(s)
        if n > 1e10:  # milliseconds
            n /= 1000
        dt = datetime.fromtimestamp(n, tz=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        pass
    return 9999

def get_resolution_days(mkt: dict) -> float:
    """
    Try every possible field name for the resolution/expiry date.
    Kalshi uses different names across market types.
    """
    for field in ("expiration_time", "close_time", "end_time",
                  "resolution_time", "end_date", "expiry"):
        d = days_until(mkt.get(field))
        if d < 9999:
            return d
    # Nested result object
    result = mkt.get("result", {}) or {}
    for field in ("resolution_time", "close_time"):
        d = days_until(result.get(field))
        if d < 9999:
            return d
    return 9999

def get_price_cents(obj: dict, field: str) -> int:
    """Read price that may be int cents or '$0.XX' string."""
    v = obj.get(field)
    if v is None: return 0
    s = str(v).strip()
    if s.startswith("$"):
        try: return round(float(s[1:]) * 100)
        except: return 0
    try: return int(float(s))
    except: return 0

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live",     action="store_true", help="Place real orders")
    parser.add_argument("--max-days", type=int, default=MAX_DAYS_DEFAULT)
    args = parser.parse_args()

    key_id, private_key = load_creds()
    mode = "🔴 LIVE" if args.live else "🟡 DRY-RUN"

    print(f"\n{'='*60}")
    print(f"  Emergency Position Liquidator  |  {mode}")
    print(f"  Selling everything resolving > {args.max_days} days out")
    print(f"{'='*60}\n")

    # 1. Fetch all positions
    try:
        data      = get(key_id, private_key, "/portfolio/positions")
        positions = data.get("market_positions", [])
    except Exception as e:
        sys.exit(f"❌  Can't fetch positions: {e}")

    active = [(p.get("ticker") or p.get("market_ticker",""),
               int(p.get("net_position", p.get("position", 0)) or 0))
              for p in positions]
    active = [(t, n) for t, n in active if t and n != 0]
    print(f"Found {len(active)} open positions. Checking resolution dates...\n")

    to_sell = []
    for ticker, net in active:
        side = "yes" if net > 0 else "no"
        qty  = abs(net)

        # Fetch market data directly — no cache
        try:
            raw = get(key_id, private_key, f"/markets/{ticker}")
            mkt = raw.get("market", raw)
            time.sleep(0.12)   # gentle rate limiting
        except Exception as e:
            print(f"  ⚠  {ticker}: market fetch failed ({e}) — SKIPPING")
            continue

        days = get_resolution_days(mkt)

        # Debug: show date fields found
        date_info = {f: mkt.get(f) for f in
                     ("expiration_time","close_time","end_time","resolution_time","end_date")
                     if mkt.get(f)}

        if days > args.max_days:
            avg = (get_price_cents(mkt, "yes_bid") if side=="yes"
                   else get_price_cents(mkt, "no_bid")) or 50
            to_sell.append({"ticker": ticker, "side": side, "qty": qty,
                             "days": days, "mkt": mkt, "date_fields": date_info})
            print(f"  🔴 SELL  {ticker:<48} {side} x{qty}  {days:.0f}d  {date_info}")
        else:
            print(f"  ✅ keep  {ticker:<48} {side} x{qty}  {days:.0f}d")

    print(f"\n{'─'*60}")
    print(f"  {len(to_sell)} positions to liquidate  |  "
          f"{len(active)-len(to_sell)} keeping\n")

    if not to_sell:
        print("Nothing to sell.")
        return

    if not args.live:
        print("🟡 DRY-RUN — add --live to place orders\n")
        return

    # 2. Place sell orders
    sold = failed = 0
    for p in to_sell:
        ticker = p["ticker"]
        side   = p["side"]
        qty    = p["qty"]
        mkt    = p["mkt"]

        # Get current bid to price the limit sell
        if side == "yes":
            cur = (get_price_cents(mkt, "yes_bid") or
                   get_price_cents(mkt, "yes_price") or 50)
        else:
            cur = (get_price_cents(mkt, "no_bid") or
                   get_price_cents(mkt, "no_price") or 50)

        # 3¢ below current bid — aggressive enough to fill quickly
        exit_price = max(cur - 3, 1)
        yes_price  = exit_price if side == "yes" else (100 - exit_price)
        no_price   = exit_price if side == "no"  else (100 - exit_price)

        print(f"  SELL {side.upper()} x{qty} @ {exit_price}¢  {ticker}", end="  ")
        sys.stdout.flush()

        try:
            post(key_id, private_key, "/portfolio/orders", {
                "ticker":    ticker,
                "action":    "sell",
                "side":      side,
                "type":      "limit",
                "count":     qty,
                "yes_price": yes_price,
                "no_price":  no_price,
            })
            print(f"✅  ~${qty * exit_price / 100:.2f} recovering")
            sold += 1
        except Exception as e:
            print(f"❌  {e}")
            # Retry at market (1¢ / 99¢)
            fallback_yes = 1 if side == "yes" else 99
            fallback_no  = 1 if side == "no"  else 99
            try:
                post(key_id, private_key, "/portfolio/orders", {
                    "ticker":    ticker,
                    "action":    "sell",
                    "side":      side,
                    "type":      "limit",
                    "count":     qty,
                    "yes_price": fallback_yes,
                    "no_price":  fallback_no,
                })
                print(f"  ↳ retry at 1¢ ✅")
                sold += 1
            except Exception as e2:
                print(f"  ↳ retry failed: {e2}")
                failed += 1

        time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"  ✅ {sold} orders placed  |  ❌ {failed} failed")
    print(f"  Check your portfolio in ~1 minute to confirm fills.")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
