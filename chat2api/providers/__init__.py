from __future__ import annotations

import logging
from functools import lru_cache

from chat2api.anti_detection.tls_client import TLSClient
from chat2api.config import get_settings
from chat2api.providers.codex import CodexBackend
from chat2api.providers.copilot import CopilotBackend
from chat2api.providers.gemini import GeminiBackend
from chat2api.providers.openai_compat import OpenAICompatBackend

logger = logging.getLogger(__name__)

# Providers that use the generic OpenAI-compatible backend.
# Each entry: provider_name → (base_url, extra_headers)
_OPENAI_COMPAT_PROVIDERS: dict[str, tuple[str, dict[str, str]]] = {
    "groq": ("https://api.groq.com/openai/v1", {}),
    "openrouter": ("https://openrouter.ai/api/v1", {"HTTP-Referer": "https://chat2api.local"}),
    "mistral": ("https://api.mistral.ai/v1", {}),
}


class BackendRegistry:
    def __init__(self) -> None:
        settings = get_settings()
        self.tls_client = TLSClient(
            strategy=settings.anti_detection.tls_strategy,
            impersonate=settings.anti_detection.tls_impersonate,
        )
        self.backends: dict[str, object] = {
            "gemini": GeminiBackend(self.tls_client),
            "codex": CodexBackend(self.tls_client),
            "copilot": CopilotBackend(self.tls_client),
        }

        # Register any OpenAI-compatible providers that appear in config
        for provider_name in settings.providers:
            if provider_name in self.backends:
                continue
            if provider_name in _OPENAI_COMPAT_PROVIDERS:
                base_url, extra_headers = _OPENAI_COMPAT_PROVIDERS[provider_name]
                self.backends[provider_name] = OpenAICompatBackend(
                    provider_name=provider_name,
                    base_url=base_url,
                    tls_client=self.tls_client,
                    extra_headers=extra_headers,
                )
                logger.info("Registered OpenAI-compat provider: %s (%s)", provider_name, base_url)

    def get(self, provider: str):
        try:
            return self.backends[provider]
        except KeyError as exc:
            raise KeyError(f"Unsupported provider '{provider}'") from exc


@lru_cache(maxsize=1)
def get_backend_registry() -> BackendRegistry:
    return BackendRegistry()
