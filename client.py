"""
utils/client.py — Kalshi API Client
=====================================
Handles RSA-PSS request signing, token management, and all API calls.

Auth flow (v2):
  1. Load RSA private key (PEM file or env string)
  2. For every request: sign  f"{timestamp_ms}{METHOD}{path_no_query}"
  3. Send headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP

Price note: Kalshi prices are in CENTS (int 1–99).
  - yes_price=45 means 45¢ per contract
  - Buying 100 contracts at 45¢ costs $45.00
  - If YES resolves, you receive $100.00 (100 contracts × $1.00)
"""

import base64
import datetime
import json
import logging
import os
import time
import uuid
from io import StringIO
from typing import Any, Dict, List, Optional

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import BASE_URL, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY

logger = logging.getLogger(__name__)


def price_cents(d: dict, field: str) -> Optional[int]:
    """
    Read a price field from a Kalshi market dict, handling both formats:
      - New (March 2026+):  field_dollars = "0.8500"  → returns 85
      - Legacy (pre-March 2026): field = 85 (integer cents) → returns 85

    Usage:
        yes_ask = price_cents(market, "yes_ask")   # tries yes_ask_dollars first
        last_px = price_cents(market, "last_price") # tries last_price_dollars first
    """
    # Try new _dollars field first (Kalshi fixed-point migration, live March 12 2026)
    v = d.get(f"{field}_dollars")
    if v is not None:
        try:
            c = int(round(float(v) * 100))
            if 1 <= c <= 99:
                return c
        except (TypeError, ValueError):
            pass

    # Fall back to legacy integer cents field
    v = d.get(field)
    if v is not None:
        try:
            c = int(round(float(v)))
            if 1 <= c <= 99:
                return c
        except (TypeError, ValueError):
            pass

    return None


def _load_private_key():
    """
    Load RSA private key — robust against Railway env var formatting.
    Handles:
      - PKCS#8  (-----BEGIN PRIVATE KEY-----)
      - PKCS#1  (-----BEGIN RSA PRIVATE KEY-----)
      - Missing newline after header (Railway squashes it)
      - Literal \\n escape sequences instead of real newlines
    """
    import re as _re, base64 as _b64

    key_data = KALSHI_PRIVATE_KEY.strip()
    if not key_data:
        raise ValueError("KALSHI_PRIVATE_KEY not set in environment variables.")

    # File path shortcut
    if not key_data.startswith("-----") and os.path.exists(key_data):
        with open(key_data, "rb") as f:
            return serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )

    # --- Normalise the PEM string ---
    # 1. Replace literal \n escape sequences with real newlines
    pem = key_data.replace("\\n", "\n")
    # 2. Ensure newline immediately after -----BEGIN ...----- header
    pem = _re.sub(r"(-----BEGIN [A-Z ]+-----)\s*(\S)", r"\1\n\2", pem)
    # 3. Ensure newline immediately before -----END ...----- footer
    pem = _re.sub(r"(\S)\s*(-----END [A-Z ]+-----)", r"\1\n\2", pem)
    # 4. Ensure trailing newline
    pem = pem.strip() + "\n"

    key_bytes = pem.encode("utf-8")

    # Try standard PEM load (works for both PKCS#8 and PKCS#1)
    try:
        return serialization.load_pem_private_key(
            key_bytes, password=None, backend=default_backend()
        )
    except Exception as e:
        logger.error(f"PEM load failed ({e}) — attempting DER fallback")

    # DER fallback: strip headers, decode base64, load raw DER
    try:
        b64 = _re.sub(r"-----[^-]+-----", "", pem).replace("\n", "").strip()
        der = _b64.b64decode(b64)
        from cryptography.hazmat.primitives.serialization import load_der_private_key
        return load_der_private_key(der, password=None, backend=default_backend())
    except Exception as e2:
        raise ValueError(
            f"Could not load private key. PEM error: {e} | DER error: {e2}\n"
            "Check that KALSHI_PRIVATE_KEY in Railway contains the full key "
            "including -----BEGIN RSA PRIVATE KEY----- and -----END RSA PRIVATE KEY----- lines."
        )


def _build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=4, backoff_factor=0.6,
                    status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


class KalshiClient:
    """
    Unified Kalshi REST client.

    All price parameters follow Kalshi conventions:
      - yes_price / no_price: integer cents (1–99)
      - count: number of contracts (integer)

    Helper methods convert to/from cents as needed.
    """

    def __init__(self):
        self.base_url    = BASE_URL
        self.session     = _build_session()
        self.private_key = _load_private_key()
        self.api_key_id  = KALSHI_API_KEY_ID
        logger.info(f"KalshiClient initialised | base={self.base_url}")

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _sign(self, method: str, path: str) -> Dict[str, str]:
        """Return signed auth headers for a request."""
        ts_ms  = str(int(datetime.datetime.now().timestamp() * 1000))
        # Kalshi requires signing the FULL path including /trade-api/v2 prefix
        path_no_query = path.split("?")[0]
        if not path_no_query.startswith("/trade-api"):
            sign_path = "/trade-api/v2" + path_no_query
        else:
            sign_path = path_no_query
        message = f"{ts_ms}{method.upper()}{sign_path}".encode("utf-8")
        logger.debug(f"SIGNING | ts={ts_ms} | msg={method.upper()}{sign_path}")

        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode("utf-8")
        return {
            "KALSHI-ACCESS-KEY":       self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "Content-Type":            "application/json",
        }

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        headers = self._sign("GET", path)
        resp    = self.session.get(
            f"{self.base_url}{path}", headers=headers, params=params, timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: Dict) -> Any:
        headers = self._sign("POST", path)
        resp    = self.session.post(
            f"{self.base_url}{path}", headers=headers,
            data=json.dumps(body), timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> Any:
        headers = self._sign("DELETE", path)
        resp    = self.session.delete(
            f"{self.base_url}{path}", headers=headers, timeout=10
        )
        resp.raise_for_status()
        return resp.json()

    # ── Account ───────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Returns total portfolio value: cash + value of open positions."""
        data = self._get("/portfolio/balance")
        # balance field is in cents (integer) — not affected by fixed-point migration
        cash = round(data.get("balance", 0) / 100, 2)

        try:
            pos_data = self._get("/portfolio/positions")
            positions = pos_data.get("market_positions", [])
            position_value = 0.0
            for p in positions:
                # Try _dollars field first (March 2026 migration), then integer cents
                v_dollars = p.get("value_dollars") or p.get("market_value_dollars")
                if v_dollars is not None:
                    try:
                        position_value += abs(float(v_dollars))
                        continue
                    except (TypeError, ValueError):
                        pass
                v = p.get("value") or p.get("market_value") or 0
                position_value += abs(int(v)) / 100
        except Exception:
            position_value = 0.0

        return round(cash + position_value, 2)

    def get_cash(self) -> float:
        """Returns spendable cash only (excludes value locked in positions)."""
        data = self._get("/portfolio/balance")
        return round(data.get("balance", 0) / 100, 2)

    def get_positions(self) -> List[Dict]:
        data = self._get("/portfolio/positions")
        return data.get("market_positions", [])

    def get_open_orders(self, ticker: Optional[str] = None) -> List[Dict]:
        params = {"status": "resting"}
        if ticker:
            params["ticker"] = ticker
        data = self._get("/portfolio/orders", params=params)
        return data.get("orders", [])

    # ── Markets ───────────────────────────────────────────────────────────────

    def get_markets(self, limit: int = 200, cursor: Optional[str] = None,
                    status: str = "open", event_ticker: Optional[str] = None) -> Dict:
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        return self._get("/markets", params=params)

    def get_events(self, limit: int = 200, cursor: Optional[str] = None,
                   status: str = "open") -> Dict:
        """Fetch Kalshi events. Each event contains multiple related markets."""
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        return self._get("/events", params=params)

    def get_all_open_markets(self) -> List[Dict]:
        """
        Fetch all open markets via the /events endpoint.
        Events are the correct grouping layer — each event holds ~1-10 markets.
        Skips KXMVE sports parlay events.
        """
        markets_by_ticker: dict = {}
        kxmve_skipped = 0

        # Step 1: Collect all events (just 2-3 API calls for ~400 events)
        all_events = []
        cursor = None
        for page in range(10):
            try:
                resp   = self.get_events(limit=200, cursor=cursor)
                events = resp.get("events", [])
                if not events:
                    break
                all_events.extend(events)
                cursor = resp.get("cursor")
                if not cursor:
                    break
                time.sleep(0.1)
            except Exception as e:
                logger.error(f"[CLIENT] Events page {page} failed: {e}")
                break

        logger.info(f"[CLIENT] {len(all_events)} total events fetched")

        # Step 2: Filter out KXMVE, extract any embedded markets
        # Split into sports vs non-sports so non-sports always get fetched
        SPORTS_EVENT_PREFIXES = (
            "KXNCAAMB", "KXNCAAFB", "KXNCAAWB", "KXUCLGAME",
            "KXWTAMATCH", "KXATPMATCH", "KXWTACHALLENGER", "KXATPCHALLENGERMATCH",
            "KXNHL", "KXNBA", "KXNFL", "KXMLB", "KXMLS",
            "KXARGPREM", "KXWBC", "KXLIGUE", "KXBUNDES", "KXSERIE",
        )
        sports_events   = []
        nonsports_events = []
        for event in all_events:
            eticker = event.get("event_ticker", event.get("ticker", ""))
            if eticker.startswith("KXMVE"):
                kxmve_skipped += 1
                continue
            if eticker.startswith(SPORTS_EVENT_PREFIXES):
                sports_events.append(event)
            else:
                nonsports_events.append(event)
            for m in event.get("markets", []):
                t = m.get("ticker", "")
                if t:
                    markets_by_ticker[t] = m

        good_events = nonsports_events + sports_events  # non-sports prioritised
        logger.info(f"[CLIENT] {len(nonsports_events)} non-sports + {len(sports_events)} sports events | {kxmve_skipped} KXMVE skipped | {len(markets_by_ticker)} embedded markets")

        # Step 3: Fetch per-event — always prioritise non-sports events
        # This ensures bond/longshot/fade see financial/political markets even during March Madness
        if not markets_by_ticker and good_events:
            # Non-sports: take up to 200 sorted by volume
            nonsports_events.sort(key=lambda e: int(e.get("volume", 0) or 0), reverse=True)
            sports_events.sort(key=lambda e: int(e.get("volume", 0) or 0), reverse=True)
            fetch_events = nonsports_events[:200] + sports_events[:100]
            logger.info(f"[CLIENT] Fetching per event: {len(nonsports_events[:200])} non-sports + {len(sports_events[:100])} sports")
            for event in fetch_events:
                eticker = event.get("event_ticker", event.get("ticker", ""))
                try:
                    mresp = self.get_markets(limit=100, event_ticker=eticker)
                    for m in mresp.get("markets", []):
                        t = m.get("ticker", "")
                        if t:
                            markets_by_ticker[t] = m
                except Exception:
                    continue

        # Step 4: Final fallback — paginate /markets directly (no KXMVE filter here —
        # there are 10k+ KXMVE markets sorted first; bond/longshot criteria exclude them naturally)
        if not markets_by_ticker:
            logger.info("[CLIENT] Per-event fallback returned 0 — paginating /markets directly")
            cursor = None
            for page in range(20):
                try:
                    resp = self.get_markets(limit=200, cursor=cursor, status="open")
                    page_markets = resp.get("markets", [])
                    if not page_markets:
                        break
                    added = 0
                    for m in page_markets:
                        t = m.get("ticker", "")
                        if t:  # API already filtered status=open, trust it
                            markets_by_ticker[t] = m
                            added += 1
                    logger.info(f"[CLIENT] Direct page {page+1}: {added} markets added (total {len(markets_by_ticker)})")
                    cursor = resp.get("cursor")
                    if not cursor or len(markets_by_ticker) >= 2000:
                        break
                    time.sleep(0.1)
                except Exception as e:
                    logger.error(f"[CLIENT] Direct pagination failed (page {page}): {e}")
                    break

        result = list(markets_by_ticker.values())
        logger.info(
            f"[CLIENT] get_all_open_markets → {len(result)} open markets "
            f"from {len(good_events)} events ({kxmve_skipped} KXMVE skipped)"
        )
        return result
    def get_market(self, ticker: str) -> Dict:
        return self._get(f"/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int = 10) -> Dict:
        return self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    def get_market_history(self, ticker: str,
                           start_ts: Optional[int] = None) -> List[Dict]:
        """Price history for a market ticker."""
        params = {}
        if start_ts:
            params["min_ts"] = start_ts
        data = self._get(f"/markets/{ticker}/history", params=params)
        return data.get("history", [])

    def get_trades(self, ticker: str, limit: int = 50) -> List[Dict]:
        """Recent fills for a given market."""
        data = self._get("/markets/trades",
                          params={"ticker": ticker, "limit": limit})
        return data.get("trades", [])

    # ── Orderbook helpers ─────────────────────────────────────────────────────

    def get_best_bid_ask(self, ticker: str) -> tuple:
        """
        Returns (best_bid_cents, best_ask_cents) for YES side.
        Returns (None, None) if no liquidity.
        Handles both legacy integer cents and new _dollars string format (March 2026).
        """
        raw = self.get_orderbook(ticker, depth=5)
        book = raw.get("orderbook", raw)
        yes_bids = book.get("yes", [])
        no_bids  = book.get("no",  [])

        def _level_cents(level):
            p = level[0] if isinstance(level, (list, tuple)) else level
            if isinstance(p, str):
                try:
                    return int(round(float(p) * 100))
                except ValueError:
                    return None
            try:
                v = int(p)
                return v if 1 <= v <= 99 else None
            except (TypeError, ValueError):
                return None

        best_bid = _level_cents(yes_bids[0]) if yes_bids else None
        no_best  = _level_cents(no_bids[0])  if no_bids  else None
        best_ask = (100 - no_best) if no_best is not None else None
        return best_bid, best_ask

    def get_mid_price_cents(self, ticker: str) -> Optional[int]:
        bid, ask = self.get_best_bid_ask(ticker)
        if bid is None or ask is None:
            return None
        return (bid + ask) // 2

    def get_spread_cents(self, ticker: str) -> Optional[int]:
        bid, ask = self.get_best_bid_ask(ticker)
        if bid is None or ask is None:
            return None
        return ask - bid

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_limit_order(self, ticker: str, side: str, action: str,
                          price_cents: int, count: int,
                          post_only: bool = True) -> Dict:
        """
        Place a limit order. post_only=True by default → maker order = ZERO FEES.
        Research across 300k contracts: makers lose 10%, takers lose 32%.
        Always use post_only unless speed is critical.
        side:   'yes' or 'no'
        action: 'buy' or 'sell'
        price_cents: integer 1–99
        count: number of contracts
        """
        price_cents = max(1, min(99, int(price_cents)))
        price_dollars_str = f"{price_cents / 100:.4f}"
        count_int = max(1, int(count))
        body = {
            "ticker":           ticker,
            "side":             side.lower(),
            "action":           action.lower(),
            "type":             "limit",
            # Send both count formats (Kalshi fixed-point migration March 2026)
            "count":            count_int,
            "count_fp":         f"{count_int:.2f}",
            "client_order_id":  str(uuid.uuid4())[:16],
            "post_only":        post_only,
        }
        if side.lower() == "yes":
            body["yes_price"]         = price_cents        # legacy
            body["yes_price_dollars"] = price_dollars_str  # new
        else:
            body["no_price"]          = price_cents        # legacy
            body["no_price_dollars"]  = price_dollars_str  # new

        result = self._post("/portfolio/orders", body)
        order  = result.get("order", result)
        order_type = "MAKER" if post_only else "TAKER"
        logger.info(
            f"ORDER | {order_type} | {action.upper()} {side.upper()} {count}x @ {price_cents}¢ "
            f"| {ticker} | id={order.get('order_id','?')[:12]}"
        )
        return order

    def cancel_order(self, order_id: str) -> Dict:
        return self._delete(f"/portfolio/orders/{order_id}")

    def cancel_all_orders(self, ticker: Optional[str] = None):
        orders = self.get_open_orders(ticker)
        cancelled = 0
        for o in orders:
            try:
                self.cancel_order(o["order_id"])
                cancelled += 1
            except Exception as e:
                logger.warning(f"Cancel failed {o.get('order_id','?')}: {e}")
        logger.info(f"Cancelled {cancelled} order(s)")
        return cancelled

    # ── Convenience price converters ──────────────────────────────────────────

    @staticmethod
    def cents_to_float(cents: int) -> float:
        """Convert Kalshi cents (45) to float prob (0.45)."""
        return round(cents / 100, 4)

    @staticmethod
    def float_to_cents(prob: float) -> int:
        """Convert float prob (0.45) to Kalshi cents (45)."""
        return max(1, min(99, round(prob * 100)))

    @staticmethod
    def cost_usd(count: int, price_cents: int) -> float:
        """Total cost in USD for a YES buy order."""
        return round(count * price_cents / 100, 2)

    @staticmethod
    def contracts_for_budget(budget_usd: float, price_cents: int) -> int:
        """How many contracts can we buy with this budget?"""
        if price_cents <= 0:
            return 0
        return max(1, int(budget_usd * 100 / price_cents))
