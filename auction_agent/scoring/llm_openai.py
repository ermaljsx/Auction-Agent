"""OpenAI review client (Chat Completions, strict JSON schema): the fallback provider.

Selected with ``LLM_PROVIDER=openai``; the key is ``OPENAI_API_KEY``. Moved unchanged from
``scoring/llm.py`` on 2026-11-07, when Anthropic Claude became the default reviewer.
"""
from __future__ import annotations

from typing import Any, Optional

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

OPENAI_BASE_URL = "https://api.openai.com/v1"
# 429 bodies that mean "no money / limit reached": retrying cannot help.
_BILLING_CODES = frozenset({
    "insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "organization_usage_limit_exceeded", "usage_limit_exceeded",
    "billing_not_active",
})
_CONTENT_CODES = frozenset({"invalid_prompt", "context_length_exceeded"})
# Optional request parameters that some models reject: dropped once, then retried.
_DROPPABLE = ("temperature", "prompt_cache_options")
# The SDK still reads these from the environment even with explicit arguments.
_SDK_ENV = ("OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_CUSTOM_HEADERS")


class OpenAIClient:
    """OpenAI Chat Completions with a strict JSON schema; the SDK is imported lazily.

    The base URL is fixed (a stray OPENAI_BASE_URL never reroutes the data), responses
    are not stored (store=False), and reasoning effort defaults to "none": the
    default model reasons at "medium" otherwise and bills those tokens as output.
    """

    provider = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        reasoning_effort: str = "none",
        timeout_s: float = 120.0,
        max_retries: int = 1,
        sdk_client: Any = None,
        http_client: Any = None,
    ):
        _check_key("OPENAI_API_KEY", api_key)
        self.model = model
        self.reasoning_effort = reasoning_effort  # "off": non-reasoning model, parameter not sent
        self.timeout_s = timeout_s
        self.notes: list[str] = _sdk_env_notes(_SDK_ENV, "OpenAI")
        self._dropped: set[str] = set()
        if sdk_client is not None:
            self._client = sdk_client
            return
        try:
            import openai  # noqa: PLC0415
        except ImportError as exc:
            raise LLMConfigError("The openai package is not installed: pip install -r requirements.txt") from exc
        try:
            self._client = openai.OpenAI(
                api_key=api_key,
                base_url=OPENAI_BASE_URL,
                timeout=timeout_s,
                max_retries=max_retries,
                http_client=http_client,
            )
        except Exception as exc:  # never echo the key
            raise LLMConfigError(f"Could not create the OpenAI client ({exc.__class__.__name__}).") from exc

    def _request(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        effort = self.reasoning_effort
        # Counts hidden reasoning tokens too, hence the larger budget above "none".
        max_tokens = (300 if effort in ("none", "off") else 1200) * len(payloads) + 1000
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=[
                {"role": "developer", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(payloads)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "vehicle_reviews", "strict": True, "schema": RESPONSE_SCHEMA},
            },
            max_completion_tokens=max_tokens,
            store=False,
            # Long answers need time: never cut off (and pay for) a generation that is
            # merely slow. About 20 output tokens per second is a conservative floor.
            timeout=max(self.timeout_s, max_tokens / 20),
        )
        if effort != "off":
            kwargs["reasoning_effort"] = effort
        if effort in ("none", "off") and "temperature" not in self._dropped:
            kwargs["temperature"] = 0  # reasoning models accept it only at effort "none"
        if "prompt_cache_options" not in self._dropped:
            # Explicit mode without breakpoints disables prompt caching: each payload is
            # unique, so caching would only add the cache-write surcharge and retention.
            kwargs["prompt_cache_options"] = {"mode": "explicit"}
        return kwargs

    @staticmethod
    def _is_timeout(exc: BaseException) -> bool:
        return _is_timeout(exc)

    @staticmethod
    def _rejected_param(exc: BaseException) -> Optional[str]:
        if getattr(exc, "status_code", None) != 400:
            return None
        param = getattr(exc, "param", None)
        if param:
            return str(param).split(".")[0].split("[")[0]
        text = str(exc)
        return next((name for name in (*_DROPPABLE, "reasoning_effort") if name in text), None)

    def _map_error(self, exc: BaseException) -> LLMTransientError | LLMConfigError:
        status = getattr(exc, "status_code", None)
        codes = {str(getattr(exc, "type", None) or ""), str(getattr(exc, "code", None) or "")}
        if status == 429 and codes & _BILLING_CODES:
            return LLMConfigError(
                "OpenAI refused the request for billing reasons (no credits left or spend limit "
                "reached): add credits or raise the project limit on platform.openai.com."
            )
        if status == 429:
            return LLMTransientError("OpenAI rate limit reached; the remaining cars are reviewed on a later run.")
        if status == 401:
            return LLMConfigError("OpenAI rejected the API key (HTTP 401): check OPENAI_API_KEY in .env.")
        if status == 403:
            return LLMConfigError(f"OpenAI denied access to model {self.model!r} (HTTP 403): check the key's project permissions.")
        if status == 404:
            return LLMConfigError(f"OpenAI does not offer model {self.model!r} to this key (HTTP 404): check LLM_MODEL.")
        if status == 400:
            param = self._rejected_param(exc)
            if param == "reasoning_effort":
                return LLMConfigError(
                    f"Model {self.model!r} does not accept reasoning effort {self.reasoning_effort!r}: set "
                    "LLM_REASONING_EFFORT to a value it supports (higher effort costs more), or to 'off' "
                    "for a model without reasoning."
                )
            if param == "messages" or codes & _CONTENT_CODES:
                return LLMBatchError(f"OpenAI rejected this batch (HTTP 400): {_short(exc)}")
            # Anything else would fail for every batch: stop for this run, mark no car.
            what = f"parameter {param!r}" if param else "the request"
            return LLMConfigError(f"OpenAI rejected {what} for model {self.model!r} (HTTP 400): {_short(exc)}")
        if status in (408, 409) or (isinstance(status, int) and status >= 500):
            return LLMTransientError(f"OpenAI temporarily unavailable (HTTP {status}).")
        if isinstance(status, int):
            return LLMConfigError(f"OpenAI rejected the request (HTTP {status}): {_short(exc)}")
        if self._is_timeout(exc):
            # Usually a batch too large for the time limit: split it rather than resend it.
            return LLMBatchError(f"OpenAI did not answer in time ({_short(exc)}).")
        if isinstance(exc, TypeError):  # SDK older than the parameters we send
            return LLMConfigError(f"The installed openai package is too old ({_short(exc)}): pip install -r requirements.txt")
        return LLMTransientError(f"{exc.__class__.__name__}: {_short(exc)}")

    def review(self, payloads: list[dict[str, Any]]) -> ReviewBatch:
        while True:
            try:
                completion = self._client.chat.completions.create(**self._request(payloads))
                break
            except Exception as exc:
                param = self._rejected_param(exc)
                if param in _DROPPABLE and param not in self._dropped:
                    self._dropped.add(param)  # at most once per parameter: the loop ends
                    effect = ("OpenAI's default prompt caching applies" if param == "prompt_cache_options"
                              else "sent without it")
                    self.notes.append(f"Model {self.model!r} rejected {param!r}; {effect}.")
                    continue
                raise self._map_error(exc) from exc
        return self._parse(completion, payloads)

    def _parse(self, completion: Any, payloads: list[dict[str, Any]]) -> ReviewBatch:
        # Usage is optional in the SDK types: a missing field never discards paid reviews.
        usage = getattr(completion, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        tokens = dict(
            tokens_in=getattr(usage, "prompt_tokens", None),
            tokens_out=getattr(usage, "completion_tokens", None),
            tokens_reasoning=getattr(details, "reasoning_tokens", None),
        )
        try:
            choices = getattr(completion, "choices", None) or []
            if not choices:
                raise LLMBatchError("OpenAI returned no answer for this batch.")
            message = getattr(choices[0], "message", None)
            finish = getattr(choices[0], "finish_reason", None)
            if getattr(message, "refusal", None):
                raise LLMBatchError("The model refused to review this batch.")
            if finish == "length":
                hint = "" if self.reasoning_effort in ("none", "off") else "; lower LLM_REASONING_EFFORT"
                raise LLMBatchError(f"The answer was cut off (max_completion_tokens reached{hint}).")
            if finish == "content_filter":
                raise LLMBatchError("The answer was blocked by OpenAI's content filter.")
            reviews = parse_reviews(getattr(message, "content", None) or "", {p["key"] for p in payloads})
        except LLMBatchError as exc:  # billed anyway: keep the usage for the record
            exc.tokens_in, exc.tokens_out, exc.tokens_reasoning = (
                tokens["tokens_in"], tokens["tokens_out"], tokens["tokens_reasoning"])
            raise
        return ReviewBatch(reviews=reviews, model=getattr(completion, "model", None) or self.model, **tokens)
