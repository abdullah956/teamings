from .base import Target


class OpenAITarget(Target):
    name = "openai"

    def generate(self, prompt: str) -> str:
        raise NotImplementedError
