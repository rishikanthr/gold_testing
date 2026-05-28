"""
llm_confirmation.py — Optional Groq LLM signal confirmation layer
===================================================================
Uses Groq's llama-3.3-70b-versatile to confirm trade signals.
The LLM NEVER overrides hard risk rules — it only filters low-
confidence setups.

If USE_LLM_CONFIRMATION is False, always returns (True, 10, "LLM disabled").
"""

import json
import logging
from typing import Optional

from config import GROQ_API_KEY, GROQ_MODEL, LLM_MIN_CONFIDENCE, USE_LLM_CONFIRMATION

logger = logging.getLogger(__name__)


def _build_prompt(signal: dict, pair: str, mtf_summary: str) -> str:
    return f"""You are an expert ICT (Inner Circle Trader) / Smart Money Concepts analyst.
A trading bot has identified the following setup on {pair}.

=== SIGNAL DETAILS ===
Strategy:     {signal.get('strategy_id')}
Direction:    {signal.get('signal')} ({"buy" if signal.get('signal') == 'LONG' else 'sell'})
Score:        {signal.get('score')}/10
Daily Bias:   {signal.get('daily_bias')}
HTF Zone:     {signal.get('htf_zone')}
Entry:        {signal.get('entry')}
Stop Loss:    {signal.get('stop_loss')} ({signal.get('risk_pips')} pips)
TP1:          {signal.get('tp1')}
TP2:          {signal.get('tp2')}
Reason:       {signal.get('reason')}
Invalidation: {signal.get('invalidation')}

=== MARKET CONTEXT ===
{mtf_summary}

=== YOUR TASK ===
Evaluate this ICT/SMC setup and respond with a JSON object ONLY (no other text):

{{
  "confidence": <integer 1-10>,
  "approve": <true|false>,
  "market_structure": "<BULLISH|BEARISH|NEUTRAL>",
  "displacement_quality": "<STRONG|MODERATE|WEAK>",
  "weaknesses": "<comma-separated list of concerns or 'none'>",
  "explanation": "<1-2 sentences explaining your decision>"
}}

Rules:
- confidence >= {LLM_MIN_CONFIDENCE} means approve=true
- Be strict about displacement quality and HTF alignment
- Reject if market structure does not confirm the signal direction
"""


def _build_mtf_summary(mtf_data: dict) -> str:
    """Creates a compact textual summary of MTF data for the LLM."""
    lines = []
    for tf, candles in mtf_data.items():
        if not candles:
            continue
        last = candles[-1]
        lines.append(
            f"{tf}: O={last['open']} H={last['high']} L={last['low']} C={last['close']}"
        )
    return "\n".join(lines) if lines else "No MTF data available"


def confirm_with_llm(
    signal: dict,
    pair: str,
    mtf_data: dict,
) -> tuple[bool, int, str]:
    """
    Returns (approved: bool, confidence: int, explanation: str).

    If LLM is disabled: returns (True, 10, "LLM disabled").
    If LLM call fails:  returns (True, 8, "LLM fallback — proceeding").
    """
    if not USE_LLM_CONFIRMATION:
        return True, 10, "LLM confirmation disabled"

    if not GROQ_API_KEY:
        logger.warning("Groq API key not set — skipping LLM confirmation")
        return True, 8, "No GROQ_API_KEY configured"

    try:
        import groq
        client = groq.Groq(api_key=GROQ_API_KEY)

        mtf_summary = _build_mtf_summary(mtf_data)
        prompt      = _build_prompt(signal, pair, mtf_summary)

        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,   # low temperature for consistent analysis
            max_tokens=400,
            response_format={"type": "json_object"},
        )

        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw)

        confidence   = int(data.get("confidence", 0))
        approved     = confidence >= LLM_MIN_CONFIDENCE
        explanation  = data.get("explanation", "")
        weaknesses   = data.get("weaknesses", "")
        ms           = data.get("market_structure", "")

        logger.info(
            "LLM confirmation %s | confidence=%d/10 | ms=%s | weaknesses=%s",
            "APPROVED" if approved else "REJECTED",
            confidence, ms, weaknesses
        )

        if weaknesses and weaknesses.lower() != "none":
            logger.info("LLM weaknesses: %s", weaknesses)

        return approved, confidence, explanation

    except json.JSONDecodeError as exc:
        logger.warning("LLM JSON parse error: %s — proceeding without confirmation", exc)
        return True, 7, "LLM response parse error — fallback"
    except Exception as exc:
        logger.warning("LLM call failed: %s — proceeding without confirmation", exc)
        return True, 7, f"LLM error: {str(exc)[:80]}"
