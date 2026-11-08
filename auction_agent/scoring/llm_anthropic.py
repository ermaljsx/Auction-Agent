"""Anthropic Claude review client (Messages API, structured output): the default provider.

Built on the official ``anthropic`` SDK (2.x, which sends API version ``2026-10-21``), imported
lazily. Facts checked in the installed SDK source (2.6.0) and the claude-api skill:

- An explicit ``api_key`` is "total": the SDK then reads no credential from the environment, so
  ``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_PROFILE`` and federation variables are ignored (no error).
  The key is sent as ``Authorization: Bearer``.
- An explicit ``base_url`` wins over ``ANTHROPIC_BASE_URL``: the data cannot be rerouted.
- ``ANTHROPIC_CUSTOM_HEADERS`` is merged into every request and ``ANTHROPIC_LOG=debug`` logs
  requests, whatever the arguments: both are reported in the run notes.
- ``open_telemetry=False`` turns the SDK's tracing spans off, so no request content reaches a
  tracer whatever the ``ANTHROPIC_OPEN_TELEMETRY*`` variables say.
- The SDK retries connection errors, timeouts, 408, 409, 429 and 5xx (529 included).

Claude Haiku 5.5 rejects ``temperature``/``top_p``/``top_k``, thinking budgets and assistant
prefill, so none is ever sent; the JSON shape comes from ``output_config.format``. Thinking
counts toward ``max_tokens``, content blocks are read by type, and a safety decline arrives as
``stop_reason == "refusal"`` (there is no server-side fallback for this model).
"""
from __future__ import annotations

import os
from typing import Any

from .llm import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    LLMBatchError,
    LLMConfigError,
    LLMTransientError,
    ReviewBatch,
    _check_key,
    _is_timeout,
    _sdk_env_notes,
    _short,
    build_prompt,
    parse_reviews,
)

ANTHROPIC_BASE_URL_FIXED = "https://api.anthropic.com"

# LLM_REASONING_EFFORT -> (thinking, output_config.effort, extra max_tokens room for thinking).
# "xhigh" and "max" are left out on purpose: their thinking needs more than the 16K
# non-streaming ceiling, and "disabled" thinking is refused with them.
EFFORTS: dict[str, tuple[dict[str, str], str, int]] = {
    "none": ({"type": "disabled"}, "low", 0),
    "low": ({"type": "adaptive"}, "low", 3_000),
    "medium": ({"type": "adaptive"}, "medium", 6_000),
    "high": ({"type": "adaptive"}, "high", 10_000),
}
TOKENS_PER_CAR = 400  # the longest valid answer for one car (3 reasons, a verdict, the JSON)
MAX_TOKENS_CAP = 16_000  # stay under the SDK's non-streaming limit

# Still applied by the SDK despite the explicit key and base URL.
_SDK_ENV = ("ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_LOG")
# Read by the SDK only when no key is passed: ignored here, reported so nobody wonders.
_SDK_ENV_IGNORED = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")
# A 400 whose message says the account cannot pay (defensive: billing is normally a 402).
_BILLING_WORDS = ("credit balance", "usage limit", "spend limit")
_BILLING_CODES = frozenset({"enforced_spend_limit_reached", "billing_frozen"})


def _error_body(exc: BaseException) -> dict[str, Any]:
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


class AnthropicClient:
    """Claude Messages API with a JSON-schema output format; the SDK is imported lazily."""

    provider = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        reasoning_effort: str = "low",
        timeout_s: float = 120.0,
        max_retries: int = 2,
        sdk_client: Any = None,
        http_client: Any = None,
    ):
        _check_key("ANTHROPIC_API_KEY", api_key)
        if reasoning_effort not in EFFORTS:
            raise LLMConfigError(
                f"LLM_REASONING_EFFORT={reasoning_effort} is not used with Claude: choose none, low, "
                "medium or high (none is the cheapest)."
            )
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_s = timeout_s
        self.notes: list[str] = []
        if not api_key.startswith("sk-ant-"):
            self.notes.append("ANTHROPIC_API_KEY does not start with sk-ant-: check that you pasted the Claude key, "
                              "not the OpenAI one.")
        self.notes += _sdk_env_notes(_SDK_ENV, "Anthropic")
        ignored = [name for name in _SDK_ENV_IGNORED if os.environ.get(name)]
        if ignored:
            self.notes.append(f"{', '.join(ignored)} is set in the environment; it is ignored (the review uses "
                              "ANTHROPIC_API_KEY only).")
        if sdk_client is not None:
            self._client = sdk_client
            return
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:
            raise LLMConfigError("The anthropic package is not installed: pip install -r requirements.txt") from exc
        try:
            self._client = anthropic.Anthropic(
                api_key=api_key,
                base_url=ANTHROPIC_BASE_URL_FIXED,
                timeout=timeout_s,
                max_retries=max_retries,
                http_client=http_client,
                open_telemetry=False,
            )
        except Exception as exc:  # never echo the key
            raise LLMConfigError(f"Could not create the Anthropic client ({exc.__class__.__name__}).") from exc

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def _request(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        thinking, api_effort, room = EFFORTS[self.reasoning_effort]
        max_tokens = min(MAX_TOKENS_CAP, TOKENS_PER_CAR * len(payloads) + room)
        return dict(
            model=self.model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(payloads)}],
            output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}, "effort": api_effort},
            thinking=dict(thinking),
            # Never cut off (and pay for) a generation that is merely slow: about 40 output
            # tokens per second is a conservative floor.
            timeout=max(self.timeout_s, max_tokens / 40),
        )

    def _map_error(self, exc: BaseException) -> LLMTransientError | LLMConfigError:
        status = getattr(exc, "status_code", None)
        error = _error_body(exc)
        etype = getattr(exc, "type", None)
        etype = etype if isinstance(etype, str) else error.get("type")
        code = getattr(exc, "error_code", None)
        if not isinstance(code, str):
            details = error.get("details")
            code = details.get("error_code") if isinstance(details, dict) else None
        text = str(getattr(exc, "api_message", None) or error.get("message") or _short(exc))[:200]
        low = text.lower()
        if (status == 402 or etype == "billing_error" or code in _BILLING_CODES
                or (status == 400 and any(w in low for w in _BILLING_WORDS))):
            return LLMConfigError(
                "Anthropic refused the request for billing reasons (no credits left or spend limit reached): "
                "add credits or raise the limit in the Claude Console (platform.claude.com)."
            )
        if status == 401:
            return LLMConfigError("Anthropic rejected the API key (HTTP 401): check ANTHROPIC_API_KEY in .env.")
        if status == 403:
            return LLMConfigError(f"Anthropic denied access to model {self.model!r} (HTTP 403): check the key's "
                                  "workspace and organisation settings in the Claude Console.")
        if status == 404:
            return LLMConfigError(f"Anthropic does not offer model {self.model!r} to this key (HTTP 404): "
                                  "check LLM_MODEL.")
        if status == 413 or etype == "request_too_large":
            return LLMBatchError("Anthropic: request too large for one batch (HTTP 413).")
        if status == 400:
            if "prompt is too long" in low:
                return LLMBatchError(f"Anthropic rejected this batch (HTTP 400): {text}")
            if "thinking" in low or "effort" in low:
                return LLMConfigError(
                    f"Model {self.model!r} does not accept reasoning effort {self.reasoning_effort!r} "
                    f"(HTTP 400: {text}): set LLM_REASONING_EFFORT to none, low, medium or high."
                )
            return LLMConfigError(f"Anthropic rejected the request for model {self.model!r} (HTTP 400): {text}")
        if status == 429:
            return LLMTransientError("Anthropic rate limit reached; the remaining cars are reviewed on a later run.")
        if status in (408, 409) or (isinstance(status, int) and status >= 500):
            what = "overloaded" if status == 529 or etype == "overloaded_error" else "temporarily unavailable"
            return LLMTransientError(f"Anthropic {what} (HTTP {status}).")
        if isinstance(status, int):
            return LLMConfigError(f"Anthropic rejected the request (HTTP {status}): {text}")
        if _is_timeout(exc):
            # Usually a batch too large for the time limit: split it rather than resend it.
            return LLMBatchError("Anthropic did not answer in time.")
        if isinstance(exc, TypeError):  # SDK older than the parameters we send
            return LLMConfigError(f"The installed anthropic package is too old ({_short(exc)}): "
                                  "pip install -r requirements.txt")
        return LLMTransientError(f"{exc.__class__.__name__}: {_short(exc)}")

    def review(self, payloads: list[dict[str, Any]]) -> ReviewBatch:
        try:
            message = self._client.messages.create(**self._request(payloads))
        except Exception as exc:
            raise self._map_error(exc) from exc
        return self._parse(message, payloads)

    def _parse(self, message: Any, payloads: list[dict[str, Any]]) -> ReviewBatch:
        # Usage is read defensively: a missing field never discards paid reviews. Thinking
        # tokens are billed inside output_tokens; the breakdown is optional.
        usage = getattr(message, "usage", None)
        details = getattr(usage, "output_tokens_details", None)
        tokens = dict(
            tokens_in=getattr(usage, "input_tokens", None),
            tokens_out=getattr(usage, "output_tokens", None),
            tokens_reasoning=getattr(details, "thinking_tokens", None),
        )
        try:
            stop = getattr(message, "stop_reason", None)
            if stop == "refusal":
                category = getattr(getattr(message, "stop_details", None), "category", None)
                raise LLMBatchError(
                    f"Claude declined to review this batch (refusal{', category ' + category if category else ''})."
                )
            if stop == "max_tokens":
                hint = "" if self.reasoning_effort == "none" else "; lower LLM_REASONING_EFFORT"
                raise LLMBatchError(f"The answer was cut off (max_tokens reached{hint}).")
            if stop not in (None, "end_turn"):
                raise LLMBatchError(f"Unexpected stop reason {stop!r}.")
            text = "".join(
                str(getattr(block, "text", "") or "")
                for block in (getattr(message, "content", None) or [])
                if getattr(block, "type", None) == "text"
            )
            if not text.strip():
                raise LLMBatchError("Claude returned no text answer for this batch.")
            reviews = parse_reviews(text, {p["key"] for p in payloads})
        except LLMBatchError as exc:  # billed anyway: keep the usage for the record
            exc.tokens_in, exc.tokens_out, exc.tokens_reasoning = (
                tokens["tokens_in"], tokens["tokens_out"], tokens["tokens_reasoning"])
            raise
        return ReviewBatch(reviews=reviews, model=getattr(message, "model", None) or self.model, **tokens)
