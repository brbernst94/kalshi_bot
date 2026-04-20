"""
daily_agent.py — Claude-powered daily performance analyst
===========================================================
Reads logs/trades.csv and the last 300 lines of logs/bot.log,
then asks Claude to produce a plain-language performance report.

Runs automatically at 00:00 UTC via main.py scheduler.
Requires ANTHROPIC_API_KEY environment variable.
"""

import csv
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

TRADE_LOG = "logs/trades.csv"
BOT_LOG   = "logs/bot.log"
LOG_LINES = 300   # recent bot.log lines to feed the model
LOOKBACK_DAYS = 7  # analyse the past week of trades


def _read_recent_trades(days: int = LOOKBACK_DAYS) -> str:
    path = Path(TRADE_LOG)
    if not path.exists():
        return "(no trades.csv found yet)"

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                    if ts >= cutoff:
                        rows.append(row)
                except Exception:
                    rows.append(row)
    except Exception as e:
        return f"(error reading trades.csv: {e})"

    if not rows:
        return f"(no trades in the past {days} days)"

    lines = [",".join(rows[0].keys())]
    for r in rows:
        lines.append(",".join(str(v) for v in r.values()))
    return "\n".join(lines)


def _read_recent_bot_log(n_lines: int = LOG_LINES) -> str:
    path = Path(BOT_LOG)
    if not path.exists():
        return "(no bot.log found)"
    try:
        with open(path, "r") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-n_lines:])
    except Exception as e:
        return f"(error reading bot.log: {e})"


def run_daily_agent() -> None:
    """Main entry point — called by the scheduler in main.py at 00:00 UTC."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("[DAILY_AGENT] ANTHROPIC_API_KEY not set — skipping Claude analysis")
        return

    try:
        import anthropic
    except ImportError:
        logger.error("[DAILY_AGENT] anthropic package not installed. Run: pip install anthropic")
        return

    logger.info("[DAILY_AGENT] Starting autonomous daily analysis...")

    trades_csv = _read_recent_trades()
    bot_log    = _read_recent_bot_log()
    today      = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    system = (
        "You are a quantitative trading analyst reviewing a Kalshi prediction-market bot. "
        "Be concise and direct. Focus on what's working, what's losing money, and what to fix. "
        "Format your response as plain text with short sections — no markdown tables."
    )

    user_msg = f"""Daily performance review for {today}.

=== TRADES (last 7 days) ===
{trades_csv}

=== BOT LOG (last {LOG_LINES} lines) ===
{bot_log}

Please analyse:
1. P&L by strategy — which strategies are profitable, which are losing?
2. Win rate per strategy (CLOSE rows where expected_pnl_usd > 0 = win)
3. Any recurring errors or warnings that suggest a bug
4. Top 3 actionable recommendations to improve profitability
5. Overall verdict: is the bot healthy, struggling, or broken?

Keep the entire response under 400 words."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=600,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )

        # Extract text from the response (skip thinking blocks)
        analysis = "\n".join(
            block.text for block in response.content
            if block.type == "text"
        )

        separator = "=" * 60
        logger.info(f"\n{separator}\n[DAILY_AGENT] AUTONOMOUS ANALYSIS — {today}\n{separator}\n{analysis}\n{separator}")

    except Exception as e:
        logger.error(f"[DAILY_AGENT] Claude API call failed: {e}", exc_info=True)


if __name__ == "__main__":
    # Allow manual trigger: python daily_agent.py
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run_daily_agent()
