from .base import Target


class AnthropicTarget(Target):
    """Stub. TODO: implement when ANTHROPIC_API_KEY is available — pattern matches OpenAITarget."""

    provider = "anthropic"

    def __init__(self, model_name: str = "claude-sonnet-4-6"):
        super().__init__(model_name)

    def query(self, prompt: str, system: str | None = None) -> str:
        raise NotImplementedError(
            "AnthropicTarget is not implemented yet. Add an ANTHROPIC_API_KEY to .env "
            "and mirror the structure of OpenAITarget."
        )
