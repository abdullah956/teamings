import logging
import os
import time

from dotenv import load_dotenv
from huggingface_hub import InferenceClient
from huggingface_hub.errors import (
    HfHubHTTPError,
    InferenceTimeoutError,
    OverloadedError,
)

from .base import Target

load_dotenv()

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Why temperature=0 (and not do_sample=False)
# -----------------------------------------------------------------------------
# huggingface_hub.InferenceClient.chat_completion mirrors the OpenAI chat API
# shape (it is in fact aliased as client.chat.completions.create on the same
# object). It accepts `temperature` and `max_tokens` directly — there is no
# `do_sample` flag at this layer; that argument belongs to the lower-level
# transformers/text-generation APIs. We pass temperature=0 for the same
# determinism reason documented in openai_target.py: same attack must yield
# the same response across runs, otherwise pass rates are sampling noise.
# -----------------------------------------------------------------------------


class _MissingHFTokenError(RuntimeError):
    """Raised when HF_TOKEN is required but not present in the environment.

    The runner catches this at registry-build time and skips the target
    with a clear warning instead of crashing the whole run.
    """


class HFInferenceTarget(Target):
    provider = "hf"

    def __init__(self, model_name: str = "Qwen/Qwen2.5-7B-Instruct"):
        super().__init__(model_name)
        token = os.environ.get("HF_TOKEN")
        if not token:
            raise _MissingHFTokenError(
                "HF_TOKEN environment variable is not set. "
                "Get a token from https://huggingface.co/settings/tokens "
                "and add HF_TOKEN=... to your .env file."
            )
        # 90s timeout — the free tier sometimes hangs mid-stream; we'd
        # rather surface that as a retryable timeout than wait forever.
        self._client = InferenceClient(model=model_name, token=token, timeout=90)

    def query(self, prompt: str, system: str | None = None) -> str:
        messages: list[dict[str, str]] = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # Free-tier-aware retry policy:
        #   * cold-start (model loading) — wait 60s, up to 2 retries
        #   * 429 rate limit — exponential 5/15/45s, up to 3 retries
        #   * timeout (hung response) — retry up to 2 times
        #   * anything else — re-raise immediately
        cold_start_retries = 2
        rate_limit_retries = 3
        timeout_retries = 2
        rate_backoffs = [5, 15, 45]

        cold_start_used = 0
        rate_limit_used = 0
        timeout_used = 0
        attempt = 0

        while True:
            t0 = time.monotonic()
            try:
                resp = self._client.chat_completion(
                    messages=messages,
                    temperature=0,
                    max_tokens=1024,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)
                content = resp.choices[0].message.content or ""
                logger.debug(
                    "hf call: model=%s prompt_chars=%d response_chars=%d latency_ms=%d retry_count=%d",
                    self.model_name,
                    len(prompt),
                    len(content),
                    latency_ms,
                    attempt,
                )
                return content
            except InferenceTimeoutError as e:
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.debug(
                    "hf timeout: model=%s latency_ms=%d retry_count=%d err=%s",
                    self.model_name,
                    latency_ms,
                    attempt,
                    type(e).__name__,
                )
                if timeout_used < timeout_retries:
                    timeout_used += 1
                    attempt += 1
                    continue
                raise
            except (HfHubHTTPError, OverloadedError) as e:
                latency_ms = int((time.monotonic() - t0) * 1000)
                status = getattr(getattr(e, "response", None), "status_code", None)
                msg = str(e).lower()
                is_loading = (status == 503) or "loading" in msg or isinstance(e, OverloadedError)
                is_rate_limited = status == 429 or "rate" in msg and "limit" in msg

                logger.debug(
                    "hf http error: model=%s status=%s latency_ms=%d retry_count=%d err=%s",
                    self.model_name,
                    status,
                    latency_ms,
                    attempt,
                    type(e).__name__,
                )

                if is_loading and cold_start_used < cold_start_retries:
                    cold_start_used += 1
                    attempt += 1
                    time.sleep(60)
                    continue
                if is_rate_limited and rate_limit_used < rate_limit_retries:
                    time.sleep(rate_backoffs[rate_limit_used])
                    rate_limit_used += 1
                    attempt += 1
                    continue
                raise
