from .base import Target


class LocalTarget(Target):
    """Placeholder for testing my own fine-tuned models from Project 2.

    Will use transformers.AutoModelForCausalLM locally. GPU-required, so
    not runnable in dev — implement when Project 2 produces an adapter.
    """

    provider = "local"

    def __init__(self, model_name: str = "local-finetune"):
        super().__init__(model_name)

    def query(self, prompt: str, system: str | None = None) -> str:
        raise NotImplementedError(
            "LocalTarget is a placeholder. Implement once Project 2 produces a "
            "fine-tuned adapter (transformers.AutoModelForCausalLM, GPU-required)."
        )
