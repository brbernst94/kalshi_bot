"""
utils/risk.py — Kalshi Risk Manager
======================================
Key Kalshi differences vs Polymarket:
  - 1% taker fee eats into every trade — minimum edge is 3.5% net
  - Prices in cents, so edge calculations use integers
  - Contracts are fungible $1 instruments priced in cents
  - No gas fees, no USDC slippage — cleaner cost model
"""

import csv
import logging
import os
from datetime import date, datetime, timezone
from typing import Dict, Optional

from config import (
    KALSHI_TAKER_FEE_PCT, MAX_DAILY_LOSS_PCT,
    MAX_OPEN_POSITIONS, MAX_SINGLE_POSITION_PCT,
    MIN_NET_EDGE, STARTING_BANKROLL_USD, TRADE_LOG_FILE,
)

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, client):
        self.client            = client
        self.daily_pnl         = 0.0   # Always starts at 0 on fresh deploy
        self.daily_date        = date.today()
        self.open_positions    = {}
        self._recently_closed  = set()
        self._init_log()
        logger.info("[RISK] RiskManager initialised — daily PnL reset to $0.00")

    def _reset_daily(self):
        today = date.today()
        if today != self.daily_date:
            logger.info(f"Daily reset | yesterday PnL: ${self.daily_pnl:+.2f}")
            self.daily_pnl  = 0.0
            self.daily_date = today

    def _init_log(self):
        os.makedirs(os.path.dirname(TRADE_LOG_FILE), exist_ok=True)
        if not os.path.exists(TRADE_LOG_FILE):
            with open(TRADE_LOG_FILE, "w", newline="") as f:
                csv.writer(f).writerow([
                    "timestamp", "strategy", "ticker", "side", "action",
                    "price_cents", "count", "cost_usd", "fee_usd",
                    "expected_pnl_usd", "status", "notes"
                ])

    def log_trade(self, strategy: str, ticker: str, side: str, action: str,
                  price_cents: int, count: int, expected_pnl: float,
                  status: str = "PLACED", notes: str = ""):
        cost    = count * price_cents / 100
        fee     = cost * KALSHI_TAKER_FEE_PCT
        ts      = datetime.now(timezone.utc).isoformat()
        with open(TRADE_LOG_FILE, "a", newline="") as f:
            csv.writer(f).writerow([
                ts, strategy, ticker, side, action,
                price_cents, count, round(cost, 2), round(fee, 4),
                round(expected_pnl, 2), status, notes[:80]
            ])
        logger.info(
            f"TRADE | {strategy} | {action.upper()} {side.upper()} "
            f"{count}x @ {price_cents}¢ | cost=${cost:.2f} fee=${fee:.2f} "
            f"| ev=${expected_pnl:.2f} | {ticker}"
        )

    def net_edge(self, gross_edge: float) -> float:
        """Subtract taker fee from gross edge."""
        return gross_edge - KALSHI_TAKER_FEE_PCT

    def sync_positions_from_api(self):
        """Sync open_positions from actual API positions to survive redeployments."""
        try:
            api_positions = self.client.get_positions()

            def _net(p):
                """Extract net contract count from any Kalshi position field variant."""
                for field in ("net_position", "position", "position_fp",
                              "quantity", "contracts", "net_contracts",
                              "total_held", "holdings", "size"):
                    v = p.get(field)
                    if v is not None:
                        try:
                            return int(float(v))
                        except (ValueError, TypeError):
                            pass
                return 0

            api_tickers = {
                p.get("ticker") or p.get("market_ticker", "")
                for p in api_positions
                if _net(p) != 0
            }
            now = datetime.now(timezone.utc)
            stale = [
                t for t in list(self.open_positions)
                if t not in api_tickers
                # Grace period: don't prune positions placed in last 10 min
                # (order may still be settling in Kalshi's API)
                and (now - self.open_positions[t].get("opened_at", now)).total_seconds() > 600
            ]
            for t in stale:
                logger.info(f"[RISK] Pruning stale position {t} (not in API)")
                self.open_positions.pop(t, None)
            # Add any API positions we don't have in memory
            for p in api_positions:
                t = p.get("ticker") or p.get("market_ticker", "")
                net = _net(p)
                if net != 0 and t and t not in self.open_positions and t not in self._recently_closed:
                    self.open_positions[t] = {
                        "count":       abs(net),
                        "entry_cents": 50,
                        "strategy":    "unknown",
                        "side":        "yes" if net > 0 else "no",
                        "net_position": net,
                        "opened_at":   datetime.now(timezone.utc),
                    }
                    logger.info(f"[RISK] Restored position {t} from API (net={net}, side={'yes' if net > 0 else 'no'})")
        except Exception as e:
            logger.debug(f"[RISK] Position sync failed: {e}")

    def approve(self, strategy: str, ticker: str,
                cost_usd: float, gross_edge: float, notes: str = "") -> bool:
        """Gate all trades. Returns True only if all checks pass."""
        self._reset_daily()
        self.sync_positions_from_api()

        net = self.net_edge(gross_edge)
        if net < MIN_NET_EDGE:
            logger.debug(f"REJECT [{strategy}] net edge {net:.3f} < {MIN_NET_EDGE}")
            return False

        try:
            balance = self.client.get_balance()
        except Exception:
            balance = STARTING_BANKROLL_USD

        # Only enforce daily loss if we actually have a negative P&L AND a real balance.
        # Guard: if balance=0 (API field mismatch), 0 <= -(0*20%) = 0<=0 = True would
        # reject every trade on a fresh deploy. Skip the check when balance is 0.
        if balance > 0 and self.daily_pnl < 0 and self.daily_pnl <= -(balance * MAX_DAILY_LOSS_PCT):
            logger.warning(f"REJECT daily loss limit: ${self.daily_pnl:.2f} (limit=${balance * MAX_DAILY_LOSS_PCT:.2f} = {MAX_DAILY_LOSS_PCT:.0%} of ${balance:.2f})")
            return False

        if len(self.open_positions) >= MAX_OPEN_POSITIONS:
            logger.warning(f"REJECT max positions ({MAX_OPEN_POSITIONS})")
            return False

        # Only enforce position size cap when we have a real balance to compare against
        if balance > 0 and cost_usd > balance * MAX_SINGLE_POSITION_PCT:
            logger.warning(f"REJECT size ${cost_usd:.2f} > cap (${balance * MAX_SINGLE_POSITION_PCT:.2f} = {MAX_SINGLE_POSITION_PCT:.0%} of ${balance:.2f})")
            return False

        if ticker in self.open_positions:
            logger.debug(f"REJECT duplicate position {ticker}")
            return False

        logger.info(f"APPROVE [{strategy}] ${cost_usd:.2f} net_edge={net:.3f} | {notes[:50]}")
        return True

    def contracts_for_strategy(self, strategy: str, price_cents: int,
                                edge: float, confidence: float = 0.6) -> int:
        """
        Kelly-inspired contract count.
        Returns number of contracts to buy for this strategy + edge level.
        """
        try:
            balance = self.client.get_balance()
        except Exception:
            balance = STARTING_BANKROLL_USD

        from config import STRATEGY_ALLOCATION
        strat_budget = balance * STRATEGY_ALLOCATION.get(strategy, 0.20)
        # Kelly fraction: (edge × confidence), capped at MAX_SINGLE_POSITION_PCT
        fraction = min(edge * confidence * 1.5, MAX_SINGLE_POSITION_PCT)
        budget   = balance * fraction
        budget   = min(budget, strat_budget * 0.6)

        return max(1, int(budget * 100 / max(price_cents, 1)))

    def record_open(self, ticker: str, count: int,
                    entry_cents: int, strategy: str, side: str = "yes"):
        self.open_positions[ticker] = {
            "count":       count,
            "entry_cents": entry_cents,
            "strategy":    strategy,
            "side":        side,
            "opened_at":   datetime.now(timezone.utc),
        }

    def record_close(self, ticker: str, exit_cents: int) -> float:
        if ticker not in self.open_positions:
            return 0.0
        self._recently_closed.add(ticker)
        pos   = self.open_positions.pop(ticker)
        count = pos["count"]
        # PnL = (exit - entry) × count / 100  (converting cents to dollars)
        pnl   = (exit_cents - pos["entry_cents"]) * count / 100
        # Subtract fees
        pnl  -= count * exit_cents / 100 * KALSHI_TAKER_FEE_PCT
        self.daily_pnl += pnl
        logger.info(f"CLOSED {ticker} | pnl=${pnl:+.2f} | daily=${self.daily_pnl:+.2f}")
        return pnl

    def status(self) -> Dict:
        try:
            balance = self.client.get_balance()
        except Exception:
            balance = 0.0
        return {
            "balance":        balance,
            "daily_pnl":      self.daily_pnl,
            "open_positions": len(self.open_positions),
            "positions":      self.open_positions,
            "daily_date":     str(self.daily_date),
        }
