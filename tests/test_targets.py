"""Target adapter tests.

These tests exercise the retry logic and credential-handling shape of
each adapter. They MUST NOT make real API calls — every external client
is patched. The point of the suite is to assert behavior under simulated
failure modes (cold-start 503, rate-limit 429, timeout, missing token).
"""

import os
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest
from huggingface_hub.errors import HfHubHTTPError, InferenceTimeoutError

from targets.hf_inference_target import HFInferenceTarget, _MissingHFTokenError
from targets.openai_target import OpenAITarget


# ---------------------------------------------------------------------------
# OpenAI adapter
# ---------------------------------------------------------------------------


def _make_openai_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    return resp


def _make_rate_limit_error() -> openai.RateLimitError:
    return openai.RateLimitError(
        "rate limited",
        response=httpx.Response(status_code=429, request=httpx.Request("POST", "http://x")),
        body=None,
    )


def test_openai_factory_methods_set_model_name():
    with patch("targets.openai_target.openai.OpenAI"):
        mini = OpenAITarget.gpt4o_mini()
        old = OpenAITarget.gpt35_turbo()
    assert mini.model_name == "gpt-4o-mini"
    assert old.model_name == "gpt-3.5-turbo"


def test_openai_arbitrary_model_name_accepted():
    with patch("targets.openai_target.openai.OpenAI"):
        t = OpenAITarget("gpt-3.5-turbo")
    assert t.model_name == "gpt-3.5-turbo"


def test_openai_retries_on_rate_limit_then_succeeds():
    with patch("targets.openai_target.openai.OpenAI") as cls, patch(
        "targets.openai_target.time.sleep"
    ) as sleep_mock:
        client = cls.return_value
        client.chat.completions.create.side_effect = [
            _make_rate_limit_error(),
            _make_openai_response("ok"),
        ]
        t = OpenAITarget("gpt-4o-mini")
        out = t.query("hi")
    assert out == "ok"
    assert client.chat.completions.create.call_count == 2
    sleep_mock.assert_called()  # backed off at least once


def test_openai_gives_up_after_three_rate_limits():
    with patch("targets.openai_target.openai.OpenAI") as cls, patch(
        "targets.openai_target.time.sleep"
    ):
        client = cls.return_value
        client.chat.completions.create.side_effect = [
            _make_rate_limit_error(),
            _make_rate_limit_error(),
            _make_rate_limit_error(),
        ]
        t = OpenAITarget("gpt-4o-mini")
        with pytest.raises(openai.RateLimitError):
            t.query("hi")
        assert client.chat.completions.create.call_count == 3


# ---------------------------------------------------------------------------
# HF Inference adapter
# ---------------------------------------------------------------------------


def _make_hf_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    return resp


def _make_hf_http_error(status: int, msg: str) -> HfHubHTTPError:
    fake_response = MagicMock()
    fake_response.status_code = status
    fake_response.headers = {}
    err = HfHubHTTPError(msg, response=fake_response)
    return err


def test_hf_target_raises_clear_error_when_token_missing():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(_MissingHFTokenError) as excinfo:
            HFInferenceTarget()
        assert "HF_TOKEN" in str(excinfo.value)


def test_hf_target_raises_specific_subclass_not_keyerror():
    # The runner catches _MissingHFTokenError specifically; a raw KeyError
    # would crash the run instead of producing the friendly skip message.
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(_MissingHFTokenError):
            HFInferenceTarget()


def test_hf_retries_on_503_loading_then_succeeds():
    with patch.dict(os.environ, {"HF_TOKEN": "fake"}), patch(
        "targets.hf_inference_target.InferenceClient"
    ) as cls, patch("targets.hf_inference_target.time.sleep") as sleep_mock:
        client = cls.return_value
        client.chat_completion.side_effect = [
            _make_hf_http_error(503, "Model is currently loading"),
            _make_hf_response("ok"),
        ]
        t = HFInferenceTarget()
        out = t.query("hi")
    assert out == "ok"
    assert client.chat_completion.call_count == 2
    # 60s cold-start wait should have been requested
    sleep_mock.assert_any_call(60)


def test_hf_retries_on_429_with_exponential_backoff():
    with patch.dict(os.environ, {"HF_TOKEN": "fake"}), patch(
        "targets.hf_inference_target.InferenceClient"
    ) as cls, patch("targets.hf_inference_target.time.sleep") as sleep_mock:
        client = cls.return_value
        client.chat_completion.side_effect = [
            _make_hf_http_error(429, "rate limit exceeded"),
            _make_hf_http_error(429, "rate limit exceeded"),
            _make_hf_response("ok"),
        ]
        t = HFInferenceTarget()
        out = t.query("hi")
    assert out == "ok"
    assert client.chat_completion.call_count == 3
    sleep_mock.assert_any_call(5)
    sleep_mock.assert_any_call(15)


def test_hf_retries_on_timeout():
    with patch.dict(os.environ, {"HF_TOKEN": "fake"}), patch(
        "targets.hf_inference_target.InferenceClient"
    ) as cls, patch("targets.hf_inference_target.time.sleep"):
        client = cls.return_value
        client.chat_completion.side_effect = [
            InferenceTimeoutError("timed out"),
            _make_hf_response("ok"),
        ]
        t = HFInferenceTarget()
        out = t.query("hi")
    assert out == "ok"
    assert client.chat_completion.call_count == 2


def test_hf_does_not_retry_on_unrelated_error():
    # 401 / 403 / 400 etc. should bubble up immediately — retrying won't
    # fix a bad token or a bad request, and burning the loop on them
    # wastes time that should fail fast.
    with patch.dict(os.environ, {"HF_TOKEN": "fake"}), patch(
        "targets.hf_inference_target.InferenceClient"
    ) as cls, patch("targets.hf_inference_target.time.sleep"):
        client = cls.return_value
        client.chat_completion.side_effect = _make_hf_http_error(401, "bad token")
        t = HFInferenceTarget()
        with pytest.raises(HfHubHTTPError):
            t.query("hi")
        assert client.chat_completion.call_count == 1


def test_hf_gives_up_after_max_cold_start_retries():
    with patch.dict(os.environ, {"HF_TOKEN": "fake"}), patch(
        "targets.hf_inference_target.InferenceClient"
    ) as cls, patch("targets.hf_inference_target.time.sleep"):
        client = cls.return_value
        # 1 initial + 2 retries = 3 calls before giving up
        client.chat_completion.side_effect = [
            _make_hf_http_error(503, "Model is loading"),
            _make_hf_http_error(503, "Model is loading"),
            _make_hf_http_error(503, "Model is loading"),
        ]
        t = HFInferenceTarget()
        with pytest.raises(HfHubHTTPError):
            t.query("hi")
        assert client.chat_completion.call_count == 3
