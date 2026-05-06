from abc import ABC, abstractmethod


class Target(ABC):
    """Abstract provider adapter. Concrete subclasses wrap a single LLM API."""

    name: str

    @abstractmethod
    def generate(self, prompt: str) -> str:
        raise NotImplementedError
