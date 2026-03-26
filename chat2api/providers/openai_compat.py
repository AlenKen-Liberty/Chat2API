"""
Generic OpenAI-compatible provider backend.

Works with any provider that implements the standard /v1/chat/completions API:
  - Groq (api.groq.com)
  - OpenRouter (openrouter.ai)
  - Mistral (api.mistral.ai)
  - etc.

Configured via config.yaml with api_keys stored in ~/.chat2api/keys/<provider>.txt
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from chat2api.anti_detection.tls_client import TLSClient, TLSClientDependencyError
from chat2api.models.openai_types import ChatCompletionRequest, content_to_text
from chat2api.models.tiers import ModelTarget
from chat2api.protocol.sse import iter_sse_json
from chat2api.providers.base import (
    ProviderAuthError,
    ProviderBackend,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)
KEYS_DIR = Path.home() / ".chat2api" / "keys"


def _provider_env_var_names(provider_name: str) -> list[str]:
    base = provider_name.upper().replace("-", "_")
    return [
        f"{base}_API_KEYS",
        f"{base}_API_KEY",
        f"CHAT2API_{base}_API_KEYS",
        f"CHAT2API_{base}_API_KEY",
    ]


def _split_key_blob(raw: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[\n,\r]+", raw)
        if item.strip()
    ]


def _mask_key(value: str) -> str:
    if len(value) <= 8:
        return value
    return f"{value[:4]}...{value[-4:]}"


def load_api_keys(provider_name: str, *, keys_dir: Path | None = None) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()

    for env_name in _provider_env_var_names(provider_name):
        raw = os.getenv(env_name, "")
        for key in _split_key_blob(raw):
            if key not in seen:
                keys.append(key)
                seen.add(key)

    key_file = (keys_dir or KEYS_DIR) / f"{provider_name}.txt"
    if key_file.exists():
        for line in key_file.read_text().splitlines():
            key = line.strip()
            if key and not key.startswith("#") and key not in seen:
                keys.append(key)
                seen.add(key)

    return keys


def describe_api_keys(provider_name: str, *, keys_dir: Path | None = None) -> dict[str, Any]:
    env_sources = [name for name in _provider_env_var_names(provider_name) if os.getenv(name, "").strip()]
    key_file = (keys_dir or KEYS_DIR) / f"{provider_name}.txt"
    keys = load_api_keys(provider_name, keys_dir=keys_dir)
    return {
        "provider": provider_name,
        "configured": bool(keys),
        "key_count": len(keys),
        "sources": [
            *env_sources,
            *([str(key_file)] if key_file.exists() else []),
        ],
        "masked_keys": [_mask_key(key) for key in keys[:3]],
    }


class OpenAICompatBackend(ProviderBackend):
    """
    A reusable backend for any OpenAI-compatible API.

    API keys are loaded from ~/.chat2api/keys/<provider_name>.txt (one key per line).
    Keys are rotated round-robin; rate-limited keys are skipped.
    """

    def __init__(
        self,
        provider_name: str,
        base_url: str,
        tls_client: TLSClient,
        extra_headers: dict[str, str] | None = None,
    ):
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/")
        self.tls_client = tls_client
        self.extra_headers = extra_headers or {}
        self._keys: list[str] = []
        self._key_index = 0
        self._load_keys()

    def _load_keys(self) -> None:
        self._keys = load_api_keys(self.provider_name)
        if not self._keys:
            logger.warning(
                "No API keys for %s in env (%s) or %s/%s.txt",
                self.provider_name,
                ", ".join(_provider_env_var_names(self.provider_name)),
                KEYS_DIR,
                self.provider_name,
            )
            return
        logger.info("Loaded %d API key(s) for %s", len(self._keys), self.provider_name)

    def _next_key(self) -> str:
        if not self._keys:
            raise ProviderAuthError(
                f"No API keys configured for {self.provider_name}. "
                f"Add keys to ~/.chat2api/keys/{self.provider_name}.txt"
            )
        key = self._keys[self._key_index % len(self._keys)]
        self._key_index += 1
        return key

    def stream_text(self, target: ModelTarget, request: ChatCompletionRequest) -> Iterator[str]:
        if not self._keys:
            raise ProviderAuthError(f"No API keys for {self.provider_name}")

        last_err: Exception | None = None
        for _ in range(len(self._keys)):
            api_key = self._next_key()
            try:
                yield from self._stream_with_key(api_key, target, request)
                return
            except ProviderRateLimitError as exc:
                logger.info("%s key ...%s rate-limited, trying next", self.provider_name, api_key[-4:])
                last_err = exc
                continue

        raise ProviderRateLimitError(
            f"All {len(self._keys)} {self.provider_name} key(s) are rate-limited. "
            f"Last error: {last_err}"
        )

    def _stream_with_key(
        self, api_key: str, target: ModelTarget, request: ChatCompletionRequest
    ) -> Iterator[str]:
        url = f"{self.base_url}/chat/completions"
        payload = self._build_payload(target, request)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **self.extra_headers,
        }

        try:
            with self.tls_client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code == 401:
                    raise ProviderAuthError(f"{self.provider_name} authentication failed")
                if response.status_code == 429:
                    raise ProviderRateLimitError(f"{self.provider_name} rate limit reached")
                if response.status_code >= 400:
                    raise ProviderRequestError(self._response_error(response))

                for event in iter_sse_json(response.iter_lines()):
                    choices = event.get("choices") or []
                    for choice in choices:
                        delta = choice.get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
        except TLSClientDependencyError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

    @staticmethod
    def _build_payload(target: ModelTarget, request: ChatCompletionRequest) -> dict[str, Any]:
        messages = [
            {"role": m.role, "content": content_to_text(m.content)}
            for m in request.messages
            if content_to_text(m.content)
        ]
        if not messages:
            raise ProviderRequestError("At least one message with text content is required")

        payload: dict[str, Any] = {
            "model": target.model_id,
            "messages": messages,
            "stream": True,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        return payload

    def _response_error(self, response: Any) -> str:
        try:
            for line in response.iter_lines():
                line_str = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
                if line_str.strip():
                    return f"{self.provider_name} request failed: HTTP {response.status_code} {line_str}"
        except Exception:
            pass
        return f"{self.provider_name} request failed: HTTP {response.status_code}"
