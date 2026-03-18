from __future__ import annotations

from functools import lru_cache

from chat2api.anti_detection.tls_client import TLSClient
from chat2api.config import get_settings
from chat2api.providers.codex import CodexBackend
from chat2api.providers.gemini import GeminiBackend


class BackendRegistry:
    def __init__(self) -> None:
        settings = get_settings()
        self.tls_client = TLSClient(
            strategy=settings.anti_detection.tls_strategy,
            impersonate=settings.anti_detection.tls_impersonate,
        )
        self.backends = {
            "gemini": GeminiBackend(self.tls_client),
            "codex": CodexBackend(self.tls_client),
        }

    def get(self, provider: str):
        try:
            return self.backends[provider]
        except KeyError as exc:
            raise KeyError(f"Unsupported provider '{provider}'") from exc


@lru_cache(maxsize=1)
def get_backend_registry() -> BackendRegistry:
    return BackendRegistry()
