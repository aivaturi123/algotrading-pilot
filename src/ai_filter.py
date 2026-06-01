"""
Layer 3: AI Risk Validation Node.
Calls Gemini 2.5 Flash only when the local math engine trips the 2.5-sigma
threshold.  Output is constrained to a strict Pydantic schema so the model
cannot emit conversational fluff — keeping latency and token cost minimal.
"""

import json
import os
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Output schema — Gemini must return exactly this JSON shape
# ---------------------------------------------------------------------------

class StopLossDecision(BaseModel):
    action: Literal["SELL", "HOLD"] = Field(
        description="SELL to cut the loss now, or HOLD if recovery is likely."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence in the recommendation (0 = uncertain, 1 = very sure).",
    )
    reasoning: str = Field(
        description=(
            "2–4 sentence explanation of why you chose SELL or HOLD, referencing "
            "specific data points from the market context provided."
        )
    )
    urgency: Literal["IMMEDIATE", "MONITOR"] = Field(
        description="IMMEDIATE = sell right now; MONITOR = keep watching but hold for now."
    )


class TradeDecision(BaseModel):
    action: Literal["BUY", "SELL", "HOLD"] = Field(
        description="Execution action based on the statistical context."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Model confidence from 0.0 (uncertain) to 1.0 (certain).",
    )
    urgency: Literal["IMMEDIATE", "NONE"] = Field(
        description="IMMEDIATE = enter now; NONE = wait or skip."
    )
    reason_code: str = Field(
        description="Short identifier for the signal pattern, e.g. 'BREAKOUT_VOL_CONFIRM'.",
    )


# ---------------------------------------------------------------------------
# Filter class
# ---------------------------------------------------------------------------

class TradingAIFilter:
    """
    Wraps Gemini 2.5 Flash as a low-latency risk validation step.

    Only called after the local math layer detects a 2.5-sigma price extension,
    so API calls are rare and per-call cost stays at fractions of a cent.
    """

    _SYSTEM_INSTRUCTION = (
        "You are a momentum signal classifier inside an automated day trading pipeline. "
        "A statistical breakout has been detected — price has exceeded 2.5 standard deviations "
        "above its short-term rolling mean. Your job is to decide whether to BUY, SELL, or HOLD. "
        "The volume_sum provided is the total shares traded across the last 30 ticks (~15 seconds) "
        "of market data — treat any non-zero volume as activity confirmation. "
        "Prefer BUY when sigma_breach is above 2.5 and volume_sum is above 5000. "
        "Only return HOLD if the sigma breach is borderline (below 2.8) AND volume is very thin (under 2000). "
        "This is a paper trading system used for strategy research — favour participation over caution."
    )

    def __init__(self) -> None:
        self.client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        self.model_name = "gemini-2.5-flash"

    _STOP_LOSS_SYSTEM = (
        "You are a stop-loss advisor for an automated equity trading system. "
        "A position is currently showing an unrealized loss. "
        "Your job is to analyse ALL provided market data — candle history, volume, "
        "bid/ask spread, day range, 52-week context — and decide whether to SELL now "
        "to cut the loss or HOLD because the dip is likely temporary. "
        "Be practical and data-driven. Reference specific numbers in your reasoning. "
        "Prefer SELL when volume confirms sustained selling pressure or price is breaking "
        "below key intraday support. Prefer HOLD when volume is thin and price is "
        "bouncing at or near the day low."
    )

    _LOG_PATH      = "gemini_decisions.jsonl"
    _STOP_LOSS_LOG = "gemini_stop_loss.jsonl"

    def _write_log(self, entry: dict) -> None:
        """Append one JSON record to the decisions log file."""
        with open(self._LOG_PATH, "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def evaluate_metrics(self, symbol: str, stats: dict) -> TradeDecision:
        """
        Send a compact statistical snapshot to Gemini and receive a
        structured TradeDecision back.

        Every call — success or error — is appended to gemini_decisions.jsonl
        with the full prompt, raw response, and parsed decision so you can
        review exactly why each trade was or wasn't taken.

        Args:
            symbol:  Ticker, e.g. "AAPL"
            stats:   Dict with keys: last_price, rolling_mean, deviation,
                     volume_sum (all numeric).

        Returns:
            TradeDecision — guaranteed by Pydantic validation.
            Falls back to HOLD on any API or parsing error.
        """
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        prompt = (
            f"Analyze a real-time breakout event for {symbol}.\n"
            f"Statistical snapshot:\n{json.dumps(stats, indent=2)}\n\n"
            "The price has exceeded 2.5 standard deviations above its rolling mean. "
            "Determine if this breakout is a tradable momentum event or noise. "
            "Return your assessment as JSON."
        )

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=self._SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=TradeDecision,
                    temperature=0.1,
                ),
            )
            decision = TradeDecision.model_validate_json(response.text)

            self._write_log({
                "timestamp":        ts,
                "symbol":           symbol,
                "input_stats":      stats,
                "system_prompt":    self._SYSTEM_INSTRUCTION,
                "user_prompt":      prompt,
                "raw_response":     response.text,
                "decision": {
                    "action":       decision.action,
                    "confidence":   decision.confidence,
                    "urgency":      decision.urgency,
                    "reason_code":  decision.reason_code,
                },
                "error": None,
            })

            return decision

        except Exception as exc:
            error_msg = str(exc)
            self._write_log({
                "timestamp":     ts,
                "symbol":        symbol,
                "input_stats":   stats,
                "system_prompt": self._SYSTEM_INSTRUCTION,
                "user_prompt":   prompt,
                "raw_response":  None,
                "decision":      None,
                "error":         error_msg,
            })
            error_code = f"AI_ERR_{error_msg[:20].replace(' ', '_').upper()}"
            return TradeDecision(
                action="HOLD",
                confidence=0.0,
                urgency="NONE",
                reason_code=error_code,
            )

    def evaluate_stop_loss(self, symbol: str, market_context: dict) -> StopLossDecision:
        """
        Ask Gemini whether a losing position should be sold or held,
        given a rich snapshot of live market data pulled from the Schwab API.

        market_context should contain:
            position      — avg_cost, current_price, pct_loss, dollar_loss
            quote         — bid, ask, last, open, high, low, volume, net_change_pct,
                            52w_high, 52w_low
            candles_5min  — list of recent OHLCV bars (last ~1 hour)
            recent_trend  — % price change across the last N candles

        Every call is logged to gemini_stop_loss.jsonl.
        Falls back to HOLD on any error so we never sell blindly on an AI crash.
        """
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        prompt = (
            f"A position in {symbol} is currently showing an unrealized loss.\n\n"
            f"Full market context (live data from Schwab API):\n"
            f"{json.dumps(market_context, indent=2)}\n\n"
            "Using all of the above data, decide:\n"
            "  SELL — cut the loss now before it gets worse\n"
            "  HOLD — the dip is likely temporary, price should recover\n\n"
            "Reference specific numbers from the data in your reasoning."
        )

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=self._STOP_LOSS_SYSTEM,
                    response_mime_type="application/json",
                    response_schema=StopLossDecision,
                    temperature=0.1,
                ),
            )
            decision = StopLossDecision.model_validate_json(response.text)

            with open(self._STOP_LOSS_LOG, "a") as fh:
                fh.write(json.dumps({
                    "timestamp":      ts,
                    "symbol":         symbol,
                    "market_context": market_context,
                    "system_prompt":  self._STOP_LOSS_SYSTEM,
                    "user_prompt":    prompt,
                    "raw_response":   response.text,
                    "decision": {
                        "action":    decision.action,
                        "confidence": decision.confidence,
                        "reasoning": decision.reasoning,
                        "urgency":   decision.urgency,
                    },
                    "error": None,
                }) + "\n")

            print(
                f"  [STOP LOSS GEMINI]  {symbol}  →  {decision.action}  "
                f"({decision.confidence:.0%} confidence)\n"
                f"  Reasoning: {decision.reasoning}"
            )
            return decision

        except Exception as exc:
            error_msg = str(exc)
            with open(self._STOP_LOSS_LOG, "a") as fh:
                fh.write(json.dumps({
                    "timestamp": ts, "symbol": symbol,
                    "market_context": market_context, "error": error_msg,
                }) + "\n")
            print(f"  [STOP LOSS GEMINI ERROR] {exc} — defaulting to HOLD")
            return StopLossDecision(
                action="HOLD",
                confidence=0.0,
                reasoning=f"AI error: {error_msg[:100]}",
                urgency="MONITOR",
            )
