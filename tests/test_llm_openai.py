"""OpenAI review client: request shape, error mapping, fallbacks, and the batch-failure policy.

No test reaches the network: unit tests use a fake SDK object, and the contract tests
run the real openai SDK against an in-memory httpx2 transport.
"""
import json
import sys
from types import SimpleNamespace

import pytest

from auction_agent import db
from auction_agent.scoring.llm import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    LLMBatchError,
    LLMConfigError,
    LLMTransientError,
    OpenAIClient,
    ReviewBatch,
    parse_reviews,
)
from auction_agent.scoring.service import rescore_all
from tests.test_scoring import TODAY
from tests.test_service_report import NOW, FakeClient, llm_settings, seed

PAYLOADS = [{"key": "1", "brand": "TOYOTA"}, {"key": "2", "brand": "PEUGEOT"}]
GOOD = json.dumps({"items": [
    {"key": "1", "adjustment": -2, "reasons": ["Boîte à surveiller"], "verdict": "Correct."},
    {"key": "2", "adjustment": 0, "reasons": [], "verdict": "Bon état."},
]})


def usage(prompt=1200, completion=300, reasoning=0):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                           completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning))


def completion(content=GOOD, *, finish="stop", refusal=None, use=usage(), model="gpt-6-luna-2026-09-22"):
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(model=model, usage=use, choices=[SimpleNamespace(finish_reason=finish, message=message)])


class ApiError(Exception):
    """Duck-typed stand-in for openai.APIStatusError (status_code, type, code, param)."""

    def __init__(self, status, *, type=None, code=None, param=None, message="error"):
        super().__init__(message)
        self.status_code, self.type, self.code, self.param, self.message = status, type, code, param, message


class FakeSDK:
    def __init__(self, *outcomes):
        self.outcomes, self.requests = list(outcomes), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make(*outcomes, effort="none"):
    sdk = FakeSDK(*outcomes)
    return OpenAIClient("sk-test", "gpt-6-luna", reasoning_effort=effort, sdk_client=sdk), sdk


# ------------------------------------------------------------------ request shape and parsing

def test_request_is_strict_unstored_and_cheap():
    client, sdk = make(completion())
    batch = client.review(PAYLOADS)
    req = sdk.requests[0]
    assert req["model"] == "gpt-6-luna" and req["store"] is False
    assert req["reasoning_effort"] == "none" and req["temperature"] == 0
    assert req["prompt_cache_options"] == {"mode": "explicit"}
    assert req["max_completion_tokens"] == 300 * 2 + 1000
    assert req["timeout"] == 120.0
    assert req["messages"][0] == {"role": "developer", "content": SYSTEM_PROMPT}
    fmt = req["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] is RESPONSE_SCHEMA
    assert "<DATA>" in req["messages"][1]["content"]
    assert batch.reviews["1"].adjustment == -2 and batch.reviews["2"].verdict == "Bon état."
    assert (batch.tokens_in, batch.tokens_out, batch.tokens_reasoning) == (1200, 300, 0)  # no double count
    assert batch.model == "gpt-6-luna-2026-09-22"


def test_higher_effort_drops_temperature_and_raises_the_token_budget():
    client, sdk = make(completion(), effort="low")
    client.review(PAYLOADS)
    req = sdk.requests[0]
    assert req["reasoning_effort"] == "low" and "temperature" not in req
    assert req["max_completion_tokens"] == 1200 * 2 + 1000
    assert req["timeout"] == (1200 * 2 + 1000) / 20  # long answers get more time


def test_effort_off_supports_models_without_reasoning():
    client, sdk = make(completion(), effort="off")
    client.review(PAYLOADS)
    req = sdk.requests[0]
    assert "reasoning_effort" not in req and req["temperature"] == 0
    assert req["max_completion_tokens"] == 300 * 2 + 1000


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_schema_follows_strict_mode_rules():
    objects = [n for n in _walk(RESPONSE_SCHEMA) if n.get("type") == "object"]
    assert len(objects) == 2
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    banned = {"minimum", "maximum", "minItems", "maxItems"}
    assert not any(banned & set(n) for n in _walk(RESPONSE_SCHEMA))


def test_usage_missing_still_returns_the_paid_reviews():
    client, _ = make(completion(use=None, model=None))
    batch = client.review(PAYLOADS)
    assert set(batch.reviews) == {"1", "2"} and batch.tokens_out is None and batch.model == "gpt-6-luna"


def test_trailing_junk_after_json_is_tolerated():
    assert set(parse_reviews(GOOD + "\n}}garbage", {"1", "2"})) == {"1", "2"}
    with pytest.raises(LLMBatchError):
        parse_reviews("not json", {"1"})


def test_out_of_spec_values_are_neutralised():
    raw = '{"items": [{"key": " 1 ", "adjustment": 1e400, "reasons": "Coucou", "verdict": "ok"}]}'
    review = parse_reviews(raw, {"1"})["1"]
    assert review.adjustment == 0 and review.reasons == []


def test_failed_answer_keeps_its_billed_usage():
    client, _ = make(completion(finish="length", use=usage(prompt=900, completion=7000, reasoning=6500)), effort="low")
    with pytest.raises(LLMBatchError, match="LLM_REASONING_EFFORT") as info:
        client.review(PAYLOADS)
    assert (info.value.tokens_in, info.value.tokens_out, info.value.tokens_reasoning) == (900, 7000, 6500)


@pytest.mark.parametrize("answer", [
    completion(refusal="I can't help with that."),
    completion(finish="length"),
    completion(finish="content_filter"),
    completion(content="{broken"),
    SimpleNamespace(model="m", usage=None, choices=[]),
])
def test_unusable_answers_are_batch_errors(answer):
    client, _ = make(answer)
    with pytest.raises(LLMBatchError):
        client.review(PAYLOADS)


# ------------------------------------------------------------------ error mapping

@pytest.mark.parametrize("error,expected,hint", [
    (ApiError(401), LLMConfigError, "OPENAI_API_KEY"),
    (ApiError(403), LLMConfigError, "403"),
    (ApiError(404), LLMConfigError, "LLM_MODEL"),
    (ApiError(429, type="insufficient_quota", code="insufficient_quota"), LLMConfigError, "credits"),
    (ApiError(429, code="project_spend_limit_exceeded"), LLMConfigError, "limit"),
    (ApiError(429, code="organization_spend_limit_exceeded"), LLMConfigError, "limit"),
    (ApiError(429, code="organization_usage_limit_exceeded"), LLMConfigError, "limit"),
    (ApiError(429, code="billing_not_active"), LLMConfigError, "credits"),
    (ApiError(429, code="credit_balance_exhausted"), LLMConfigError, "credits"),
    (ApiError(400, code="unsupported_parameter", message="Unknown parameter"), LLMConfigError, "the request"),
    (ApiError(400, param="response_format", message="Invalid schema"), LLMConfigError, "response_format"),
    (ApiError(422), LLMConfigError, "422"),
    (ApiError(400, code="invalid_prompt", message="flagged"), LLMBatchError, "batch"),
    (ApiError(400, code="invalid_prompt", param="input", message="flagged"), LLMBatchError, "batch"),
    (ApiError(400, code="context_length_exceeded", param="messages"), LLMBatchError, "batch"),
    (ApiError(429, type="requests", code="rate_limit_exceeded"), LLMTransientError, "rate limit"),
    (ApiError(500), LLMTransientError, "500"),
    (ApiError(503), LLMTransientError, "503"),
    (TimeoutError("read timed out"), LLMTransientError, "TimeoutError"),
    (type("APITimeoutError", (Exception,), {})("Request timed out."), LLMBatchError, "in time"),
    (TypeError("create() got an unexpected keyword argument 'prompt_cache_options'"), LLMConfigError, "too old"),
])
def test_errors_are_mapped(error, expected, hint):
    client, _ = make(error)
    with pytest.raises(expected, match=hint) as info:
        client.review(PAYLOADS)
    if expected is LLMTransientError:
        assert not isinstance(info.value, LLMBatchError)
    assert "sk-test" not in str(info.value)


@pytest.mark.parametrize("param", ["temperature", "prompt_cache_options"])
def test_rejected_optional_parameter_is_dropped_once(param):
    client, sdk = make(ApiError(400, param=param, message=f"Unsupported parameter: '{param}'"), completion())
    assert client.review(PAYLOADS).reviews
    assert param in sdk.requests[0] and param not in sdk.requests[1]
    sdk.outcomes.append(completion())
    client.review(PAYLOADS)
    assert param not in sdk.requests[2]  # remembered for the rest of the run


def test_rejected_parameter_named_only_in_the_message_is_dropped_and_noted():
    client, sdk = make(ApiError(400, param=None, message="Unsupported parameter: 'prompt_cache_options'"), completion())
    assert client.review(PAYLOADS).reviews and "prompt_cache_options" not in sdk.requests[1]
    assert any("prompt caching" in n for n in client.notes)


def test_parameter_fallback_is_bounded():
    client, sdk = make(ApiError(400, param="temperature"), ApiError(400, param="temperature"))
    with pytest.raises(LLMConfigError):
        client.review(PAYLOADS)
    assert len(sdk.requests) == 2


def test_rejected_reasoning_effort_never_falls_back_to_a_costlier_default():
    client, sdk = make(ApiError(400, param="reasoning_effort", message="Unsupported value: 'none'"))
    with pytest.raises(LLMConfigError, match="LLM_REASONING_EFFORT"):
        client.review(PAYLOADS)
    assert len(sdk.requests) == 1


# ------------------------------------------------------------------ construction

@pytest.mark.parametrize("key", ["", '"sk-abc"', "sk-abc def", "sk-abcé"])
def test_bad_keys_are_config_errors(key):
    with pytest.raises(LLMConfigError) as info:
        OpenAIClient(key, "gpt-6-luna", sdk_client=FakeSDK())
    if key:
        assert key not in str(info.value)  # never echo the key


def test_missing_sdk_is_a_config_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)
    with pytest.raises(LLMConfigError, match="pip install"):
        OpenAIClient("sk-test", "gpt-6-luna")


def test_sdk_environment_overrides_are_reported(monkeypatch):
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_x")
    client = OpenAIClient("sk-test", "gpt-6-luna", sdk_client=FakeSDK())
    assert any("OPENAI_PROJECT_ID" in n for n in client.notes)


# ------------------------------------------------------------------ contract with the real SDK (offline)

class Recorder:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request):
        import httpx2

        self.requests.append(request)
        status, body = self.responses.pop(0)
        return httpx2.Response(status, json=body)


def _real_client(handler):
    import httpx2

    return OpenAIClient("sk-test", "gpt-6-luna", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def _ok_body():
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1_760_000_000, "model": "gpt-6-luna-2026-09-22",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": GOOD, "refusal": None}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020,
                  "completion_tokens_details": {"reasoning_tokens": 0}},
    }


def _error_body(message, type_, code, param=None):
    return {"error": {"message": message, "type": type_, "code": code, "param": param}}


def test_real_sdk_sends_the_expected_request(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example/v1")  # must be ignored
    handler = Recorder((200, _ok_body()))
    client = _real_client(handler)
    try:
        batch = client.review(PAYLOADS)
    finally:
        client._client.close()
    request = handler.requests[0]
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "gpt-6-luna" and body["store"] is False and body["reasoning_effort"] == "none"
    assert body["temperature"] == 0 and body["prompt_cache_options"] == {"mode": "explicit"}
    assert body["response_format"]["json_schema"]["strict"] is True
    assert batch.reviews["1"].adjustment == -2 and batch.tokens_in == 900 and batch.model == "gpt-6-luna-2026-09-22"


def test_real_sdk_timeout_is_a_batch_error_and_retries_are_limited():
    import httpx2

    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx2.ReadTimeout("timed out", request=request)

    client = _real_client(handler)
    try:
        with pytest.raises(LLMBatchError, match="in time"):
            client.review(PAYLOADS)
    finally:
        client._client.close()
    assert len(attempts) == 1
    default = OpenAIClient("sk-test", "gpt-6-luna")
    try:
        assert default._client.max_retries == 1  # a timed-out generation is billed: resend at most once
    finally:
        default._client.close()


def test_real_sdk_quota_error_is_a_config_error():
    handler = Recorder((429, _error_body("You exceeded your current quota", "insufficient_quota", "insufficient_quota")))
    client = _real_client(handler)
    try:
        with pytest.raises(LLMConfigError, match="credits"):
            client.review(PAYLOADS)
    finally:
        client._client.close()


def test_real_sdk_rejected_parameter_is_dropped_and_retried():
    handler = Recorder(
        (400, _error_body("Unsupported parameter: 'temperature'", "invalid_request_error",
                          "unsupported_parameter", "temperature")),
        (200, _ok_body()),
    )
    client = _real_client(handler)
    try:
        assert client.review(PAYLOADS).reviews
    finally:
        client._client.close()
    first, second = (json.loads(r.content) for r in handler.requests)
    assert "temperature" in first and "temperature" not in second


# ------------------------------------------------------------------ service: batch failures and accounting

class BatchFailClient(FakeClient):
    """Fails every batch that contains one of the given payload keys."""

    def __init__(self, bad_keys, **kw):
        super().__init__(**kw)
        self.bad_keys = set(bad_keys)

    def review(self, payloads):
        if self.bad_keys & {p["key"] for p in payloads}:
            self.calls.append(payloads)
            raise LLMBatchError("The model refused to review this batch.")
        return super().review(payloads)


def _rows(conn, status):
    return conn.execute("SELECT vehicle_id, llm_input_hash FROM llm_assessments WHERE status = ?", (status,)).fetchall()


def _probe_keys(conn, s):
    probe = FakeClient()
    rescore_all(conn, s, today=TODAY, now=NOW, client=probe)
    conn.execute("DELETE FROM llm_assessments")
    return [p["key"] for batch in probe.calls for p in batch], len(probe.calls)


def test_a_failed_batch_does_not_stop_the_others_and_is_retried_alone(settings):
    s = llm_settings(settings).replace(llm_batch_size=2)
    with db.open_db(s.db_path) as conn:
        seed(conn)
        keys, n_batches = _probe_keys(conn, s)
        assert n_batches >= 2, "the seed must trigger at least two batches"

        first = BatchFailClient({keys[0]})
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=first)
        assert len(first.calls) == n_batches  # every batch was still attempted
        assert len(_rows(conn, "failed")) == 2  # the whole first batch is marked failed
        assert any("could not review" in n for n in out["notes"])

        # Next run: each failed car is sent alone; keys[0] fails again, keys[1] succeeds.
        second = BatchFailClient({keys[0]})
        rescore_all(conn, s, today=TODAY, now=NOW, client=second)
        assert sorted(len(c) for c in second.calls) == [1, 1]
        assert conn.execute("SELECT count(*) FROM llm_assessments WHERE status = 'failed'").fetchone()[0] == 3

        # Third run: keys[0] failed twice on the same data and settings, so it is not sent.
        third = FakeClient()
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=third)
        assert third.calls == [] and any("failed on twice" in n for n in out["notes"])

        # Changing the reasoning effort (the usual fix) gives it a fresh start.
        fourth = FakeClient()
        rescore_all(conn, s.replace(llm_reasoning_effort="low"), today=TODAY, now=NOW, client=fourth)
        assert [[p["key"] for p in c] for c in fourth.calls] == [[keys[0]]]


class OmittingClient(FakeClient):
    """Answers correctly but leaves one car out of the items."""

    def __init__(self, omit, **kw):
        super().__init__(**kw)
        self.omit = omit

    def review(self, payloads):
        batch = super().review(payloads)
        batch.reviews.pop(self.omit, None)
        return batch


def test_a_car_missing_from_the_answer_is_retried_alone_then_given_up(settings):
    s = llm_settings(settings)
    with db.open_db(s.db_path) as conn:
        seed(conn)
        keys, _ = _probe_keys(conn, s)
        lost = keys[0]
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=OmittingClient(lost))
        assert any("no review for 1 vehicle" in n for n in out["notes"])
        again = OmittingClient(lost)
        rescore_all(conn, s, today=TODAY, now=NOW, client=again)
        assert [[p["key"] for p in c] for c in again.calls] == [[lost]]
        last = FakeClient()
        rescore_all(conn, s, today=TODAY, now=NOW, client=last)
        assert last.calls == []


class BilledFailureClient(FakeClient):
    def review(self, payloads):
        self.calls.append(payloads)
        exc = LLMBatchError("The answer was cut off (max_completion_tokens reached).")
        exc.tokens_in, exc.tokens_out, exc.tokens_reasoning = 900, 7000, 6500
        raise exc


def test_usage_of_a_failed_batch_is_recorded(settings):
    s = llm_settings(settings)
    with db.open_db(s.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=BilledFailureClient())
        assert any("6500 hidden reasoning tokens" in n for n in out["notes"])
        rows = conn.execute("SELECT tokens_in, tokens_out FROM llm_assessments WHERE status = 'failed' "
                            "ORDER BY id").fetchall()
        assert tuple(rows[0]) == (900, 7000) and all(r[0] is None for r in rows[1:])


def test_client_is_built_from_settings(settings, monkeypatch):
    built = []

    class Recording(FakeClient):
        def __init__(self, api_key, model, *, reasoning_effort):
            super().__init__()
            built.append((api_key, model, reasoning_effort))
            self.model, self.reasoning_effort, self.notes = model, reasoning_effort, ["env note"]

    monkeypatch.setattr("auction_agent.scoring.service.OpenAIClient", Recording)
    s = settings.replace(openai_api_key="sk-x", llm_model="gpt-5.4-nano", llm_reasoning_effort="low")
    with db.open_db(s.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, s, today=TODAY, now=NOW)
    assert built == [("sk-x", "gpt-5.4-nano", "low")] and "env note" in out["notes"]


def test_malformed_key_falls_back_to_rules(settings):
    s = settings.replace(openai_api_key="sk bad key")
    with db.open_db(s.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, s, today=TODAY, now=NOW)
        assert any("malformed" in n for n in out["notes"]) and "sk bad key" not in " ".join(out["notes"])
        assert conn.execute("SELECT count(*) FROM assessments").fetchone()[0] == 8


class ReasoningClient(FakeClient):
    def review(self, payloads):
        batch = super().review(payloads)
        return ReviewBatch(batch.reviews, tokens_in=100, tokens_out=600, tokens_reasoning=500,
                           model="gpt-6-luna-2026-09-22")


def test_reasoning_tokens_and_answering_model_are_recorded(settings):
    s = llm_settings(settings)
    with db.open_db(s.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=ReasoningClient())
        assert any("hidden reasoning tokens" in n for n in out["notes"])
        models = {r[0] for r in conn.execute("SELECT llm_model FROM llm_assessments")}
        assert models == {"gpt-6-luna-2026-09-22"}
