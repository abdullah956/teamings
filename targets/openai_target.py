import logging
import time

import openai
from dotenv import load_dotenv

from .base import Target

load_dotenv()

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Why temperature=0 (read this before changing it)
# -----------------------------------------------------------------------------
# This is the single most important reproducibility decision in the project.
#
# An evaluation suite is only useful if the same attack against the same model
# produces the same response across runs. Otherwise pass rates are noise, not
# signal: a flaky attack might "succeed" 30% of the time purely from sampling
# variance, and you'd never know whether a config change or model upgrade
# actually moved the number.
#
# temperature=0 makes OpenAI's sampler greedy (always pick the highest-prob
# token), which is the closest thing the API offers to determinism. It is
# *not* perfectly deterministic — backend routing, version drift, and tied
# logits can still cause occasional divergence — but it removes the dominant
# source of run-to-run variance.
#
# If you raise temperature later (e.g. to study refusal robustness under
# stochastic decoding), do it explicitly with N>1 samples per attack and
# report mean +/- variance. Do NOT silently flip this default.
# -----------------------------------------------------------------------------


class OpenAITarget(Target):
    provider = "openai"

    def __init__(self, model_name: str = "gpt-4o-mini"):
        super().__init__(model_name)
        self._client = openai.OpenAI()

    @classmethod
    def gpt4o_mini(cls) -> "OpenAITarget":
        return cls("gpt-4o-mini")

    @classmethod
    def gpt35_turbo(cls) -> "OpenAITarget":
        return cls("gpt-3.5-turbo")

    def query(self, prompt: str, system: str | None = None) -> str:
        messages: list[dict[str, str]] = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        backoffs = [2, 4, 8]
        last_exc: Exception | None = None
        for attempt in range(3):
            t0 = time.monotonic()
            try:
                resp = self._client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    temperature=0,
                    max_tokens=1024,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)
                content = resp.choices[0].message.content or ""
                logger.debug(
                    "openai call: model=%s prompt_chars=%d response_chars=%d latency_ms=%d retry_count=%d",
                    self.model_name,
                    len(prompt),
                    len(content),
                    latency_ms,
                    attempt,
                )
                return content
            except (openai.RateLimitError, openai.APIConnectionError) as e:
                last_exc = e
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.debug(
                    "openai retryable error: model=%s prompt_chars=%d latency_ms=%d retry_count=%d err=%s",
                    self.model_name,
                    len(prompt),
                    latency_ms,
                    attempt,
                    type(e).__name__,
                )
                if attempt < 2:
                    time.sleep(backoffs[attempt])
                    continue
                raise
        assert last_exc is not None
        raise last_exc
