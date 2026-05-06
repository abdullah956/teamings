from abc import ABC, abstractmethod


class Target(ABC):
    """Abstract adapter for a single LLM provider + model.

    A Target wraps one provider's API and exposes a uniform method (`query`)
    so the runner can iterate over heterogeneous providers without branching
    on type. Concrete subclasses (e.g. OpenAITarget, AnthropicTarget) set
    the class-level `provider` string and implement `query`.
    """

    provider: str = "base"

    def __init__(self, model_name: str):
        """Bind this adapter instance to a specific model id.

        `model_name` is the provider-native identifier (e.g. "gpt-4o-mini",
        "claude-sonnet-4-6"). It is stored verbatim and echoed back via
        `name`, which means it must be filesystem-safe — `name` ends up as
        a CSV column header and may appear in result file paths.
        """
        self.model_name = model_name

    @property
    def name(self) -> str:
        """Stable, filesystem-safe identifier used as the result-CSV column header.

        Format: ``"{provider}/{model_name}"``. Must remain stable across
        runs so historical results join cleanly. Do not embed timestamps,
        request ids, or anything that varies per call.
        """
        return f"{self.provider}/{self.model_name}"

    @abstractmethod
    def query(self, prompt: str, system: str | None = None) -> str:
        """Send `prompt` to the model and return its text response.

        Contract:
          * MUST return a ``str``. Never ``None``. If the provider returns
            an empty completion, return ``""`` so the judge can still run.
          * If `system` is provided, send it as a system-role message
            before the user prompt. If ``None``, send only the user prompt
            — do not invent a default system instruction (that would
            silently change attack semantics).
          * Determinism settings (temperature, top_p, seed) live in the
            subclass, not the caller.

        Allowed exceptions (subclasses MAY raise these; the runner catches
        them and records the attack as ``error`` instead of pass/fail):
          * Provider auth errors (e.g. ``openai.AuthenticationError``).
          * Provider bad-request errors (model id wrong, context too long).
          * Network / connection errors after the subclass's own retry
            budget is exhausted.
          * Rate-limit errors after the subclass's own retry budget is
            exhausted.

        Subclasses MUST NOT swallow exceptions and return a placeholder
        string — that would mask outages as "model refused" in results.
        """
        raise NotImplementedError
