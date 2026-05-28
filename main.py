"""
main.py — Central orchestrator that wires all four pipeline layers together.

TRADING_MODE (set in .env):
  "paper"  →  uses mock_stream (synthetic data) + LocalPaperLedger
  "live"   →  uses live_stream (real Schwab WebSocket) + LocalPaperLedger
              (swap LocalPaperLedger for a real broker call when ready)

Run:
  python main.py
"""

import os
import signal
import sys

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRADING_MODE    = os.getenv("TRADING_MODE", "paper").lower()
WATCH_SYMBOLS   = [s.strip() for s in os.getenv("WATCH_SYMBOLS", "AAPL").split(",")]
STD_DEV_TRIGGER = 2.5    # sigma threshold that wakes the AI filter
AI_CONFIDENCE   = 0.85   # minimum Gemini confidence to allow a fill
ORDER_QTY       = 10     # shares per simulated fill
MOCK_DURATION   = 120    # seconds to run mock stream (paper mode)
MOCK_TICK_DELAY = 0.04   # seconds between synthetic ticks (~25 ticks/sec)

# ---------------------------------------------------------------------------
# Layer imports
# ---------------------------------------------------------------------------

from src.feature_engineering import PolarsTickAggregator
from src.ai_filter import TradingAIFilter
from src.simulation_ledger import LocalPaperLedger

if TRADING_MODE == "live":
    from src.live_stream import SchwabLiveStream
else:
    from src.mock_stream import stream_market_data


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline() -> None:
    # Layer 2 — rolling analytics engine
    aggregator = PolarsTickAggregator(window_size=30)

    # Layer 3 — AI risk filter (only called on threshold trips)
    ai_validator = TradingAIFilter()

    # Layer 4 — paper execution ledger
    paper_broker = LocalPaperLedger(initial_cash=100_000.0)

    mode_label = "LIVE (paper ledger)" if TRADING_MODE == "live" else "DRY-RUN (mock data)"
    print(f"\n  Trading Pipeline — {mode_label}")
    print(f"  Watching : {', '.join(WATCH_SYMBOLS)}")
    print(f"  Trigger  : price > rolling_mean + {STD_DEV_TRIGGER} * std_dev")
    print(f"  AI gate  : confidence > {AI_CONFIDENCE}")
    print("  Press Ctrl+C to stop.\n")

    # ------------------------------------------------------------------
    # Choose tick source based on TRADING_MODE
    # ------------------------------------------------------------------
    if TRADING_MODE == "live":
        stream = SchwabLiveStream(symbols=WATCH_SYMBOLS)
        stream.start()
        tick_source = stream.iter_ticks()
    else:
        tick_source = stream_market_data(
            symbols=WATCH_SYMBOLS,
            duration_seconds=MOCK_DURATION,
            delay=MOCK_TICK_DELAY,
        )

    # ------------------------------------------------------------------
    # Main tick loop
    # ------------------------------------------------------------------
    try:
        for raw_tick in tick_source:
            # Layer 1 → Layer 2: ingest and compute rolling stats
            aggregator.add_tick(raw_tick)

            symbol = raw_tick["content"][0]["key"]
            metrics = aggregator.calculate_signals(symbol)

            if metrics.is_empty():
                continue   # not enough history yet

            current_bid  = metrics["bid"][0]
            rolling_mean = metrics["rolling_mean_bid"][0]
            std_dev      = metrics["rolling_std_bid"][0] or 0.0

            upper_band = rolling_mean + (STD_DEV_TRIGGER * std_dev)

            # ----------------------------------------------------------
            # Gate 1: local math filter (zero token cost)
            # ----------------------------------------------------------
            if current_bid <= upper_band or std_dev == 0:
                continue   # normal market noise — discard silently

            print(
                f"\n  [THRESHOLD TRIP] {symbol}  "
                f"bid=${current_bid:.2f}  "
                f"band=${upper_band:.2f}  "
                f"(mean={rolling_mean:.2f}, sigma={std_dev:.4f})"
            )
            print("  Consulting Gemini 2.5 Flash...")

            context_snapshot = {
                "symbol":       symbol,
                "last_price":   round(current_bid, 4),
                "rolling_mean": round(rolling_mean, 4),
                "deviation":    round(std_dev, 6),
                "sigma_breach": round((current_bid - rolling_mean) / std_dev, 3),
                "volume_sum":   int(metrics["cumulative_volume"][0]),
            }

            # ----------------------------------------------------------
            # Gate 2: AI risk validation (micro-cost Gemini call)
            # ----------------------------------------------------------
            decision = ai_validator.evaluate_metrics(symbol, context_snapshot)

            print(
                f"  [AI VERDICT] action={decision.action}  "
                f"confidence={decision.confidence:.2f}  "
                f"urgency={decision.urgency}  "
                f"code={decision.reason_code}"
            )

            # ----------------------------------------------------------
            # Gate 3: execute only on high-confidence BUY signal
            # ----------------------------------------------------------
            if decision.action == "BUY" and decision.confidence > AI_CONFIDENCE:
                paper_broker.execute_paper_order(
                    symbol=symbol,
                    action="BUY",
                    qty=ORDER_QTY,
                    market_price=current_bid,
                )
            else:
                print(f"  [SKIPPED] Not enough conviction — staying flat.")

    except KeyboardInterrupt:
        print("\n  Interrupted by user.")
    finally:
        if TRADING_MODE == "live":
            stream.stop()
        paper_broker.print_summary()


if __name__ == "__main__":
    run_pipeline()
