from .base import Target


class AnthropicTarget(Target):
    name = "anthropic"

    def generate(self, prompt: str) -> str:
        raise NotImplementedError
