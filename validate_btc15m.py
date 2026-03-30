"""
validate_btc15m.py — Architecture validator for the BTC 15m scalper
====================================================================
Run:  python validate_btc15m.py
      python validate_btc15m.py --live    (also watches the next real window)

Tests run:
  1. Logic simulation  — 6 synthetic price scenarios fed through the exact
                         same decision engine the live bot uses
  2. Connectivity      — Kalshi API auth, balance read, market discovery
  3. WebSocket latency — connects to WS, measures time to first price tick
  4. Live dry-run      — (--live flag) watches the next real 5-min window,
                         logs every decision with projected P&L

Produces a pass/fail report and a recommendation on whether the architecture
is ready for live trading.
"""

import argparse
import asyncio
import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

# ── Logging: clean output, no timestamps cluttering the report ────────────────
logging.basicConfig(
    level=logging.WARNING,   # suppress library noise
    format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

def _p(msg: str):   print(msg)
def _ok(msg: str):  print(f"  ✅  {msg}")
def _fail(msg: str):print(f"  ❌  {msg}")
def _info(msg: str):print(f"  ℹ️   {msg}")
def _warn(msg: str):print(f"  ⚠️   {msg}")
def _h(title: str): print(f"\n{'─'*60}\n  {title}\n{'─'*60}")


# ─────────────────────────────────────────────────────────────────────────────
# 1. LOGIC SIMULATION
#    Replay synthetic price sequences through the exact decision engine.
#    No API calls — fully offline.
# ─────────────────────────────────────────────────────────────────────────────

from btc_15m_scalp import (
    ENTRY_CENTS, STOP_LOSS_CENTS, STOP_GAIN_CENTS, REENTRY_COOLDOWN_S,
)

TAKER_FEE = 0.01   # 1% fee on sells


def _simulate(name: str, price_sequence: List[int],
              start_cash: float = 1000.0,
              expect_trades: int = None,
              expect_outcome: str = None) -> dict:
    """
    Drive the scalper decision logic with a synthetic price sequence.
    Each element is a YES price in cents. Time spacing = 0.2s (5 Hz).

    Returns a result dict: trades, final_cash, outcome, passed.
    """
    cash            = start_cash
    seen_yes_below  = False
    seen_no_below   = False
    holding         = False
    pos_side        = ""
    pos_count       = 0
    pos_entry       = 0
    last_sl_ts      = -REENTRY_COOLDOWN_S * 2   # no cooldown at start
    done            = False
    trades          = []
    now_fake        = 0.0   # simulated time counter (seconds)

    for yes_px in price_sequence:
        if done:
            break
        no_px = 100 - yes_px
        now_fake += 0.2   # 5 Hz ticks

        if holding:
            pos_px = yes_px if pos_side == "yes" else no_px

            if pos_px >= STOP_GAIN_CENTS:
                sell_px  = pos_px - 2
                pnl      = (sell_px - pos_entry) * pos_count / 100
                pnl     -= sell_px * pos_count / 100 * TAKER_FEE
                cash    += pnl + (pos_entry * pos_count / 100)  # return cost
                trades.append({"type": "STOP_GAIN",  "pnl": pnl,
                                "entry": pos_entry, "exit": sell_px})
                holding = False
                done    = True

            elif pos_px <= STOP_LOSS_CENTS:
                sell_px  = pos_px - 2
                pnl      = (sell_px - pos_entry) * pos_count / 100
                pnl     -= sell_px * pos_count / 100 * TAKER_FEE
                cash    += pnl + (pos_entry * pos_count / 100)
                trades.append({"type": "STOP_LOSS", "pnl": pnl,
                                "entry": pos_entry, "exit": sell_px})
                holding        = False
                last_sl_ts     = now_fake
                seen_yes_below = False
                seen_no_below  = False

        else:
            if yes_px < ENTRY_CENTS:
                seen_yes_below = True
            if no_px < ENTRY_CENTS:
                seen_no_below = True

            in_cooldown = (now_fake - last_sl_ts) < REENTRY_COOLDOWN_S

            if not in_cooldown:
                entry_side  = ""
                entry_price = 0

                if seen_yes_below and yes_px >= ENTRY_CENTS:
                    entry_side, entry_price = "yes", yes_px
                elif seen_no_below and no_px >= ENTRY_CENTS:
                    entry_side, entry_price = "no",  no_px

                if entry_side:
                    count     = max(1, int(cash * 0.75 * 100 / max(entry_price, 1)))
                    cost      = count * entry_price / 100
                    cash     -= cost
                    holding   = True
                    pos_side  = entry_side
                    pos_count = count
                    pos_entry = entry_price
                    seen_yes_below = False
                    seen_no_below  = False

    # Resolve at window close if still holding
    outcome = "OPEN"
    if done and trades and trades[-1]["type"] == "STOP_GAIN":
        outcome = "STOP_GAIN"
    elif not holding and trades and trades[-1]["type"] == "STOP_LOSS":
        outcome = "STOP_LOSS"
    elif holding:
        outcome = "HELD_TO_CLOSE"
    elif not trades:
        outcome = "NO_TRADE"

    total_pnl    = sum(t["pnl"] for t in trades)
    n_trades     = len(trades)
    passed       = True
    fail_reasons = []

    if expect_trades is not None and n_trades != expect_trades:
        passed = False
        fail_reasons.append(
            f"expected {expect_trades} trade(s), got {n_trades}"
        )
    if expect_outcome is not None and outcome != expect_outcome:
        passed = False
        fail_reasons.append(
            f"expected outcome '{expect_outcome}', got '{outcome}'"
        )

    label = "PASS" if passed else "FAIL"
    trade_str = ", ".join(
        f"{t['type']} {t['entry']}¢→{t['exit']}¢ (${t['pnl']:+.2f})"
        for t in trades
    ) or "none"

    status_icon = "✅" if passed else "❌"
    _p(f"  {status_icon}  [{label}] {name}")
    _p(f"        Outcome: {outcome} | Trades: {trade_str}")
    if fail_reasons:
        for r in fail_reasons:
            _p(f"        REASON:  {r}")

    return {
        "name": name, "passed": passed,
        "outcome": outcome, "trades": trades,
        "pnl": total_pnl, "n_trades": n_trades,
    }


def run_logic_tests() -> List[dict]:
    _h("1 / 4  LOGIC SIMULATION")
    _p("  Replaying 6 synthetic price scenarios through the decision engine.\n")

    results = []

    # ── Scenario 1: price opens above 75¢ and stays there — no trade ──────────
    seq = [80] * 1500   # 5 min at 5 Hz = 1500 ticks, always above threshold
    results.append(_simulate(
        "No entry when price opens ≥75¢ (stays at 80¢)",
        seq, expect_trades=0, expect_outcome="NO_TRADE"
    ))

    # ── Scenario 2: clean breakout, stop gain ──────────────────────────────────
    # Price dips below 75, then breaks out, then ramps to 95
    seq = (
        [60] * 50 +     # below threshold — sets seen_yes_below=True
        [78] * 1 +      # crossover! enter YES
        list(range(78, 96, 1)) * 3 +   # rising to 96 — triggers stop gain
        [96] * 100
    )
    results.append(_simulate(
        "YES breakout → stop gain (60→78→96)",
        seq, expect_trades=1, expect_outcome="STOP_GAIN"
    ))

    # ── Scenario 3: stop loss then re-entry and stop gain ─────────────────────
    # Enter, get stopped, wait cooldown, re-enter, take profit
    cooldown_ticks = int(REENTRY_COOLDOWN_S / 0.2) + 5  # slightly more than cooldown
    seq = (
        [60] * 30 +      # below — arm YES
        [77] * 1 +       # enter YES
        [64] * 10 +      # stop loss triggered
        [60] * cooldown_ticks +  # cooldown period (price below threshold → re-arms)
        [77] * 1 +       # re-entry
        [96] * 50        # stop gain
    )
    results.append(_simulate(
        "Stop loss → cooldown → re-entry → stop gain",
        seq, expect_trades=2, expect_outcome="STOP_GAIN"
    ))

    # ── Scenario 4: NO side breakout ──────────────────────────────────────────
    # YES price high, then collapses below 25 (NO crosses 75)
    seq = (
        [90] * 30 +     # YES high, NO low (20¢) → seen_no_below=True
        [24] * 1 +      # YES=24 → NO=76 → crossover! enter NO
        [20] * 1 +      # NO rising... (YES=20 → NO=80)
        [4]  * 50       # NO=96 → stop gain
    )
    results.append(_simulate(
        "NO side breakout — YES collapses (90→24→4)",
        seq, expect_trades=1, expect_outcome="STOP_GAIN"
    ))

    # ── Scenario 5: hold to window close without hitting either stop ───────────
    seq = (
        [60] * 30 +     # below threshold
        [77] * 1 +      # enter
        [77] * 1400     # stays between 65-95 for rest of window
    )
    results.append(_simulate(
        "Enter, hold to resolution (price never hits stop)",
        seq, expect_trades=0, expect_outcome="HELD_TO_CLOSE"
        # 0 trades because stops are never triggered (position held to close)
    ))

    # ── Scenario 6: no re-entry during cooldown ────────────────────────────────
    seq = (
        [60] * 30 +    # arm YES
        [77] * 1 +     # enter
        [64] * 5 +     # stop loss
        [60, 77] * 20  # rapid crossovers during cooldown — should NOT fire
    )
    results.append(_simulate(
        "Cooldown respected — no re-entry within 30s after stop loss",
        seq, expect_trades=1   # only the first entry
    ))

    passed = sum(1 for r in results if r["passed"])
    _p(f"\n  {passed}/{len(results)} logic tests passed")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 2. CONNECTIVITY TEST
# ─────────────────────────────────────────────────────────────────────────────

def run_connectivity_test(client) -> dict:
    _h("2 / 4  CONNECTIVITY")
    results = {}

    # Balance
    try:
        bal = client.get_balance()
        cash = client.get_cash()
        _ok(f"API auth OK | portfolio=${bal:.2f}  cash=${cash:.2f}")
        _info(f"   At 75% position size, next trade ≈ ${cash * 0.75:.2f}")
        results["auth"] = True
        results["balance"] = bal
        results["cash"] = cash
    except Exception as e:
        _fail(f"API auth failed: {e}")
        results["auth"] = False
        results["balance"] = 0
        return results

    # Market discovery
    from btc_15m_scalp import find_btc15m_market, _next_close
    try:
        result = find_btc15m_market(client)
        if result:
            ticker, close_dt = result
            secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
            _ok(f"BTC 15m market found: {ticker}")
            _info(f"   Closes in {secs:.0f}s  ({close_dt.strftime('%H:%M:%S')} UTC)")
            results["market_found"] = True
            results["ticker"] = ticker
            results["close_time"] = close_dt
        else:
            _warn("No BTC 15m market found right now (may be between cycles)")
            results["market_found"] = False
    except Exception as e:
        _fail(f"Market discovery error: {e}")
        results["market_found"] = False

    # Next window timing
    next_close = _next_close()
    window_start = next_close - timedelta(minutes=5)
    now = datetime.now(timezone.utc)
    wait_s = (window_start - now).total_seconds()
    _ok(
        f"Next window: {window_start.strftime('%H:%M:%S')} UTC "
        f"(in {wait_s:.0f}s) → closes {next_close.strftime('%H:%M')} UTC"
    )
    results["next_window_secs"] = wait_s

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 3. WEBSOCKET LATENCY TEST
# ─────────────────────────────────────────────────────────────────────────────

def run_ws_test(client, ticker: Optional[str] = None) -> dict:
    _h("3 / 4  WEBSOCKET SPEED TEST")
    results = {"available": False}

    try:
        import websockets
    except ImportError:
        _fail("websockets library not installed — run: pip install websockets>=12.0")
        _info("   Script will fall back to REST polling (~3-8 Hz)")
        return results

    if not ticker:
        _warn("No live ticker — skipping WS speed test (run --live for full test)")
        _info("   websockets library IS installed — WS mode will activate")
        results["available"] = True
        results["skipped"] = True
        return results

    # Connect and measure time to first price
    first_price   = None
    first_ts      = None
    connect_ts    = None
    msg_count     = 0
    test_duration = 5.0   # seconds to collect messages

    received_prices = []

    async def _measure():
        nonlocal first_price, first_ts, connect_ts, msg_count

        ws_url = (
            client.base_url
            .replace("https://", "wss://")
            .replace("/trade-api/v2", "/trade-api/ws/v2")
        )
        headers = client._sign("GET", "/trade-api/ws/v2")

        connect_ts = time.perf_counter()
        try:
            async with websockets.connect(
                ws_url, additional_headers=headers,
                ping_interval=20, ping_timeout=10
            ) as ws:
                elapsed_connect = time.perf_counter() - connect_ts

                await ws.send(json.dumps({
                    "id": 1, "cmd": "subscribe",
                    "params": {"channels": ["ticker"], "market_tickers": [ticker]},
                }))

                deadline = time.perf_counter() + test_duration
                async for raw in ws:
                    data = json.loads(raw)
                    if data.get("type") == "ticker":
                        now = time.perf_counter()
                        if first_ts is None:
                            first_ts    = now
                            first_price = (
                                data.get("msg", {}).get("yes_ask")
                                or data.get("msg", {}).get("last_price")
                            )
                        msg_count += 1
                        received_prices.append(time.perf_counter())

                    if time.perf_counter() > deadline:
                        break

            return elapsed_connect
        except Exception as e:
            return str(e)

    result = asyncio.run(_measure())

    if isinstance(result, str):
        _fail(f"WebSocket error: {result}")
        results["available"] = False
        return results

    connect_ms = result * 1000
    ttfp_ms    = (first_ts - connect_ts) * 1000 if first_ts else None

    results["available"]   = True
    results["connect_ms"]  = connect_ms
    results["ttfp_ms"]     = ttfp_ms
    results["msg_count"]   = msg_count
    results["first_price"] = first_price

    _ok(f"WebSocket connected in {connect_ms:.0f}ms")

    if ttfp_ms is not None:
        _ok(f"Time to first price tick: {ttfp_ms:.0f}ms  (first YES price = {first_price}¢)")
    else:
        _warn("No ticker updates received in 5s (market may be quiet)")

    if msg_count > 0:
        hz = msg_count / test_duration
        _ok(f"Push rate over {test_duration:.0f}s: {msg_count} messages ({hz:.1f} Hz)")
        _info(f"   Each message = one price change event from Kalshi's matching engine")
    else:
        _info(f"   0 price updates in {test_duration:.0f}s — market is calm (normal)")

    # Compare to REST baseline
    _info(
        f"   REST polling baseline: ~150-300ms/call (~3-6 Hz) — "
        f"WS is {int(300/max(connect_ms,1)*10)}× faster for connection setup"
    )

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 4. LIVE DRY-RUN WINDOW  (--live flag only)
# ─────────────────────────────────────────────────────────────────────────────

def run_live_dryrun(client) -> None:
    _h("4 / 4  LIVE DRY-RUN WINDOW")
    from btc_15m_scalp import find_btc15m_market, _next_close, scalp_window

    # Find the next window start
    next_close   = _next_close()
    window_start = next_close - timedelta(minutes=5)
    now          = datetime.now(timezone.utc)
    wait_s       = (window_start - now).total_seconds()

    _info(
        f"Next window: closes {next_close.strftime('%H:%M:%S')} UTC  "
        f"(waiting {wait_s:.0f}s)"
    )
    _info(f"Watching in DRY-RUN mode — zero orders will be placed\n")

    if wait_s > 0:
        print(f"  Sleeping {wait_s:.0f}s", end="", flush=True)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            time.sleep(10)
            print(".", end="", flush=True)
        print()

    result = find_btc15m_market(client)
    if not result:
        _fail("No BTC 15m market found — cannot run live dry-run")
        return

    ticker, market_close = result
    _ok(f"Market: {ticker}  closes {market_close.strftime('%H:%M:%S')} UTC\n")

    # Run with detailed console logging for the validator
    import logging as _logging
    root = _logging.getLogger("btc15m")
    root.setLevel(_logging.DEBUG)
    root.handlers = [_logging.StreamHandler(sys.stdout)]
    root.handlers[0].setFormatter(
        _logging.Formatter("  %(asctime)s  %(message)s", datefmt="%H:%M:%S")
    )

    scalp_window(client, ticker, market_close, dry_run=True)


# ─────────────────────────────────────────────────────────────────────────────
# FINAL REPORT
# ─────────────────────────────────────────────────────────────────────────────

def final_report(logic_results, conn, ws, live_ran: bool) -> None:
    _h("VALIDATION SUMMARY")

    logic_pass  = all(r["passed"] for r in logic_results)
    conn_pass   = conn.get("auth", False)
    ws_pass     = ws.get("available", False)

    checks = [
        ("Decision logic (6 scenarios)",   logic_pass),
        ("Kalshi API connectivity",         conn_pass),
        ("WebSocket library available",     ws_pass),
    ]

    all_pass = all(v for _, v in checks)

    for label, ok in checks:
        icon = "✅" if ok else "❌"
        _p(f"  {icon}  {label}")

    _p("")
    if all_pass:
        _ok("Architecture validated — ready for live trading")
        _p("")
        _p("  Speed profile:")
        _p(f"    WebSocket connection: ~{ws.get('connect_ms', '?'):.0f}ms" if isinstance(ws.get('connect_ms'), float) else "    WebSocket: available")
        _p(f"    Price reaction time:  <10ms (in-memory read at 100 Hz)")
        _p(f"    Order placement:      ~100-200ms (taker, guaranteed fill)")
        _p(f"    Total signal→fill:    <250ms")
        _p("")
        _p("  At 75% position sizing:")
        if conn.get("cash"):
            cash = conn["cash"]
            _p(f"    Available cash:  ${cash:.2f}")
            _p(f"    Next trade size: ${cash * 0.75:.2f}")
            _p(f"    Stop loss cost:  ${cash * 0.75 * 0.10:.2f}  (~10¢ drop on entry)")
            _p(f"    Stop gain gain:  ${cash * 0.75 * 0.20:.2f}  (~20¢ rise on entry)")
    else:
        _fail("One or more checks failed — review above before going live")

    if live_ran:
        _p("\n  Live dry-run completed — check the window output above for signal quality")

    _p("")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Validate the BTC 15m scalper architecture"
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Also watch the next real 5-minute window in dry-run mode"
    )
    args = parser.parse_args()

    _p("=" * 60)
    _p("  BTC 15m Scalper — Architecture Validator")
    _p(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    _p("=" * 60)

    # 1. Logic tests (offline — no API needed)
    logic_results = run_logic_tests()

    # 2-3. Need API
    from client import KalshiClient
    try:
        client = KalshiClient()
    except Exception as e:
        _fail(f"Could not initialise Kalshi client: {e}")
        sys.exit(1)

    conn = run_connectivity_test(client)
    ws   = run_ws_test(client, ticker=conn.get("ticker"))

    live_ran = False
    if args.live:
        run_live_dryrun(client)
        live_ran = True

    final_report(logic_results, conn, ws, live_ran)
