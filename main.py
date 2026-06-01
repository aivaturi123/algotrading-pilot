"""
main.py — Central orchestrator that wires all four pipeline layers together.

TRADING_MODE (set in .env):
  "paper"  →  mock stream (synthetic ticks) + LocalPaperLedger (no real orders)
  "live"   →  SchwabLiveStream (real WebSocket) + SchwabLiveBroker (real orders)

Run:
  python main.py
"""

import atexit
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Session logger — tees every print() to spot_check_logs/logN.txt
# ---------------------------------------------------------------------------

class _Tee:
    """Mirrors every write to the real stdout AND an open log file."""

    def __init__(self, log_path: Path) -> None:
        self._real_stdout = sys.stdout
        self._file = log_path.open("w", encoding="utf-8", buffering=1)
        self._start = time.time()

    def write(self, data: str) -> None:
        self._real_stdout.write(data)
        self._file.write(data)

    def flush(self) -> None:
        self._real_stdout.flush()
        self._file.flush()

    def close(self) -> None:
        elapsed = time.time() - self._start
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        s = int(elapsed % 60)
        duration_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"
        footer = (
            f"\n  Session ended  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"  Total runtime  : {duration_str}\n"
        )
        self._real_stdout.write(footer)
        self._file.write(footer)
        self._file.flush()
        sys.stdout = self._real_stdout   # restore before closing
        self._file.close()

    def __getattr__(self, name):
        # Forward isatty(), fileno(), etc. to the real stdout
        return getattr(self._real_stdout, name)


def _setup_session_log() -> Path:
    """
    Create spot_check_logs/ if needed, pick the next log number,
    redirect sys.stdout through _Tee, and register cleanup with atexit
    so the file is closed cleanly on Ctrl-C, normal exit, or crash.
    """
    log_dir = Path("spot_check_logs")
    log_dir.mkdir(exist_ok=True)

    # Find the highest existing logN number and increment
    nums = []
    for f in log_dir.glob("log*.txt"):
        try:
            nums.append(int(f.stem[3:]))
        except ValueError:
            pass
    next_num = max(nums, default=0) + 1
    log_path = log_dir / f"log{next_num}.txt"

    tee = _Tee(log_path)
    sys.stdout = tee
    atexit.register(tee.close)   # guaranteed cleanup on any exit

    return log_path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRADING_MODE = os.getenv("TRADING_MODE", "paper").lower()

# Load symbols — set WATCH_SYMBOLS="volatile" in .env to use the curated 50-stock list
# or set WATCH_SYMBOLS="AAPL,TSLA" for a custom list
_symbols_env = os.getenv("WATCH_SYMBOLS", "volatile").strip().lower()
if _symbols_env == "volatile":
    from src.watchlist import VOLATILE
    WATCH_SYMBOLS = VOLATILE
elif _symbols_env == "crypto":
    from src.watchlist import CRYPTO_PROXIES
    WATCH_SYMBOLS = CRYPTO_PROXIES
elif _symbols_env == "ai":
    from src.watchlist import AI_PLAYS
    WATCH_SYMBOLS = AI_PLAYS
elif _symbols_env == "ev":
    from src.watchlist import EV_ENERGY
    WATCH_SYMBOLS = EV_ENERGY
else:
    WATCH_SYMBOLS = [s.strip().upper() for s in _symbols_env.split(",")]

STD_DEV_TRIGGER       = 2.0    # sigma threshold that wakes the AI filter
MIN_BREACH_SIGMA      = 0.5    # price must exceed the band by this many extra sigmas before calling Gemini
AI_CONFIDENCE         = 0.65   # minimum Gemini confidence to allow a fill
ORDER_QTY             = 10     # shares per order (ignored in live mode — fractional qty calculated from balance)
MOCK_TICK_DELAY       = 0.04   # seconds between synthetic ticks (~25 ticks/sec)
AI_COOLDOWN_SECS      = 60     # seconds to wait per symbol before calling Gemini again (buy signals)
STOP_LOSS_PCT         = float(os.getenv("STOP_LOSS_PCT", "0.05"))   # consult Gemini when loss >= this
STOP_LOSS_COOLDOWN    = 120    # seconds between stop-loss Gemini calls per symbol
DEBUG_MODE            = os.getenv("DEBUG_MODE", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Layer imports
# ---------------------------------------------------------------------------

from src.feature_engineering import PolarsTickAggregator
from src.ai_filter import TradingAIFilter

if TRADING_MODE == "live":
    from src.live_stream  import SchwabLiveStream
    from src.live_broker  import SchwabLiveBroker
else:
    from src.mock_stream       import stream_market_data
    from src.simulation_ledger import LocalPaperLedger


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline() -> None:
    # Start session log — every print() from here on is saved to spot_check_logs/logN.txt
    log_path = _setup_session_log()
    print(f"  Session log : {log_path}  (started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")

    # Layer 2 — rolling analytics engine
    aggregator = PolarsTickAggregator(window_size=30)

    # Layer 3 — AI risk filter (only called on threshold trips)
    ai_validator = TradingAIFilter()

    # Layer 4 — execution layer (live orders or paper simulation)
    if TRADING_MODE == "live":
        broker = SchwabLiveBroker()
    else:
        broker = LocalPaperLedger(initial_cash=200_000.0)

    # Tracks the last time Gemini was called per symbol to enforce cooldown
    last_ai_call: dict[str, float] = {}

    # Per-symbol cooldown for stop-loss Gemini calls (separate from buy-signal cooldown)
    last_stop_loss_call: dict[str, float] = {}

    # Latest bid price per symbol — used by the take-profit scanner
    current_prices: dict[str, float] = {}

    if TRADING_MODE == "live":
        mode_label = "LIVE TRADING  ⚠  REAL ORDERS WILL BE PLACED"
    else:
        mode_label = "PAPER (mock data, no real orders)"

    _order_cap_pct      = int(float(os.getenv("MAX_ORDER_PCT_OF_ACCOUNT", "0.05")) * 100)
    _take_profit_pct    = int(float(os.getenv("TAKE_PROFIT_PCT", "0.03")) * 100)

    print(f"\n  Trading Pipeline — {mode_label}")
    print(f"  Watching    : {', '.join(WATCH_SYMBOLS)}")
    print(f"  Trigger     : price > rolling_mean + {STD_DEV_TRIGGER} * std_dev")
    print(f"  AI gate     : confidence > {AI_CONFIDENCE}")
    print(f"  Order cap   : {_order_cap_pct}% of balance (fractional shares)")
    print(f"  Take-profit : +{_take_profit_pct}%  |  Stop-loss : -{int(STOP_LOSS_PCT * 100)}% (AI-assisted)")
    print("  Press Ctrl+C to stop.\n")

    # ------------------------------------------------------------------
    # Choose tick source based on TRADING_MODE
    # ------------------------------------------------------------------
    if TRADING_MODE == "live":
        stream = SchwabLiveStream(symbols=WATCH_SYMBOLS, debug=DEBUG_MODE)
        stream.start()
        tick_source = stream.iter_ticks()
    else:
        tick_source = stream_market_data(
            symbols=WATCH_SYMBOLS,
            delay=MOCK_TICK_DELAY,
        )

    # ------------------------------------------------------------------
    # Main tick loop
    # ------------------------------------------------------------------
    try:
        for raw_tick in tick_source:
            # Debug: print every raw tick received before any filtering
            if DEBUG_MODE:
                content = raw_tick.get("content", [{}])[0]
                print(
                    f"  [RAW TICK] {datetime.now().strftime('%H:%M:%S')}  "
                    f"{content.get('key','?'):<6}  "
                    f"bid=${content.get('1', '?')}"
                )

            # Layer 1 → Layer 2: ingest and compute rolling stats
            aggregator.add_tick(raw_tick)

            symbol  = raw_tick["content"][0]["key"]

            # Keep current_prices up to date for the take-profit scanner
            bid = raw_tick["content"][0].get("1")
            if bid is not None:
                current_prices[symbol] = float(bid)
                broker.check_profit_targets(current_prices)

                # ----------------------------------------------------------
                # Stop-loss monitor — runs on every tick that has a bid price
                # ----------------------------------------------------------
                now_sl = time.time()
                for sl_symbol, pos in broker.get_positions().items():
                    sl_price = current_prices.get(sl_symbol)
                    if sl_price is None:
                        continue

                    avg_cost = pos["avg_cost"]
                    pct_loss = (sl_price - avg_cost) / avg_cost   # negative = loss

                    if pct_loss >= -STOP_LOSS_PCT:
                        continue   # not in loss territory yet

                    # Respect per-symbol stop-loss cooldown
                    if now_sl - last_stop_loss_call.get(sl_symbol, 0) < STOP_LOSS_COOLDOWN:
                        continue

                    last_stop_loss_call[sl_symbol] = now_sl

                    print(
                        f"\n  [STOP LOSS TRIGGER] {sl_symbol}  "
                        f"avg_cost=${avg_cost:.4f}  now=${sl_price:.4f}  "
                        f"loss={pct_loss:.2%}  — Gemini disabled, auto-selling."
                    )

                    # --- GEMINI STOP-LOSS CONSULTATION (disabled — token limit) ---
                    # Uncomment this entire block to re-enable AI-assisted stop-loss decisions.
                    #
                    # # Fetch rich context from Schwab (live) or use lightweight dict (paper)
                    # if TRADING_MODE == "live":
                    #     market_ctx = broker.get_stop_loss_context(sl_symbol, pos, sl_price)
                    # else:
                    #     # Paper mode: no live API — build a minimal context from what we know
                    #     market_ctx = {
                    #         "position": {
                    #             "symbol":        sl_symbol,
                    #             "qty":           pos["qty"],
                    #             "avg_cost":      round(avg_cost, 4),
                    #             "current_price": round(sl_price, 4),
                    #             "pct_loss":      round(pct_loss * 100, 3),
                    #             "dollar_loss":   round((sl_price - avg_cost) * pos["qty"], 2),
                    #         },
                    #         "quote":        None,
                    #         "candles_5min": None,
                    #         "recent_trend": None,
                    #         "note":         "Paper mode — live quote and candle data unavailable.",
                    #     }
                    #
                    # sl_decision = ai_validator.evaluate_stop_loss(sl_symbol, market_ctx)
                    #
                    # if sl_decision.action == "SELL":
                    #     print(
                    #         f"  [STOP LOSS SELL] Gemini recommends SELL "
                    #         f"({sl_decision.confidence:.0%} confidence, "
                    #         f"urgency={sl_decision.urgency})"
                    #     )
                    #     broker.execute_order(
                    #         symbol=sl_symbol,
                    #         action="SELL",
                    #         qty=pos["qty"],
                    #         market_price=sl_price,
                    #     )
                    # else:
                    #     print(
                    #         f"  [STOP LOSS HOLD] Gemini says HOLD "
                    #         f"({sl_decision.confidence:.0%} confidence) — keeping position."
                    #     )
                    # --- END GEMINI STOP-LOSS CONSULTATION ---

                    # Fallback while Gemini is disabled: auto-sell on stop-loss trigger
                    broker.execute_order(
                        symbol=sl_symbol,
                        action="SELL",
                        qty=pos["qty"],
                        market_price=sl_price,
                    )

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

            # Guard A: breach must be meaningful, not just barely over the band
            sigma_breach = (current_bid - rolling_mean) / std_dev
            if sigma_breach < STD_DEV_TRIGGER + MIN_BREACH_SIGMA:
                continue   # too shallow — not worth an AI call

            # Guard B: per-symbol cooldown — don't hammer Gemini on every tick
            now = time.time()
            if now - last_ai_call.get(symbol, 0) < AI_COOLDOWN_SECS:
                continue   # still in cooldown for this symbol

            print(
                f"\n  [THRESHOLD TRIP] {symbol}  "
                f"bid=${current_bid:.2f}  "
                f"band=${upper_band:.2f}  "
                f"(mean={rolling_mean:.2f}, sigma={std_dev:.4f}, breach={sigma_breach:.2f}σ)"
            )
            last_ai_call[symbol] = now

            # --- GEMINI BUY-SIGNAL VALIDATION (disabled — token limit) ---
            # Uncomment this entire block to re-enable AI-gated buy decisions.
            #
            # print("  Consulting Gemini 2.5 Flash...")
            #
            # context_snapshot = {
            #     "symbol":       symbol,
            #     "last_price":   round(current_bid, 4),
            #     "rolling_mean": round(rolling_mean, 4),
            #     "deviation":    round(std_dev, 6),
            #     "sigma_breach": round((current_bid - rolling_mean) / std_dev, 3),
            #     "volume_sum":   int(metrics["cumulative_volume"][0]),
            # }
            #
            # # Gate 2: AI risk validation (micro-cost Gemini call)
            # decision = ai_validator.evaluate_metrics(symbol, context_snapshot)
            #
            # print(
            #     f"  [AI VERDICT] action={decision.action}  "
            #     f"confidence={decision.confidence:.2f}  "
            #     f"urgency={decision.urgency}  "
            #     f"code={decision.reason_code}"
            # )
            #
            # # Gate 3: execute only on high-confidence BUY signal
            # if decision.action == "BUY" and decision.confidence > AI_CONFIDENCE:
            #     broker.execute_order(
            #         symbol=symbol,
            #         action="BUY",
            #         qty=ORDER_QTY,
            #         market_price=current_bid,
            #     )
            # else:
            #     print(f"  [SKIPPED] Not enough conviction — staying flat.")
            # --- END GEMINI BUY-SIGNAL VALIDATION ---

            # Fallback while Gemini is disabled: buy on every sigma breach
            print(f"  [AUTO BUY] Gemini disabled — buying on sigma breach.")
            broker.execute_order(
                symbol=symbol,
                action="BUY",
                qty=ORDER_QTY,
                market_price=current_bid,
            )

    except KeyboardInterrupt:
        print("\n  Interrupted by user.")
    finally:
        if TRADING_MODE == "live":
            stream.stop()
        broker.print_summary()


if __name__ == "__main__":
    run_pipeline()
