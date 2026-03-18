from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from chat2api.models.openai_types import ChatCompletionRequest
from chat2api.models.tiers import ModelTarget


class ProviderError(RuntimeError):
    status_code = 502


class ProviderRequestError(ProviderError):
    status_code = 400


class ProviderAuthError(ProviderError):
    status_code = 401


class ProviderRateLimitError(ProviderError):
    status_code = 429


class ProviderUnavailableError(ProviderError):
    status_code = 503


class ProviderBackend(ABC):
    provider_name: str

    @abstractmethod
    def stream_text(self, target: ModelTarget, request: ChatCompletionRequest) -> Iterator[str]:
        raise NotImplementedError

    def generate_text(self, target: ModelTarget, request: ChatCompletionRequest) -> str:
        return "".join(self.stream_text(target, request))
