"""Anthropic Claude review client: request shape, answer handling, error mapping, construction.

No test reaches the network: unit tests use a fake SDK object, and the contract tests run
the real anthropic SDK against an in-memory httpx2 transport (clients are always closed:
the suite turns ResourceWarning into an error).
"""
import json
import sys
from types import SimpleNamespace

import pytest

from auction_agent.config import Settings
from auction_agent.scoring import llm_anthropic, llm_openai
from auction_agent.scoring.llm import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    LLMBatchError,
    LLMConfigError,
    LLMTransientError,
    make_client,
)
from auction_agent.scoring.llm_anthropic import ANTHROPIC_BASE_URL_FIXED, AnthropicClient

KEY = "sk-ant-test"
PAYLOADS = [{"key": "1", "brand": "TOYOTA"}, {"key": "2", "brand": "PEUGEOT"}]
GOOD = json.dumps({"items": [
    {"key": "1", "adjustment": -2, "reasons": ["Boîte à surveiller"], "verdict": "Correct."},
    {"key": "2", "adjustment": 0, "reasons": [], "verdict": "Bon état."},
]})
SAMPLING = ("temperature", "top_p", "top_k", "fallbacks", "cache_control", "metadata", "stop_sequences")


def usage(inp=1200, out=300, thinking=None):
    details = SimpleNamespace(thinking_tokens=thinking) if thinking is not None else None
    return SimpleNamespace(input_tokens=inp, output_tokens=out, output_tokens_details=details)


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def thinking_block():
    return SimpleNamespace(type="thinking", thinking="", signature="sig")  # display omitted by default


def message(text=GOOD, *, stop="end_turn", category=None, use=None, model="claude-haiku-5-5", blocks=None):
    content = blocks if blocks is not None else [thinking_block(), text_block(text)]
    details = SimpleNamespace(type="refusal", category=category, explanation=None) if stop == "refusal" else None
    return SimpleNamespace(model=model, usage=usage() if use is None else use, content=content,
                           stop_reason=stop, stop_details=details)


class ApiError(Exception):
    """Duck-typed stand-in for anthropic.APIStatusError (status_code, type, api_message, error_code)."""

    def __init__(self, status, *, type=None, message="error", error_code=None):
        super().__init__(message)
        self.status_code, self.type, self.message, self.api_message = status, type, message, message
        self.error_code = error_code


class FakeAnthropicSDK:
    def __init__(self, *outcomes):
        self.outcomes, self.requests, self.closed = list(outcomes), [], False
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


def make(*outcomes, effort="low"):
    sdk = FakeAnthropicSDK(*outcomes)
    return AnthropicClient(KEY, "claude-haiku-5-5", reasoning_effort=effort, sdk_client=sdk), sdk


# ------------------------------------------------------------------ request shape

def test_default_request_uses_structured_output_and_adaptive_thinking_at_low_effort():
    client, sdk = make(message(use=usage(900, 450, thinking=200)))
    batch = client.review(PAYLOADS)
    req = sdk.requests[0]
    assert client.provider == "anthropic"
    assert req["model"] == "claude-haiku-5-5" and req["system"] == SYSTEM_PROMPT
    assert req["output_config"] == {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}, "effort": "low"}
    assert req["thinking"] == {"type": "adaptive"}
    assert req["max_tokens"] == 400 * 2 + 3000
    assert req["timeout"] == 120.0  # explicit per-request timeout
    assert len(req["messages"]) == 1 and req["messages"][0]["role"] == "user"  # no assistant prefill
    assert "<DATA>" in req["messages"][0]["content"]
    assert not set(SAMPLING) & set(req)
    assert batch.reviews["1"].adjustment == -2 and batch.reviews["2"].verdict == "Bon état."
    assert (batch.tokens_in, batch.tokens_out, batch.tokens_reasoning) == (900, 450, 200)
    assert batch.model == "claude-haiku-5-5"


def test_effort_none_turns_thinking_off_at_api_effort_low():
    client, sdk = make(message(), effort="none")
    client.review(PAYLOADS)
    req = sdk.requests[0]
    assert req["thinking"] == {"type": "disabled"} and req["output_config"]["effort"] == "low"
    assert req["max_tokens"] == 400 * 2


@pytest.mark.parametrize("effort,room", [("medium", 6000), ("high", 10000)])
def test_higher_efforts_get_more_thinking_room(effort, room):
    client, sdk = make(message(), effort=effort)
    client.review(PAYLOADS)
    req = sdk.requests[0]
    assert req["thinking"] == {"type": "adaptive"} and req["output_config"]["effort"] == effort
    assert req["max_tokens"] == 400 * 2 + room


def test_max_tokens_stays_under_the_non_streaming_ceiling():
    client, sdk = make(message(), effort="high")
    client.review([{"key": str(i)} for i in range(40)])
    assert sdk.requests[0]["max_tokens"] == 16_000
    assert sdk.requests[0]["timeout"] == 16_000 / 40


@pytest.mark.parametrize("effort", ["off", "minimal", "xhigh", "max"])
def test_efforts_claude_does_not_take_are_config_errors(effort):
    with pytest.raises(LLMConfigError, match="LLM_REASONING_EFFORT"):
        AnthropicClient(KEY, "claude-haiku-5-5", reasoning_effort=effort, sdk_client=FakeAnthropicSDK())


# ------------------------------------------------------------------ answers

def test_text_is_read_by_block_type_and_joined():
    half = len(GOOD) // 2
    blocks = [thinking_block(), text_block(GOOD[:half]), SimpleNamespace(type="redacted_thinking", data="x"),
              text_block(GOOD[half:])]
    client, _ = make(message(blocks=blocks))
    assert set(client.review(PAYLOADS).reviews) == {"1", "2"}


@pytest.mark.parametrize("answer,hint", [
    (message(stop="refusal", category="cyber"), "category cyber"),
    (message(stop="refusal"), "refusal"),
    (message(stop="max_tokens"), "LLM_REASONING_EFFORT"),
    (message(blocks=[thinking_block()]), "no text"),
    (message(stop="tool_use"), "tool_use"),
    (message(stop="pause_turn"), "pause_turn"),
    (message(stop="model_context_window_exceeded"), "model_context_window_exceeded"),
    (message(text="{broken"), "Invalid JSON"),
])
def test_unusable_answers_are_batch_errors(answer, hint):
    client, _ = make(answer)
    with pytest.raises(LLMBatchError, match=hint):
        client.review(PAYLOADS)


def test_cut_off_answer_without_thinking_gives_no_effort_hint():
    client, _ = make(message(stop="max_tokens"), effort="none")
    with pytest.raises(LLMBatchError) as info:
        client.review(PAYLOADS)
    assert "LLM_REASONING_EFFORT" not in str(info.value)


def test_failed_answer_keeps_its_billed_usage():
    client, _ = make(message(stop="max_tokens", use=usage(900, 3800, thinking=3500)))
    with pytest.raises(LLMBatchError) as info:
        client.review(PAYLOADS)
    assert (info.value.tokens_in, info.value.tokens_out, info.value.tokens_reasoning) == (900, 3800, 3500)


def test_missing_usage_and_model_still_return_the_paid_reviews():
    answer = SimpleNamespace(model=None, usage=None, content=[text_block(GOOD)], stop_reason=None)
    client, _ = make(answer)
    batch = client.review(PAYLOADS)
    assert set(batch.reviews) == {"1", "2"} and batch.tokens_in is None and batch.model == "claude-haiku-5-5"


# ------------------------------------------------------------------ error mapping

@pytest.mark.parametrize("error,expected,hint", [
    (ApiError(401, type="authentication_error"), LLMConfigError, "ANTHROPIC_API_KEY"),
    (ApiError(402, type="billing_error"), LLMConfigError, "Claude Console"),
    (ApiError(400, type="invalid_request_error", message="Your credit balance is too low"), LLMConfigError, "credits"),
    (ApiError(429, type="rate_limit_error", error_code="enforced_spend_limit_reached"), LLMConfigError, "limit"),
    (ApiError(403, type="permission_error", error_code="billing_frozen"), LLMConfigError, "billing"),
    (ApiError(403, type="permission_error"), LLMConfigError, "403"),
    (ApiError(404, type="not_found_error", message="model: claude-x"), LLMConfigError, "LLM_MODEL"),
    (ApiError(413, type="request_too_large"), LLMBatchError, "too large"),
    (ApiError(400, message="prompt is too long: 1200000 tokens > 1000000 maximum"), LLMBatchError, "batch"),
    (ApiError(400, message='"thinking.type.disabled" is not supported'), LLMConfigError, "LLM_REASONING_EFFORT"),
    (ApiError(400, message="output_config.effort: invalid"), LLMConfigError, "LLM_REASONING_EFFORT"),
    (ApiError(400, message="output_config.format.schema: invalid"), LLMConfigError, "HTTP 400"),
    (ApiError(422), LLMConfigError, "422"),
    (ApiError(429, type="rate_limit_error"), LLMTransientError, "rate limit"),
    (ApiError(500, type="api_error"), LLMTransientError, "500"),
    (ApiError(529, type="overloaded_error"), LLMTransientError, "overloaded"),
    (ApiError(408), LLMTransientError, "408"),
    (type("APITimeoutError", (Exception,), {})("Request timed out."), LLMBatchError, "in time"),
    (TypeError("create() got an unexpected keyword argument 'output_config'"), LLMConfigError, "too old"),
    (ConnectionError("network unreachable"), LLMTransientError, "ConnectionError"),
])
def test_errors_are_mapped(error, expected, hint):
    client, _ = make(error)
    with pytest.raises(expected, match=hint) as info:
        client.review(PAYLOADS)
    if expected is LLMTransientError:
        assert not isinstance(info.value, LLMBatchError)
    assert KEY not in str(info.value)


def test_error_type_is_read_from_the_body_when_the_attribute_is_missing():
    exc = Exception("Error code: 402")
    exc.status_code, exc.body = 400, {"type": "error", "error": {"type": "billing_error", "message": "no credit"}}
    client, _ = make(exc)
    with pytest.raises(LLMConfigError, match="billing"):
        client.review(PAYLOADS)


# ------------------------------------------------------------------ construction

@pytest.mark.parametrize("key", ["", '"sk-ant-abc"', "sk-ant abc", "sk-ant-abcé"])
def test_bad_keys_are_config_errors(key):
    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY") as info:
        AnthropicClient(key, "claude-haiku-5-5", sdk_client=FakeAnthropicSDK())
    if key:
        assert key not in str(info.value)  # never echo the key


def test_a_key_of_the_other_provider_gets_a_note():
    client = AnthropicClient("sk-proj-openai", "claude-haiku-5-5", sdk_client=FakeAnthropicSDK())
    assert any("sk-ant-" in n and "OpenAI" in n for n in client.notes)
    assert not any("sk-proj-openai" in n for n in client.notes)


def test_missing_sdk_is_a_config_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(LLMConfigError, match="pip install"):
        AnthropicClient(KEY, "claude-haiku-5-5")


def test_sdk_environment_variables_are_reported(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Test: 1")
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-secret")
    client = AnthropicClient(KEY, "claude-haiku-5-5", sdk_client=FakeAnthropicSDK())
    applied = next(n for n in client.notes if "applies it" in n)
    assert "ANTHROPIC_CUSTOM_HEADERS" in applied and "ANTHROPIC_LOG" in applied
    assert any("ANTHROPIC_AUTH_TOKEN" in n and "ignored" in n for n in client.notes)
    assert "tok-secret" not in " ".join(client.notes)


def test_close_closes_the_sdk_client():
    client, sdk = make()
    client.close()
    assert sdk.closed


# ------------------------------------------------------------------ make_client

def _settings(tmp_path, **changes):
    return Settings.from_env(data_dir=tmp_path).replace(**changes)


def test_make_client_builds_the_claude_client_by_default(tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(llm_anthropic, "AnthropicClient",
                        lambda key, model, *, reasoning_effort: built.append((key, model, reasoning_effort)) or "c")
    assert make_client(_settings(tmp_path, anthropic_api_key=KEY, openai_api_key="sk-x")) == "c"
    assert built == [(KEY, "claude-haiku-5-5", "low")]


def test_make_client_builds_the_openai_client_when_chosen(tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(llm_openai, "OpenAIClient",
                        lambda key, model, *, reasoning_effort: built.append((key, model, reasoning_effort)) or "o")
    s = Settings.from_env(data_dir=tmp_path, llm_provider="openai").replace(openai_api_key="sk-x",
                                                                           anthropic_api_key=KEY)
    assert make_client(s) == "o" and built == [("sk-x", "gpt-6-luna", "none")]


def test_make_client_never_imports_an_sdk_it_does_not_need(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(LLMConfigError, match="anthropic package is not installed"):
        make_client(_settings(tmp_path, anthropic_api_key=KEY))


# ------------------------------------------------------------------ contract with the real SDK (offline)

@pytest.fixture(autouse=False)
def real_sdk():
    """The installed anthropic SDK (these tests skip when it is absent)."""
    pytest.importorskip("anthropic")
    return pytest.importorskip("httpx2")


class Recorder:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request):
        import httpx2

        self.requests.append(request)
        status, body = self.responses.pop(0)
        return httpx2.Response(status, json=body)


def _real_client(handler, **kw):
    import httpx2

    return AnthropicClient(KEY, "claude-haiku-5-5", max_retries=0,
                           http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **kw)


def _ok_body(stop="end_turn", text=GOOD, stop_details=None):
    return {
        "id": "msg_01", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "c2ln"}, {"type": "text", "text": text}],
        "stop_reason": stop, "stop_sequence": None, "stop_details": stop_details,
        "usage": {"input_tokens": 900, "output_tokens": 420, "output_tokens_details": {"thinking_tokens": 250}},
    }


def _error_body(type_, message, error_code=None):
    error = {"type": type_, "message": message}
    if error_code:
        error["details"] = {"error_code": error_code}
    return {"type": "error", "error": error, "request_id": "req_01"}


def test_real_sdk_sends_the_expected_request(real_sdk, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")  # must be ignored
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-should-not-be-sent")  # ignored with an explicit key
    handler = Recorder((200, _ok_body()))
    client = _real_client(handler)
    try:
        batch = client.review(PAYLOADS)
    finally:
        client.close()
    request = handler.requests[0]
    assert str(request.url) == f"{ANTHROPIC_BASE_URL_FIXED}/v1/messages"
    auth = request.headers.get("authorization") or ""
    assert auth == f"Bearer {KEY}" or request.headers.get("x-api-key") == KEY
    assert "tok-should-not-be-sent" not in str(request.headers)
    assert request.headers.get("anthropic-version")  # value set by the SDK major (2026-10-21 on 2.x)
    body = json.loads(request.content)
    assert body["model"] == "claude-haiku-5-5" and body["system"] == SYSTEM_PROMPT
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}, "effort": "low"}
    assert body["messages"][-1]["role"] == "user" and not set(SAMPLING) & set(body)
    assert batch.reviews["1"].adjustment == -2 and batch.model == "claude-haiku-5-5"
    assert (batch.tokens_in, batch.tokens_out, batch.tokens_reasoning) == (900, 420, 250)


def test_real_sdk_billing_error_is_a_config_error(real_sdk):
    client = _real_client(Recorder((402, _error_body("billing_error", "Your credit balance is too low."))))
    try:
        with pytest.raises(LLMConfigError, match="Claude Console"):
            client.review(PAYLOADS)
    finally:
        client.close()


def test_real_sdk_spend_limit_is_a_config_error_not_a_retry(real_sdk):
    body = _error_body("rate_limit_error", "Spend limit reached.", "enforced_spend_limit_reached")
    client = _real_client(Recorder((429, body)))
    try:
        with pytest.raises(LLMConfigError, match="limit"):
            client.review(PAYLOADS)
    finally:
        client.close()


def test_real_sdk_overload_is_transient_after_one_attempt(real_sdk):
    handler = Recorder((529, _error_body("overloaded_error", "Overloaded")))
    client = _real_client(handler)
    try:
        with pytest.raises(LLMTransientError, match="overloaded") as info:
            client.review(PAYLOADS)
    finally:
        client.close()
    assert not isinstance(info.value, LLMBatchError) and len(handler.requests) == 1


def test_real_sdk_timeout_is_a_batch_error(real_sdk):
    attempts = []

    def handler(request):
        attempts.append(request)
        raise real_sdk.ReadTimeout("timed out", request=request)

    client = _real_client(handler)
    try:
        with pytest.raises(LLMBatchError, match="in time"):
            client.review(PAYLOADS)
    finally:
        client.close()
    assert len(attempts) == 1


def test_real_sdk_refusal_is_a_batch_error(real_sdk):
    details = {"type": "refusal", "category": "general_harms", "explanation": None}
    client = _real_client(Recorder((200, _ok_body(stop="refusal", text="", stop_details=details))))
    try:
        with pytest.raises(LLMBatchError, match="category general_harms") as info:
            client.review(PAYLOADS)
    finally:
        client.close()
    assert info.value.tokens_in == 900


def test_real_sdk_custom_headers_from_the_environment_are_applied_and_noted(real_sdk, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Test-Header: yes")
    handler = Recorder((200, _ok_body()))
    client = _real_client(handler)
    try:
        client.review(PAYLOADS)
    finally:
        client.close()
    assert handler.requests[0].headers.get("x-test-header") == "yes"
    assert any("ANTHROPIC_CUSTOM_HEADERS" in n for n in client.notes)


def test_real_sdk_default_client_is_pinned_and_retries_twice(real_sdk):
    client = AnthropicClient(KEY, "claude-haiku-5-5")
    try:
        assert client._client.max_retries == 2
        assert str(client._client.base_url).rstrip("/") == ANTHROPIC_BASE_URL_FIXED
    finally:
        client.close()
