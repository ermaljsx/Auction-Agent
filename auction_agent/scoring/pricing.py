"""List prices of the AI review models, and the cost of a call.

Prices are dated and their source recorded; the provider's console spend limit is the real
cap. An unknown model is reported as such ("cost not computed"), never guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens. Above ``long_threshold`` prompt tokens, the long rate card applies
    to the whole request (input and output)."""

    provider: str
    input_per_mtok: float
    output_per_mtok: float
    long_input_per_mtok: Optional[float] = None
    long_output_per_mtok: Optional[float] = None
    long_threshold: Optional[int] = None
    source: str = ""


PRICES: dict[str, Price] = {
    # $0.10 / $0.50 for prompts of 100K tokens or fewer, $0.50 / $2.50 above.
    "claude-haiku-5-5": Price(
        "anthropic", 0.10, 0.50, 0.50, 2.50, 100_000,
        source="claude-api skill, Claude Haiku 5.5 migration guide and model list, read 2026-11-07 "
               "(https://platform.claude.com/docs/en/about-claude/pricing)",
    ),
    "gpt-6-luna": Price(
        "openai", 0.10, 0.50,
        source="OpenAI pricing as reported by search snippets and price tables, 2026-10-14 (README)",
    ),
}

BATCH_DISCOUNT = 0.5  # Message Batches / Batch API: half price, results within 24 h (not used yet)

# Estimates used by `llm-estimate` only (labelled as such in its output).
CHARS_PER_TOKEN = 3.0  # compact French JSON; Claude Haiku 5.5 counts about 30% more tokens than Haiku 4.5
OUTPUT_TOKENS_TYPICAL = 150  # per car: a short verdict, usually no reasons
OUTPUT_TOKENS_WORST = 400  # per car: 3 reasons, a 2-sentence verdict and the JSON
THINKING_TOKENS_PER_BATCH = {"none": 0, "off": 0, "minimal": 500, "low": 1_500, "medium": 4_000, "high": 8_000,
                             "xhigh": 12_000, "max": 16_000}


def price_for(model: Optional[str]) -> Optional[Price]:
    """Exact id first, then a dated id of a known model ("gpt-6-luna-2026-09-22")."""
    if not model:
        return None
    if model in PRICES:
        return PRICES[model]
    best = None
    for key in PRICES:
        if re.match(rf"{re.escape(key)}-\d", model) and (best is None or len(key) > len(best)):
            best = key
    return PRICES[best] if best else None


def cost_usd(model: Optional[str], tokens_in: Optional[int], tokens_out: Optional[int]) -> Optional[float]:
    """Cost of one call in USD, or None when the price or the token counts are unknown.

    No prompt caching is used, so ``tokens_in`` is the prompt size that picks the rate card.
    """
    price = price_for(model)
    if price is None or tokens_in is None or tokens_out is None:
        return None
    long_card = (price.long_threshold is not None and tokens_in > price.long_threshold
                 and price.long_input_per_mtok is not None and price.long_output_per_mtok is not None)
    rate_in = price.long_input_per_mtok if long_card else price.input_per_mtok
    rate_out = price.long_output_per_mtok if long_card else price.output_per_mtok
    return (tokens_in * rate_in + tokens_out * rate_out) / 1_000_000  # type: ignore[operator]
