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
        "You are a risk validation layer inside an automated day trading pipeline. "
        "Your job is to decide whether a statistical price breakout is a genuine "
        "institutional momentum event or high-risk noise that should be avoided. "
        "Be conservative: protect capital above all else. "
        "Only recommend BUY when volume confirms the price move and risk/reward is favourable."
    )

    def __init__(self) -> None:
        self.client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        self.model_name = "gemini-2.5-flash"

    _LOG_PATH = "gemini_decisions.jsonl"

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
